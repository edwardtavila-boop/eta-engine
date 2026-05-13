"""
EVOLUTIONARY TRADING ALGO  //  scripts.verify_ibkr_subscriptions
================================================================
Probe IBKR Pro market-data subscriptions per exchange and report
realtime vs delayed status.

Why this exists
---------------
Per docs/IBKR_PRO_DATA_INVENTORY.md and the operator's 2026-05-08
directive: "Verify CME / NYMEX / COMEX / CBOT / ICE realtime
subscriptions are ACTIVE on IBKR Pro. Any not yet enabled = silent
15-min delayed data on those symbols = silently bad."

This script connects to the local TWS Gateway, requests
``reqMktData`` for one representative contract per exchange, then
inspects the ``mktDataType`` callback to see whether IBKR returned
real-time (1), frozen (2), delayed (3), or delayed-frozen (4) data.

A realtime account on the right exchange gets type 1.  Anything
else means the operator's IBKR Pro subscription is missing or not
yet activated for that exchange -- the live supervisor would be
trading on 15-minute stale prices on those symbols.

Output
------
* Pretty table to stdout: exchange | symbol probed | data type | verdict
* JSONL append to logs/eta_engine/ibkr_subscription_status.jsonl
* Exit code:
    0 -- all probed exchanges return realtime
    1 -- one or more exchanges return delayed / frozen / errored
    2 -- connection / setup error (no probe completed)

The verifier is read-only -- no orders, no order requests, no
historical-data calls.  Single-shot probe per exchange, ~1-2
seconds per probe, total runtime under 30 seconds.

Run
---
::

    # default exchange set: CME (MNQ), NYMEX (CL), COMEX (GC),
    # CBOT (ZN), ICE (none -- IBKR routes 6E via CME)
    python -m eta_engine.scripts.verify_ibkr_subscriptions

    # custom port (live gateway)
    python -m eta_engine.scripts.verify_ibkr_subscriptions --port 4001

    # JSON output (machine-readable)
    python -m eta_engine.scripts.verify_ibkr_subscriptions --json
"""

from __future__ import annotations

# ruff: noqa: ANN401, BLE001, SIM105
# ib_insync returns Any everywhere; defensive try/except wraps every
# external callback so one bad probe doesn't crash the whole audit.
# SIM105 disabled because the bare try/except/pass pattern is more
# readable than contextlib.suppress for these callback-attach calls.
import argparse
import json
import logging
import os
import socket
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATUS_LOG = LOG_DIR / "ibkr_subscription_status.jsonl"
IBC_PASSWORD_FILE = ROOT.parent / "var" / "eta_engine" / "state" / "ibkr_pw.txt"
IBC_CREDENTIAL_JSON = ROOT / "secrets" / "ibkr_credentials.json"
IBC_PRIVATE_CONFIG = ROOT.parent / "var" / "eta_engine" / "ibc" / "private" / "config.ini"
IBC_PRIVATE_PASSWORD_FILES = (
    ROOT.parent / "var" / "eta_engine" / "ibc" / "private" / "password.txt",
    ROOT.parent / "var" / "eta_engine" / "ibc" / "private" / "ibkr_password.txt",
    ROOT / "secrets" / "ibkr_password.txt",
)


# Representative probe symbol per exchange.  These are the most-liquid
# contracts on each venue so the probe always gets a fresh quote.
PROBES: dict[str, dict[str, str]] = {
    "CME": {
        "symbol": "MNQ",
        "secType": "CONTFUT",
        "exchange": "CME",
        "purpose": "Equity-index futures (MNQ/NQ/MES/ES/M2K/RTY)",
    },
    "NYMEX": {"symbol": "CL", "secType": "CONTFUT", "exchange": "NYMEX", "purpose": "Energy futures (CL/MCL/NG)"},
    "COMEX": {"symbol": "GC", "secType": "CONTFUT", "exchange": "COMEX", "purpose": "Metals futures (GC/MGC)"},
    "CBOT": {"symbol": "ZN", "secType": "CONTFUT", "exchange": "CBOT", "purpose": "Rates futures (ZN/ZB/YM/MYM)"},
    # IBKR routes 6E via CME (Globex), so a separate ICE probe is
    # only relevant if the operator subscribes to ICE-listed FX
    # crosses.  Skip by default; can be re-enabled with --include-ice.
}

OPTIONAL_PROBES: dict[str, dict[str, str]] = {
    "ICE": {
        "symbol": "DX",
        "secType": "CONTFUT",
        "exchange": "NYBOT",
        "purpose": "ICE Forex (DX dollar index, optional)",
    },
}


# IBKR mktDataType callback values:
DATA_TYPE_LABEL = {
    1: ("REALTIME", "PASS", "live tick stream"),
    2: ("FROZEN", "WARN", "frozen at last close -- outside RTH or no subscription"),
    3: ("DELAYED", "FAIL", "15-min delayed -- subscription INACTIVE for this exchange"),
    4: ("DELAYED-FROZEN", "FAIL", "delayed AND frozen -- subscription INACTIVE + outside RTH"),
}


def _read_first_line(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (IndexError, OSError, UnicodeDecodeError):
        return ""


def _read_json_map(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_key_value_map(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _is_secret_sentinel(value: object) -> bool:
    if not isinstance(value, str):
        return False
    token = value.strip()
    if not token:
        return True
    upper = token.upper()
    return (
        any(marker in upper for marker in ("REPLACE", "PLACEHOLDER", "TODO", "CHANGEME"))
        or "REAL_IBKR_PASSWORD" in upper
        or (token.startswith("<") and token.endswith(">") and "PASSWORD" in upper)
    )


def _usable_secret(value: object) -> str:
    if not isinstance(value, str):
        return ""
    token = value.strip()
    if _is_secret_sentinel(token):
        return ""
    return token


def _first_usable_secret(candidates: list[tuple[str, object]]) -> tuple[str | None, bool]:
    saw_placeholder = False
    for source, value in candidates:
        if _is_secret_sentinel(value):
            saw_placeholder = True
            continue
        if _usable_secret(value):
            return source, saw_placeholder
    return None, saw_placeholder


def _ibc_credential_status(
    env: dict[str, str] | None = None,
    *,
    password_file: Path = IBC_PASSWORD_FILE,
    credential_json: Path = IBC_CREDENTIAL_JSON,
    ibc_private_config: Path | None = IBC_PRIVATE_CONFIG,
    ibc_password_files: tuple[Path, ...] = IBC_PRIVATE_PASSWORD_FILES,
) -> dict[str, object]:
    """Return non-secret IBC credential readiness for dashboard/setup gates."""
    env_map = env if env is not None else os.environ
    credential_payload = _read_json_map(credential_json)
    private_config = _read_key_value_map(ibc_private_config)
    file_password = _read_first_line(password_file)
    private_file_passwords = [(path, _read_first_line(path)) for path in ibc_password_files]
    password_file_placeholder = bool(password_file.exists() and _is_secret_sentinel(file_password))
    private_password_placeholder = any(
        path.exists() and _is_secret_sentinel(value) for path, value in private_file_passwords
    )

    login_source, _ = _first_usable_secret(
        [
            ("env", env_map.get("ETA_IBC_LOGIN_ID")),
            ("env", env_map.get("IBKR_USERNAME")),
            ("env", env_map.get("IBKR_LOGIN_ID")),
            ("ibc_private_config", private_config.get("IbLoginId")),
            ("credential_json", credential_payload.get("username")),
            ("credential_json", credential_payload.get("user")),
            ("credential_json", credential_payload.get("login")),
            ("credential_json", credential_payload.get("ib_login_id")),
            ("credential_json", credential_payload.get("user_id")),
        ]
    )
    password_source, password_placeholder_seen = _first_usable_secret(
        [
            ("env", env_map.get("ETA_IBC_PASSWORD")),
            ("env", env_map.get("IBKR_PASSWORD")),
            ("ibc_private_config", private_config.get("IbPassword")),
            *[("ibc_password_file", value) for _, value in private_file_passwords],
            ("password_file", file_password),
            ("credential_json", credential_payload.get("password")),
            ("credential_json", credential_payload.get("pass")),
            ("credential_json", credential_payload.get("ib_password")),
        ]
    )

    login_present = login_source is not None
    password_present = password_source is not None
    ready = login_present and password_present
    if ready:
        status = "READY"
        operator_action = None
    elif not login_present:
        status = "MISSING_LOGIN"
        operator_action = (
            "Seed ETA_IBC_LOGIN_ID/IBKR_USERNAME or add a username to eta_engine/secrets/ibkr_credentials.json."
        )
    elif password_placeholder_seen or password_file_placeholder or private_password_placeholder:
        status = "PLACEHOLDER_PASSWORD"
        operator_action = (
            "Seed ETA_IBC_PASSWORD with set_ibc_credentials.ps1 -PromptForPassword "
            "or replace the protected password file with the real IBKR paper password."
        )
    else:
        status = "MISSING_PASSWORD"
        operator_action = (
            "Seed ETA_IBC_PASSWORD with set_ibc_credentials.ps1 -PromptForPassword "
            "or create the protected IBC password file."
        )

    return {
        "ready": ready,
        "status": status,
        "login_present": login_present,
        "password_present": password_present,
        "login_source": login_source,
        "password_source": password_source,
        "password_file_exists": password_file.exists(),
        "password_file_placeholder": password_file_placeholder,
        "credential_json_exists": credential_json.exists(),
        "ibc_private_config_exists": bool(ibc_private_config and ibc_private_config.exists()),
        "ibc_private_password_file_exists": any(path.exists() for path, _ in private_file_passwords),
        "ibc_private_password_placeholder": private_password_placeholder,
        "operator_action": operator_action,
    }


def _write_status_digest(digest: dict) -> None:
    try:
        with STATUS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(digest, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _tws_port() -> int | None:
    for port in (4002, 7497, 4001):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            s.close()
            continue
    return None


def _probe_one_exchange(
    ib: object, exchange: str, spec: dict[str, str], timeout: float = 5.0, log: logging.Logger | None = None
) -> dict:
    """Issue a reqMktData on one contract, wait for first mktDataType
    callback, then cancel.  Returns a result dict.

    Bug fix 2026-05-11: now ALSO listens for IBKR Error 354
    ("Requested market data is not subscribed").  The prior version
    silently reported PASS because mktDataType=1 was the requested
    value and the ticker object echoed it back even when the actual
    subscription was missing.  Result: silent false-PASS for any
    exchange where the subscription isn't active.
    """
    log = log or logging.getLogger(__name__)
    from ib_insync import ContFuture  # noqa: PLC0415  # local import -- only when running

    try:
        # Use ContFuture (continuous front month) -- cheapest probe contract.
        contract = ContFuture(spec["symbol"], spec["exchange"])
        qualified = ib.qualifyContracts(contract)  # type: ignore[attr-defined]
        if not qualified:
            return {
                "exchange": exchange,
                "symbol": spec["symbol"],
                "data_type": None,
                "verdict": "ERROR",
                "reason": f"qualifyContracts returned empty for {spec['symbol']}@{spec['exchange']}",
            }
        contract = qualified[0]
    except Exception as e:
        return {
            "exchange": exchange,
            "symbol": spec["symbol"],
            "data_type": None,
            "verdict": "ERROR",
            "reason": f"contract qualify failed: {e}",
        }

    # Force realtime request; IBKR will silently downgrade if the sub
    # isn't active on this exchange -- we read the response back.
    captured_type: list[int] = []
    captured_errors: list[dict] = []
    target_conid = getattr(contract, "conId", None)

    def _on_market_data_type(msg) -> None:  # noqa: ANN001
        # mktDataTypeEvent fires with attribute marketDataType
        try:
            captured_type.append(int(getattr(msg, "marketDataType", 0)))
        except Exception:
            pass

    captured_ticks: list[dict] = []  # actual price/size data received

    def _on_error(reqId, errorCode, errorString, contract_arg=None) -> None:  # noqa: ANN001, ARG001, N803
        # IBKR error codes that mean "subscription not active":
        #   354 = Requested market data is not subscribed
        #   10168 = Same, with delayed-disabled note
        #   10089 / 10090 / 10091 = depth subscription required
        #   200 = No security definition has been found
        #   162 = Historical Market Data Service error
        if errorCode in (354, 10089, 10090, 10091, 10168, 200, 162):
            captured_errors.append(
                {
                    "code": int(errorCode),
                    "message": str(errorString)[:200],
                    "req_id": int(reqId) if reqId is not None else -1,
                }
            )

    def _on_pending_ticks(tickers) -> None:  # noqa: ANN001
        # ib_insync.pendingTickersEvent fires when actual tick data
        # arrives — bid/ask/last update.  This is the ONLY definitive
        # proof that the subscription is active.  mktDataType=1 alone
        # is not enough because IBKR sends type=1 on request-accepted
        # then errors out 100ms later when the sub is missing.
        try:
            for t in tickers:
                if getattr(t, "contract", None) is None:
                    continue
                if target_conid and getattr(t.contract, "conId", None) != target_conid:
                    continue
                bid = getattr(t, "bid", None)
                ask = getattr(t, "ask", None)
                last = getattr(t, "last", None)
                if bid not in (None, -1) or ask not in (None, -1) or last not in (None, -1):
                    captured_ticks.append({"bid": bid, "ask": ask, "last": last})
                    return
        except Exception:
            pass

    try:
        ib.reqMarketDataType(1)  # type: ignore[attr-defined]
        try:
            ib.mktDataTypeEvent += _on_market_data_type  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            ib.errorEvent += _on_error  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            ib.pendingTickersEvent += _on_pending_ticks  # type: ignore[attr-defined]
        except Exception:
            pass
        ticker = ib.reqMktData(contract, "", False, False)  # type: ignore[attr-defined]
        deadline = time.time() + timeout
        # Wait for EITHER:
        #   - real tick data to arrive (PASS)
        #   - subscription error to fire (FAIL)
        # Do NOT exit early on mktDataType callback alone — it's a
        # request-accepted echo, not proof the subscription works.
        while time.time() < deadline:
            ib.sleep(0.25)  # type: ignore[attr-defined]
            if captured_errors:
                break
            if captured_ticks:
                break
        # If we never got ticks AND never got errors but did get
        # mktDataType, also check the ticker for live values
        if not captured_ticks and not captured_errors:
            bid = getattr(ticker, "bid", None)
            ask = getattr(ticker, "ask", None)
            last = getattr(ticker, "last", None)
            if bid not in (None, -1) or ask not in (None, -1) or last not in (None, -1):
                captured_ticks.append({"bid": bid, "ask": ask, "last": last})
        try:
            ib.cancelMktData(contract)  # type: ignore[attr-defined]
        except Exception:
            pass
        for ev_name in ("errorEvent", "mktDataTypeEvent", "pendingTickersEvent"):
            try:
                ev = getattr(ib, ev_name)
                handler = {
                    "errorEvent": _on_error,
                    "mktDataTypeEvent": _on_market_data_type,
                    "pendingTickersEvent": _on_pending_ticks,
                }[ev_name]
                ev -= handler
            except Exception:
                pass
    except Exception as e:
        return {
            "exchange": exchange,
            "symbol": spec["symbol"],
            "data_type": None,
            "verdict": "ERROR",
            "reason": f"reqMktData failed: {e}",
        }

    # Subscription error wins — even if mktDataType=1 was echoed back,
    # an Error 354/10168 means the underlying tick stream will never arrive.
    if captured_errors:
        err = captured_errors[0]
        return {
            "exchange": exchange,
            "symbol": spec["symbol"],
            "data_type": None,
            "verdict": "FAIL",
            "reason": f"Error {err['code']}: {err['message'][:80]}",
            "purpose": spec["purpose"],
            "ibkr_errors": captured_errors,
        }

    if not captured_ticks:
        return {
            "exchange": exchange,
            "symbol": spec["symbol"],
            "data_type": captured_type[0] if captured_type else None,
            "verdict": "TIMEOUT",
            "reason": f"no real tick data within {timeout}s -- subscription may be missing or market closed",
        }

    # Real ticks arrived → subscription is genuinely active
    dt = captured_type[0] if captured_type else 1
    label, verdict, note = DATA_TYPE_LABEL.get(dt, (f"UNKNOWN({dt})", "ERROR", "unrecognized data type code"))
    sample = captured_ticks[0]
    return {
        "exchange": exchange,
        "symbol": spec["symbol"],
        "data_type": dt,
        "data_type_label": label,
        "verdict": verdict,
        "reason": f"{note} (last={sample.get('last')}, bid={sample.get('bid')}, ask={sample.get('ask')})",
        "purpose": spec["purpose"],
        "target_conid": target_conid,
        "sample_tick": sample,
    }


def _probe_depth_of_book(
    ib: object, symbol: str, exchange: str, *, timeout: float = 5.0, log: logging.Logger | None = None
) -> dict:
    """Probe whether reqMktDepth works for the given symbol — this is
    a SEPARATE subscription from real-time tick data.

    Returns dict with verdict ∈ {PASS, FAIL, TIMEOUT, ERROR}."""
    log = log or logging.getLogger(__name__)
    from ib_insync import ContFuture  # noqa: PLC0415

    try:
        contract = ContFuture(symbol, exchange)
        qualified = ib.qualifyContracts(contract)  # type: ignore[attr-defined]
        if not qualified:
            return {
                "exchange": exchange,
                "symbol": symbol,
                "verdict": "ERROR",
                "reason": "qualifyContracts returned empty",
            }
        contract = qualified[0]
    except Exception as e:
        return {"exchange": exchange, "symbol": symbol, "verdict": "ERROR", "reason": f"qualify failed: {e}"}

    captured_errors: list[dict] = []
    n_updates = [0]

    def _on_depth_error(reqId, errorCode, errorString, contract_arg=None) -> None:  # noqa: ANN001, ARG001, N803
        if errorCode in (309, 354, 10089, 10090, 10091, 322, 200):
            captured_errors.append(
                {
                    "code": int(errorCode),
                    "message": str(errorString)[:200],
                }
            )

    try:
        try:
            ib.errorEvent += _on_depth_error  # type: ignore[attr-defined]
        except Exception:
            pass
        ticker = ib.reqMktDepth(contract, numRows=5, isSmartDepth=False)  # type: ignore[attr-defined]
        deadline = time.time() + timeout
        while time.time() < deadline:
            ib.sleep(0.25)  # type: ignore[attr-defined]
            if captured_errors:
                break
            bids = getattr(ticker, "domBids", None)
            asks = getattr(ticker, "domAsks", None)
            if bids and asks and (len(bids) > 0 or len(asks) > 0):
                n_updates[0] = len(bids) + len(asks)
                break
        try:
            ib.cancelMktDepth(contract)  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            ib.errorEvent -= _on_depth_error  # type: ignore[attr-defined]
        except Exception:
            pass
    except Exception as e:
        return {"exchange": exchange, "symbol": symbol, "verdict": "ERROR", "reason": f"reqMktDepth failed: {e}"}

    if captured_errors:
        err = captured_errors[0]
        return {
            "exchange": exchange,
            "symbol": symbol,
            "verdict": "FAIL",
            "reason": f"Error {err['code']}: {err['message']}",
            "ibkr_errors": captured_errors,
        }

    if n_updates[0] == 0:
        return {
            "exchange": exchange,
            "symbol": symbol,
            "verdict": "TIMEOUT",
            "reason": f"no depth updates in {timeout}s -- subscription may be inactive or market closed",
        }

    return {
        "exchange": exchange,
        "symbol": symbol,
        "verdict": "PASS",
        "n_levels_seen": n_updates[0],
        "reason": "depth-of-book streaming",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=None, help="TWS API port (auto-detect from 4002/7497/4001)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument(
        "--client-id", type=int, default=33, help="ib_insync client ID (default 33 -- separate from supervisor)"
    )
    ap.add_argument("--include-ice", action="store_true", help="Also probe ICE/NYBOT (e.g. DX dollar index)")
    ap.add_argument(
        "--probe-timeout", type=float, default=5.0, help="Seconds to wait for mktDataType callback per exchange"
    )
    ap.add_argument("--json", action="store_true", help="Output JSON only (machine-readable)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("verify_ibkr_subs")
    credential_status = _ibc_credential_status()

    # Port auto-detect
    port = args.port or _tws_port()
    if port is None:
        if credential_status.get("ready"):
            msg = "TWS Gateway unreachable on 4002/7497/4001 -- start TWS or pass --port"
            setup_error_code = "gateway_unreachable"
            operator_action = "Start IB Gateway or run ETA-IBGateway-RunNow."
        else:
            msg = (
                "TWS Gateway unreachable and IBC credentials are not seeded or still "
                "placeholder -- seed credentials before starting Gateway."
            )
            setup_error_code = "ibc_credentials_missing"
            operator_action = credential_status.get("operator_action")
        digest = {
            "ts": datetime.now(UTC).isoformat(),
            "host": args.host,
            "port": None,
            "client_id": args.client_id,
            "setup_status": "BLOCKED",
            "setup_error_code": setup_error_code,
            "error": msg,
            "credential_status": credential_status,
            "operator_action": operator_action,
            "results": [],
            "depth_results": [],
            "all_realtime": False,
            "all_depth_ok": False,
        }
        _write_status_digest(digest)
        if args.json:
            print(json.dumps(digest | {"exit_code": 2}))
        else:
            log.error(msg)
            if operator_action:
                log.error("operator_action: %s", operator_action)
        return 2

    try:
        from ib_insync import IB
    except ImportError:
        msg = "ib_insync not installed -- pip install ib_insync"
        digest = {
            "ts": datetime.now(UTC).isoformat(),
            "host": args.host,
            "port": port,
            "client_id": args.client_id,
            "setup_status": "BLOCKED",
            "setup_error_code": "missing_ib_insync",
            "error": msg,
            "credential_status": credential_status,
            "operator_action": "Install ib_insync in the ETA runtime environment.",
            "results": [],
            "depth_results": [],
            "all_realtime": False,
            "all_depth_ok": False,
        }
        _write_status_digest(digest)
        if args.json:
            print(json.dumps(digest | {"exit_code": 2}))
        else:
            log.error(msg)
        return 2

    ib = IB()
    try:
        ib.connect(args.host, port, clientId=args.client_id, timeout=10)
    except Exception as e:
        msg = f"TWS connect failed at {args.host}:{port} clientId={args.client_id} -- {e}"
        digest = {
            "ts": datetime.now(UTC).isoformat(),
            "host": args.host,
            "port": port,
            "client_id": args.client_id,
            "setup_status": "BLOCKED",
            "setup_error_code": "gateway_connect_failed",
            "error": msg,
            "credential_status": credential_status,
            "operator_action": "Verify IB Gateway is running and API access is enabled.",
            "results": [],
            "depth_results": [],
            "all_realtime": False,
            "all_depth_ok": False,
        }
        _write_status_digest(digest)
        if args.json:
            print(json.dumps(digest | {"exit_code": 2}))
        else:
            log.error(msg)
        return 2

    probes = dict(PROBES)
    if args.include_ice:
        probes.update(OPTIONAL_PROBES)

    results: list[dict] = []
    for exch, spec in probes.items():
        if not args.json:
            log.info(f"probing {exch} via {spec['symbol']}@{spec['exchange']}...")
        r = _probe_one_exchange(ib, exch, spec, timeout=args.probe_timeout, log=log)
        results.append(r)

    # Phase 1 capture daemons need DEPTH-OF-BOOK subscriptions which are
    # SEPARATE from real-time tick.  Probe MNQ depth specifically because
    # that's what the L2 strategy stack depends on.
    depth_results: list[dict] = []
    if not args.json:
        log.info("probing CME depth-of-book via MNQ@CME (reqMktDepth)...")
    depth_results.append(_probe_depth_of_book(ib, "MNQ", "CME", timeout=args.probe_timeout, log=log))

    try:
        ib.disconnect()
    except Exception:
        pass

    # Persist to status log
    all_realtime = all(r.get("verdict") == "PASS" for r in results)
    all_depth_ok = all(r.get("verdict") == "PASS" for r in depth_results)
    digest = {
        "ts": datetime.now(UTC).isoformat(),
        "host": args.host,
        "port": port,
        "client_id": args.client_id,
        "setup_status": "OK",
        "setup_error_code": None,
        "credential_status": credential_status,
        "results": results,
        "depth_results": depth_results,
        "all_realtime": all_realtime,
        "all_depth_ok": all_depth_ok,
    }
    _write_status_digest(digest)

    if args.json:
        print(json.dumps(digest, indent=2))
    else:
        print()
        print("=" * 78)
        print(f"IBKR subscription audit  ({digest['ts']})  port={port}")
        print("=" * 78)
        print(f"  {'Exchange':<8s}  {'Symbol':<6s}  {'Type':<14s}  {'Verdict':<8s}  Note")
        print(f"  {'-' * 8:<8s}  {'-' * 6:<6s}  {'-' * 14:<14s}  {'-' * 8:<8s}  {'-' * 40}")
        for r in results:
            label = r.get("data_type_label", "ERROR")
            verdict = r.get("verdict", "ERROR")
            mark = {"PASS": "[OK]", "WARN": "[??]", "FAIL": "[!!]", "ERROR": "[!!]", "TIMEOUT": "[--]"}.get(
                verdict, "[?]"
            )
            print(
                f"  {r['exchange']:<8s}  {r['symbol']:<6s}  {label:<14s}  "
                f"{mark} {verdict:<5s}  {r.get('reason', '')[:50]}"
            )
        print()
        if digest["all_realtime"]:
            print("  >>> ALL REALTIME -- IBKR Pro tick subscriptions active across probed exchanges.")
        else:
            failed = [r["exchange"] for r in results if r.get("verdict") in {"FAIL", "ERROR"}]
            warned = [r["exchange"] for r in results if r.get("verdict") == "WARN"]
            print("  >>> ATTENTION REQUIRED (tick subscriptions)")
            if failed:
                print(f"      FAIL  : {', '.join(failed)} -- subscription likely INACTIVE")
            if warned:
                print(f"      WARN  : {', '.join(warned)} -- frozen (outside RTH or no subscription)")
            print("      Action: log into IBKR account management ->")
            print("              Settings -> User Settings -> Market Data Subscriptions")
        print()

        # Depth-of-book section (separate paid subscription per exchange)
        print("-" * 78)
        print("  Depth-of-book (reqMktDepth) -- required by Phase 1 capture_depth_snapshots")
        print("-" * 78)
        for r in depth_results:
            verdict = r.get("verdict", "ERROR")
            mark = {"PASS": "[OK]", "FAIL": "[!!]", "ERROR": "[!!]", "TIMEOUT": "[--]"}.get(verdict, "[?]")
            print(f"  {r['exchange']:<8s}  {r['symbol']:<6s}  {mark} {verdict:<8s}  {r.get('reason', '')[:50]}")
        if not all_depth_ok:
            print()
            print("      L2 capture daemons WILL NOT WRITE DATA without depth subscription.")
            print("      Action: enable 'CME Real-Time Depth-of-Book (NP, L2)' in IBKR")
            print("              Account Management for $11/month per exchange.")
        print()

    # Exit code 0 if all PASS (both tick AND depth), 1 otherwise
    return 0 if (all_realtime and all_depth_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
