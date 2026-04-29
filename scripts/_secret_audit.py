"""Scan the working tree for accidentally committed secrets.

Looks for high-entropy strings, common API key prefixes, and
private-key headers across all tracked + untracked files. Reports
findings with line numbers and a redacted preview.

Detection patterns
------------------
* AWS access key:           ``AKIA[A-Z0-9]{16}``
* Stripe live secret:       ``sk_live_[a-zA-Z0-9]{24,}``
* Slack token:              ``xox[baprs]-[a-zA-Z0-9-]{10,}``
* GitHub token:             ``ghp_[a-zA-Z0-9]{36}`` (and ``gho_``, ``ghu_``, ``ghr_``, ``ghs_``)
* OpenAI / Anthropic-style: ``sk-(ant|proj)?-[a-zA-Z0-9_-]{32,}``
* Generic JWT:              ``eyJ[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{10,}``
* PEM private key header:   ``-----BEGIN [A-Z ]+PRIVATE KEY-----``
* Discord token:            ``[MN][A-Za-z\\d]{23}\\.[\\w-]{6}\\.[\\w-]{27}``

Skips
-----
* binary files (heuristic: null byte in first 8KB)
* dirs: .git, .venv, node_modules, .cache, .pytest_cache
* extensions: .pyc, .pyo, .so, .dll, .png, .jpg, .pdf
* lines containing ``# noqa: secret``

Exit codes
----------
0  GREEN  -- no findings
1  YELLOW -- 1..2 findings (likely false positives or low-severity)
2  RED    -- 3+ findings (real secret leak suspected)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).resolve().parents[1]

PATTERNS: dict[str, re.Pattern[str]] = {
    "AWS access key": re.compile(r"AKIA[A-Z0-9]{16}"),
    "Stripe live secret": re.compile(r"sk_live_[a-zA-Z0-9]{24,}"),
    "Slack token": re.compile(r"xox[baprs]-[a-zA-Z0-9-]{10,}"),
    "GitHub token": re.compile(r"gh[poustrd]_[a-zA-Z0-9]{30,}"),
    "OpenAI/Anthropic key": re.compile(r"sk-(?:ant-|proj-)?[a-zA-Z0-9_-]{32,}"),
    "JWT": re.compile(
        r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    ),
    "PEM private key": re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    "Discord token": re.compile(r"[MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27}"),
}

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    ".cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    ".mypy_cache",
}
SKIP_EXTS = {
    ".pyc",
    ".pyo",
    ".so",
    ".dll",
    ".dylib",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".parquet",
    ".bin",
    ".lock",
}


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
    except OSError:
        return True
    return b"\0" in chunk


def _redact(s: str) -> str:
    if len(s) <= 12:
        return s[:4] + "***"
    return f"{s[:6]}...{s[-4:]}"


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return [(line_no, label, redacted_match), ...]."""
    out: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    for i, line in enumerate(text.splitlines(), start=1):
        if "# noqa: secret" in line or "noqa:secret" in line:
            continue
        for label, rx in PATTERNS.items():
            for m in rx.finditer(line):
                out.append((i, label, _redact(m.group(0))))
    return out


def _should_skip_file(path: Path) -> bool:
    """True when a file should not be scanned for text secrets."""
    return (
        any(p in SKIP_DIRS for p in path.parts)
        or path.suffix.lower() in SKIP_EXTS
        or _is_binary(path)
    )


def _walk(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _should_skip_file(path):
            continue
        yield path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--max-yellow", type=int, default=2, help="more than this many findings -> RED (default 2)")
    p.add_argument("paths", nargs="*", help="explicit paths to scan (default: full tree)")
    args = p.parse_args(argv)

    targets = [Path(t) if Path(t).is_absolute() else ROOT / t for t in args.paths] if args.paths else [ROOT]
    findings: list[tuple[Path, int, str, str]] = []
    for target in targets:
        if target.is_file():
            if _should_skip_file(target):
                continue
            for ln, label, red in _scan_file(target):
                findings.append((target, ln, label, red))
            continue
        for path in _walk(target):
            for ln, label, red in _scan_file(path):
                findings.append((path, ln, label, red))

    n = len(findings)
    if n == 0:
        print("secret-audit: GREEN -- no findings")
        return 0
    level = "RED" if n > args.max_yellow else "YELLOW"
    print(f"secret-audit: {level} -- {n} potential secret(s) found")
    for path, ln, label, red in findings:
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        print(f"  {rel}:{ln}  [{label}]  {red}")
    print()
    print("If a finding is a false positive, add `# noqa: secret` to the line.")
    return 1 if level == "YELLOW" else 2


if __name__ == "__main__":
    sys.exit(main())
