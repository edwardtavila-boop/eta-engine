# EVOLUTIONARY TRADING ALGO — Cross-regime OOS validation

_generated_: `2026-04-27T03:54:04.650405+00:00`

## Verdict: FAIL

| Regime | IS trades | IS exp (R) | IS Sharpe | OOS trades | OOS exp (R) | OOS Sharpe | Degradation |
|---|---:|---:|---:|---:|---:|---:|---:|
| TRENDING | 56 | +0.930 | 14.07 | 23 | +1.012 | 16.42 | -8.8% |
| RANGING | 39 | +0.109 | 1.39 | 11 | +0.069 | 0.88 | +36.1% |
| HIGH_VOL | 44 | +0.216 | 2.74 | 14 | -0.559 | -9.57 | +358.7% |
| LOW_VOL | 41 | +0.366 | 4.66 | 17 | +0.073 | 0.93 | +80.0% |

## Gate

- at least one regime live-tradeable: **True**  (TRENDING)
- no overfit collapse: **False**

### Regimes not cleared for live trading

- **RANGING**: OOS exp +0.069R < 0.15R; OOS trades 11 < 20
- **HIGH_VOL**: OOS exp -0.559R < 0.15R; OOS trades 14 < 20; degradation +358.7% > 60%
- **LOW_VOL**: OOS exp +0.073R < 0.15R; OOS trades 17 < 20; degradation +80.0% > 60%

### Fail reasons

- HIGH_VOL: IS +0.216R -> OOS -0.559R (sign flip, deg +358.7%) -- exclude this regime
