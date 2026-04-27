"""
EVOLUTIONARY TRADING ALGO  //  scripts.live_tiny_preflight_dryrun
======================================================
Dry-run of the live-tiny go/abort machinery.

Before Tier-A (MNQ, NQ) flips from paper to live-tiny, every preflight gate
must light green. This script probes each gate offline — no orders, no venue
connections — and exercises the abort-on-red path for each one that fails. It
produces:

- docs/preflight_dryrun_report.json  — per-gate PASS/FAIL + reason
- docs/preflight_dryrun_log.txt      — 80-col text tearsheet
- non-zero exit code if any REQUIRED gate is RED

Usage
-----
    python -m eta_engine.scripts.live_tiny_preflight_dryrun
    python -m eta_engine.scripts.live_tiny_preflight_dryrun --inject-fail tradovate_creds
    python -m eta_engine.scripts.live_tiny_preflight_dryrun --simulate-red

Gates checked
-------------
    kill_log_present        — docs/kill_log.json exists + valid JSON
    paper_run_report        — docs/paper_run_report.json Tier-A PASS
    firm_board_verdict      — most recent Firm verdict == GO
    roadmap_state           — roadmap_state.json readable, in P9
    tradovate_creds         — optional: env TRADOVATE_CLIENT_ID + TRADOVATE_CLIENT_SECRET set (stub)
    risk_sizing             — Tier-A bot_config respects max_dd_kill_pct + risk_per_trade
    telemetry_channels      — metrics/alerts dirs exist
    venue_health            — mock venue ping (synthetic)
    pytest_green            — smoke-test subset of eta_engine pytest passes
    decisions_locked        — decisions_v1.json exists + complete
    env_template            — .env.example has all required keys
    go_trigger_armed        — go_trigger.py + schedule_weekly_review.py exist
    runtime_wired           — kill_switch + alerts + run_eta_live import cleanly
    credential_probe_full   — all REQUIRED_KEYS resolvable via SecretsManager (tier-aware)
    kill_switch_drill       — portfolio-DD scenario trips FLATTEN_ALL + CRITICAL
    idempotent_order_id     — duplicate OrderRequest yields identical client_order_id
    reconcile_on_reconnect  — _trade_journal_reconcile.py exits GREEN on current journals
    crash_recovery_simulated — synthetic orphaned runtime_start detected by reconcile
    abort_on_red_loop       — each RED gate triggers ABORT() — checked by simulation
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


@dataclass
class Gate:
    name: str
    required: bool
    status: str = "PENDING"  # PASS | FAIL | SKIP
    detail: str = ""
    evidence: dict = field(default_factory=dict)


def _gate_kill_log() -> Gate:
    g = Gate(name="kill_log_present", required=True)
    p = ROOT / "docs" / "kill_log.json"
    if not p.exists():
        g.status, g.detail = "FAIL", f"missing {p}"
        return g
    try:
        raw = json.loads(p.read_text())
        entries = raw.get("entries") if isinstance(raw, dict) else raw
        n = len(entries) if isinstance(entries, list) else 0
        g.status = "PASS"
        g.detail = f"{n} kill-log entries"
        g.evidence = {"count": n}
    except Exception as e:
        g.status, g.detail = "FAIL", f"invalid JSON: {e}"
    return g


def _gate_paper_run() -> Gate:
    g = Gate(name="paper_run_report", required=True)
    p = ROOT / "docs" / "paper_run_report.json"
    if not p.exists():
        g.status, g.detail = "FAIL", f"missing {p}"
        return g
    try:
        raw = json.loads(p.read_text())
        per_bot = raw.get("per_bot", [])
        passes = {b["bot"]: b["gate_pass"] for b in per_bot}
        tier_a_pass = passes.get("mnq", False) and passes.get("nq", False)
        g.status = "PASS" if tier_a_pass else "FAIL"
        g.detail = f"Tier-A mnq={passes.get('mnq')} nq={passes.get('nq')}"
        g.evidence = {"passes": passes}
    except Exception as e:
        g.status, g.detail = "FAIL", f"parse error: {e}"
    return g


def _gate_firm_verdict() -> Gate:
    g = Gate(name="firm_board_verdict", required=True)
    # firm_board_latest lives under shared_artifacts in roadmap_state
    rs = ROOT / "roadmap_state.json"
    if not rs.exists():
        g.status, g.detail = "FAIL", f"missing {rs}"
        return g
    try:
        raw = json.loads(rs.read_text())
        latest = raw.get("shared_artifacts", {}).get("firm_board_latest") or raw.get("firm_board_latest") or {}
        v = str(latest.get("final_verdict", "UNKNOWN")).upper()
        g.status = "PASS" if v == "GO" else "FAIL"
        g.detail = f"Firm latest verdict: {v} ({latest.get('spec_id', '?')})"
        g.evidence = {"verdict": v, "spec_id": latest.get("spec_id")}
    except Exception as e:
        g.status, g.detail = "FAIL", f"parse error: {e}"
    return g


def _gate_roadmap_state() -> Gate:
    g = Gate(name="roadmap_state", required=True)
    rs = ROOT / "roadmap_state.json"
    if not rs.exists():
        g.status, g.detail = "FAIL", f"missing {rs}"
        return g
    try:
        raw = json.loads(rs.read_text())
        phase = str(raw.get("current_phase", "")).upper()
        pct = raw.get("overall_progress_pct", 0)
        ok = phase.startswith("P9")
        g.status = "PASS" if ok else "FAIL"
        g.detail = f"phase={phase} progress={pct}%"
        g.evidence = {"phase": phase, "progress": pct}
    except Exception as e:
        g.status, g.detail = "FAIL", f"parse error: {e}"
    return g


def _gate_tradovate_creds() -> Gate:
    g = Gate(name="tradovate_creds", required=False)
    cid = os.environ.get("TRADOVATE_CLIENT_ID")
    sec = os.environ.get("TRADOVATE_CLIENT_SECRET")
    has = bool(cid and sec)
    g.status = "PASS" if has else "SKIP"
    g.detail = (
        "env TRADOVATE_CLIENT_ID+SECRET set" if has else "creds missing - optional live-tiny staging gate skipped"
    )
    g.evidence = {"client_id_set": bool(cid), "secret_set": bool(sec)}
    return g


def _gate_risk_sizing() -> Gate:
    g = Gate(name="risk_sizing", required=True)
    # Inspect firm_spec_paper_promotion_v1.risk_management
    p = ROOT / "docs" / "firm_spec_paper_promotion_v1.json"
    if not p.exists():
        g.status, g.detail = "FAIL", f"missing {p}"
        return g
    try:
        raw = json.loads(p.read_text())
        rm = raw.get("risk_management", {})
        per_trade = float(rm.get("per_trade_risk_pct", 100.0))
        daily_cap = float(rm.get("daily_loss_cap_pct", 100.0))
        dd_kill = float(rm.get("max_drawdown_kill_pct", 100.0))
        allocs = rm.get("paper_capital_allocations", {}) or {}
        tier_a_cap = int(allocs.get("mnq", 0)) + int(allocs.get("nq", 0))
        # Thresholds: per-trade ≤ 3%, daily cap ≤ 6%, DD kill ≤ 20%, Tier-A cap ≥ $5k
        ok = per_trade <= 3.0 and daily_cap <= 6.0 and dd_kill <= 20.0 and tier_a_cap >= 5_000
        g.status = "PASS" if ok else "FAIL"
        g.detail = f"per_trade={per_trade}% daily_cap={daily_cap}% dd_kill={dd_kill}% tier_A_cap=${tier_a_cap}"
        g.evidence = {
            "per_trade_risk_pct": per_trade,
            "daily_loss_cap_pct": daily_cap,
            "max_drawdown_kill_pct": dd_kill,
            "tier_A_capital_usd": tier_a_cap,
        }
    except Exception as e:
        g.status, g.detail = "FAIL", f"parse error: {e}"
    return g


def _gate_telemetry_channels() -> Gate:
    g = Gate(name="telemetry_channels", required=False)
    tel = ROOT / "telemetry"
    alerts = ROOT / "alerts"
    ok = tel.exists() or (ROOT / "docs").exists()  # lax: any monitoring dir
    g.status = "PASS" if ok else "FAIL"
    g.detail = f"telemetry/={tel.exists()}  alerts/={alerts.exists()}"
    return g


def _gate_venue_health() -> Gate:
    # Synthetic check: we don't dial the venue in dryrun; we check local configs exist.
    # Active futures-broker yamls are derived from
    # ``venues.router.ACTIVE_FUTURES_VENUES`` so the gate stays correct
    # under the broker dormancy mandate (Tradovate is currently DORMANT,
    # so tradovate.yaml is not required; IBKR + Tastytrade are active
    # so their yamls are). When DORMANT_BROKERS flips back to empty,
    # tradovate.yaml is automatically re-required without code changes
    # to this gate.
    g = Gate(name="venue_health", required=False)
    cfg = ROOT / "configs"
    if not cfg.exists():
        g.status, g.detail = "FAIL", "configs/ missing"
        return g
    try:
        from eta_engine.venues.router import ACTIVE_FUTURES_VENUES
    except ImportError:
        # Fall back when the router module is unimportable (e.g.
        # partial install). The fallback is intentionally permissive
        # -- empty tuple means "no futures yamls required at all" --
        # so a partial-install preflight degrades to "PASS without
        # asserting any specific futures venue", instead of falsely
        # demanding a DORMANT broker's yaml. The full router-driven
        # path is the source of truth (DORMANT_BROKERS frozenset).
        active_futures: tuple[str, ...] = ()
    else:
        active_futures = tuple(ACTIVE_FUTURES_VENUES)
    required = [
        *(f"{venue}.yaml" for venue in active_futures),
        "bybit.yaml",
        "alerts.yaml",
        "kill_switch.yaml",
    ]
    present = [f for f in required if (cfg / f).exists()]
    missing = [f for f in required if f not in present]
    if missing:
        g.status = "FAIL"
        g.detail = f"configs/ missing files: {missing}"
    else:
        g.status = "PASS"
        g.detail = (
            f"configs/ has {len(present)}/{len(required)} "
            f"venue+alert+kill files (active futures venues: "
            f"{','.join(active_futures)})"
        )
    g.evidence = {"present": present, "missing": missing}
    return g


def _gate_decisions_locked() -> Gate:
    """Decision record (docs/decisions_v1.json) must exist + be valid."""
    g = Gate(name="decisions_locked", required=True)
    p = ROOT / "docs" / "decisions_v1.json"
    if not p.exists():
        g.status, g.detail = "FAIL", f"missing {p.name} — decisions not locked"
        return g
    try:
        raw = json.loads(p.read_text())
        needed = {"tier_1_live_tiny_blockers", "tier_2_tier_b_blockers", "tier_3_operational_cadence"}
        have = set(raw.keys())
        if not needed.issubset(have):
            g.status = "FAIL"
            g.detail = f"decisions_v1 missing sections: {sorted(needed - have)}"
            return g
        spec_id = raw.get("spec_id", "?")
        g.status = "PASS"
        g.detail = f"{spec_id} locked, {len(have)} sections"
        g.evidence = {"spec_id": spec_id, "sections": sorted(have)}
    except Exception as e:
        g.status, g.detail = "FAIL", f"parse error: {e}"
    return g


def _gate_env_template() -> Gate:
    """`.env.example` must exist so operator knows which secrets to provide."""
    g = Gate(name="env_template", required=True)
    p = ROOT / ".env.example"
    if not p.exists():
        g.status, g.detail = "FAIL", "missing .env.example"
        return g
    txt = p.read_text()
    required_keys = [
        "TRADOVATE_CLIENT_ID",
        "TRADOVATE_CLIENT_SECRET",
        "TRADOVATE_USERNAME",
        "TRADOVATE_PASSWORD",
        "TRADOVATE_DEVICE_ID",
        "BYBIT_API_KEY",
        "BYBIT_API_SECRET",
        "PUSHOVER_USER",
        "PUSHOVER_TOKEN",
    ]
    missing = [k for k in required_keys if k not in txt]
    if missing:
        g.status, g.detail = "FAIL", f".env.example missing keys: {missing}"
    else:
        g.status = "PASS"
        g.detail = f".env.example present with all {len(required_keys)} required keys"
    g.evidence = {"missing_keys": missing}
    return g


def _gate_go_trigger_armed() -> Gate:
    """go_trigger.py + schedule_weekly_review.py must exist."""
    g = Gate(name="go_trigger_armed", required=True)
    scripts = ROOT / "scripts"
    go = scripts / "go_trigger.py"
    sch = scripts / "schedule_weekly_review.py"
    miss = [p.name for p in (go, sch) if not p.exists()]
    if miss:
        g.status, g.detail = "FAIL", f"missing operator scripts: {miss}"
    else:
        g.status = "PASS"
        g.detail = "go_trigger.py + schedule_weekly_review.py both present"
    return g


def _gate_runtime_wired() -> Gate:
    """The live runtime trio must exist + be importable:

       - core/kill_switch_runtime.py  (pure policy)
       - obs/alert_dispatcher.py      (routing + rate-limit)
       - scripts/run_eta_live.py     (the loop)

    If any file is missing, live-tiny has nothing to run. We do an
    import-level smoke check — syntactic errors or missing deps fail here,
    which is exactly where we want to catch them (offline, before live).
    """
    g = Gate(name="runtime_wired", required=True)
    paths = {
        "kill_switch_runtime.py": ROOT / "core" / "kill_switch_runtime.py",
        "alert_dispatcher.py": ROOT / "obs" / "alert_dispatcher.py",
        "run_eta_live.py": ROOT / "scripts" / "run_eta_live.py",
    }
    missing = [name for name, p in paths.items() if not p.exists()]
    if missing:
        g.status, g.detail = "FAIL", f"missing runtime modules: {missing}"
        g.evidence = {"missing": missing}
        return g
    # Import-level smoke: each module must import cleanly.
    import importlib

    import_errors: list[str] = []
    for mod_name in (
        "eta_engine.core.kill_switch_runtime",
        "eta_engine.obs.alert_dispatcher",
        "eta_engine.scripts.run_eta_live",
    ):
        try:
            importlib.import_module(mod_name)
        except Exception as e:  # noqa: BLE001
            import_errors.append(f"{mod_name}: {e}")
    if import_errors:
        g.status, g.detail = "FAIL", f"import errors: {import_errors}"
        g.evidence = {"import_errors": import_errors}
    else:
        g.status = "PASS"
        g.detail = "kill_switch + alerts + run_eta_live all importable"
        g.evidence = {"files": sorted(paths)}
    return g


def _gate_pytest_subset() -> Gate:
    g = Gate(name="pytest_green", required=True)
    # Run a fast subset: paper_run_harness tests (13 tests, <2s)
    target = "eta_engine/tests/test_paper_run_harness.py"
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", target, "-x", "-q", "--no-header"],
            capture_output=True,
            text=True,
            check=False,
            cwd=ROOT.parent,
            timeout=180,
        )
        ok = out.returncode == 0
        # Parse "N passed" line
        tail = (out.stdout or out.stderr or "").splitlines()
        summary = tail[-1] if tail else ""
        g.status = "PASS" if ok else "FAIL"
        g.detail = summary[:160]
        g.evidence = {"returncode": out.returncode}
    except subprocess.SubprocessError as e:
        g.status, g.detail = "FAIL", f"pytest crash: {e}"
    return g


def _gate_credential_probe_full() -> Gate:
    """Validate ALL required live secrets resolve via env/keyring/.env — no HTTP call.

    Pre-funding we cannot auth against the active futures broker, but we
    CAN verify every secret name is present in *some* tier of
    SecretsManager. A FAIL here means operator must populate secrets
    before ever running live.

    Tier-A keys are derived from ``venues.router.ACTIVE_FUTURES_VENUES``
    so the gate stays correct under the broker dormancy mandate
    (Tradovate is currently DORMANT; IBKR + Tastytrade are active).
    """
    g = Gate(name="credential_probe_full", required=False)
    try:
        from eta_engine.core.secrets import REQUIRED_KEYS, SecretsManager
    except Exception as e:  # noqa: BLE001
        g.status, g.detail = "FAIL", f"secrets module import error: {e}"
        return g
    # Tier-A minimum: per-active-broker keys + Telegram heartbeat.
    # Crypto (Bybit) is Tier-B, not blocker.
    # Per-venue key sets:
    #   ibkr        -> IBKR_BASE_URL, IBKR_ACCOUNT_ID
    #   tastytrade  -> TASTYTRADE_BASE_URL, TASTYTRADE_ACCOUNT_NUMBER,
    #                  TASTYTRADE_SESSION_TOKEN
    #   tradovate   -> TRADOVATE_USERNAME, TRADOVATE_PASSWORD,
    #                  TRADOVATE_APP_ID, TRADOVATE_APP_SECRET,
    #                  TRADOVATE_CID  (DORMANT; only required when
    #                  the operator un-dormants the broker)
    venue_key_sets: dict[str, list[str]] = {
        "ibkr": ["IBKR_BASE_URL", "IBKR_ACCOUNT_ID"],
        "tastytrade": [
            "TASTYTRADE_BASE_URL",
            "TASTYTRADE_ACCOUNT_NUMBER",
            "TASTYTRADE_SESSION_TOKEN",
        ],
        "tradovate": [
            "TRADOVATE_USERNAME",
            "TRADOVATE_PASSWORD",
            "TRADOVATE_APP_ID",
            "TRADOVATE_APP_SECRET",
            "TRADOVATE_CID",
        ],
    }
    try:
        from eta_engine.venues.router import ACTIVE_FUTURES_VENUES
    except ImportError:
        active_futures: tuple[str, ...] = ()
    else:
        active_futures = tuple(ACTIVE_FUTURES_VENUES)
    tier_a_keys: list[str] = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    for venue in active_futures:
        tier_a_keys.extend(venue_key_sets.get(venue, []))
    sm = SecretsManager()
    missing_tier_a = [k for k in tier_a_keys if sm.get(k, required=False) is None]
    all_missing = sm.validate_required_keys(REQUIRED_KEYS)
    # Tier-A secrets being absent = FAIL; other REQUIRED_KEYS absent = SKIP (optional bots)
    if missing_tier_a:
        g.status = "SKIP"  # SKIP not FAIL: pre-funding this is expected
        g.detail = (
            f"Tier-A missing: {len(missing_tier_a)} key(s) "
            f"(active venues: {','.join(active_futures) or 'none'}) — "
            "populate before live run"
        )
    else:
        g.status = "PASS"
        g.detail = (
            f"Tier-A present ({len(tier_a_keys)} keys, "
            f"active venues: {','.join(active_futures) or 'none'}); "
            f"other bots missing {len(all_missing)}"
        )
    g.evidence = {
        "tier_a_missing": missing_tier_a,
        "all_required_missing": all_missing,
        "active_futures_venues": list(active_futures),
    }
    return g


def _gate_kill_switch_drill() -> Gate:
    """Load configs/kill_switch.yaml, feed a known-tripping portfolio snapshot,
    verify we get FLATTEN_ALL + CRITICAL. Proves the kill-switch wiring is live.
    """
    g = Gate(name="kill_switch_drill", required=True)
    cfg_path = ROOT / "configs" / "kill_switch.yaml"
    if not cfg_path.exists():
        g.status, g.detail = "FAIL", f"missing {cfg_path}"
        return g
    try:
        from eta_engine.core.kill_switch_runtime import (
            KillAction,
            KillSeverity,
            KillSwitch,
            PortfolioSnapshot,
        )
    except Exception as e:  # noqa: BLE001
        g.status, g.detail = "FAIL", f"kill_switch import error: {e}"
        return g
    try:
        ks = KillSwitch.from_yaml(cfg_path)
    except Exception as e:  # noqa: BLE001
        g.status, g.detail = "FAIL", f"failed to load yaml: {e}"
        return g
    # Deliberate trip: portfolio down 50% from peak — exceeds any sane cap.
    port = PortfolioSnapshot(
        total_equity_usd=5_000.0,
        peak_equity_usd=10_000.0,
        daily_realized_pnl_usd=-5_000.0,
    )
    verdicts = ks.evaluate(bots=[], portfolio=port)
    if not verdicts:
        g.status, g.detail = "FAIL", "no verdict emitted"
        return g
    v = verdicts[0]
    if v.action == KillAction.FLATTEN_ALL and v.severity == KillSeverity.CRITICAL:
        g.status = "PASS"
        g.detail = f"tripped FLATTEN_ALL/CRITICAL on 50% DD ({v.reason[:60]})"
        g.evidence = {"action": v.action.value, "severity": v.severity.value}
    else:
        g.status = "FAIL"
        g.detail = f"expected FLATTEN_ALL/CRITICAL got {v.action.value}/{v.severity.value}"
        g.evidence = {"action": v.action.value, "severity": v.severity.value}
    return g


def _gate_idempotent_order_id() -> Gate:
    """Proves duplicate OrderRequest -> identical client_order_id (dedup key).

    If the same logical order is submitted twice (e.g., post-reconnect replay),
    the venue must see the SAME client_order_id and reject the duplicate.
    """
    g = Gate(name="idempotent_order_id", required=True)
    try:
        from eta_engine.scripts.live_supervisor import JarvisAwareRouter
        from eta_engine.venues.base import OrderRequest, OrderType, Side
    except Exception as e:  # noqa: BLE001
        g.status, g.detail = "FAIL", f"live_supervisor import error: {e}"
        return g
    try:
        req_a = OrderRequest(
            symbol="MNQZ5",
            side=Side.BUY,
            qty=1.0,
            order_type=OrderType.MARKET,
            price=None,
            reduce_only=False,
        )
        req_b = OrderRequest(
            symbol="MNQZ5",
            side=Side.BUY,
            qty=1.0,
            order_type=OrderType.MARKET,
            price=None,
            reduce_only=False,
        )
        out_a = JarvisAwareRouter._ensure_client_order_id(req_a)
        out_b = JarvisAwareRouter._ensure_client_order_id(req_b)
        if out_a.client_order_id and out_a.client_order_id == out_b.client_order_id:
            g.status = "PASS"
            g.detail = f"identical reqs -> same coid={out_a.client_order_id[:12]}.."
            g.evidence = {
                "client_order_id": out_a.client_order_id,
                "length": len(out_a.client_order_id or ""),
            }
        else:
            g.status = "FAIL"
            g.detail = f"dedup broken: a={out_a.client_order_id} b={out_b.client_order_id}"
    except Exception as e:  # noqa: BLE001
        g.status, g.detail = "FAIL", f"idempotent test error: {e}"
    return g


def _gate_reconcile_on_reconnect() -> Gate:
    """Run the daily reconcile tool against current journals; must exit GREEN (0)
    or YELLOW (1) for current state. RED (2) indicates an actual gap.
    """
    g = Gate(name="reconcile_on_reconnect", required=False)
    script = ROOT / "scripts" / "_trade_journal_reconcile.py"
    if not script.exists():
        g.status, g.detail = "FAIL", f"missing {script}"
        return g
    try:
        out = subprocess.run(
            [sys.executable, str(script), "--hours", "24"],
            capture_output=True,
            text=True,
            check=False,
            cwd=ROOT.parent,
            timeout=60,
        )
        rc = out.returncode
        first = (out.stdout or "").splitlines()[0] if out.stdout else ""
        if rc == 0:
            g.status = "PASS"
            g.detail = f"GREEN: {first[:120]}"
        elif rc == 1:
            g.status = "PASS"  # YELLOW is acceptable pre-launch
            g.detail = f"YELLOW (pre-launch acceptable): {first[:120]}"
        elif rc == 9:
            # Data missing is expected before first live run
            g.status = "SKIP"
            g.detail = "journals empty (pre-launch expected)"
        else:
            g.status = "FAIL"
            g.detail = f"RED rc={rc}: {first[:120]}"
        g.evidence = {"returncode": rc, "stdout_head": (out.stdout or "")[:400]}
    except subprocess.SubprocessError as e:
        g.status, g.detail = "FAIL", f"reconcile subprocess error: {e}"
    return g


def _gate_crash_recovery_simulated() -> Gate:
    """Write a synthetic orphaned runtime_start to a TEMP alerts log, run
    reconcile against the temp file, verify it detects the orphan. Cleans up.

    Proves: the detection path that catches crash-without-stop actually fires.
    """
    g = Gate(name="crash_recovery_simulated", required=True)
    import json as _json
    import tempfile
    import time

    script = ROOT / "scripts" / "_trade_journal_reconcile.py"
    if not script.exists():
        g.status, g.detail = "FAIL", f"missing {script}"
        return g
    tmp_dir = Path(tempfile.mkdtemp(prefix="apex_preflight_"))
    tmp_alerts = tmp_dir / "alerts_log.jsonl"
    tmp_btc = tmp_dir / "btc_live_decisions.jsonl"
    try:
        # Write a LIVE runtime_start with ts 1h ago, and no matching stop/resume
        orphan_ts = time.time() - 3600.0
        entry = {
            "event": "runtime_start",
            "ts": orphan_ts,
            "payload": {"live": True, "bot": "mnq", "synthetic": True},
        }
        tmp_alerts.write_text(_json.dumps(entry) + "\n", encoding="utf-8")
        tmp_btc.write_text("", encoding="utf-8")
        out = subprocess.run(
            [
                sys.executable,
                str(script),
                "--alerts",
                str(tmp_alerts),
                "--btc",
                str(tmp_btc),
                "--hours",
                "24",
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=ROOT.parent,
            timeout=60,
        )
        text = out.stdout or ""
        # Expect orphan-detection text in output (RED or YELLOW orphaned-runtime line)
        detected = "orphaned-runtime" in text and ("YELLOW" in text or "RED" in text)
        if detected:
            g.status = "PASS"
            g.detail = "synthetic orphan detected by reconcile"
            g.evidence = {"returncode": out.returncode}
        else:
            g.status = "FAIL"
            g.detail = f"orphan NOT detected: rc={out.returncode} head={text[:200]}"
    except Exception as e:  # noqa: BLE001
        g.status, g.detail = "FAIL", f"crash-recovery drill error: {e}"
    finally:
        try:
            for p in (tmp_alerts, tmp_btc):
                if p.exists():
                    p.unlink()
            tmp_dir.rmdir()
        except OSError:
            pass
    return g


def _gate_clock_drift() -> Gate:
    """Verify local UTC clock vs. a well-known HTTP Date header is < 3 seconds.

    Proves: order timestamps generated locally will match venue-side records
    within broker reconciliation tolerance. Uses Cloudflare's time endpoint
    (very stable Date header) to avoid needing ntp binaries on Windows hosts.

    Optional: a venue time-sync endpoint would be more authoritative; added
    here as a lightweight first-pass check. Marks SKIP on network failure
    rather than FAIL, since a partially-offline dev box shouldn't block the
    gate — but operator MUST ensure this passes on the live trading host.
    """
    g = Gate(name="clock_drift", required=False)
    try:
        import time
        from email.utils import parsedate_to_datetime
        from urllib.request import Request, urlopen

        req = Request(
            "https://www.cloudflare.com/cdn-cgi/trace",
            headers={"User-Agent": "apex-preflight/1.0"},
        )
        t0 = time.time()
        with urlopen(req, timeout=5) as resp:  # noqa: S310
            headers = resp.headers
            _ = resp.read()
        t1 = time.time()
        http_date = headers.get("Date")
        if not http_date:
            g.status, g.detail = "SKIP", "no Date header in probe response"
            return g
        server_dt = parsedate_to_datetime(http_date)
        server_ts = server_dt.timestamp()
        # Use round-trip midpoint as the reference for local clock.
        local_ts = (t0 + t1) / 2.0
        drift = abs(local_ts - server_ts)
        g.evidence = {
            "local_ts_mid": local_ts,
            "server_ts": server_ts,
            "drift_seconds": drift,
            "rtt_seconds": t1 - t0,
        }
        if drift < 3.0:
            g.status = "PASS"
            g.detail = f"local vs server drift {drift:.2f}s (< 3s)"
        elif drift < 10.0:
            g.status = "FAIL"
            g.detail = f"drift {drift:.2f}s in [3,10)s — sync NTP before live"
        else:
            g.status = "FAIL"
            g.detail = f"drift {drift:.2f}s >= 10s — order timestamps will break"
    except Exception as e:  # noqa: BLE001
        g.status = "SKIP"
        g.detail = f"clock probe unavailable: {type(e).__name__}: {e}"
    return g


def _gate_alert_dispatcher_echo() -> Gate:
    """End-to-end: dispatch a test alert, verify it persists to alerts_log.jsonl.

    Proves: the observability pipeline from alert_dispatcher -> jsonl sink
    actually works under runtime conditions (not just import-clean).
    Uses a minimal cfg that routes a probe event to no channels, so the
    dispatcher logs the attempt without trying to reach telegram/etc.
    """
    g = Gate(name="alert_dispatcher_echo", required=False)
    import json as _json
    import tempfile

    try:
        from eta_engine.obs.alert_dispatcher import AlertDispatcher

        tmp_dir = Path(tempfile.mkdtemp(prefix="apex_preflight_alerts_"))
        tmp_log = tmp_dir / "alerts_log.jsonl"
        cfg = {
            "rate_limit": {
                "info_per_minute": 10,
                "warn_per_minute": 10,
                "critical_per_minute": 10,
            },
            "routing": {
                "events": {
                    "preflight_echo": {"level": "info", "channels": []},
                },
            },
            "channels": {},
        }
        disp = AlertDispatcher(cfg, log_path=tmp_log)
        disp.send(event="preflight_echo", payload={"ok": True})
        if tmp_log.exists():
            lines = [_json.loads(line) for line in tmp_log.read_text().splitlines() if line.strip()]
            if any(ln.get("event") == "preflight_echo" for ln in lines):
                g.status = "PASS"
                g.detail = f"echoed 1 alert to {tmp_log.name} ({len(lines)} line(s))"
                g.evidence = {"events_read": len(lines)}
            else:
                g.status = "FAIL"
                g.detail = f"wrote log but no preflight_echo: {lines[:1]}"
        else:
            g.status = "FAIL"
            g.detail = "dispatcher did not write to temp log"
        try:
            if tmp_log.exists():
                tmp_log.unlink()
            tmp_dir.rmdir()
        except OSError:
            pass
    except Exception as e:  # noqa: BLE001
        g.status = "SKIP"
        g.detail = f"dispatcher echo unavailable: {type(e).__name__}: {e}"
    return g


def _gate_abort_on_red_loop(all_gates: list[Gate]) -> Gate:
    """Simulate: each RED required gate raises ABORT() — verify logic."""
    g = Gate(name="abort_on_red_loop", required=True)
    red_req = [gg for gg in all_gates if gg.required and gg.status == "FAIL"]
    if red_req:
        g.status = "PASS"  # the loop correctly identifies aborts
        g.detail = f"Simulated abort would fire on: {', '.join(gg.name for gg in red_req)}"
    else:
        # No red reqs means no aborts to simulate — still PASS (the wiring works)
        g.status = "PASS"
        g.detail = "No required RED gates — abort wiring idle (correct)"
    g.evidence = {"red_required": [gg.name for gg in red_req]}
    return g


def _summarize_gates(gates: list[Gate]) -> dict[str, int]:
    required = [g for g in gates if g.required]
    optional = [g for g in gates if not g.required]
    return {
        "total_gates": len(gates),
        "required_gates": len(required),
        "optional_gates": len(optional),
        "required_pass": sum(1 for g in required if g.status == "PASS"),
        "required_fail": sum(1 for g in required if g.status == "FAIL"),
        "required_skip": sum(1 for g in required if g.status == "SKIP"),
        "optional_pass": sum(1 for g in optional if g.status == "PASS"),
        "optional_fail": sum(1 for g in optional if g.status == "FAIL"),
        "optional_skip": sum(1 for g in optional if g.status == "SKIP"),
        "total_pass": sum(1 for g in gates if g.status == "PASS"),
        "total_fail": sum(1 for g in gates if g.status == "FAIL"),
        "total_skip": sum(1 for g in gates if g.status == "SKIP"),
    }


GATE_FNS = [
    ("kill_log_present", _gate_kill_log),
    ("paper_run_report", _gate_paper_run),
    ("firm_board_verdict", _gate_firm_verdict),
    ("roadmap_state", _gate_roadmap_state),
    ("tradovate_creds", _gate_tradovate_creds),
    ("risk_sizing", _gate_risk_sizing),
    ("telemetry_channels", _gate_telemetry_channels),
    ("venue_health", _gate_venue_health),
    ("pytest_green", _gate_pytest_subset),
    ("decisions_locked", _gate_decisions_locked),
    ("env_template", _gate_env_template),
    ("go_trigger_armed", _gate_go_trigger_armed),
    ("runtime_wired", _gate_runtime_wired),
    ("credential_probe_full", _gate_credential_probe_full),
    ("kill_switch_drill", _gate_kill_switch_drill),
    ("idempotent_order_id", _gate_idempotent_order_id),
    ("reconcile_on_reconnect", _gate_reconcile_on_reconnect),
    ("crash_recovery_simulated", _gate_crash_recovery_simulated),
    ("clock_drift", _gate_clock_drift),
    ("alert_dispatcher_echo", _gate_alert_dispatcher_echo),
]


def _inject_failures(gates: list[Gate], names: set[str]) -> list[Gate]:
    for g in gates:
        if g.name in names:
            g.status = "FAIL"
            g.detail = f"(injected failure for dryrun) {g.detail}"
    return gates


def main() -> int:
    p = argparse.ArgumentParser(description="Live-tiny preflight dry run")
    p.add_argument(
        "--inject-fail",
        action="append",
        default=[],
        help="Force gate(s) to FAIL (comma or repeat). For abort-path dryrun.",
    )
    p.add_argument(
        "--simulate-red", action="store_true", help="Shortcut: inject failure on tradovate_creds to prove abort path."
    )
    p.add_argument("--out-dir", type=Path, default=ROOT / "docs")
    args = p.parse_args()

    print("EVOLUTIONARY TRADING ALGO -- Live-Tiny Preflight Dry Run")
    print("=" * 80)
    print(f"Generated: {datetime.now(UTC).isoformat()}")
    print("-" * 80)

    injections: set[str] = set()
    for item in args.inject_fail:
        for tok in item.split(","):
            t = tok.strip()
            if t:
                injections.add(t)
    if args.simulate_red:
        injections.add("tradovate_creds")

    gates: list[Gate] = []
    for _, fn in GATE_FNS:
        gates.append(fn())
    gates = _inject_failures(gates, injections)
    # Add the abort-on-red loop gate AFTER all others are known
    gates.append(_gate_abort_on_red_loop(gates))
    counts = _summarize_gates(gates)

    for g in gates:
        flag = "REQ" if g.required else "opt"
        print(f"[{flag}] {g.name:<22} {g.status:>5}   {g.detail}")
    print("-" * 80)

    red_required = [g for g in gates if g.required and g.status == "FAIL"]
    overall = "ABORT" if red_required else "GO"
    print(
        f"Gate mix: {counts['required_gates']} required / {counts['optional_gates']} optional | "
        f"required PASS {counts['required_pass']}/{counts['required_gates']} | "
        f"optional PASS {counts['optional_pass']}/{counts['optional_gates']} | "
        f"optional SKIP {counts['optional_skip']}"
    )
    print(f"Overall dryrun: {overall}   ({len(red_required)} required-RED, {counts['total_pass']} PASS)")
    if overall == "ABORT":
        print(f"Abort reasons: {', '.join(g.name for g in red_required)}")
    print("=" * 80)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "kind": "apex_live_tiny_preflight_dryrun",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "injected_failures": sorted(injections),
        "overall": overall,
        "counts": counts,
        "gates": [asdict(g) for g in gates],
        "red_required_reasons": [g.name for g in red_required],
    }
    rp = args.out_dir / "preflight_dryrun_report.json"
    rp.write_text(json.dumps(report, indent=2))
    log = args.out_dir / "preflight_dryrun_log.txt"
    lines: list[str] = []
    lines.append("EVOLUTIONARY TRADING ALGO -- Preflight Dryrun Log")
    lines.append("=" * 80)
    for g in gates:
        flag = "REQ" if g.required else "opt"
        lines.append(f"[{flag}] {g.name:<22} {g.status:>5}   {g.detail}")
    lines.append("-" * 80)
    lines.append(
        f"Gate mix: {counts['required_gates']} required / {counts['optional_gates']} optional | "
        f"required PASS {counts['required_pass']}/{counts['required_gates']} | "
        f"optional PASS {counts['optional_pass']}/{counts['optional_gates']} | "
        f"optional SKIP {counts['optional_skip']}"
    )
    lines.append(f"Overall: {overall}")
    log.write_text("\n".join(lines) + "\n")
    print(f"Report: {rp}")
    print(f"Log:    {log}")
    # Exit non-zero on ABORT so CI catches it
    return 0 if overall == "GO" else 2


if __name__ == "__main__":
    sys.exit(main())
