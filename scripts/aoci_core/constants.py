"""Shared constants, regexes, and low-level path/atomic-IO helpers for `aoci_index`."""

from __future__ import annotations

import datetime as dt
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

AOCI_DIR_NAME = ".aoci"
MANIFEST_FILE = "aoci.txt"
META_FILE = "aoci.meta.txt"
CODE_FILE = "aoci.code.txt"
HUBS_FILE = "aoci.hubs.txt"
GLOSSARY_FILE = "glossary.txt"
BASELINE_FILE = "baseline.json"
IGNORE_FILE = ".aociignore"
LOCK_FILE = ".lock"
CACHE_DIR = ".cache"
SESSIONS_DIR = ".sessions"
SHARDS_DIR = "shards"
SHARDS_MANIFEST = "manifest.json"
SHARDS_INDEX = "INDEX.txt"
GIT_EXCLUSION_CACHE = "git_exclusion.json"
GIT_DISCOVERY_CACHE = "git_discovery.json"
OUTLINE_CACHE = "outline_cache.json"
SCAN_FP_CACHE = "scan_fps.json"
BATCH_PLAN_CACHE = "batch_plan.json"
GIT_FAST_AUTO_THRESHOLD = 2000
LINE_SKETCH_K = 16
SIMILAR_RENAME_THRESHOLD = 0.60
BASELINE_SCHEMA_VERSION = 2

EXCLUDE_BEGIN = "# >>> aoci-index (local exclude - do not track)"
EXCLUDE_END = "# <<< aoci-index"

VALID_LAYERS: Set[str] = {"E", "A", "D", "K", "P", "I", "M", "U", "C", "R", "T", "O"}
VALID_SIZES: Set[str] = {"T", "S", "M", "L", "H"}
DEFAULT_FORBIDDEN_LAYER_EDGES: Dict[str, Set[str]] = {
    "E": {"T"},
    "A": {"T"},
    "M": {"T"},
    "U": {"T"},
    "D": {"E", "U", "T"},
    "K": {"E", "U", "T"},
    "P": {"E", "U", "T"},
    "I": {"E", "U", "T"},
    "C": {"E", "A", "U", "T"},
}

# Regex for a valid object-fras-v2 single-line entry:
#   <relpath>[LDIS]: F:<...> | R:<...> | A:<...> | S:<...>
ENTRY_RE = re.compile(
    r"^(?P<path>[^\s:][^:\r\n]*?)\[(?P<layer>[A-Z])(?P<domain>[A-Z0-9])(?P<imp>[1-9])(?P<size>[TSMLH])\]:\s*"
    r"F:(?P<f>.+?)\s*\|\s*R:(?P<r>.+?)\s*\|\s*A:(?P<a>.+?)\s*\|\s*S:(?P<s>.+?)\s*$"
)

DOMAIN_TOKEN_RE = re.compile(r"(?<![\w-])([A-Z0-9])=([^\s#,;|()\[\]=<>，；。：（）]+)")
_PATH_SEG_CHAR = r"[\w.+$@-]"
_PATH_SEG_PATTERN = rf"(?:\[[\w.+$@\[\]-]+\]|\({_PATH_SEG_CHAR}+\)|{_PATH_SEG_CHAR})+"
RELPATH_TOKEN_RE = re.compile(rf"{_PATH_SEG_PATTERN}(?:/{_PATH_SEG_PATTERN})+")

_REF_BOUNDARY_LEFT = r"(?<![\w./+$@])(?<![\w.+$@]-)"
_REF_BOUNDARY_RIGHT = r"(?![\w./+$@-])"

ROOT_FILE_TOKEN_RE = re.compile(
    rf"{_REF_BOUNDARY_LEFT}(?:\.[\w][\w.-]*|{_PATH_SEG_PATTERN}\.[A-Za-z][A-Za-z0-9]*){_REF_BOUNDARY_RIGHT}"
)

_LIKELY_FILE_EXTS: Set[str] = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts",
    ".vue", ".svelte", ".astro", ".go", ".rs", ".java", ".kt", ".kts", ".cs", ".fs",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".m", ".mm", ".swift", ".dart",
    ".rb", ".php", ".scala", ".clj", ".ex", ".exs", ".erl", ".lua", ".r", ".jl",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".sql", ".prisma", ".proto",
    ".graphql", ".gql", ".json", ".jsonc", ".toml", ".yaml", ".yml", ".ini", ".cfg",
    ".conf", ".env", ".xml", ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".md", ".mdx", ".rst", ".txt", ".tf", ".hcl", ".nix", ".gradle", ".cmake", ".mk",
}

# Directories skipped in ALL modes (even in Git repos lacking a `.gitignore`)
ALWAYS_SKIP_DIRS: Set[str] = {
    ".aoci",
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".next",
    ".nuxt",
    ".turbo",
    ".cache",
    ".idea",
    ".vscode",
    ".claude",
    ".agents",
    ".agent",
}

# Additional build/vendor directories skipped when walking non-Git workspaces
DEFAULT_SKIP_DIRS: Set[str] = ALWAYS_SKIP_DIRS | {
    "bower_components",
    "dist",
    "build",
    "out",
    "target",
    ".svelte-kit",
    ".gradle",
    ".mvn",
    "vendor",
    "coverage",
    ".nyc_output",
    "Pods",
    "DerivedData",
    "bin",
    "obj",
}

DEFAULT_SKIP_FILENAMES: Set[str] = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "bun.lock",
    "Cargo.lock",
    "poetry.lock",
    "Pipfile.lock",
    "composer.lock",
    "Gemfile.lock",
    "go.sum",
    "flake.lock",
    ".DS_Store",
    "Thumbs.db",
}

DEFAULT_SKIP_EXTENSIONS: Set[str] = {
    # Images / media / fonts
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff", ".tif", ".psd",
    ".mp3", ".mp4", ".wav", ".flac", ".ogg", ".avi", ".mov", ".mkv", ".webm",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Documents / archives / compiled binaries / data blobs
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tgz",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat", ".o", ".obj", ".a", ".lib",
    ".class", ".jar", ".war", ".ear", ".pyc", ".pyo", ".pyd", ".wasm",
    ".sqlite", ".sqlite3", ".db", ".mdb", ".parquet", ".arrow", ".pkl", ".pt", ".onnx",
    # Source maps
    ".map",
}

DEFAULT_SKIP_SUFFIXES: Tuple[str, ...] = (".min.js", ".min.css", ".bundle.js", ".chunk.js")

DEFAULT_MAX_BYTES = 512 * 1024  # 512 KB
DEFAULT_MAX_BATCH_LINES = 2500
PARALLEL_HASH_THRESHOLD = 96

# Files modified within this window of being hashed are "racily clean"
RACY_WINDOW_NS = 3_000_000_000

# Exact-content rename detection ignores trivial files
MIN_RENAME_LINES = 3

LOCK_TIMEOUT_S = 60.0
LOCK_STALE_S = 300.0
MAX_SEMANTIC_POOL_ENTRIES = 50000

# `lint` heuristics
CJK_CHAR_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
VAGUE_F_RE = re.compile(
    r"(?:\b(?:helpers?|utils?|utilities|misc(?:ellaneous)?|stuff|various|core logic|main logic|"
    r"some functions|common code|wrapper|handles? things)\b|"
    r"(?:辅助函数|工具函数|通用工具|工具类|杂项|核心逻辑|主要逻辑|公共代码))",
    re.IGNORECASE,
)
IMPORTANCE_TOKEN_BUDGET: Dict[int, Tuple[int, int]] = {
    9: (80, 150), 8: (80, 150),
    7: (45, 80), 6: (45, 80), 5: (45, 80),
    4: (25, 45), 3: (25, 45),
    2: (15, 30), 1: (15, 30),
}

SIG_HASH_EXTS: Set[str] = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".mts",
    ".cts",
    ".vue",
    ".svelte",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".cs",
}

GIT_HOOK_BEGIN = "# >>> aoci-index git-hook (managed - do not edit)"
GIT_HOOK_END = "# <<< aoci-index git-hook"
MANAGED_GIT_HOOKS: Tuple[str, ...] = ("post-checkout", "post-merge", "post-rewrite", "pre-commit")


def configure_utf8_streams() -> None:
    """Prevent Windows console encoding errors when reading/writing UTF-8 paths or entries."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_relpath(raw: str) -> str:
    """Normalize a relative path to POSIX style without stripping leading dotfiles (e.g. `.gitignore`)."""
    norm = raw.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm.lstrip("/")


def compute_size_tier(line_count: int) -> str:
    if line_count < 50:
        return "T"
    if line_count < 200:
        return "S"
    if line_count < 500:
        return "M"
    if line_count < 1000:
        return "L"
    return "H"


def write_text_lf(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write text with LF line endings using Python 3.8-compatible `Path.open(..., newline='\\n')`."""
    with path.open("w", encoding=encoding, newline="\n") as fh:
        fh.write(text)


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a unique temp file + `os.replace`, retrying transient Windows sharing violations."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    write_text_lf(tmp, text)
    delay = 0.02
    for attempt in range(12):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 11:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def _stat_key(path: Optional[Path]) -> Optional[List[int]]:
    if path is None:
        return None
    try:
        st = os.stat(path)
        return [st.st_mtime_ns, st.st_size]
    except OSError:
        return [0, -1]


def _resolve_cli_file_path(root: Path, raw: str, must_exist: bool = False) -> Path:
    """Resolve CLI file arguments relative to `--root` instead of process CWD (with optional CWD fallback)."""
    p = Path(raw)
    if p.is_absolute():
        return p
    under_root = root / p
    if must_exist and not under_root.exists() and p.exists():
        return p
    return under_root


def _is_within_root(root: Path, target: Path) -> bool:
    """Python 3.8-compatible containment check confirming `target` resolves inside `root`."""
    try:
        root_norm = os.path.normcase(str(root.resolve()))
        target_norm = os.path.normcase(str(target.resolve()))
        return os.path.commonpath([root_norm, target_norm]) == root_norm
    except (OSError, ValueError):
        return False


def _parse_code_filter_set(raw: Optional[str]) -> Optional[Set[str]]:
    if not raw:
        return None
    chars = {ch for part in raw.upper().split(",") for ch in part.strip() if ch.isalnum()}
    return chars or None
