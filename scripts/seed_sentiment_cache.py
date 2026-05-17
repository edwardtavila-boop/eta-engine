"""Seed the canonical ETA sentiment cache with synthetic fallback snapshots."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eta_engine.brain.jarvis_v3 import sentiment_overlay
from eta_engine.scripts import workspace_roots

DEFAULT_SEED_VALUES: dict[str, dict[str, float]] = {
    "BTC": {
        "fear_greed": 0.58,
        "social_volume_z": 0.12,
    },
    "ETH": {
        "fear_greed": 0.51,
        "social_volume_z": -0.08,
    },
}
DEFAULT_ASSETS = tuple(DEFAULT_SEED_VALUES)
DEFAULT_SEEDED_REASON = "LunarCrush MCP not available; seeded default"


def build_seed_snapshot(
    asset_class: str,
    *,
    asof: datetime | None = None,
    seeded_reason: str = DEFAULT_SEEDED_REASON,
) -> dict[str, Any]:
    """Return the synthetic fallback sentiment snapshot for ``asset_class``."""
    asset_key = asset_class.strip().upper()
    if asset_key not in DEFAULT_SEED_VALUES:
        raise ValueError(f"unsupported seeded asset: {asset_class}")

    ts = (asof or datetime.now(UTC)).astimezone(UTC).isoformat()
    seed_values = DEFAULT_SEED_VALUES[asset_key]
    return {
        "fear_greed": seed_values["fear_greed"],
        "social_volume_z": seed_values["social_volume_z"],
        "topic_flags": {
            "squeeze": False,
            "capitulation": False,
            "fomo": False,
        },
        "raw_source": "lunarcrush_seeded",
        "extras": {
            "synthetic": True,
            "seeded_reason": seeded_reason,
            "seeded_at": ts,
        },
        "asof": ts,
    }


def seed_sentiment_cache(
    *,
    cache_dir: Path | None = None,
    assets: tuple[str, ...] = DEFAULT_ASSETS,
    seeded_reason: str = DEFAULT_SEEDED_REASON,
    asof: datetime | None = None,
) -> dict[str, Any]:
    """Write seeded fallback sentiment snapshots into the canonical cache."""
    target_dir = Path(cache_dir) if cache_dir is not None else workspace_roots.ETA_SENTIMENT_CACHE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, bool] = {}
    snapshots: dict[str, dict[str, Any]] = {}
    for asset in assets:
        asset_key = asset.strip().upper()
        snapshot = build_seed_snapshot(asset_key, asof=asof, seeded_reason=seeded_reason)
        snapshots[asset_key] = snapshot
        written[asset_key] = sentiment_overlay.write_sentiment_snapshot(
            asset_key,
            snapshot,
            cache_dir=target_dir,
        )

    return {
        "cache_dir": str(target_dir),
        "assets": list(assets),
        "written": written,
        "snapshots": snapshots,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=workspace_roots.ETA_SENTIMENT_CACHE_DIR,
        help="Override the canonical sentiment cache directory.",
    )
    parser.add_argument(
        "--asset",
        action="append",
        dest="assets",
        help="Repeatable asset class to seed. Defaults to BTC and ETH.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the result payload as JSON.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    assets = tuple(args.assets) if args.assets else DEFAULT_ASSETS
    payload = seed_sentiment_cache(cache_dir=args.cache_dir, assets=assets)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"cache_dir: {payload['cache_dir']}")
        for asset in payload["assets"]:
            print(f"{asset}: write={payload['written'][asset]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
