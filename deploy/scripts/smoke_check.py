"""
Deploy // smoke_check
=====================
Post-install sanity check. Run BEFORE starting the systemd services.

Verifies:
  1. Python imports all core modules (no syntax / import errors)
  2. .env file has required variables
  3. State + log directories are writable
  4. Avengers dispatch plan for a sample context runs without Claude
  5. All 12 BackgroundTask handlers can be instantiated
  6. systemd --user units are present (if install_vps.sh ran step 6)
  7. Crontab has the eta-engine:avengers tag (if install step 7 ran)

Exit 0 on clean; exit non-zero with a table of failures.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REQUIRED_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "JARVIS_HOURLY_USD_BUDGET",
    "JARVIS_DAILY_USD_BUDGET",
)
OPTIONAL_ENV_VARS = (
    "TRADOVATE_USERNAME",
    "TRADOVATE_CID",
    "TRADOVATE_APP_SECRET",
)


def check_imports() -> tuple[bool, str]:
    try:
        from eta_engine.brain.avengers import (
            AvengersDispatch,  # noqa: F401
            Fleet,  # noqa: F401
        )
        from eta_engine.brain.jarvis_v3.claude_layer.cost_governor import (
            CostGovernor,  # noqa: F401
        )

        return True, "imports OK"
    except Exception as exc:  # noqa: BLE001
        return False, f"import failure: {exc}"


def check_env(env_path: Path) -> tuple[bool, str]:
    if not env_path.exists():
        return False, f".env not found at {env_path}"
    text = env_path.read_text(encoding="utf-8")
    missing: list[str] = []
    for var in REQUIRED_ENV_VARS:
        if f"{var}=" not in text:
            missing.append(var)
    if missing:
        return False, f"missing required vars in .env: {', '.join(missing)}"
    return True, ".env looks complete"


def check_dirs() -> tuple[bool, str]:
    state_dir = Path.home() / ".local" / "state" / "eta_engine"
    log_dir = Path.home() / ".local" / "log" / "eta_engine"
    for d in (state_dir, log_dir):
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_probe"
            probe.write_text("ok")
            probe.unlink()
        except Exception as exc:  # noqa: BLE001
            return False, f"{d} not writable: {exc}"
    return True, f"dirs OK ({state_dir}, {log_dir})"


def check_dispatch() -> tuple[bool, str]:
    try:
        from eta_engine.brain.avengers import (
            AvengersDispatch,
            DryRunExecutor,
            Fleet,
        )
        from eta_engine.brain.jarvis_v3.claude_layer.cost_governor import (
            CostGovernor,
        )
        from eta_engine.brain.jarvis_v3.claude_layer.escalation import (
            EscalationInputs,
        )
        from eta_engine.brain.jarvis_v3.claude_layer.prompts import (
            StructuredContext,
        )
        from eta_engine.brain.jarvis_v3.claude_layer.stakes import (
            StakesInputs,
        )
        from eta_engine.brain.jarvis_v3.claude_layer.usage_tracker import (
            UsageTracker,
        )

        fleet = Fleet(executor=DryRunExecutor())
        gov = CostGovernor(UsageTracker())
        disp = AvengersDispatch(governor=gov, fleet=fleet)
        ctx = StructuredContext(
            ts="smoke",
            subsystem="bot.mnq",
            action="ORDER_PLACE",
            regime="NEUTRAL",
            regime_confidence=0.8,
            session_phase="MORNING",
            stress_composite=0.2,
            binding_constraint="equity_dd",
            sizing_mult=0.9,
            hours_until_event=None,
            event_label=None,
            r_at_risk=1.0,
            daily_dd_pct=0.01,
            portfolio_breach=False,
            doctrine_net_bias=-0.1,
            precedent_n=30,
            precedent_win_rate=0.6,
            precedent_mean_r=0.4,
            operator_overrides_24h=0,
            jarvis_baseline_verdict="APPROVED",
        )
        result = disp.decide(
            escalation_inputs=EscalationInputs(
                regime="NEUTRAL",
                stress_composite=0.2,
                precedent_n=30,
            ),
            stakes_inputs=StakesInputs(),
            context=ctx,
        )
        return True, f"dispatch OK -- route={result.route.value}, vote={result.final_vote}"
    except Exception as exc:  # noqa: BLE001
        return False, f"dispatch failure: {exc}"


def check_task_handlers() -> tuple[bool, str]:
    try:
        from eta_engine.brain.avengers import BackgroundTask
        from eta_engine.deploy.scripts.run_task import HANDLERS

        missing = [t for t in BackgroundTask if t not in HANDLERS]
        if missing:
            return False, f"missing handlers: {[t.value for t in missing]}"
        return True, f"all {len(HANDLERS)} task handlers wired"
    except Exception as exc:  # noqa: BLE001
        return False, f"task handler check failure: {exc}"


def check_systemd() -> tuple[bool, str]:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    expected = {"jarvis-live.service", "avengers-fleet.service"}
    found = {p.name for p in unit_dir.glob("*.service")} if unit_dir.exists() else set()
    missing = expected - found
    if missing:
        return False, f"systemd units missing: {missing}. Re-run install_vps.sh step 6."
    return True, f"systemd units present: {sorted(found)}"


def check_crontab() -> tuple[bool, str]:
    try:
        out = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        return False, f"crontab check failed: {exc}"
    if "eta-engine:avengers" not in out:
        return False, "no eta-engine:avengers tag in crontab. Re-run install step 7."
    count = out.count("eta-engine:avengers")
    return True, f"crontab has {count} Avengers entries"


CHECKS = (
    ("imports", check_imports),
    ("env", lambda: check_env(Path.cwd() / ".env")),
    ("state/log dirs", check_dirs),
    ("dispatch", check_dispatch),
    ("task handlers", check_task_handlers),
    ("systemd", check_systemd),
    ("crontab", check_crontab),
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-systemd", action="store_true", help="skip systemd + crontab checks (useful pre-install)")
    args = ap.parse_args(argv)

    print("EVOLUTIONARY TRADING ALGO  //  smoke check")
    print("=" * 60)
    failures = 0
    for name, fn in CHECKS:
        if args.skip_systemd and name in {"systemd", "crontab"}:
            continue
        ok, msg = fn()
        tag = "\033[32m[ OK ]\033[0m" if ok else "\033[31m[FAIL]\033[0m"
        print(f"{tag}  {name:18s}  {msg}")
        if not ok:
            failures += 1
    print("=" * 60)
    if failures:
        print(f"\033[31m{failures} check(s) failed\033[0m")
        return 1
    print("\033[32mall checks passed\033[0m")
    return 0


if __name__ == "__main__":
    # Mode where we're invoked by install_vps.sh before systemd is wired
    if os.environ.get("SMOKE_PRE_INSTALL"):
        sys.argv.append("--skip-systemd")
    raise SystemExit(main())
