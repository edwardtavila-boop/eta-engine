"""Strategy Lab — Streamlit UI for walk-forward backtesting.

Run: streamlit run eta_engine/feeds/strategy_lab/app.py
"""

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from eta_engine.feeds.strategy_lab.engine import (
    WalkForwardEngine,
    parse_strategy_yaml,
    save_lab_report,
)
from eta_engine.scripts import workspace_roots

st.set_page_config(page_title="ETA Strategy Lab", page_icon="🧪", layout="wide")
st.title("🧪 ETA Strategy Lab — Walk-Forward Sandbox")
st.markdown("Define a strategy in YAML, run walk-forward validation, get a lab report.")

col1, col2 = st.columns([1, 1])

with col1:
    st.subheader("Strategy Spec (YAML)")
    default_yaml = """id: my_strategy_v1
symbol: MNQ
entry: ema_cross
atr_period: 14
stop_loss: atr*1.5
    take_profit: atr*3.0
description: EMA 9/21 cross with ATR-based stop/target
"""
    yaml_text = st.text_area("YAML", default_yaml, height=300)
    symbol = st.selectbox("Symbol", ["MNQ", "NQ", "BTC", "ETH", "SOL"])
    bar_dir = st.text_input("Bar Data Directory", str(workspace_roots.WORKSPACE_ROOT / "data"))
    run_btn = st.button("▶ Run Walk-Forward", type="primary")

with col2:
    st.subheader("Results")
    results_placeholder = st.empty()

if run_btn:
    with st.spinner("Running walk-forward analysis..."):
        try:
            spec = parse_strategy_yaml(yaml_text)
            spec["symbol"] = symbol
            engine = WalkForwardEngine(bar_dir=bar_dir)
            result = engine.run(spec, symbol=symbol)

            with results_placeholder.container():
                col_a, col_b, col_c, col_d = st.columns(4)
                col_a.metric("Trades", result.total_trades)
                col_b.metric("Win Rate", f"{result.win_rate:.1%}")
                col_c.metric("Expectancy", f"${result.expectancy:.2f}")
                col_d.metric("Sharpe", f"{result.sharpe:.2f}")

                col_e, col_f, col_g = st.columns(3)
                col_e.metric("Max DD", f"{result.max_drawdown:.1%}")
                col_f.metric("Windows", result.walk_forward_windows)
                col_g.metric("Passed", "✅ YES" if result.passed else "❌ NO")

                if result.parameter_heatmap:
                    st.subheader("Parameter Sensitivity")
                    st.json(result.parameter_heatmap)

                if result.regime_conditional_pnl:
                    st.subheader("Regime-Conditional PnL")
                    st.json(result.regime_conditional_pnl)

                if result.passed:
                    out_dir = workspace_roots.WORKSPACE_ROOT / "reports" / "lab_reports"
                    path = save_lab_report(result, out_dir)
                    st.success(f"Lab report saved: {path}")
                    st.info("Strategy passed — can be promoted to paper_soak.")
                else:
                    st.warning("Strategy did not pass minimum thresholds (WR≥40%, Sharpe≥0.5, expectancy>0, DD<30%)")

        except Exception as e:
            st.error(f"Error: {e}")

st.markdown("---")
st.markdown(
    "**Lab Report Integration:** Passed strategies generate a `lab_report.json` that attaches to `StrategyAssignment.extras` for automatic promotion to paper_soak lane."
)
