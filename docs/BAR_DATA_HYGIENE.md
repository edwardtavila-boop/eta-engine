# Bar Data Hygiene Validator — Operator Runbook

This runbook covers `scripts/validate_bar_data_hygiene.py`, a stdlib-only
validator that scans bar CSVs for the classic data corruption patterns that
fabricate signal:

- Continuous-front-month rollover splices (the 2026-05-07 audit found 65 in
  `NG1_1h.csv` and 14 in `CL1_1h.csv`).
- yfinance hygiene faults — non-finite OHLC, low > high, low/high outside the
  open/close envelope (the audit found a row in `ES1_5m.csv` with `low=31.75`
  while close was ~3875).
- Negative or NaN volumes.
- Duplicate or out-of-order timestamps.
- Gaps larger than `N` expected bar intervals (default 3, weekend window
  excluded for futures, never excluded for crypto).

The validator is read-only. It never edits a CSV. Output is a JSON report
plus a console summary.

## Standard runs

Daily fleet pass (every CSV under `mnq_data/history/` and
`data/crypto/{,ibkr/}history/`):

```powershell
cd C:\EvolutionaryTradingAlgo\eta_engine
python -m eta_engine.scripts.validate_bar_data_hygiene `
    --all-csvs `
    --output reports/bar_data_hygiene/$(Get-Date -Format yyyyMMdd).json
```

Targeted run on a couple of suspect files:

```powershell
python -m eta_engine.scripts.validate_bar_data_hygiene `
    --files mnq_data/history/NG1_1h.csv mnq_data/history/CL1_1h.csv `
    --threshold-pct 5.0 `
    --output reports/bar_data_hygiene/ng_cl.json
```

Exit codes:

| Code | Verdict | Action |
| --- | --- | --- |
| 0 | PASS | nothing to do |
| 1 | WARN | review report; usually only adjacent jumps or gaps |
| 2 | FAIL | halt promotion of any bot whose primary data file is in this report |

## Recommended thresholds

| Asset class | Default | Rationale |
| --- | --- | --- |
| Index futures (ES1, NQ1, M2K1, MES1, MNQ1, M2K1, RTY1) | 5.0% | Real-session moves rarely exceed this; honest sessions clipping above 5% are rare enough that they're worth manual eyes. |
| Energies (CL1, NG1, RB1, HO1, BZ1) | 5.0% | NG headline shocks can hit 5% legitimately; raise to 7% if false-positive rate is too high after rollover tagging absorbs most of the noise. |
| Metals (GC1, SI1, HG1) | 5.0% | Same as index futures. |
| Currency / FX futures (6E1, 6B1, 6J1) | 3.0% | Currency vol is much lower; tighten threshold so splices stand out. Pass `--threshold-pct 3.0`. |
| Treasury / rates (ZN, ZT, ZB, ZF) | 3.0% | Rates are even lower vol — anything above 3% is almost certainly a splice. The script uses 3% by default for symbols starting with `ZN`/`ZT`/`ZB`/`ZF`/`TN`/`FV`/`TY`/`US`. |
| Crypto (BTC, ETH, SOL, XRP, MBT, MET) | 10.0% | Crypto routinely posts >5% candle moves; 10% catches the actually-broken bars while letting honest volatility through. |

If you're scanning a non-canonical symbol the validator falls back to the
futures default (5%). Override with `--threshold-pct`.

## Scheduled run (Windows Task Scheduler)

Add to the nightly maintenance window so reports land before the morning
backtest pass:

```powershell
$action = New-ScheduledTaskAction `
    -Execute "python" `
    -Argument "-m eta_engine.scripts.validate_bar_data_hygiene --all-csvs --output reports/bar_data_hygiene/auto_$(Get-Date -Format yyyyMMdd).json --quiet" `
    -WorkingDirectory "C:\EvolutionaryTradingAlgo\eta_engine"
$trigger = New-ScheduledTaskTrigger -Daily -At 05:30
Register-ScheduledTask -TaskName "ETA-BarDataHygiene" -Action $action -Trigger $trigger
```

The task writes to `eta_engine/reports/bar_data_hygiene/` (canonical write
path; never to OneDrive or `%LOCALAPPDATA%`).

## Interpreting the report

```json
{
  "scanned_at": "2026-05-07T05:30:00+00:00",
  "overall": "WARN",
  "files": [
    {
      "path": "mnq_data/history/NG1_1h.csv",
      "rows": 13460,
      "error": null,
      "summary": {
        "total_issues": 65,
        "by_type": {"adjacent_jump": 65},
        "rollover_candidates": 12,
        "asset_class": "futures",
        "threshold_pct": 5.0
      },
      "issues": [
        {
          "row": 5234,
          "type": "adjacent_jump",
          "ts": 1717459200,
          "magnitude_pct": 7.21,
          "prev_close": 2.41,
          "close": 2.585,
          "rollover_candidate": true,
          "detail": "log_return=0.0696 (+7.21%) threshold=5.00%"
        }
      ]
    }
  ]
}
```

- `summary.rollover_candidates` counts adjacent jumps that fall inside a known
  rollover window for the symbol's asset class. They're warnings, not blockers
  — for each one, ideally confirm against the actual contract roll calendar
  (3rd Friday of the next-quarter month for index futures; specific roll
  schedules per energy/metal/grain).
- A `SUSPECT_DATA` (i.e. `rollover_candidate=false`) jump is the dangerous
  case: real backtest signals built on top of these are fictitious. Halt
  promotion of any bot whose primary data file shows non-rollover adjacent
  jumps until the file is refetched / cleaned.
- An `ohlc_invalid`, `non_finite_ohlc`, `duplicate_timestamp`, or
  `out_of_order_timestamp` always trips overall=FAIL. These can never be
  rationalized — re-fetch the file.

## Promotion gate

A bot must NOT be promoted from paper to live (or expanded in size) if its
primary data CSV's most recent hygiene report has:

- `overall = FAIL`, OR
- > 10 non-rollover adjacent jumps, OR
- any `ohlc_invalid` or duplicate/out-of-order timestamps in the last 30 days
  of bars.

Document the override and the data-refetch in `ACTIONS_FOR_EDWARD.md` if a
bot must move forward despite warnings.

## Tests

```powershell
cd C:\EvolutionaryTradingAlgo\eta_engine
python -m pytest tests/test_validate_bar_data_hygiene.py -q
```
