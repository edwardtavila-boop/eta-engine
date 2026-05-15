from __future__ import annotations

from eta_engine.scripts import health_check as _script_health_check

HealthComponent = _script_health_check.HealthComponent
VpsHealthReport = _script_health_check.VpsHealthReport
build_parser = _script_health_check.build_parser
run_health_check = _script_health_check.run_health_check

__all__ = [
    "HealthComponent",
    "VpsHealthReport",
    "build_parser",
    "run_health_check",
    "main",
]


def main(argv: list[str] | None = None) -> int:
    return _script_health_check.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
