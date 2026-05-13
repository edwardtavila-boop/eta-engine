"""Statistical rigor extensions for the WalkForwardEngine.

Five additions, designed to replace the soft passed=True gate (count +
sharpe + positive expectancy) with a strict gate that demands the result
survive multiple-comparison adjustment, friction, and split-half stability.

All math is pure numpy (+ stdlib) - no scipy. Functions are deterministic
when seeded, so identical CSV inputs produce identical CIs across runs.

Honest caveats (read these before trusting the gate):

* 5000 block-bootstrap reps stabilize CI percentile estimates to about
  +/-0.01 for samples of N>=100 trades. Below N=50 the bootstrap itself
  is unreliable - the resampled distribution is dominated by a handful
  of raw observations, so the CI is narrower than the true sampling
  error.
* multi_test_count defaults to the size of ASSIGNMENTS at import time
  but the operator MUST override this to the actual number of parameter
  combinations tested if a sweep is run. Bonferroni is only correct
  when N reflects all hypotheses examined.
* Deflated Sharpe assumes IID returns. Strategy R-multiples are NOT iid
  (autocorrelation, regime clustering). The block-bootstrap CI is the
  more honest signal. Treat sharpe_deflated as a rough corroboration,
  not as a gate-replacement on its own.
* Friction is computed as a per-trade R deduction using a fixed assumed
  stop distance per symbol. Real friction varies with the actual stop
  ATR - the per-trade stop_R distance from the engine would be more
  accurate but is not surfaced through the current _simulate_trade
  interface.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from eta_engine.feeds.instrument_specs import get_spec

# Euler-Mascheroni constant - used in Lopez de Prado Expected-Max-SR.
EULER_MASCHERONI: float = 0.5772156649015329


# --- Inverse normal CDF (probit) - pure numpy/math ---
# Beasley-Springer-Moro algorithm. Accurate to ~1e-9 in (0.001, 0.999).
# Needed for Lopez de Prado E[max k SR] - no scipy dependency.

_BSM_A: tuple[float, ...] = (
    -3.969683028665376e1,
    2.209460984245205e2,
    -2.759285104469687e2,
    1.383577518672690e2,
    -3.066479806614716e1,
    2.506628277459239e0,
)
_BSM_B: tuple[float, ...] = (
    -5.447609879822406e1,
    1.615858368580409e2,
    -1.556989798598866e2,
    6.680131188771972e1,
    -1.328068155288572e1,
)
_BSM_C: tuple[float, ...] = (
    -7.784894002430293e-3,
    -3.223964580411365e-1,
    -2.400758277161838e0,
    -2.549732539343734e0,
    4.374664141464968e0,
    2.938163982698783e0,
)
_BSM_D: tuple[float, ...] = (
    7.784695709041462e-3,
    3.224671290700398e-1,
    2.445134137142996e0,
    3.754408661907416e0,
)


def norm_ppf(p: float) -> float:
    """Inverse normal CDF (a.k.a. probit). Pure-numpy approximation."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"norm_ppf requires 0 < p < 1, got {p}")
    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        num = ((((_BSM_C[0] * q + _BSM_C[1]) * q + _BSM_C[2]) * q + _BSM_C[3]) * q + _BSM_C[4]) * q + _BSM_C[5]
        den = (((_BSM_D[0] * q + _BSM_D[1]) * q + _BSM_D[2]) * q + _BSM_D[3]) * q + 1
        return num / den
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1 - p))
        num = ((((_BSM_C[0] * q + _BSM_C[1]) * q + _BSM_C[2]) * q + _BSM_C[3]) * q + _BSM_C[4]) * q + _BSM_C[5]
        den = (((_BSM_D[0] * q + _BSM_D[1]) * q + _BSM_D[2]) * q + _BSM_D[3]) * q + 1
        return -num / den
    q = p - 0.5
    r = q * q
    num = (((((_BSM_A[0] * r + _BSM_A[1]) * r + _BSM_A[2]) * r + _BSM_A[3]) * r + _BSM_A[4]) * r + _BSM_A[5]) * q
    den = ((((_BSM_B[0] * r + _BSM_B[1]) * r + _BSM_B[2]) * r + _BSM_B[3]) * r + _BSM_B[4]) * r + 1
    return num / den


# --- 1. Block bootstrap on expR ---


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    p5: float
    p50: float
    p95: float
    p_value_raw: float
    block_size: int
    n_resamples: int


def block_bootstrap_expR(
    pnl_r: np.ndarray,
    *,
    block_size: int = 5,
    n_resamples: int = 5000,
    seed: int = 12345,
) -> BootstrapResult:
    """Stationary block bootstrap on expectancy-R.

    Block size 5-10 handles short-range autocorrelation typical of
    intraday strategies. We use circular blocks (wrap around) so every
    bar contributes equally and the resampled length matches the input
    length exactly.
    """
    n = int(pnl_r.size)
    if n == 0:
        return BootstrapResult(0.0, 0.0, 0.0, 1.0, block_size, n_resamples)
    rng = np.random.default_rng(seed)
    n_blocks = (n + block_size - 1) // block_size
    starts = rng.integers(0, n, size=(n_resamples, n_blocks))
    offsets = np.arange(block_size)
    idx = (starts[:, :, None] + offsets[None, None, :]) % n
    idx = idx.reshape(n_resamples, n_blocks * block_size)[:, :n]
    samples = pnl_r[idx]
    means = samples.mean(axis=1)
    p5, p50, p95 = np.percentile(means, [5.0, 50.0, 95.0])
    p_raw = float((means <= 0.0).mean())
    return BootstrapResult(
        p5=float(p5),
        p50=float(p50),
        p95=float(p95),
        p_value_raw=p_raw,
        block_size=block_size,
        n_resamples=n_resamples,
    )


# --- 2. Bonferroni adjustment ---


def bonferroni_adjust(p_raw: float, multi_test_count: int) -> float:
    """Bonferroni-adjusted p-value, capped at 1.0."""
    if multi_test_count <= 0:
        return float(p_raw)
    return float(min(1.0, p_raw * multi_test_count))


def default_multi_test_count() -> int:
    """Number of active strategies in the production registry, used as
    the default multi-test multiplier when a spec does not provide one.

    Read at call-time (not import-time) so test isolation is not broken
    by the registry being unavailable.
    """
    try:
        from eta_engine.strategies.per_bot_registry import ASSIGNMENTS, is_active

        n = sum(1 for a in ASSIGNMENTS if is_active(a))
        return max(1, n)
    except (ImportError, AttributeError):
        return 1


# --- 3. Friction-aware net expR ---


def friction_R_per_trade(
    symbol: str,
    avg_stop_atr_mult: float = 1.5,
    typical_atr_pts: float | None = None,
) -> float:
    """Estimate per-trade friction expressed in R-multiples.

    R = (commission_rt + 2*half_spread_dollars) / stop_distance_dollars
    where stop_distance_dollars ~ avg_stop_atr_mult * typical_atr_dollars.

    typical_atr_pts defaults to a per-symbol ballpark - operator can
    override per-spec if they have a real ATR estimate. The point of
    this function is NOT to be perfectly accurate; it is to make sure
    a strategy that nets +0.05 R on raw cannot quietly survive after
    realistic round-trip costs.
    """
    spec = get_spec(symbol)
    half_spread_dollars = spec.half_spread_ticks * spec.tick_value_usd
    rt_friction_dollars = spec.commission_rt + 2.0 * half_spread_dollars
    fallback_atr_pts = {
        "MNQ": 30.0,
        "MNQ1": 30.0,
        "NQ": 30.0,
        "NQ1": 30.0,
        "ES": 6.0,
        "ES1": 6.0,
        "MES": 6.0,
        "GC": 8.0,
        "GC1": 8.0,
        "MGC": 8.0,
        "CL": 1.0,
        "CL1": 1.0,
        "MCL": 1.0,
        "NG": 0.10,
        "NG1": 0.10,
        "6E": 0.0030,
        "6E1": 0.0030,
        "M6E": 0.0030,
        "ZN": 0.30,
        "ZN1": 0.30,
        "MBT": 1500.0,
        "BTC": 1500.0,
        "MET": 60.0,
        "ETH": 60.0,
        "SOL": 5.0,
        "XRP": 0.05,
    }
    atr_pts = (
        typical_atr_pts
        if typical_atr_pts is not None
        else fallback_atr_pts.get(
            symbol.upper(),
            1.0,
        )
    )
    # 2026-05-07: was ``spec.point_value`` directly. That returned 5.0 for
    # BTC and 50.0 for ETH from the CME futures table even when the audit
    # is evaluating spot-routed bots (multiplier=1.0). The result was
    # under-stated friction per R for BTC/ETH spot bots, which inflated
    # net_expR. ``effective_point_value`` resolves the spot vs futures
    # ambiguity correctly.
    try:
        from eta_engine.feeds.instrument_specs import effective_point_value

        pv = float(effective_point_value(symbol, route="auto") or spec.point_value)
    except Exception:  # noqa: BLE001
        pv = spec.point_value
    stop_distance_dollars = avg_stop_atr_mult * atr_pts * pv
    if stop_distance_dollars <= 0.0:
        return 0.0
    return float(rt_friction_dollars / stop_distance_dollars)


def net_expR(pnl_r: np.ndarray, friction_per_trade_r: float) -> float:
    """Mean R-multiple after subtracting per-trade friction."""
    if pnl_r.size == 0:
        return 0.0
    return float(pnl_r.mean() - friction_per_trade_r)


# --- 4. Split-half stability ---


@dataclass(frozen=True, slots=True)
class SplitHalfResult:
    expR_half_1: float
    expR_half_2: float
    sign_stable: bool


def split_half_stability(pnl_r: np.ndarray) -> SplitHalfResult:
    """Compute expR on the first half and second half of the OOS sample.

    Sign-stable means both halves have the same sign of mean. A flat-zero
    half (mean exactly 0.0) is treated as NOT stable on the conservative
    side - we want positive evidence in both halves for a passing gate.
    """
    n = pnl_r.size
    if n < 2:
        return SplitHalfResult(0.0, 0.0, False)
    half = n // 2
    h1 = float(pnl_r[:half].mean())
    h2 = float(pnl_r[half:].mean())
    stable = (h1 > 0 and h2 > 0) or (h1 < 0 and h2 < 0)
    return SplitHalfResult(expR_half_1=h1, expR_half_2=h2, sign_stable=stable)


# --- 5. Deflated Sharpe (Lopez de Prado) ---


def expected_max_sr(n_trials: int) -> float:
    """E[max of N standard-normal SRs] via Lopez de Prado approximation:

        E[max] ~ (1 - gamma) * Phi^-1(1 - 1/N)
                 + gamma * Phi^-1(1 - 1/(N*e))

    Returns 0.0 for N <= 1 (no multi-test penalty).
    """
    if n_trials <= 1:
        return 0.0
    return (1 - EULER_MASCHERONI) * norm_ppf(1 - 1.0 / n_trials) + EULER_MASCHERONI * norm_ppf(
        1 - 1.0 / (n_trials * math.e)
    )


def deflated_sharpe(
    pnl_r: np.ndarray,
    *,
    n_trials: int,
) -> float:
    """Deflated Sharpe ratio (Lopez de Prado 2014).

    SR_deflated = (SR_observed - SR_expected_max) /
                  sqrt(1/T * (1 - skew*SR + (kurt - 1)/4 * SR^2))

    where skew is the third central moment ratio, kurt is the raw fourth
    moment ratio (Pearson kurtosis, normal=3), T is sample size, and
    SR_expected_max comes from expected_max_sr.

    This is a unit-less z-score: SR_deflated > 1.0 is roughly
    "Sharpe is non-zero at the ~85% confidence level after trial-count
    adjustment". Caller decides the threshold.
    """
    n = pnl_r.size
    if n < 4:
        return 0.0
    mean = float(pnl_r.mean())
    sd = float(pnl_r.std(ddof=1))
    if sd <= 0.0:
        return 0.0
    sr = mean / sd
    centered = pnl_r - mean
    m2 = float((centered**2).mean())
    m3 = float((centered**3).mean())
    m4 = float((centered**4).mean())
    if m2 <= 0.0:
        return 0.0
    skew = m3 / (m2**1.5)
    kurt = m4 / (m2**2)
    # Bailey & Lopez de Prado (2014): the expected max of N estimated
    # Sharpe ratios under the null (true SR = 0) has SD = 1/sqrt(T),
    # so E[max_N hat_SR] = (1/sqrt(T)) * E[max_N standard_normals].
    # Without this scaling we'd compare a per-period SR (~0.5) against a
    # raw expected-max (~2.4) and incorrectly fail every honest signal.
    sr_max = expected_max_sr(n_trials) / math.sqrt(n)
    denom_var = (1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr) / n
    if denom_var <= 0.0:
        return 0.0
    return float((sr - sr_max) / math.sqrt(denom_var))


# --- Aggregator ---


@dataclass(frozen=True, slots=True)
class RigorReport:
    expR_p5: float
    expR_p50: float
    expR_p95: float
    bootstrap_block_size: int
    p_value_raw: float
    p_value_bonferroni: float
    multi_test_count: int
    expR_net: float
    friction_R_per_trade: float
    expR_half_1: float
    expR_half_2: float
    split_half_sign_stable: bool
    sharpe_deflated: float
    passed_strict: bool
    strict_fail_reasons: tuple[str, ...]


def compute_rigor(
    pnl_r: np.ndarray,
    *,
    symbol: str,
    multi_test_count: int | None = None,
    block_size: int = 5,
    n_resamples: int = 5000,
    avg_stop_atr_mult: float = 1.5,
    typical_atr_pts: float | None = None,
    seed: int = 12345,
    min_trades: int = 30,
    sharpe_deflated_min: float = 1.0,
) -> RigorReport:
    """Run all 5 extensions and return the strict-gate verdict."""
    n_trials = int(multi_test_count) if multi_test_count is not None else default_multi_test_count()
    boot = block_bootstrap_expR(
        pnl_r,
        block_size=block_size,
        n_resamples=n_resamples,
        seed=seed,
    )
    p_adj = bonferroni_adjust(boot.p_value_raw, n_trials)
    fric = friction_R_per_trade(symbol, avg_stop_atr_mult, typical_atr_pts)
    net = net_expR(pnl_r, fric)
    sh = split_half_stability(pnl_r)
    ds = deflated_sharpe(pnl_r, n_trials=n_trials)

    fails: list[str] = []
    if pnl_r.size < min_trades:
        fails.append(f"total_trades {pnl_r.size} < {min_trades}")
    if boot.p5 <= 0.0:
        fails.append(f"expR_p5 {boot.p5:.3f} <= 0 (CI brackets 0)")
    if p_adj >= 0.05:
        fails.append(f"p_value_bonferroni {p_adj:.3f} >= 0.05")
    if net <= 0.0:
        fails.append(f"expR_net {net:.3f} <= 0 (friction kills edge)")
    if not sh.sign_stable:
        fails.append(
            f"split_half not sign-stable (h1={sh.expR_half_1:.3f}, h2={sh.expR_half_2:.3f})",
        )
    if ds < sharpe_deflated_min:
        fails.append(f"sharpe_deflated {ds:.2f} < {sharpe_deflated_min}")
    passed = not fails

    return RigorReport(
        expR_p5=boot.p5,
        expR_p50=boot.p50,
        expR_p95=boot.p95,
        bootstrap_block_size=boot.block_size,
        p_value_raw=boot.p_value_raw,
        p_value_bonferroni=p_adj,
        multi_test_count=n_trials,
        expR_net=net,
        friction_R_per_trade=fric,
        expR_half_1=sh.expR_half_1,
        expR_half_2=sh.expR_half_2,
        split_half_sign_stable=sh.sign_stable,
        sharpe_deflated=ds,
        passed_strict=passed,
        strict_fail_reasons=tuple(fails),
    )
