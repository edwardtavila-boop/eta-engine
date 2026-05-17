"""Compatibility wrapper for the canonical MNQ latency scorecard."""

from __future__ import annotations

from eta_engine.scripts.mnq_latency_scorecard import main


if __name__ == "__main__":
    raise SystemExit(main())
