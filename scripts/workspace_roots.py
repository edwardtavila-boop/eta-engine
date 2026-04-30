r"""Canonical ETA workspace paths for script defaults.

These helpers keep research and ops scripts anchored under the single
workspace root instead of legacy external data roots or per-user app paths.
"""

from __future__ import annotations

from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
ETA_ENGINE_ROOT = Path(__file__).resolve().parents[1]

MNQ_DATA_ROOT = WORKSPACE_ROOT / "mnq_data"
MNQ_HISTORY_ROOT = MNQ_DATA_ROOT / "history"

CRYPTO_DATA_ROOT = WORKSPACE_ROOT / "data" / "crypto"
CRYPTO_HISTORY_ROOT = CRYPTO_DATA_ROOT / "history"
CRYPTO_IBKR_HISTORY_ROOT = CRYPTO_DATA_ROOT / "ibkr" / "history"
CRYPTO_ONCHAIN_ROOT = CRYPTO_DATA_ROOT / "onchain"
CRYPTO_SENTIMENT_ROOT = CRYPTO_DATA_ROOT / "sentiment"
CRYPTO_MACRO_ROOT = CRYPTO_DATA_ROOT / "macro"

ROOT_LOGS_DIR = WORKSPACE_ROOT / "logs"
ROOT_VAR_DIR = WORKSPACE_ROOT / "var"
ETA_RUNTIME_STATE_DIR = ROOT_VAR_DIR / "eta_engine" / "state"
ETA_RUNTIME_LOG_DIR = ROOT_LOGS_DIR / "eta_engine"
ETA_RUNTIME_DECISION_JOURNAL_PATH = ETA_RUNTIME_STATE_DIR / "decision_journal.jsonl"
ETA_DATA_INVENTORY_SNAPSHOT_PATH = ETA_RUNTIME_STATE_DIR / "data_inventory_latest.json"
ETA_RESEARCH_GRID_RUNTIME_DIR = ETA_RUNTIME_STATE_DIR / "research_grid"
ETA_LIVE_DATA_RUNTIME_DIR = ETA_RUNTIME_STATE_DIR / "live_data"
ETA_TRADINGVIEW_AUTH_STATE_PATH = ETA_RUNTIME_STATE_DIR / "tradingview_auth.json"
ETA_TRADINGVIEW_DATA_ROOT = ETA_LIVE_DATA_RUNTIME_DIR / "tradingview"
ETA_OPERATOR_QUEUE_SNAPSHOT_PATH = ETA_RUNTIME_STATE_DIR / "operator_queue_snapshot.json"
ETA_OPERATOR_QUEUE_PREVIOUS_SNAPSHOT_PATH = ETA_RUNTIME_STATE_DIR / "operator_queue_snapshot.previous.json"
ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH = ETA_RUNTIME_STATE_DIR / "bot_strategy_readiness_latest.json"
ETA_STRATEGY_SUPERCHARGE_SCORECARD_PATH = ETA_RUNTIME_STATE_DIR / "strategy_supercharge_scorecard_latest.json"
ETA_STRATEGY_SUPERCHARGE_MANIFEST_PATH = ETA_RUNTIME_STATE_DIR / "strategy_supercharge_manifest_latest.json"
ETA_STRATEGY_SUPERCHARGE_RESULTS_PATH = ETA_RUNTIME_STATE_DIR / "strategy_supercharge_results_latest.json"
ETA_DRIFT_WATCHDOG_LOG_PATH = ETA_RUNTIME_STATE_DIR / "drift_watchdog.jsonl"
ETA_JARVIS_DRIFT_JOURNAL_PATH = ETA_RUNTIME_STATE_DIR / "jarvis_drift.jsonl"
ETA_SHARED_BREAKER_STATE_PATH = ETA_RUNTIME_STATE_DIR / "breaker.json"
ETA_DEADMAN_SENTINEL_PATH = ETA_RUNTIME_STATE_DIR / "operator.sentinel"
ETA_DEADMAN_JOURNAL_PATH = ETA_RUNTIME_STATE_DIR / "operator_activity.jsonl"
ETA_PROMOTION_STATE_PATH = ETA_RUNTIME_STATE_DIR / "promotion.json"
ETA_PROMOTION_JOURNAL_PATH = ETA_RUNTIME_STATE_DIR / "promotion.jsonl"
ETA_AVENGERS_JOURNAL_PATH = ETA_RUNTIME_STATE_DIR / "avengers.jsonl"
ETA_CALIBRATION_JOURNAL_PATH = ETA_RUNTIME_STATE_DIR / "calibration.jsonl"
ETA_AVENGER_DAEMON_PID_DIR = ETA_RUNTIME_STATE_DIR / "avenger_daemons"
ETA_RUNTIME_ALERTS_LOG_PATH = ETA_RUNTIME_LOG_DIR / "alerts_log.jsonl"
ETA_RUNTIME_LOG_PATH = ETA_RUNTIME_LOG_DIR / "runtime_log.jsonl"
ETA_AVENGER_METRICS_PATH = ETA_RUNTIME_LOG_DIR / "metrics.prom"
ETA_LEGACY_DOCS_DRIFT_WATCHDOG_LOG_PATH = ETA_ENGINE_ROOT / "docs" / "drift_watchdog.jsonl"
ETA_LEGACY_DOCS_ALERTS_LOG_PATH = ETA_ENGINE_ROOT / "docs" / "alerts_log.jsonl"
ETA_LEGACY_DOCS_RUNTIME_LOG_PATH = ETA_ENGINE_ROOT / "docs" / "runtime_log.jsonl"
ETA_LEGACY_JARVIS_DRIFT_JOURNAL_PATH = Path.home() / ".jarvis" / "drift.jsonl"
ETA_LEGACY_SHARED_BREAKER_STATE_PATH = Path.home() / ".jarvis" / "breaker.json"
ETA_LEGACY_DEADMAN_SENTINEL_PATH = Path.home() / ".jarvis" / "operator.sentinel"
ETA_LEGACY_DEADMAN_JOURNAL_PATH = Path.home() / ".jarvis" / "operator_activity.jsonl"
ETA_LEGACY_PROMOTION_STATE_PATH = Path.home() / ".jarvis" / "promotion.json"
ETA_LEGACY_PROMOTION_JOURNAL_PATH = Path.home() / ".jarvis" / "promotion.jsonl"
ETA_LEGACY_AVENGERS_JOURNAL_PATH = Path.home() / ".jarvis" / "avengers.jsonl"
ETA_LEGACY_CALIBRATION_JOURNAL_PATH = Path.home() / ".jarvis" / "calibration.jsonl"


def ensure_dir(path: Path) -> Path:
    """Create a directory tree when a script writes outputs there."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_parent(path: Path) -> Path:
    """Create the parent directory for a file path and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def default_alerts_log_path() -> Path:
    """Prefer the canonical runtime alert log, with legacy docs fallback.

    Read-only diagnostics use this so older snapshots remain inspectable
    while live runtime writes stay out of tracked docs.
    """
    if ETA_RUNTIME_ALERTS_LOG_PATH.exists() or not ETA_LEGACY_DOCS_ALERTS_LOG_PATH.exists():
        return ETA_RUNTIME_ALERTS_LOG_PATH
    return ETA_LEGACY_DOCS_ALERTS_LOG_PATH


def default_runtime_log_path() -> Path:
    """Prefer the canonical runtime log, with legacy docs fallback."""
    if ETA_RUNTIME_LOG_PATH.exists() or not ETA_LEGACY_DOCS_RUNTIME_LOG_PATH.exists():
        return ETA_RUNTIME_LOG_PATH
    return ETA_LEGACY_DOCS_RUNTIME_LOG_PATH


def default_drift_watchdog_log_path() -> Path:
    """Prefer canonical drift-watchdog state, with legacy docs fallback."""
    if ETA_DRIFT_WATCHDOG_LOG_PATH.exists() or not ETA_LEGACY_DOCS_DRIFT_WATCHDOG_LOG_PATH.exists():
        return ETA_DRIFT_WATCHDOG_LOG_PATH
    return ETA_LEGACY_DOCS_DRIFT_WATCHDOG_LOG_PATH
