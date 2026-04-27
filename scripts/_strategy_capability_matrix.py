"""Auto-generate a capability matrix for every strategy.

Walks ``strategies/`` AST and produces a markdown table summarizing
what each strategy supports. Useful for spotting gaps (e.g., "no
short-side strategies in the SOL fleet") and for documentation.

Capabilities detected
---------------------
* supports_long       method name contains 'long' OR has SignalType.LONG
* supports_short      method name contains 'short' OR has SignalType.SHORT
* uses_regime         imports/references RegimeType
* uses_leverage       references 'leverage', 'tier', or 'margin' attrs
* has_async           any async def
* has_decision_sink   imports from strategies.decision_sink
* loc                 non-blank line count

Usage
-----
    python scripts/_strategy_capability_matrix.py            # markdown table to stdout
    python scripts/_strategy_capability_matrix.py --csv      # CSV output
    python scripts/_strategy_capability_matrix.py --json     # JSON output
    python scripts/_strategy_capability_matrix.py --gap      # only show strategies missing key caps

Why
---
The Firm has dozens of strategies in strategies/. Operator can't keep
the full feature matrix in memory. This script makes the gaps obvious
("XrpPerp has no short-side -- need to add").
"""

from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES_DIR = ROOT / "strategies"


@dataclass
class StratInfo:
    name: str
    path: str
    supports_long: bool = False
    supports_short: bool = False
    uses_regime: bool = False
    uses_leverage: bool = False
    has_async: bool = False
    has_decision_sink: bool = False
    loc: int = 0


def _scan_file(path: Path) -> StratInfo | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    info = StratInfo(name=path.stem, path=str(path.relative_to(ROOT)).replace("\\", "/"))
    info.loc = sum(1 for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#"))
    text_lower = text.lower()
    if "regimetype" in text_lower or "regime_type" in text_lower:
        info.uses_regime = True
    if any(k in text_lower for k in ("leverage", "tier", "margin_mode")):
        info.uses_leverage = True
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            info.has_async = True
        if isinstance(node, ast.FunctionDef):
            n = node.name.lower()
            if "long" in n or "buy" in n:
                info.supports_long = True
            if "short" in n or "sell" in n:
                info.supports_short = True
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if "decision_sink" in mod:
                info.has_decision_sink = True
        if isinstance(node, ast.Attribute):
            attr = node.attr
            if attr in ("LONG", "BUY"):
                info.supports_long = True
            elif attr in ("SHORT", "SELL"):
                info.supports_short = True
    return info


def _scan_all() -> list[StratInfo]:
    out: list[StratInfo] = []
    if not STRATEGIES_DIR.exists():
        return out
    for path in STRATEGIES_DIR.rglob("*.py"):
        if path.name == "__init__.py" or path.name.startswith("_"):
            continue
        info = _scan_file(path)
        if info:
            out.append(info)
    out.sort(key=lambda s: s.path)
    return out


def _yn(b: bool) -> str:
    return "Y" if b else "."


def _render_markdown(strats: list[StratInfo]) -> str:
    cols = ["strategy", "long", "short", "regime", "lev", "async", "sink", "loc"]
    out = ["| " + " | ".join(c.ljust(8) for c in cols) + " |", "|" + "|".join("-" * 10 for _ in cols) + "|"]
    for s in strats:
        out.append(
            "| "
            + " | ".join(
                [
                    s.name[:8].ljust(8),
                    _yn(s.supports_long).ljust(8),
                    _yn(s.supports_short).ljust(8),
                    _yn(s.uses_regime).ljust(8),
                    _yn(s.uses_leverage).ljust(8),
                    _yn(s.has_async).ljust(8),
                    _yn(s.has_decision_sink).ljust(8),
                    str(s.loc).ljust(8),
                ]
            )
            + " |",
        )
    return "\n".join(out)


def _render_csv(strats: list[StratInfo]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "name",
            "path",
            "long",
            "short",
            "regime",
            "leverage",
            "async",
            "decision_sink",
            "loc",
        ]
    )
    for s in strats:
        w.writerow(
            [
                s.name,
                s.path,
                s.supports_long,
                s.supports_short,
                s.uses_regime,
                s.uses_leverage,
                s.has_async,
                s.has_decision_sink,
                s.loc,
            ]
        )
    return buf.getvalue()


def _render_gaps(strats: list[StratInfo]) -> str:
    out: list[str] = []
    out.append("Strategies missing LONG support:")
    out.extend(f"  - {s.path}" for s in strats if not s.supports_long)
    out.append("")
    out.append("Strategies missing SHORT support:")
    out.extend(f"  - {s.path}" for s in strats if not s.supports_short)
    out.append("")
    out.append("Strategies NOT regime-aware:")
    out.extend(f"  - {s.path}" for s in strats if not s.uses_regime)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--csv", action="store_true")
    fmt.add_argument("--json", action="store_true")
    fmt.add_argument("--gap", action="store_true", help="only show strategies missing key caps")
    args = p.parse_args(argv)

    strats = _scan_all()
    if not strats:
        print("strategy-capability-matrix: no strategies found", file=sys.stderr)
        return 1

    if args.csv:
        print(_render_csv(strats))
    elif args.json:
        print(json.dumps([asdict(s) for s in strats], indent=2))
    elif args.gap:
        print(_render_gaps(strats))
    else:
        print(f"# Strategy capability matrix ({len(strats)} strategies)")
        print()
        print(_render_markdown(strats))
        print()
        print(
            f"Total: {len(strats)} strategies, "
            f"{sum(s.supports_long for s in strats)} long, "
            f"{sum(s.supports_short for s in strats)} short, "
            f"{sum(s.uses_regime for s in strats)} regime-aware"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
