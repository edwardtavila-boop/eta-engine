# OOS Regime Tearsheet Sample

Sample generated shape for reviewing the tearsheet's OOS regime section. The
numbers below are fixture values only; they are not live trading performance.

## Headline Metrics

| Metric | Value |
|---|---|
| Strategy | `sample-oos-regime` |
| Trades | 3 |
| Win Rate | 33.33% |
| Expectancy (R) | +0.0000 |
| Max DD | 2.50% |

## OOS Regime Performance

Regime source: `regime_state.json` contract fallback

| Regime | OOS Trades | Win Rate | Avg R | Sum R |
|---|---:|---:|---:|---:|
| chop | 1 | 0.0% | -1.000 | -1.000 |
| trending_up | 2 | 50.0% | +0.500 | +1.000 |

## Operator Note

Use this section to spot OOS concentration risk: a strategy can pass headline
metrics while still bleeding in one regime. The live renderer prefers
trade-level `regime` labels, then falls back to the existing
`regime_state.json` contract without introducing a new classifier.
