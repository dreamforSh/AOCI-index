"""
Opt-in hybrid retrieval (`--hybrid`): Reciprocal Rank Fusion of BM25, a zero-dependency character-trigram
channel, and an optional dense-embedding channel (`model2vec`, installed via the `[embed]` extra).

`HybridIndex` exposes the same `search_detailed` / `search` contract as `graph.BM25Index`, so `pack`,
`query`, `eval`, and the MCP server can swap it in without changing their ranking pipelines.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
from array import array
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .constants import AOCI_DIR_NAME, CACHE_DIR, atomic_write_text
from .graph import BM25Index, ParsedEntry, parse_entry

DEFAULT_EMBED_MODEL = "minishlab/potion-base-8M"
DEFAULT_WEIGHTS: Dict[str, float] = {"bm25": 1.0, "trigram": 0.5, "dense": 1.0}
RRF_K = 60
CHANNEL_DEPTH = 50
TRIGRAM_MIN_SIM = 0.15
DENSE_MIN_SIM = 0.15
EMBED_CACHE_SUBDIR = "embed"

_WORD_RE = re.compile(r"[A-Za-z0-9]+|[㐀-䶿一-鿿豈-﫿]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z0-9])|[A-Z]?[a-z0-9]+|[A-Z]+")


class EmbedderUnavailable(RuntimeError):
    """The requested dense backend cannot be loaded (package missing, model download failed, ...)."""


def _a_lhs(pe: ParsedEntry) -> str:
    return " ".join(chunk.split("<-", 1)[0] for chunk in re.split(r"[,;]", pe.a))


def entry_search_text(rel: str, pe: ParsedEntry) -> str:
    """Text both auxiliary channels index: path, exported symbols, responsibility, and constraints."""
    parts = [rel.replace("/", " ").replace("_", " "), _a_lhs(pe), pe.f]
    if pe.s and pe.s.strip() != "-":
        parts.append(pe.s)
    return " ".join(p for p in parts if p and p.strip() != "-")


def _words(text: str) -> List[str]:
    out: List[str] = []
    for w in _WORD_RE.findall(text):
        low = w.lower()
        out.append(low)
        if w.isascii():
            camel = _CAMEL_RE.findall(w)
            if len(camel) > 1:
                out.extend(c.lower() for c in camel if len(c) >= 2)
    return out


class TrigramIndex:
    """
    Zero-dependency character-trigram TF-IDF cosine channel. Recovers morphology and partial-identifier
    matches BM25's whole-token postings miss (`subscribe` ~ `subscriptions`, `notify` ~ `notification`).
    Trigrams of each distinct word are computed once and shared across documents.
    """

    def __init__(self, texts: Mapping[str, str], max_df_ratio: float = 0.5) -> None:
        self._word_cache: Dict[str, Tuple[str, ...]] = {}
        self.paths: List[str] = []
        doc_tfs: List[Dict[str, int]] = []
        df: Dict[str, int] = {}
        for rel in sorted(texts):
            tf: Dict[str, int] = {}
            for word in _words(texts[rel]):
                for tri in self._trigrams(word):
                    tf[tri] = tf.get(tri, 0) + 1
            self.paths.append(rel)
            doc_tfs.append(tf)
            for tri in tf:
                df[tri] = df.get(tri, 0) + 1
        n = len(self.paths)
        self.n_docs = n
        cutoff = max(2, int(n * max_df_ratio))
        self.idf: Dict[str, float] = {
            tri: math.log((n + 1) / (cnt + 1)) + 1.0 for tri, cnt in df.items() if n < 8 or cnt <= cutoff
        }
        self.postings: Dict[str, List[Tuple[int, float]]] = {}
        for doc_idx, tf in enumerate(doc_tfs):
            weights = {tri: (1.0 + math.log(cnt)) * self.idf[tri] for tri, cnt in tf.items() if tri in self.idf}
            norm = math.sqrt(math.fsum(w * w for w in weights.values())) or 1.0
            for tri, w in weights.items():
                self.postings.setdefault(tri, []).append((doc_idx, w / norm))

    def _trigrams(self, word: str) -> Tuple[str, ...]:
        cached = self._word_cache.get(word)
        if cached is None:
            padded = f" {word} "
            cached = tuple(padded[i : i + 3] for i in range(len(padded) - 2)) if len(word) >= 2 else ()
            self._word_cache[word] = cached
        return cached

    def search(
        self,
        query: str,
        candidate_paths: Optional[Set[str]] = None,
        top_k: int = CHANNEL_DEPTH,
        min_sim: float = TRIGRAM_MIN_SIM,
    ) -> List[Tuple[str, float]]:
        q_tf: Dict[str, int] = {}
        for word in _words(query):
            for tri in self._trigrams(word):
                if tri in self.idf:
                    q_tf[tri] = q_tf.get(tri, 0) + 1
        if not q_tf:
            return []
        q_w = {tri: (1.0 + math.log(cnt)) * self.idf[tri] for tri, cnt in q_tf.items()}
        q_norm = math.sqrt(math.fsum(w * w for w in q_w.values())) or 1.0
        scores: Dict[int, float] = {}
        for tri, w in q_w.items():
            qn = w / q_norm
            for doc_idx, dw in self.postings.get(tri, ()):
                scores[doc_idx] = scores.get(doc_idx, 0.0) + qn * dw
        ranked = sorted(
            (
                (sc, self.paths[i])
                for i, sc in scores.items()
                if sc >= min_sim and (candidate_paths is None or self.paths[i] in candidate_paths)
            ),
            key=lambda item: (-item[0], item[1]),
        )
        return [(p, round(sc, 4)) for sc, p in ranked[: max(1, top_k)]]


class _Model2VecEmbedder:
    def __init__(self, model_name: str) -> None:
        try:
            from model2vec import StaticModel  # type: ignore[import-not-found]
        except ImportError as exc:
            raise EmbedderUnavailable(
                "dense backend `model2vec` is not installed (pip install 'aoci-index[embed]')"
            ) from exc
        try:
            self._model = StaticModel.from_pretrained(model_name)
        except Exception as exc:  # network / cache / format errors all degrade to BM25 + trigram
            raise EmbedderUnavailable(f"could not load model2vec model `{model_name}`: {exc}") from exc
        self.name = f"model2vec:{model_name}"

    def encode(self, texts: Sequence[str]) -> List[List[float]]:
        vecs = self._model.encode(list(texts))
        return [list(map(float, row)) for row in vecs]


def load_embedder(backend: Optional[str], model: Optional[str]) -> Optional[Any]:
    """Resolve `--embed-backend` / `AOCI_EMBED_BACKEND`; returns `None` for the default `none` backend."""
    name = (backend or os.environ.get("AOCI_EMBED_BACKEND") or "none").strip().lower()
    if name in ("", "none", "off"):
        return None
    model_name = (model or os.environ.get("AOCI_EMBED_MODEL") or DEFAULT_EMBED_MODEL).strip()
    if name == "model2vec":
        return _Model2VecEmbedder(model_name)
    raise EmbedderUnavailable(f"unknown dense backend `{name}` (supported: none, model2vec)")


def _normalize(vec: Sequence[float]) -> List[float]:
    norm = math.sqrt(math.fsum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _line_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class DenseChannel:
    """
    Brute-force cosine search over unit vectors from a pluggable embedder (`name`, `encode(texts)`).
    Vectors are cached per embedder in `.aoci/.cache/embed/<slug>.json`, keyed by the SHA-1 of the indexed
    text, so only new or edited entries are re-embedded. Storage is stdlib `array('f')` + base64.
    """

    def __init__(self, texts: Mapping[str, str], embedder: Any, cache_dir: Optional[Path] = None) -> None:
        self.embedder = embedder
        self.paths: List[str] = sorted(texts)
        keys = [_line_key(texts[p]) for p in self.paths]
        cache_file = self._cache_file(cache_dir)
        cached = self._load_cache(cache_file)
        missing = [(i, texts[p]) for i, p in enumerate(self.paths) if keys[i] not in cached]
        if missing:
            fresh = embedder.encode([t for _, t in missing])
            for (i, _), vec in zip(missing, fresh):
                cached[keys[i]] = _normalize(vec)
        self.vectors: List[List[float]] = [cached[k] for k in keys]
        if missing and cache_file is not None:
            self._save_cache(cache_file, {k: cached[k] for k in set(keys)})

    def _cache_file(self, cache_dir: Optional[Path]) -> Optional[Path]:
        if cache_dir is None:
            return None
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(getattr(self.embedder, "name", "embedder")))[:80]
        return cache_dir / f"{slug}.json"

    @staticmethod
    def _load_cache(path: Optional[Path]) -> Dict[str, List[float]]:
        if path is None or not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            out: Dict[str, List[float]] = {}
            for key, b64 in raw.get("vectors", {}).items():
                arr = array("f")
                arr.frombytes(base64.b64decode(b64))
                out[key] = list(arr)
            return out
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _save_cache(self, path: Path, vectors: Mapping[str, Sequence[float]]) -> None:
        payload = {
            "schema_version": 1,
            "embedder": str(getattr(self.embedder, "name", "embedder")),
            "vectors": {k: base64.b64encode(array("f", v).tobytes()).decode("ascii") for k, v in sorted(vectors.items())},
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(payload, separators=(",", ":")))
        except OSError:
            pass

    def search(
        self,
        query: str,
        candidate_paths: Optional[Set[str]] = None,
        top_k: int = CHANNEL_DEPTH,
        min_sim: float = DENSE_MIN_SIM,
    ) -> List[Tuple[str, float]]:
        if not self.paths:
            return []
        q = _normalize(self.embedder.encode([query])[0])
        scored: List[Tuple[float, str]] = []
        for path, vec in zip(self.paths, self.vectors):
            if candidate_paths is not None and path not in candidate_paths:
                continue
            sim = math.fsum(a * b for a, b in zip(q, vec))
            if sim >= min_sim:
                scored.append((sim, path))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [(p, round(sc, 4)) for sc, p in scored[: max(1, top_k)]]


class HybridIndex:
    """
    RRF fusion (`score = sum_c w_c / (60 + rank_c)`) of BM25, char-trigram, and optional dense channels.
    Drop-in for `BM25Index.search_detailed`: hits keep `path` / `score` / `entry` / `matched_terms` and add
    `channels` (per-channel rank) plus `bm25_score` when BM25 matched.
    """

    def __init__(
        self,
        entries: Dict[str, str],
        parsed_entries: Optional[Mapping[str, ParsedEntry]] = None,
        root: Optional[Path] = None,
        *,
        dense_backend: Optional[str] = None,
        dense_model: Optional[str] = None,
        embedder: Optional[Any] = None,
        weights: Optional[Mapping[str, float]] = None,
        bm25_index: Optional[BM25Index] = None,
        rrf_k: int = RRF_K,
        channel_depth: int = CHANNEL_DEPTH,
        trigram_min_sim: float = TRIGRAM_MIN_SIM,
        dense_min_sim: float = DENSE_MIN_SIM,
    ) -> None:
        self.entries = entries
        self.trigram_min_sim = float(trigram_min_sim)
        self.dense_min_sim = float(dense_min_sim)
        parsed: Dict[str, ParsedEntry] = {}
        for rel, line in entries.items():
            pe = parsed_entries.get(rel) if parsed_entries is not None else None
            if pe is None:
                pe = parse_entry(rel, line)
            if pe is not None:
                parsed[rel] = pe
        self.parsed = parsed
        self.bm25 = bm25_index if bm25_index is not None else BM25Index(entries, parsed_entries=parsed)
        texts = {rel: entry_search_text(rel, pe) for rel, pe in parsed.items()}
        self.trigram = TrigramIndex(texts)
        self.weights = dict(DEFAULT_WEIGHTS)
        if weights:
            self.weights.update({k: float(v) for k, v in weights.items()})
        self.rrf_k = max(1, int(rrf_k))
        self.channel_depth = max(1, int(channel_depth))
        self.warnings: List[str] = []
        self.dense: Optional[DenseChannel] = None
        try:
            emb = embedder if embedder is not None else load_embedder(dense_backend, dense_model)
        except EmbedderUnavailable as exc:
            emb = None
            self.warnings.append(f"dense channel disabled: {exc}")
        if emb is not None:
            cache_dir = (root / AOCI_DIR_NAME / CACHE_DIR / EMBED_CACHE_SUBDIR) if root is not None else None
            try:
                self.dense = DenseChannel(texts, emb, cache_dir=cache_dir)
            except Exception as exc:  # embedding failures must never break retrieval
                self.warnings.append(f"dense channel disabled: {exc}")

    def channel_info(self) -> List[str]:
        chans = ["bm25", "trigram"]
        if self.dense is not None:
            chans.append(f"dense({getattr(self.dense.embedder, 'name', 'embedder')})")
        return chans

    def describe(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"mode": "hybrid", "channels": self.channel_info(), "weights": dict(self.weights)}
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out

    def search_detailed(
        self,
        query_text: str,
        candidate_paths: Optional[Set[str]] = None,
        top_k: int = 20,
        glossary: Optional[Sequence[Sequence[str]]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        if candidate_paths is not None and not candidate_paths:
            return [], []
        depth = max(self.channel_depth, top_k)
        bm_hits, expansions = self.bm25.search_detailed(
            query_text, candidate_paths=candidate_paths, top_k=depth, glossary=glossary
        )
        rankings: Dict[str, List[str]] = {"bm25": [str(h["path"]) for h in bm_hits]}
        # Feed glossary aliases to the auxiliary channels too, so bilingual expansions help them as well
        aux_query = " ".join([query_text, *[a for e in expansions for a in e.get("aliases", ())]])
        rankings["trigram"] = [
            p for p, _ in self.trigram.search(aux_query, candidate_paths, depth, min_sim=self.trigram_min_sim)
        ]
        if self.dense is not None:
            try:
                rankings["dense"] = [
                    p for p, _ in self.dense.search(aux_query, candidate_paths, depth, min_sim=self.dense_min_sim)
                ]
            except Exception as exc:
                self.warnings.append(f"dense search failed: {exc}")

        fused: Dict[str, float] = {}
        ranks: Dict[str, Dict[str, int]] = {}
        for chan, paths in rankings.items():
            w = self.weights.get(chan, 0.0)
            if w <= 0.0:
                continue
            for rank, path in enumerate(paths, start=1):
                fused[path] = fused.get(path, 0.0) + w / (self.rrf_k + rank)
                ranks.setdefault(path, {})[chan] = rank

        bm_by_path = {str(h["path"]): h for h in bm_hits}
        ordered = sorted(
            fused.items(),
            key=lambda kv: (-kv[1], -(self.parsed[kv[0]].imp if kv[0] in self.parsed else 0), kv[0]),
        )
        limit = max(1, top_k) if top_k > 0 else len(ordered)
        hits: List[Dict[str, Any]] = []
        for path, score in ordered[:limit]:
            hit: Dict[str, Any] = {
                "path": path,
                "score": round(score * 1000.0, 3),
                "channels": ranks[path],
                "entry": self.entries.get(path, ""),
            }
            bm = bm_by_path.get(path)
            if bm is not None:
                hit["bm25_score"] = bm.get("score")
                hit["matched_terms"] = list(bm.get("matched_terms", []))
                if bm.get("matched_glossary"):
                    hit["matched_glossary"] = list(bm["matched_glossary"])
            else:
                hit["matched_terms"] = []
            hits.append(hit)
        return hits, expansions

    def search(
        self,
        query_text: str,
        candidate_paths: Optional[Set[str]] = None,
        top_k: int = 20,
        glossary: Optional[Sequence[Sequence[str]]] = None,
    ) -> List[Tuple[float, str, str]]:
        hits, _ = self.search_detailed(query_text, candidate_paths=candidate_paths, top_k=top_k, glossary=glossary)
        return [(float(h["score"]), str(h["path"]), str(h["entry"])) for h in hits]


def options_from(source: Any) -> Dict[str, Any]:
    """
    Map CLI args (`embed_backend` / `embed_model` attributes) or MCP arguments (same keys in a dict)
    to `HybridIndex` keyword options; unset values fall back to `AOCI_EMBED_*` env vars at load time.
    """
    getter = source.get if isinstance(source, Mapping) else (lambda key: getattr(source, key, None))
    opts: Dict[str, Any] = {}
    backend = getter("embed_backend")
    model = getter("embed_model")
    if backend:
        opts["dense_backend"] = str(backend)
    if model:
        opts["dense_model"] = str(model)
    return opts


def iter_channel_names(index: Any) -> Iterable[str]:
    info = getattr(index, "channel_info", None)
    return info() if callable(info) else ["bm25"]
