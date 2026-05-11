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


# Representative probe symbol per exchange.  These are the most-liquid
# contracts on each venue so the probe always gets a fresh quote.
PROBES: dict[str, dict[str, str]] = {
    "CME":   {"symbol": "MNQ", "secType": "CONTFUT", "exchange": "CME",
              "purpose": "Equity-index futures (MNQ/NQ/MES/ES/M2K/RTY)"},
    "NYMEX": {"symbol": "CL",  "secType": "CONTFUT", "exchange": "NYMEX",
              "purpose": "Energy futures (CL/MCL/NG)"},
    "COMEX": {"symbol": "GC",  "secType": "CONTFUT", "exchange": "COMEX",
              "purpose": "Metals futures (GC/MGC)"},
    "CBOT":  {"symbol": "ZN",  "secType": "CONTFUT", "exchange": "CBOT",
              "purpose": "Rates futures (ZN/ZB/YM/MYM)"},
    # IBKR routes 6E via CME (Globex), so a separate ICE probe is
    # only relevant if the operator subscribes to ICE-listed FX
    # crosses.  Skip by default; can be re-enabled with --include-ice.
}

OPTIONAL_PROBES: dict[str, dict[str, str]] = {
    "ICE":   {"symbol": "DX",  "secType": "CONTFUT", "exchange": "NYBOT",
              "purpose": "ICE Forex (DX dollar index, optional)"},
}


# IBKR mktDataType callback values:
DATA_TYPE_LABEL = {
    1: ("REALTIME",        "PASS",  "live tick stream"),
    2: ("FROZEN",          "WARN",  "frozen at last close -- outside RTH or no subscription"),
    3: ("DELAYED",         "FAIL",  "15-min delayed -- subscription INACTIVE for this exchange"),
    4: ("DELAYED-FROZEN",  "FAIL",  "delayed AND frozen -- subscription INACTIVE + outside RTH"),
}


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


def _probe_one_exchange(ib: object, exchange: str, spec: dict[str, str],
                        timeout: float = 5.0, log: logging.Logger | None = None) -> dict:
    """Issue a reqMktData on one contract, wait for first mktDataType
    callback, then cancel.  Returns a result dict."""
    log = log or logging.getLogger(__name__)
    from ib_insync import ContFuture  # noqa: PLC0415  # local import -- only when running

    try:
        # Use ContFuture (continuous front month) -- cheapest probe contract.
        contract = ContFuture(spec["symbol"], spec["exchange"])
        qualified = ib.qualifyContracts(contract)  # type: ignore[attr-defined]
        if not qualified:
            return {"exchange": exchange, "symbol": spec["symbol"],
                    "data_type": None, "verdict": "ERROR",
                    "reason": f"qualifyContracts returned empty for {spec['symbol']}@{spec['exchange']}"}
        contract = qualified[0]
    except Exception as e:
        return {"exchange": exchange, "symbol": spec["symbol"],
                "data_type": None, "verdict": "ERROR",
                "reason": f"contract qualify failed: {e}"}

    # Force realtime request; IBKR will silently downgrade if the sub
    # isn't active on this exchange -- we read the response back.
    captured_type: list[int] = []

    def _on_market_data_type(msg) -> None:  # noqa: ANN001
        # mktDataTypeEvent fires with attribute marketDataType
        try:
            captured_type.append(int(getattr(msg, "marketDataType", 0)))
        except Exception:
            pass

    try:
        ib.reqMarketDataType(1)  # type: ignore[attr-defined]
        # Subscribe to mktDataType events (best-effort -- ib_insync naming)
        try:
            ib.mktDataTypeEvent += _on_market_data_type  # type: ignore[attr-defined]
        except Exception:
            pass
        ticker = ib.reqMktData(contract, "", False, False)  # type: ignore[attr-defined]
        deadline = time.time() + timeout
        while time.time() < deadline:
            ib.sleep(0.25)  # type: ignore[attr-defined]
            # If we got at least one mktDataType callback, that's our answer.
            if captured_type:
                break
            # Some setups don't emit the callback; infer from ticker.marketDataType
            mdt = getattr(ticker, "marketDataType", None)
            if mdt is not None:
                captured_type.append(int(mdt))
                break
        try:
            ib.cancelMktData(contract)  # type: ignore[attr-defined]
        except Exception:
            pass
    except Exception as e:
        return {"exchange": exchange, "symbol": spec["symbol"],
                "data_type": None, "verdict": "ERROR",
                "reason": f"reqMktData failed: {e}"}

    if not captured_type:
        return {"exchange": exchange, "symbol": spec["symbol"],
                "data_type": None, "verdict": "TIMEOUT",
                "reason": f"no mktDataType callback within {timeout}s -- exchange may be closed"}

    dt = captured_type[0]
    label, verdict, note = DATA_TYPE_LABEL.get(
        dt, (f"UNKNOWN({dt})", "ERROR", "unrecognized data type code"))
    return {"exchange": exchange, "symbol": spec["symbol"],
            "data_type": dt, "data_type_label": label,
            "verdict": verdict, "reason": note,
            "purpose": spec["purpose"]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=None,
                    help="TWS API port (auto-detect from 4002/7497/4001)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--client-id", type=int, default=33,
                    help="ib_insync client ID (default 33 -- separate from supervisor)")
    ap.add_argument("--include-ice", action="store_true",
                    help="Also probe ICE/NYBOT (e.g. DX dollar index)")
    ap.add_argument("--probe-timeout", type=float, default=5.0,
                    help="Seconds to wait for mktDataType callback per exchange")
    ap.add_argument("--json", action="store_true",
                    help="Output JSON only (machine-readable)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("verify_ibkr_subs")

    # Port auto-detect
    port = args.port or _tws_port()
    if port is None:
        msg = "TWS Gateway unreachable on 4002/7497/4001 -- start TWS or pass --port"
        if args.json:
            print(json.dumps({"error": msg, "exit_code": 2}))
        else:
            log.error(msg)
        return 2

    try:
        from ib_insync import IB
    except ImportError:
        msg = "ib_insync not installed -- pip install ib_insync"
        if args.json:
            print(json.dumps({"error": msg, "exit_code": 2}))
        else:
            log.error(msg)
        return 2

    ib = IB()
    try:
        ib.connect(args.host, port, clientId=args.client_id, timeout=10)
    except Exception as e:
        msg = f"TWS connect failed at {args.host}:{port} clientId={args.client_id} -- {e}"
        if args.json:
            print(json.dumps({"error": msg, "exit_code": 2}))
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

    try:
        ib.disconnect()
    except Exception:
        pass

    # Persist to status log
    digest = {
        "ts": datetime.now(UTC).isoformat(),
        "host": args.host, "port": port, "client_id": args.client_id,
        "results": results,
        "all_realtime": all(r.get("verdict") == "PASS" for r in results),
    }
    try:
        with STATUS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(digest, separators=(",", ":")) + "\n")
    except OSError:
        pass

    if args.json:
        print(json.dumps(digest, indent=2))
    else:
        print()
        print("=" * 78)
        print(f"IBKR subscription audit  ({digest['ts']})  port={port}")
        print("=" * 78)
        print(f"  {'Exchange':<8s}  {'Symbol':<6s}  {'Type':<14s}  {'Verdict':<8s}  Note")
        print(f"  {'-'*8:<8s}  {'-'*6:<6s}  {'-'*14:<14s}  {'-'*8:<8s}  {'-'*40}")
        for r in results:
            label = r.get("data_type_label", "ERROR")
            verdict = r.get("verdict", "ERROR")
            mark = {"PASS": "[OK]", "WARN": "[??]", "FAIL": "[!!]",
                    "ERROR": "[!!]", "TIMEOUT": "[--]"}.get(verdict, "[?]")
            print(f"  {r['exchange']:<8s}  {r['symbol']:<6s}  {label:<14s}  "
                  f"{mark} {verdict:<5s}  {r.get('reason', '')[:50]}")
        print()
        if digest["all_realtime"]:
            print("  >>> ALL REALTIME -- IBKR Pro subscriptions active across probed exchanges.")
        else:
            failed = [r["exchange"] for r in results if r.get("verdict") in {"FAIL", "ERROR"}]
            warned = [r["exchange"] for r in results if r.get("verdict") == "WARN"]
            print("  >>> ATTENTION REQUIRED")
            if failed:
                print(f"      FAIL  : {', '.join(failed)} -- subscription likely INACTIVE")
            if warned:
                print(f"      WARN  : {', '.join(warned)} -- frozen (outside RTH or no subscription)")
            print("      Action: log into IBKR account management ->")
            print("              Settings -> User Settings -> Market Data Subscriptions")
        print()

    # Exit code 0 if all PASS, 1 otherwise
    return 0 if digest["all_realtime"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
