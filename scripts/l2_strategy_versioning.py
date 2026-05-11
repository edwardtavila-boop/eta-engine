"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_strategy_versioning
=============================================================
Strategy versioning + migration system.  Tracks config versions of
each L2 strategy so retired versions' results don't pollute the
current strategy's metrics.

Why this exists
---------------
Strategy iteration looks like:
  v1: entry_threshold=1.75, consecutive_snaps=3
  v2: entry_threshold=2.0,  consecutive_snaps=4

If both run in shadow and both write to l2_backtest_runs.jsonl
under strategy="book_imbalance", later evaluators will treat them
as one continuous strategy — but they're different things.  v1's
sharpe shouldn't be used to weight the ensemble that has v2 deployed.

This module:
  1. Records each strategy version's effective date + config hash
  2. Filters backtest records to the current version's effective window
  3. Provides a registry of all versions per strategy

Schema
------
``versions[strategy] = [{
    "version": "v1",
    "effective_from": iso_ts,
    "effective_to": iso_ts | null (null = current),
    "config_hash": str,
    "config": dict,
    "rationale": str,
}]``

Stored in logs/eta_engine/l2_strategy_versions.json (JSON, not JSONL,
because the schema is a tree, not a stream).

Run
---
::

    # Register a new version
    python -m eta_engine.scripts.l2_strategy_versioning \\
        --register --strategy book_imbalance --version v2 \\
        --config '{"entry_threshold": 2.0, "consecutive_snaps": 4}' \\
        --rationale "tighter threshold based on shadow soak"

    # List versions
    python -m eta_engine.scripts.l2_strategy_versioning --list

    # Show active version for a strategy
    python -m eta_engine.scripts.l2_strategy_versioning \\
        --active --strategy book_imbalance
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
VERSIONS_FILE = LOG_DIR / "l2_strategy_versions.json"


@dataclass
class StrategyVersion:
    version: str
    effective_from: str
    effective_to: str | None
    config_hash: str
    config: dict
    rationale: str = ""


@dataclass
class VersionsRegistry:
    versions: dict[str, list[StrategyVersion]] = field(default_factory=dict)


def _config_hash(config: dict) -> str:
    """Stable short hash of a config dict."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def load_registry(*, _path: Path | None = None) -> VersionsRegistry:
    path = _path if _path is not None else VERSIONS_FILE
    if not path.exists():
        return VersionsRegistry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        out: dict[str, list[StrategyVersion]] = {}
        for strategy, versions in data.get("versions", {}).items():
            out[strategy] = [StrategyVersion(**v) for v in versions]
        return VersionsRegistry(versions=out)
    except (OSError, json.JSONDecodeError, TypeError):
        return VersionsRegistry()


def save_registry(reg: VersionsRegistry,
                    *, _path: Path | None = None) -> None:
    path = _path if _path is not None else VERSIONS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"versions": {s: [asdict(v) for v in vs]
                                for s, vs in reg.versions.items()}}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"WARN: versions save failed: {e}", file=sys.stderr)


def register_version(strategy: str, version: str, config: dict,
                       *, rationale: str = "",
                       effective_from: datetime | None = None,
                       _path: Path | None = None) -> StrategyVersion:
    """Register a new version of a strategy.  Closes the prior version's
    effective_to window if it was open."""
    reg = load_registry(_path=_path)
    versions = reg.versions.setdefault(strategy, [])
    effective_from = effective_from or datetime.now(UTC)
    eff_from_iso = effective_from.isoformat()
    # Close any open prior version
    for prior in versions:
        if prior.effective_to is None:
            prior.effective_to = eff_from_iso
    new_version = StrategyVersion(
        version=version,
        effective_from=eff_from_iso,
        effective_to=None,
        config_hash=_config_hash(config),
        config=config,
        rationale=rationale,
    )
    versions.append(new_version)
    save_registry(reg, _path=_path)
    return new_version


def active_version(strategy: str,
                     *, _path: Path | None = None) -> StrategyVersion | None:
    reg = load_registry(_path=_path)
    versions = reg.versions.get(strategy, [])
    for v in reversed(versions):
        if v.effective_to is None:
            return v
    return None


def version_for_date(strategy: str, when: datetime,
                       *, _path: Path | None = None) -> StrategyVersion | None:
    """Return the version that was active for the given datetime."""
    reg = load_registry(_path=_path)
    versions = reg.versions.get(strategy, [])
    when_iso = when.isoformat()
    for v in versions:
        if v.effective_from <= when_iso and (v.effective_to is None or when_iso < v.effective_to):
            return v
    return None


def filter_records_to_version(records: list[dict], strategy: str,
                                 version: str,
                                 *, _path: Path | None = None) -> list[dict]:
    """Filter a list of records (must have 'ts' field) to those that
    fall within the given version's effective window."""
    reg = load_registry(_path=_path)
    versions = reg.versions.get(strategy, [])
    target = next((v for v in versions if v.version == version), None)
    if target is None:
        return []
    out: list[dict] = []
    eff_from = target.effective_from
    eff_to = target.effective_to
    for r in records:
        ts = r.get("ts")
        if not ts:
            continue
        ts_str = str(ts)
        if ts_str >= eff_from and (eff_to is None or ts_str < eff_to):
            out.append(r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--register", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--active", action="store_true")
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--version", default=None)
    ap.add_argument("--config", default=None,
                    help="JSON-encoded config dict")
    ap.add_argument("--rationale", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.register:
        if not args.strategy or not args.version or not args.config:
            print("--register requires --strategy + --version + --config",
                  file=sys.stderr)
            return 2
        config = json.loads(args.config)
        v = register_version(args.strategy, args.version, config,
                              rationale=args.rationale)
        if args.json:
            print(json.dumps(asdict(v), indent=2))
        else:
            print(f"Registered {args.strategy} {args.version} "
                    f"(hash {v.config_hash}, effective from {v.effective_from})")
        return 0

    if args.active:
        if not args.strategy:
            print("--active requires --strategy", file=sys.stderr)
            return 2
        v = active_version(args.strategy)
        if v is None:
            print(f"No active version for {args.strategy}")
            return 1
        if args.json:
            print(json.dumps(asdict(v), indent=2))
        else:
            print(f"Active version of {args.strategy}: {v.version}")
            print(f"  effective_from : {v.effective_from}")
            print(f"  config_hash    : {v.config_hash}")
            print(f"  rationale      : {v.rationale}")
            print(f"  config         : {json.dumps(v.config)}")
        return 0

    # Default to --list
    reg = load_registry()
    if args.json:
        print(json.dumps({s: [asdict(v) for v in vs]
                            for s, vs in reg.versions.items()}, indent=2))
        return 0
    print()
    print("=" * 78)
    print("L2 STRATEGY VERSIONS REGISTRY")
    print("=" * 78)
    if not reg.versions:
        print("  (no versions registered yet)")
        return 0
    for strategy in sorted(reg.versions.keys()):
        print(f"\n{strategy}:")
        for v in reg.versions[strategy]:
            active = " [ACTIVE]" if v.effective_to is None else ""
            print(f"  {v.version}{active}")
            print(f"    from   : {v.effective_from}")
            print(f"    to     : {v.effective_to or '(open)'}")
            print(f"    hash   : {v.config_hash}")
            if v.rationale:
                print(f"    why    : {v.rationale}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
