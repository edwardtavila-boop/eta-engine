from __future__ import annotations

from eta_engine.feeds import health_check as feed_health_check
from eta_engine.scripts import health_check as script_health_check


def test_feed_health_check_reexports_script_contract() -> None:
    assert feed_health_check.HealthComponent is script_health_check.HealthComponent
    assert feed_health_check.VpsHealthReport is script_health_check.VpsHealthReport
    assert feed_health_check.run_health_check is script_health_check.run_health_check


def test_feed_health_check_main_delegates_to_script_main(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 23

    monkeypatch.setattr(script_health_check, "main", _fake_main)

    assert feed_health_check.main(["--output-dir", "C:/tmp/eta-health"]) == 23
    assert seen["argv"] == ["--output-dir", "C:/tmp/eta-health"]
