#!/usr/bin/env python3
"""Zero-dependency release packager, checksum generator, and changelog builder for AOCI CI."""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

SKIP_DIR_NAMES = {
    "__pycache__",
    "_skill_bundle",
    "site-packages",
    "tests",
    ".git",
    ".hg",
    ".svn",
    ".aoci",
    ".github",
    ".venv",
    "venv",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".vscode",
    ".idea",
}
SKIP_FILE_PATTERNS = (
    "*.pyc",
    "*.pyo",
    "*.bak",
    "*.orig.bak",
    "*.tmp",
    ".DS_Store",
    ".aoci-install.json",
)
DETERMINISTIC_MTIME = 1704067200  # 2024-01-01T00:00:00Z for reproducible zip/tar builds
DETERMINISTIC_ZIP_DT = (2024, 1, 1, 0, 0, 0)

_COMMIT_PREFIX_RE = re.compile(r"^([a-zA-Z]+)(?:\(([^)]+)\))?(!)?:\s*(.+)$")


def normalize_tag_version(tag: str) -> str:
    ver = tag.strip()
    if ver.startswith("v"):
        ver = ver[1:]
    # Strip prerelease/build suffixes (e.g. 1.5.0-rc.1 -> 1.5.0)
    ver = ver.split("-", 1)[0].split("+", 1)[0]
    return ver


def _accepted_versions_for_tag(tag: str) -> Tuple[str, List[str]]:
    raw = tag.strip()
    if raw.startswith("v"):
        raw = raw[1:]
    raw_no_build = raw.split("+", 1)[0]
    base = normalize_tag_version(tag)
    accepted = [base]
    if raw_no_build and raw_no_build not in accepted:
        accepted.append(raw_no_build)
    # PEP 440 normalization for prereleases (e.g. 1.5.0-rc.1 -> 1.5.0rc1, 1.5.0-alpha.2 -> 1.5.0a2)
    pep440 = re.sub(r"-(?:alpha|a)\.?(\d+)$", r"a\1", raw_no_build, flags=re.IGNORECASE)
    pep440 = re.sub(r"-(?:beta|b)\.?(\d+)$", r"b\1", pep440, flags=re.IGNORECASE)
    pep440 = re.sub(r"-(?:rc|c|pre|preview)\.?(\d+)$", r"rc\1", pep440, flags=re.IGNORECASE)
    if pep440 and pep440 not in accepted:
        accepted.append(pep440)
    return base, accepted


def verify_version_consistency(repo_root: Path, tag: str) -> str:
    expected, accepted = _accepted_versions_for_tag(tag)
    if not expected:
        raise SystemExit(f"ERROR: Cannot derive semantic version from tag `{tag}`.")

    pyproject_text = (repo_root / "pyproject.toml").read_text(encoding="utf-8-sig")
    m_py = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject_text)
    if not m_py:
        raise SystemExit("ERROR: Could not parse `version` from `pyproject.toml`.")
    py_ver = m_py.group(1)

    const_text = (repo_root / "scripts" / "aoci_core" / "constants.py").read_text(encoding="utf-8-sig")
    m_const = re.search(r'(?m)^AOCI_VERSION\s*=\s*"([^"]+)"', const_text)
    if not m_const:
        raise SystemExit("ERROR: Could not parse `AOCI_VERSION` from `scripts/aoci_core/constants.py`.")
    const_ver = m_const.group(1)

    plugin_data = json.loads((repo_root / "resources" / "plugin.json").read_text(encoding="utf-8-sig"))
    plugin_ver = str(plugin_data.get("version", ""))

    mismatches = []
    for label, found in (
        ("pyproject.toml", py_ver),
        ("scripts/aoci_core/constants.py", const_ver),
        ("resources/plugin.json", plugin_ver),
    ):
        if found not in accepted:
            mismatches.append(f"{label}={found!r} (expected one of {accepted!r})")

    if mismatches:
        raise SystemExit("ERROR: Release tag/version mismatch: " + ", ".join(mismatches))
    return expected


def _should_skip_dir(name: str) -> bool:
    return name in SKIP_DIR_NAMES or name.endswith(".egg-info")


def _should_skip_file(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in SKIP_FILE_PATTERNS)


def collect_skill_files(repo_root: Path, outdir: Path) -> List[Tuple[Path, str]]:
    resolved_out = outdir.resolve()
    items: List[Tuple[Path, str]] = []
    for cur_root, dirs, files in os.walk(repo_root):
        cur_path = Path(cur_root).resolve()
        dirs[:] = sorted(
            d for d in dirs if not _should_skip_dir(d) and (cur_path / d).resolve() != resolved_out
        )
        for fname in sorted(files):
            if _should_skip_file(fname):
                continue
            fpath = cur_path / fname
            rel = fpath.relative_to(repo_root.resolve()).as_posix()
            items.append((fpath, f"aoci-index/{rel}"))
    items.sort(key=lambda pair: pair[1])
    return items


def _file_mode(fpath: Path, data: bytes) -> int:
    if fpath.suffix == ".sh" or data.startswith(b"#!"):
        return 0o755
    return 0o644


def _read_normalized_archive_bytes(fpath: Path) -> bytes:
    data = fpath.read_bytes()
    if b"\x00" not in data[:8192]:
        if data.startswith(b"\xef\xbb\xbf#!"):
            data = data[3:]
        if b"\r\n" in data:
            data = data.replace(b"\r\n", b"\n")
    return data


def build_skill_archives(repo_root: Path, outdir: Path, tag: str) -> Tuple[Path, Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    files = collect_skill_files(repo_root, outdir)
    tar_path = outdir / f"aoci-index-{tag}-skill.tar.gz"
    zip_path = outdir / f"aoci-index-{tag}-skill.zip"

    with tar_path.open("wb") as raw_fh:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_fh, compresslevel=9, mtime=DETERMINISTIC_MTIME) as gz_fh:
            with tarfile.open(fileobj=gz_fh, mode="w", format=tarfile.USTAR_FORMAT) as tf:
                for fpath, arcname in files:
                    data = _read_normalized_archive_bytes(fpath)
                    info = tarfile.TarInfo(name=arcname)
                    info.size = len(data)
                    info.mtime = DETERMINISTIC_MTIME
                    info.mode = _file_mode(fpath, data)
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    tf.addfile(info, io.BytesIO(data))

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for fpath, arcname in files:
            data = _read_normalized_archive_bytes(fpath)
            zinfo = zipfile.ZipInfo(filename=arcname, date_time=DETERMINISTIC_ZIP_DT)
            mode = _file_mode(fpath, data)
            zinfo.external_attr = (0o100000 | mode) << 16
            zf.writestr(zinfo, data)

    return tar_path, zip_path


def write_sha256sums(outdir: Path) -> Tuple[Path, List[str]]:
    sums_path = outdir / "SHA256SUMS.txt"
    artifact_names: List[str] = []
    lines: List[str] = []
    for p in sorted(outdir.iterdir(), key=lambda x: x.name):
        if not p.is_file() or p.name == "SHA256SUMS.txt":
            continue
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        lines.append(f"{digest}  {p.name}")
        artifact_names.append(p.name)
    sums_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="\n")
    return sums_path, artifact_names


def extract_changelog_section(changelog_path: Path, tag: str, base_ver: str) -> str:
    if not changelog_path.is_file():
        return ""
    lines = changelog_path.read_text(encoding="utf-8", errors="replace").splitlines()
    candidates = {
        tag.strip().lower(),
        base_ver.strip().lower(),
        f"v{base_ver.strip().lower()}",
    }
    capturing = False
    collected: List[str] = []
    for line in lines:
        m = re.match(r"^##\s+\[([^\]]+)\]", line.strip())
        if m:
            if capturing:
                break
            if m.group(1).strip().lower() in candidates:
                capturing = True
                continue
        elif capturing:
            collected.append(line)
    return "\n".join(collected).strip()


def _run_git(repo_root: Path, args: Sequence[str]) -> Optional[str]:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return r.stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def find_previous_tag(repo_root: Path, tag: str, target_ref: str) -> Optional[str]:
    out = _run_git(repo_root, ["tag", "--merged", target_ref, "--list", "v*", "--sort=-v:refname"])
    if not out:
        return None
    for raw in out.splitlines():
        cand = raw.strip()
        if cand and cand != tag:
            return cand
    return None


def collect_categorized_commits(
    repo_root: Path,
    prev_tag: Optional[str],
    target_ref: str,
) -> Dict[str, List[Tuple[str, str]]]:
    rev_range = f"{prev_tag}..{target_ref}" if prev_tag else target_ref
    out = _run_git(repo_root, ["log", "--no-merges", "--format=%H%x1f%s", rev_range])
    buckets: Dict[str, List[Tuple[str, str]]] = {
        "Features": [],
        "Bug Fixes": [],
        "Performance": [],
        "Documentation": [],
        "CI, Build & Maintenance": [],
    }
    if not out:
        return buckets
    for raw in out.splitlines():
        if "\x1f" not in raw:
            continue
        sha, subject = raw.split("\x1f", 1)
        sha = sha.strip()
        subject = subject.strip()
        if not sha or not subject:
            continue
        m = _COMMIT_PREFIX_RE.match(subject)
        if m:
            kind = m.group(1).lower()
            scope = m.group(2)
            breaking = "!" if m.group(3) else ""
            desc = m.group(4).strip()
            formatted = f"**{scope}{breaking}:** {desc}" if scope else f"{desc}"
            if kind == "feat":
                buckets["Features"].append((sha, formatted))
            elif kind == "fix":
                buckets["Bug Fixes"].append((sha, formatted))
            elif kind == "perf":
                buckets["Performance"].append((sha, formatted))
            elif kind == "docs":
                buckets["Documentation"].append((sha, formatted))
            else:
                buckets["CI, Build & Maintenance"].append((sha, subject))
        else:
            buckets["CI, Build & Maintenance"].append((sha, subject))
    return buckets


def render_release_notes(
    repo_root: Path,
    tag: str,
    base_ver: str,
    repo: Optional[str],
    artifact_names: Sequence[str],
) -> str:
    tag_exists = _run_git(repo_root, ["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"]) is not None
    target_ref = f"refs/tags/{tag}" if tag_exists else "HEAD"
    prev_tag = find_previous_tag(repo_root, tag, target_ref)
    changelog_body = extract_changelog_section(repo_root / "CHANGELOG.md", tag, base_ver)
    buckets = collect_categorized_commits(repo_root, prev_tag, target_ref)

    sections: List[str] = []
    if changelog_body:
        sections.append("## Highlights\n\n" + changelog_body)

    commit_blocks: List[str] = []
    for title in ("Features", "Bug Fixes", "Performance", "Documentation", "CI, Build & Maintenance"):
        entries = buckets[title]
        if not entries:
            continue
        lines = [f"### {title}"]
        for sha, msg in entries:
            short = sha[:7]
            ref_str = f"[`{short}`](https://github.com/{repo}/commit/{sha})" if repo else f"`{short}`"
            lines.append(f"- {msg} ({ref_str})")
        commit_blocks.append("\n".join(lines))

    if commit_blocks:
        sections.append("## What's Changed\n\n" + "\n\n".join(commit_blocks))

    if artifact_names:
        art_lines = ["## Release Artifacts", ""]
        for name in artifact_names:
            art_lines.append(f"- `{name}`")
        art_lines.append("- `SHA256SUMS.txt`")
        sections.append("\n".join(art_lines))

    if repo:
        if prev_tag:
            sections.append(f"**Full Changelog**: https://github.com/{repo}/compare/{prev_tag}...{tag}")
        else:
            sections.append(f"**Full Changelog**: https://github.com/{repo}/commits/{tag}")

    if not sections:
        return f"Release `{tag}`\n"
    return "\n\n".join(sections) + "\n"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build AOCI release skill archives, checksums, and release notes")
    p.add_argument("--tag", required=True, help="Release tag name (e.g. v1.5.0)")
    p.add_argument("--repo", default="", help="GitHub repository slug (e.g. owner/repo)")
    p.add_argument("--outdir", default="dist", help="Directory containing built sdist/wheel artifacts (default: dist)")
    p.add_argument("--notes-out", default="release_notes.md", help="Output path for generated release notes Markdown")
    p.add_argument("--skip-version-check", action="store_true", help="Skip verifying tag matches repository version metadata")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent.parent
    outdir = Path(args.outdir)
    if not outdir.is_absolute():
        outdir = repo_root / outdir
    notes_out = Path(args.notes_out)
    if not notes_out.is_absolute():
        notes_out = repo_root / notes_out

    if args.skip_version_check:
        base_ver = normalize_tag_version(args.tag)
    else:
        base_ver = verify_version_consistency(repo_root, args.tag)

    tar_path, zip_path = build_skill_archives(repo_root, outdir, args.tag)
    sums_path, artifact_names = write_sha256sums(outdir)
    notes_text = render_release_notes(repo_root, args.tag, base_ver, args.repo.strip() or None, artifact_names)
    notes_out.parent.mkdir(parents=True, exist_ok=True)
    notes_out.write_text(notes_text, encoding="utf-8", newline="\n")

    print(f"[release] version={base_ver} tag={args.tag}")
    print(f"[release] archives={tar_path.name}, {zip_path.name}")
    print(f"[release] checksums={sums_path.name} ({len(artifact_names)} artifacts)")
    print(f"[release] notes={notes_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
