"""Manifest, meta-dictionary, and `.aoci/` template rendering & parsing helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from .constants import CODE_FILE, DOMAIN_TOKEN_RE, MANIFEST_FILE, META_FILE


def parse_manifest_field(aoci_dir: Path, prefix: str) -> str:
    manifest_path = aoci_dir / MANIFEST_FILE
    if not manifest_path.exists():
        return ""
    try:
        lines = manifest_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return ""
    for idx, line in enumerate(lines):
        if line.startswith(prefix):
            parts: List[str] = []
            first = line[len(prefix) :].strip()
            if first:
                parts.append(first)
            for cont in lines[idx + 1 :]:
                if not cont.strip() or re.match(r"^#[A-Za-z][A-Za-z0-9_-]*:", cont):
                    break
                m_comment = re.match(r"^#\s+(?:[-*]\s+|\d+[.)]\s+)?(\S.*)$", cont)
                if m_comment:
                    parts.append(m_comment.group(1).strip())
                elif cont.startswith((" ", "\t")):
                    cleaned = re.sub(r"^(?:[-*]\s+|\d+[.)]\s+)", "", cont.strip())
                    if cleaned:
                        parts.append(cleaned)
                else:
                    break
            return "; ".join(parts)
    return ""


def parse_declared_domains(aoci_dir: Path) -> Dict[str, str]:
    """Extract declared single-character Domain codes (`D=Name`, ASCII or Unicode/CJK) from `.aoci/aoci.meta.txt`."""
    meta_path = aoci_dir / META_FILE
    if not meta_path.exists():
        return {}
    domains: Dict[str, str] = {}
    in_domain_section = False
    try:
        meta_lines = meta_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return {}
    for raw in meta_lines:
        line = raw.strip()
        if re.match(r"^#\s*D\s*\(Domain", line, re.IGNORECASE):
            in_domain_section = True
            continue
        if in_domain_section:
            if re.match(r"^#\s*[A-Z]\s*\(", line) or line.startswith("# ["):
                in_domain_section = False
                continue
            low = line.lower()
            if any(marker in low for marker in ("(example", "example:", "（示例", "示例:", "示例：")):
                continue
            for code, label in DOMAIN_TOKEN_RE.findall(line):
                domains[code] = label
    return domains


def render_manifest_template(project: str, locale: str, git_excluded: bool) -> str:
    git_line = "true (.git/info/exclude)" if git_excluded else "n/a (non-git workspace)"
    return (
        "#AOCI-ROOT-MANIFEST: 1\n"
        "#Format-Version: cognition-volumes/v1\n"
        f"#Locale: {locale}\n"
        f"#Project: {project}\n"
        f"#Git-Excluded: {git_line}\n"
        "#Global-Invariants: -\n"
        "\n"
        f"#Volume: id=meta kind=meta path={META_FILE} format=meta-v1 depends=- state=enabled\n"
        f"#Volume: id=code kind=code path={CODE_FILE} format=object-fras-v2 depends=meta state=enabled\n"
        "\n"
        "#Read-Order: aoci.txt -> aoci.meta.txt -> aoci.code.txt\n"
        "#Protocol: Read-Whole-First before exploring source files; sync after code updates.\n"
    )


def render_meta_template() -> str:
    return (
        "#AOCI-META-VOLUME: 1\n"
        "#Format: meta-v1\n"
        "\n"
        "# [Tag Dictionary: [LDIS] 4-char Symbolic Coordinate]\n"
        "# L (Layer - architectural tier):\n"
        "#   E=Entry  A=Application  D=Domain  K=Compute  P=Persistence  I=Integration\n"
        "#   M=Middleware  U=UI  C=Common  R=Runtime  T=Test  O=Docs\n"
        "#\n"
        "# D (Domain - extend with project-specific single-character module codes during init):\n"
        "#   G=Global  X=Core\n"
        "#   (Example: A=Auth  B=Billing  N=Network  S=Storage  W=Workflow)\n"
        "#\n"
        "# I (Importance 1-9 and token length budget):\n"
        "#   9-8=Core hub / high-risk critical path (80-150 tokens: rich F/R/A and strict S invariants)\n"
        "#   7-5=Primary business module or public contract (45-80 tokens: complete FRAS)\n"
        "#   4-3=Supporting utility or routine config (25-45 tokens: compact FRAS)\n"
        "#   2-1=Peripheral script, boilerplate, or simple test (15-30 tokens: minimal FRAS)\n"
        "#\n"
        "# S (Size - auto-verified by aoci_index.py from actual line count):\n"
        "#   T=<50L  S=50-199L  M=200-499L  L=500-999L  H=>=1000L\n"
        "\n"
        "# [FRAS Schema: object-fras-v2]\n"
        "# Syntax: <relpath>[LDIS]: F:<Responsibility> | R:<Read-Along/Deps> | A:<API/Callers> | S:<Non-Obvious Constraints>\n"
        "# Authoring Rules:\n"
        "#   1. Strictly one line per managed file using POSIX relative paths (/).\n"
        "#   2. F must state concrete responsibility; never use vague filler or `-`.\n"
        "#   3. R lists files/schemas that must be read alongside edits; use `-` when none.\n"
        "#   4. A lists key exported symbols/routes and primary upstream callers; use `-` when none.\n"
        "#   5. S captures non-obvious invariants (concurrency, ordering, security, compatibility, side effects); use `-` when none.\n"
        "#   6. Never guess semantics from filenames: read the actual source file before authoring.\n"
        "\n"
        "# [Glossary: Optional Bilingual / Domain Term Aliases for BM25 Search]\n"
        "#   Syntax: `#   termA = aliasB, aliasC` (or single-line `#Glossary: A = B; C = D` or `.aoci/glossary.txt`)\n"
        "#   (Example: 密钥轮换 = key rotation, kid rotation)\n"
    )


def render_ignore_template() -> str:
    return (
        "# Extra ignore patterns for AOCI codebase indexing (gitignore-like globs).\n"
        "# `.aoci/`, `.git/`, standard dependency/build dirs, lockfiles, and binaries are already skipped.\n"
        "# Examples:\n"
        "# generated/\n"
        "# *.pb.go\n"
        "# docs/legacy/\n"
    )
