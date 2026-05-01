"""Lightweight JARVIS dashboard state helpers.

This module keeps the dashboard read path intentionally small: it tails
canonical state artifacts and returns JSON-serializable panel payloads. The
drift card is active first because the promotion pipeline already writes a
structured drift journal.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

try:
    from eta_engine.brain.avengers.drift_detector import (
        DRIFT_JOURNAL as DEFAULT_DRIFT_JOURNAL,
    )
    from eta_engine.brain.avengers.drift_detector import (
        read_drift_journal,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - direct script execution guard
    if exc.name != "eta_engine":
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from eta_engine.brain.avengers.drift_detector import (
        DRIFT_JOURNAL as DEFAULT_DRIFT_JOURNAL,
    )
    from eta_engine.brain.avengers.drift_detector import (
        read_drift_journal,
    )

DRIFT_JOURNAL: Path = DEFAULT_DRIFT_JOURNAL

INDEX_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>JARVIS Dashboard</title>
  </head>
  <body>
    <section id="drift-card" aria-label="Strategy drift">
      <h1>JARVIS Drift Watch</h1>
      <dl>
        <dt>State</dt><dd id="drift-state">NO_DATA</dd>
        <dt>KL</dt><dd id="drift-kl">-</dd>
        <dt>Sharpe Delta</dt><dd id="drift-dsharpe">-</dd>
        <dt>Mean Delta</dt><dd id="drift-dmean">-</dd>
        <dt>Samples</dt><dd id="drift-n">-</dd>
        <dt>Reason</dt><dd id="drift-reason">No drift journal entries yet.</dd>
      </dl>
    </section>
  </body>
</html>
"""


def _number_or_none(value: object) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    return None


def _int_or_zero(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return int(value)
    return 0


def _text_or_empty(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _reason_text(value: object) -> str:
    if isinstance(value, list):
        return "; ".join(str(item) for item in value if str(item))
    return _text_or_empty(value)


def _empty_panel(name: str) -> dict[str, object]:
    return {
        "name": name,
        "state": "NO_DATA",
    }


def _render_drift() -> dict[str, object]:
    records = read_drift_journal(DRIFT_JOURNAL, n=50)
    if not records:
        return {
            "state": "NO_DATA",
            "journal": str(DRIFT_JOURNAL),
            "strategy_id": "",
            "kl": None,
            "sharpe_delta": None,
            "mean_delta": None,
            "n_live": 0,
            "n_backtest": 0,
            "entries": 0,
            "counts": {},
            "reason": "No drift journal entries yet.",
        }

    last = records[-1]
    counts = Counter(_text_or_empty(row.get("verdict")) or "UNKNOWN" for row in records)
    reason = _reason_text(last.get("reasons")) or _reason_text(last.get("reason"))
    return {
        "state": _text_or_empty(last.get("verdict")) or "UNKNOWN",
        "journal": str(DRIFT_JOURNAL),
        "strategy_id": _text_or_empty(last.get("strategy_id")),
        "kl": _number_or_none(last.get("kl_divergence")),
        "sharpe_delta": _number_or_none(last.get("sharpe_delta_sigma")),
        "mean_delta": _number_or_none(last.get("mean_return_delta")),
        "n_live": _int_or_zero(last.get("live_sample_size")),
        "n_backtest": _int_or_zero(last.get("bt_sample_size")),
        "entries": len(records),
        "counts": dict(counts),
        "reason": reason,
    }


def collect_state() -> dict[str, object]:
    return {
        "drift": _render_drift(),
        "sage": _render_sage(),
        "sage_edge": _render_sage_edge(),
        "breaker": _empty_panel("breaker"),
        "deadman": _empty_panel("deadman"),
        "forecast": _empty_panel("forecast"),
        "daemons": _empty_panel("daemons"),
        "promotion": _empty_panel("promotion"),
        "calibration": _empty_panel("calibration"),
        "journal": _empty_panel("journal"),
        "alerts": _empty_panel("alerts"),
    }


def _render_sage() -> dict[str, object]:
    try:
        from eta_engine.brain.jarvis_v3.sage.edge_tracker import default_tracker
        from eta_engine.brain.jarvis_v3.sage.health import default_monitor
        tracker = default_tracker()
        edges = tracker.snapshot()
        monitor = default_monitor()
        issues = monitor.check_health()
        return {
            "status": "ok",
            "n_schools_tracked": len(edges),
            "n_schools_degraded": len(issues),
            "degraded": [{"school": i.school, "severity": i.severity} for i in issues[:10]],
            "top_edge": [
                {"school": k, **v}
                for k, v in sorted(
                    edges.items(),
                    key=lambda x: x[1].get("expectancy", 0),
                    reverse=True,
                )[:10]
            ],
        }
    except Exception:
        return {"status": "unavailable", "error": "sage not loaded"}


def _render_sage_edge() -> dict[str, object]:
    try:
        from eta_engine.brain.jarvis_v3.sage.edge_tracker import default_tracker
        tracker = default_tracker()
        return {
            "status": "ok",
            "weights": tracker.all_weight_modifiers(),
            "snapshot": tracker.snapshot(),
        }
    except Exception:
        return {"status": "unavailable", "error": "edge tracker not loaded"}


def main() -> int:
    print(json.dumps(collect_state(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
