"""MLflow experiment tracker for Sage backtests and parameter sweeps.

Wires into ``backtester.py`` to log every backtest run as an MLflow
experiment with params, metrics, and artifacts. Enables reproducible
experimentation and automatic comparison across sweep iterations.

Activation: set ``ETA_MLFLOW_TRACKING_URI`` env var (defaults to
``state/mlflow`` as a local file store). Falls back silently when
mlflow is not installed.

Usage::

    from eta_engine.brain.jarvis_v3.sage.mlflow_tracker import track_backtest

    with track_backtest(name="sage_sweep_2026", params={"window_bars": 120}) as run:
        # ... run backtest ...
        run.log_metrics({"avg_realized_r": 0.45, "avg_alignment": 0.72})
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

_DEFAULT_TRACKING_URI = (Path(__file__).resolve().parents[4] / "state" / "mlflow").as_posix()


def _mlflow_available() -> bool:
    try:
        import mlflow  # noqa: F401

        return True
    except ImportError:
        return False


@contextlib.contextmanager
def track_backtest(
    *,
    name: str = "sage_backtest",
    params: dict[str, Any] | None = None,
    tracking_uri: str | None = None,
    experiment: str = "sage_backtests",
) -> Iterator[_FakeRun | _MlflowRun]:
    """Context manager that creates an MLflow run for a backtest.

    Logs params at entry, metrics on exit, and auto-closes the run.
    Silent no-op when mlflow is not installed.
    """
    if not _mlflow_available():
        yield _FakeRun()
        return

    import mlflow

    uri = tracking_uri or os.environ.get("ETA_MLFLOW_TRACKING_URI", _DEFAULT_TRACKING_URI)
    try:
        mlflow.set_tracking_uri(uri)
        mlflow.set_experiment(experiment)
    except Exception as exc:  # noqa: BLE001
        logger.debug("MLflow tracking URI setup failed: %s", exc)
        yield _FakeRun()
        return

    started_at = time.time()
    try:
        active_run = mlflow.start_run(run_name=name)
        if params:
            mlflow.log_params(params)
    except Exception as exc:  # noqa: BLE001
        logger.debug("MLflow start_run failed: %s", exc)
        yield _FakeRun()
        return

    wrapper = _MlflowRun(active_run)
    try:
        yield wrapper
    finally:
        try:
            elapsed = time.time() - started_at
            mlflow.log_metric("elapsed_seconds", round(elapsed, 2))
            mlflow.end_run()
        except Exception as exc:  # noqa: BLE001
            logger.debug("MLflow end_run failed: %s", exc)


class _MlflowRun:
    def __init__(self, run: object) -> None:
        self._run = run

    def log_metrics(self, metrics: dict[str, float]) -> None:
        try:
            import mlflow

            for k, v in metrics.items():
                mlflow.log_metric(k, v)
        except Exception:  # noqa: BLE001
            pass

    def log_artifact(self, path: str | Path) -> None:
        try:
            import mlflow

            mlflow.log_artifact(str(path))
        except Exception:  # noqa: BLE001
            pass

    def log_params(self, params: dict[str, Any]) -> None:
        try:
            import mlflow

            mlflow.log_params(params)
        except Exception:  # noqa: BLE001
            pass


class _FakeRun:
    def log_metrics(self, metrics: dict[str, float]) -> None:
        pass

    def log_artifact(self, path: str | Path) -> None:
        pass

    def log_params(self, params: dict[str, Any]) -> None:
        pass
