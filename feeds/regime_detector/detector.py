"""Cross-Asset Regime Detector — standalone service.

Ingests bar data from canonical bar library, computes rolling correlations
across MNQ/NQ/BTC/ETH/SOL/DXY/VIX, and emits a 6-state regime classification
to a JSON file that JARVIS consumes (replacing hardcoded neutral regime).

States: trending_up, trending_down, chop, crisis, vol_expansion, vol_compression
Output: state/jarvis_intel/regime_state.json
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

log = logging.getLogger("regime_detector")

# ── Constants ────────────────────────────────────────────────────────────────

SYMBOLS = ("MNQ", "NQ", "BTC", "ETH", "SOL", "DXY", "VIX")
REGIMES = ("trending_up", "trending_down", "chop", "crisis", "vol_expansion", "vol_compression")

_SHORT_WINDOW = 20  # rolling correlation window (bars)
_LONG_WINDOW = 60  # longer horizon for vol regime
_VOL_LOOKBACK = 20  # ATR-style vol lookback
_CRISIS_THRESHOLD = 0.15  # 15% drawdown from recent peak
_CHOP_THRESHOLD = 0.15  # ATR % of price for chop detection


@dataclass
class RegimeState:
    """One regime snapshot."""

    timestamp: str
    primary_regime: str
    confidence: float
    per_asset: dict[str, str]
    cross_asset_correlations: dict[str, float]
    vol_regime: str
    drawdown_regime: str
    score_vector: dict[str, float]


class CrossAssetRegimeDetector:
    """Compute market regime from bar data and emit to JSON."""

    def __init__(
        self,
        bar_dir: str | Path,
        output_path: str | Path,
        *,
        short_window: int = _SHORT_WINDOW,
        long_window: int = _LONG_WINDOW,
    ) -> None:
        self.bar_dir = Path(bar_dir)
        self.output_path = Path(output_path)
        self.short_window = short_window
        self.long_window = long_window
        self._bar_cache: dict[str, np.ndarray] = {}

    # ── Public API ──────────────────────────────────────────────────────────

    def run(self) -> RegimeState:
        """Full detection pipeline: load → compute → classify → write."""
        bars = self._load_all_bars()
        if not bars:
            log.warning("No bar data loaded; emitting neutral regime")
            return self._neutral_fallback()

        returns = {sym: self._compute_returns(arr) for sym, arr in bars.items()}
        correlations = self._rolling_correlations(returns)
        vol_regime = self._volatility_regime(bars)
        drawdown_regime = self._drawdown_regime(bars)
        per_asset = self._per_asset_regime(bars, returns)
        primary, confidence, scores = self._classify_primary(correlations, vol_regime, drawdown_regime, per_asset)

        state = RegimeState(
            timestamp=datetime.now(UTC).isoformat(),
            primary_regime=primary,
            confidence=round(confidence, 3),
            per_asset=per_asset,
            cross_asset_correlations={k: round(v, 3) for k, v in correlations.items()},
            vol_regime=vol_regime,
            drawdown_regime=drawdown_regime,
            score_vector=scores,
        )

        self._write(state)
        log.info("Regime: %s (conf=%.2f, vol=%s, dd=%s)", primary, confidence, vol_regime, drawdown_regime)
        return state

    # ── Data Loading ────────────────────────────────────────────────────────

    def _load_all_bars(self) -> dict[str, np.ndarray]:
        """Load close prices for all tracked symbols from bar CSV files."""
        result: dict[str, np.ndarray] = {}
        for sym in SYMBOLS:
            arr = self._load_symbol_bars(sym)
            if arr is not None and len(arr) > self.long_window:
                result[sym] = arr
        return result

    def _load_symbol_bars(self, symbol: str) -> np.ndarray | None:
        """Load close prices from the canonical bar CSV for a symbol."""
        # Try multiple bar file locations
        candidates = [
            self.bar_dir / f"{symbol}_5m.csv",
            self.bar_dir / f"{symbol}_1h.csv",
            self.bar_dir / f"{symbol}.csv",
            self.bar_dir / "bars" / f"{symbol}.csv",
            # Also check common data directories
            self.bar_dir.parent / f"{symbol}_5m.csv",
            self.bar_dir.parent / f"{symbol}.csv",
        ]
        for path in candidates:
            if path.is_file():
                try:
                    return self._read_bars(path)
                except Exception:
                    continue
        return None

    @staticmethod
    def _read_bars(path: Path) -> np.ndarray | None:
        """Read close prices from CSV. Expects a 'close' column or 5th column."""
        try:
            data = np.genfromtxt(
                path,
                delimiter=",",
                skip_header=1,
                usecols=(4,),  # close is typically column 4
                dtype=float,
                missing_values="",
                filling_values=np.nan,
            )
            data = data[~np.isnan(data)]
            if len(data) < 30:
                return None
            return data
        except Exception:
            return None

    # ── Computations ────────────────────────────────────────────────────────

    @staticmethod
    def _compute_returns(prices: np.ndarray) -> np.ndarray:
        """Simple returns: (p_t - p_{t-1}) / p_{t-1}."""
        if len(prices) < 2:
            return np.array([])
        return np.diff(prices) / prices[:-1]

    def _rolling_correlations(self, returns: dict[str, np.ndarray]) -> dict[str, float]:
        """Compute pairwise rolling correlations between all asset pairs."""
        symbols = list(returns.keys())
        corr_map: dict[str, float] = {}
        for i in range(len(symbols)):
            for j in range(i + 1, len(symbols)):
                s1, s2 = symbols[i], symbols[j]
                r1, r2 = returns[s1], returns[s2]
                if len(r1) < self.short_window or len(r2) < self.short_window:
                    continue
                n = min(len(r1), len(r2), self.short_window)
                corr = np.corrcoef(r1[-n:], r2[-n:])[0, 1]
                if not np.isnan(corr):
                    corr_map[f"{s1}_{s2}"] = float(corr)
        return corr_map

    def _volatility_regime(self, bars: dict[str, np.ndarray]) -> str:
        """Classify vol: expansion vs compression vs normal."""
        vol_scores = []
        for prices in bars.values():
            if len(prices) < _VOL_LOOKBACK + 1:
                continue
            recent_vol = np.std(prices[-_VOL_LOOKBACK:])
            prior_vol = np.std(prices[-_VOL_LOOKBACK * 2 : -_VOL_LOOKBACK])
            if prior_vol > 0:
                vol_scores.append(recent_vol / prior_vol)
        if not vol_scores:
            return "normal"
        avg_ratio = np.mean(vol_scores)
        if avg_ratio > 1.5:
            return "vol_expansion"
        if avg_ratio < 0.6:
            return "vol_compression"
        return "normal"

    def _drawdown_regime(self, bars: dict[str, np.ndarray]) -> str:
        """Detect crisis regime via drawdown from rolling peak."""
        dd_flags = []
        for prices in bars.values():
            if len(prices) < self.short_window:
                continue
            peak = np.maximum.accumulate(prices[-self.short_window :])
            dd = (peak - prices[-self.short_window :]) / peak
            max_dd = np.max(dd)
            dd_flags.append(max_dd > _CRISIS_THRESHOLD)
        crisis_ratio = sum(dd_flags) / max(len(dd_flags), 1)
        if crisis_ratio > 0.5:
            return "crisis"
        return "normal"

    def _per_asset_regime(self, bars: dict[str, np.ndarray], returns: dict[str, np.ndarray]) -> dict[str, str]:
        """Classify each asset individually."""
        result: dict[str, str] = {}
        for sym in bars:
            r = returns.get(sym)
            if r is None or len(r) < 10:
                result[sym] = "neutral"
                continue
            p = bars[sym]
            recent_ret = np.mean(r[-10:])
            atr = np.std(p[-_VOL_LOOKBACK:])
            atr_pct = atr / np.mean(p[-_VOL_LOOKBACK:]) if np.mean(p[-_VOL_LOOKBACK:]) > 0 else 0

            if atr_pct < _CHOP_THRESHOLD:
                if recent_ret > 0.005:
                    result[sym] = "trending_up"
                elif recent_ret < -0.005:
                    result[sym] = "trending_down"
                else:
                    result[sym] = "chop"
            else:
                if recent_ret > 0.01:
                    result[sym] = "trending_up"
                elif recent_ret < -0.01:
                    result[sym] = "trending_down"
                else:
                    result[sym] = "chop"
        return result

    def _classify_primary(
        self,
        correlations: dict[str, float],
        vol_regime: str,
        drawdown_regime: str,
        per_asset: dict[str, str],
    ) -> tuple[str, float, dict[str, float]]:
        """Combine all signals into a single regime classification."""
        scores = {
            "trending_up": 0.0,
            "trending_down": 0.0,
            "chop": 0.0,
            "crisis": 0.0,
            "vol_expansion": 0.0,
            "vol_compression": 0.0,
        }

        # Vol regime contributes
        if vol_regime == "vol_expansion":
            scores["vol_expansion"] += 2.0
        elif vol_regime == "vol_compression":
            scores["vol_compression"] += 2.0

        # Drawdown regime
        if drawdown_regime == "crisis":
            scores["crisis"] += 3.0

        # Per-asset voting
        up_votes = sum(1 for v in per_asset.values() if v == "trending_up")
        down_votes = sum(1 for v in per_asset.values() if v == "trending_down")
        chop_votes = sum(1 for v in per_asset.values() if v == "chop")
        total = len(per_asset) or 1

        scores["trending_up"] += up_votes / total * 3
        scores["trending_down"] += down_votes / total * 3
        scores["chop"] += chop_votes / total * 2

        # Cross-asset correlation bonus
        avg_corr = np.mean(list(correlations.values())) if correlations else 0
        if avg_corr > 0.6:
            scores["trending_up" if up_votes > down_votes else "trending_down"] += 1.0
        elif avg_corr < -0.3:
            scores["chop"] += 1.0

        # Select winner
        best = max(scores, key=scores.get)
        second = sorted(scores.values())[-2] if len(scores) > 1 else 0
        total_score = sum(scores.values()) or 1
        confidence = (scores[best] - second) / total_score + 0.5
        confidence = min(max(confidence, 0.0), 1.0)

        return best, confidence, scores

    # ── Output ──────────────────────────────────────────────────────────────

    def _write(self, state: RegimeState) -> None:
        """Write regime state JSON to output path."""
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            json.dumps(asdict(state), indent=2, default=str),
            encoding="utf-8",
        )

    def _neutral_fallback(self) -> RegimeState:
        """Neutral regime when no data is available."""
        state = RegimeState(
            timestamp=datetime.now(UTC).isoformat(),
            primary_regime="neutral",
            confidence=0.5,
            per_asset={s: "neutral" for s in SYMBOLS},
            cross_asset_correlations={},
            vol_regime="normal",
            drawdown_regime="normal",
            score_vector={"neutral": 1.0},
        )
        self._write(state)
        return state


# ── CLI Entrypoint ───────────────────────────────────────────────────────────


def main() -> None:
    """CLI entrypoint: reads bars, writes regime_state.json."""
    from argparse import ArgumentParser

    parser = ArgumentParser(description="Cross-Asset Regime Detector")
    parser.add_argument(
        "--bar-dir",
        type=Path,
        default=Path("C:/EvolutionaryTradingAlgo/data"),
        help="Directory containing bar CSV files",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state/jarvis_intel/regime_state.json"),
        help="Output path for regime_state.json",
    )
    parser.add_argument("--interval", type=int, default=0, help="Run every N seconds (0 = one-shot)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    detector = CrossAssetRegimeDetector(bar_dir=args.bar_dir, output_path=args.output)

    if args.interval > 0:
        log.info("Regime detector running every %ds...", args.interval)
        while True:
            detector.run()
            time.sleep(args.interval)
    else:
        state = detector.run()
        print(f"Regime: {state.primary_regime} (conf={state.confidence})")
        print(f"Vol: {state.vol_regime} | Drawdown: {state.drawdown_regime}")
        print(f"Per-asset: {state.per_asset}")


if __name__ == "__main__":
    main()
