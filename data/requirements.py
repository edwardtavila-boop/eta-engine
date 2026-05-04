"""
EVOLUTIONARY TRADING ALGO  //  data.requirements
=================================================
Per-bot data requirements — the canonical "what this bot NEEDS to
run honest research" registry.

Why this exists
---------------
The ``data.library`` catalogs what's available locally. The
strategy registry assigns a bot to a (symbol, timeframe). But
neither answers the question that actually matters for optimization:

   "What data does this bot need that we don't have yet?"

For futures bots (MNQ, NQ, MBT, MET) that's mostly self-contained — bar data
+ ES1 correlation + DXY + VIX is enough. For crypto bots (BTC, ETH,
XRP, SOL) the answer is much bigger: native bars per exchange,
funding rates per symbol per exchange, on-chain metrics (whale
transfers, exchange netflow, active addresses), sentiment
(LunarCrush galaxy_score / fear_greed), cross-asset correlation.

This module is the registry of requirements. The audit function in
``data.audit`` cross-references it with the library to produce a
coverage report — "BTC bot is missing 1h bars + funding + onchain."

When a new feed gets added (e.g. you start writing a new CSV under the
workspace ``data/crypto/history`` root), add a matching
``DataRequirement`` here and the audit will flip from MISSING to
AVAILABLE on the next ``announce_data_library`` run.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DataRequirement:
    """One piece of data a bot needs to operate.

    ``kind`` is intentionally an open string so future categories
    (e.g. "options_chain", "macro_calendar") can be added without
    changing the data class. ``critical`` is True iff the bot can't
    produce honest signals without this data; non-critical
    requirements are nice-to-haves.
    """

    kind: str  # "bars" | "funding" | "onchain" | "sentiment" | "correlation" | "macro"
    symbol: str  # e.g. "BTC", "ETH", "ES1"; for macro use "DXY", "VIX", etc.
    timeframe: str | None  # e.g. "1m", "1h", "D"; None for non-bar data
    critical: bool = True
    note: str = ""


@dataclass(frozen=True)
class BotRequirements:
    """All requirements for a single bot."""

    bot_id: str
    requirements: tuple[DataRequirement, ...]
    sources_hint: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Per-bot requirements
# ---------------------------------------------------------------------------


REQUIREMENTS: tuple[BotRequirements, ...] = (
    # ── Futures bots ──
    BotRequirements(
        bot_id="mnq_futures",
        requirements=(
            DataRequirement("bars", "MNQ1", "5m", critical=True),
            DataRequirement("bars", "MNQ1", "1h", critical=True),
            DataRequirement("bars", "MNQ1", "4h", critical=False,
                note="best DSR-pass timeframe per 2026-04-27 grid"),
            DataRequirement("correlation", "ES1", "5m", critical=True,
                note="ES correlation is a primary MNQ price driver"),
            DataRequirement("correlation", "DXY", "5m", critical=False,
                note="dollar-index regime context"),
            DataRequirement("correlation", "VIX", "5m", critical=False,
                note="volatility regime"),
        ),
        sources_hint=(
            "tradingview-mcp",
            "scripts/dual_data_collector.py",
        ),
    ),
    BotRequirements(
        bot_id="nq_futures",
        requirements=(
            DataRequirement("bars", "NQ1", "1h", critical=True),
            DataRequirement("bars", "NQ1", "4h", critical=True),
            DataRequirement("bars", "NQ1", "D", critical=True,
                note="27-yr daily history is the strongest available"),
            DataRequirement("correlation", "ES1", "5m", critical=True),
            DataRequirement("correlation", "VIX", "1m", critical=False),
        ),
        sources_hint=("tradingview-mcp",),
    ),
    # mnq_futures_sage is a sage-overlay variant of mnq_futures. Same
    # underlying bot directory + same data needs as plain MNQ ORB; the
    # only difference is the sage-consensus gate at decision time, which
    # consumes the same bar/correlation streams. Listed separately so
    # the registry-sync audit treats it as its own promotable subject.
    BotRequirements(
        bot_id="mnq_futures_sage",
        requirements=(
            DataRequirement("bars", "MNQ1", "5m", critical=True),
            DataRequirement("bars", "MNQ1", "1h", critical=True),
            DataRequirement("correlation", "ES1", "5m", critical=True,
                note="ES correlation feeds the sage's institutional school"),
            DataRequirement("correlation", "DXY", "5m", critical=False),
            DataRequirement("correlation", "VIX", "5m", critical=False),
        ),
        sources_hint=(
            "tradingview-mcp",
            "scripts/dual_data_collector.py",
        ),
    ),
    # mnq_sage_consensus is a multi-school sage-consensus MNQ variant.
    # Shares bar/correlation needs with mnq_futures_sage; the consensus
    # layer reads the same streams.
    BotRequirements(
        bot_id="mnq_sage_consensus",
        requirements=(
            DataRequirement("bars", "MNQ1", "5m", critical=True),
            DataRequirement("bars", "MNQ1", "1h", critical=True),
            DataRequirement("correlation", "ES1", "5m", critical=True,
                note="ES correlation feeds the sage's institutional school"),
            DataRequirement("correlation", "DXY", "5m", critical=False),
            DataRequirement("correlation", "VIX", "5m", critical=False),
        ),
        sources_hint=(
            "tradingview-mcp",
            "scripts/dual_data_collector.py",
        ),
    ),
    # nq_futures_sage mirrors mnq_futures_sage but on NQ — sage-overlay
    # variant of plain ORB. Same data needs as nq_futures; only the
    # decision-time gate differs.
    BotRequirements(
        bot_id="nq_futures_sage",
        requirements=(
            DataRequirement("bars", "NQ1", "5m", critical=True),
            DataRequirement("bars", "NQ1", "1h", critical=True),
            DataRequirement("correlation", "ES1", "5m", critical=True,
                note="ES correlation feeds the sage's institutional school"),
            DataRequirement("correlation", "VIX", "5m", critical=False),
        ),
        sources_hint=("tradingview-mcp",),
    ),
    # nq_daily_drb is a daily-timeframe DRB variant of nq_futures. Same
    # underlying bot directory, different strategy_kind in the per_bot
    # registry. Listed here as a separate bot_id so the audit + sync
    # tests treat it as its own promotable subject.
    BotRequirements(
        bot_id="nq_daily_drb",
        requirements=(
            DataRequirement("bars", "NQ1", "D", critical=True,
                note="DRB walk-forward needs the 27-yr daily history"),
            DataRequirement("correlation", "ES1", "5m", critical=False),
        ),
        sources_hint=("tradingview-mcp",),
    ),
    # ── Crypto bots ── (placeholder symbols until real feeds wired)
    BotRequirements(
        bot_id="btc_hybrid",
        requirements=(
            DataRequirement("bars", "BTC", "1m", critical=False,
                note="entry timing; optional — 5m/1h/D enough for paper"),
            DataRequirement("bars", "BTC", "5m", critical=True),
            DataRequirement("bars", "BTC", "1h", critical=True),
            DataRequirement("bars", "BTC", "D", critical=True,
                note="regime + macro lens"),
            DataRequirement("funding", "BTC", "8h", critical=False,
                note="funding skew is the dominant edge for BTC perps; "
                "optional for paper"),
            DataRequirement("onchain", "BTC", None, critical=False,
                note="whale transfers, exchange netflow, active addresses; "
                "optional — bar data sufficient for paper"),
            DataRequirement("sentiment", "BTC", "1h", critical=False,
                note="LunarCrush galaxy_score / fear_greed"),
            DataRequirement("correlation", "ETH", "1h", critical=False),
            DataRequirement("correlation", "DXY", "1h", critical=False),
        ),
        sources_hint=(
            "scripts/btc_paper_lane.py (Coinbase/Binance bars)",
            "scripts/btc_broker_fleet.py",
            "scripts/dual_data_collector.py",
            "blockscout MCP (on-chain)",
            "data/sentiment_lunarcrush.py (sentiment)",
        ),
    ),
    # btc_hybrid_sage is a sage-overlay variant of btc_hybrid. Same
    # underlying bot directory + same data needs as plain BTC crypto_orb;
    # only the decision-time sage-consensus gate differs.
    BotRequirements(
        bot_id="btc_hybrid_sage",
        requirements=(
            DataRequirement("bars", "BTC", "1h", critical=True),
            DataRequirement("bars", "BTC", "D", critical=True,
                note="regime + macro lens"),
            DataRequirement("funding", "BTC", "8h", critical=False,
                note="sage panel reads funding skew when available"),
        ),
        sources_hint=(
            "scripts/fetch_btc_bars.py (Coinbase spot bars)",
            "scripts/fetch_funding_rates.py (OKX funding)",
        ),
    ),
    # btc_regime_trend is a regime-classifier variant of btc_hybrid
    # that only fires in trending regimes (the inverse of crypto_seed's
    # 2026-04-27 grid-safe gate). Same data needs as plain BTC crypto_orb.
    BotRequirements(
        bot_id="btc_regime_trend",
        requirements=(
            DataRequirement("bars", "BTC", "1h", critical=True),
            DataRequirement("bars", "BTC", "D", critical=True,
                note="regime classifier baseline"),
            DataRequirement("correlation", "ETH", "1h", critical=False,
                note="ETH-BTC correlation as regime confirmation"),
        ),
        sources_hint=("scripts/fetch_btc_bars.py (Coinbase spot bars)",),
    ),
    # btc_sage_daily_etf and btc_regime_trend_etf are ETF-targeted
    # variants (IBIT / BITB style execution) of the parent strategies.
    # Same upstream BTC bar/correlation needs; the ETF dimension only
    # affects venue routing at execution time.
    BotRequirements(
        bot_id="btc_sage_daily_etf",
        requirements=(
            DataRequirement("bars", "BTC", "1h", critical=True),
            DataRequirement("bars", "BTC", "D", critical=True,
                note="daily timeframe is the primary signal frame"),
            DataRequirement("funding", "BTC", "8h", critical=False,
                note="sage panel reads funding skew when available"),
        ),
        sources_hint=(
            "scripts/fetch_btc_bars.py (Coinbase spot bars)",
            "scripts/fetch_funding_rates.py (OKX funding)",
        ),
    ),
    BotRequirements(
        bot_id="btc_regime_trend_etf",
        requirements=(
            DataRequirement("bars", "BTC", "1h", critical=True),
            DataRequirement("bars", "BTC", "D", critical=True,
                note="regime classifier baseline"),
            DataRequirement("correlation", "ETH", "1h", critical=False,
                note="ETH-BTC correlation as regime confirmation"),
        ),
        sources_hint=("scripts/fetch_btc_bars.py (Coinbase spot bars)",),
    ),
    # btc_ensemble_2of3 is a vote-ensemble across regime_trend +
    # regime_trend+ETF + sage-daily-gated. Same bar/ETF data needs as
    # the components it votes across; only the decision-time vote logic
    # differs.
    BotRequirements(
        bot_id="btc_ensemble_2of3",
        requirements=(
            DataRequirement("bars", "BTC", "1h", critical=True),
            DataRequirement("bars", "BTC", "D", critical=True,
                note="regime classifier baseline + sage daily cadence"),
            DataRequirement("correlation", "ETH", "1h", critical=False,
                note="ETH-BTC correlation as regime confirmation"),
        ),
        sources_hint=(
            "scripts/fetch_btc_bars.py (Coinbase spot bars)",
            "Farside ETF flow feed (per ensemble component)",
        ),
    ),
    BotRequirements(
        bot_id="eth_perp",
        requirements=(
            DataRequirement("bars", "ETH", "5m", critical=True),
            DataRequirement("bars", "ETH", "1h", critical=True),
            DataRequirement("bars", "ETH", "D", critical=True),
            DataRequirement("funding", "ETH", "8h", critical=False,
                note="sage panel reads funding skew when available"),
            DataRequirement("onchain", "ETH", None, critical=False,
                note="whale transfers + gas-fee regime + staking yield; optional for paper"),
            DataRequirement("sentiment", "ETH", "1h", critical=False),
            DataRequirement("correlation", "BTC", "1h", critical=True,
                note="ETH-BTC correlation is a primary regime indicator"),
        ),
        sources_hint=(
            "Coinbase/Binance ETH bars + funding",
            "blockscout MCP (on-chain)",
            "lunarcrush MCP (sentiment)",
        ),
    ),
    # eth_sage_daily is the daily-frame sage variant of eth_perp.
    # Same upstream ETH bar/funding/correlation needs as eth_perp;
    # only the decision-frame and sage-consensus gate differ.
    BotRequirements(
        bot_id="eth_sage_daily",
        requirements=(
            DataRequirement("bars", "ETH", "1h", critical=True),
            DataRequirement("bars", "ETH", "D", critical=True,
                note="daily timeframe is the primary signal frame"),
            DataRequirement("funding", "ETH", "8h", critical=False,
                note="sage panel reads funding skew when available"),
            DataRequirement("correlation", "BTC", "1h", critical=True,
                note="ETH-BTC correlation as regime confirmation"),
        ),
        sources_hint=(
            "Coinbase/Binance ETH bars + funding",
        ),
    ),
    # eth_compression is the compression-breakout variant of eth_perp.
    # Same ETH 1h bars as eth_perp; needs ATR + BB-width (computed
    # internally from price). PROMOTED 2026-04-27 as the cleanest
    # gate-passer of the foundation supercharge sweep.
    BotRequirements(
        bot_id="crypto_seed",
        requirements=(
            DataRequirement("bars", "BTC", "1h", critical=True),
            DataRequirement("bars", "BTC", "4h", critical=True,
                note="grid-level sizing decision interval"),
            DataRequirement("bars", "BTC", "D", critical=True,
                note="regime + macro lens"),
            DataRequirement("correlation", "ETH", "1h", critical=False,
                note="ETH-BTC correlation for dual regime"),
        ),
        sources_hint=(
            "scripts/fetch_btc_bars.py (Coinbase spot bars)",
            "CryptoSeed — grid+DCA overlay bot (BTCUSDT on Binance/Tastytrade)",
        ),
    ),
    BotRequirements(
        bot_id="sol_perp",
        requirements=(
            DataRequirement("bars", "SOL", "1h", critical=True),
            DataRequirement("bars", "SOL", "D", critical=True,
                note="regime + macro lens"),
            DataRequirement("correlation", "BTC", "1h", critical=False,
                note="SOL-BTC correlation for regime"),
        ),
        sources_hint=(
            "Coinbase/Binance SOL bars",
            "SolPerp — SOL perpetual (SOLUSDT on Binance/Tastytrade)",
        ),
    ),
    BotRequirements(
        bot_id="xrp_perp",
        requirements=(),
        sources_hint=("DEACTIVATED — no news/regulatory feed for XRP.",),
    ),

    # ── MBT/MET — CME micro crypto futures ─────────────────────────
    # MBT (Micro Bitcoin) / MET (Micro Ether) track BTCUSDT/ETHUSDT
    # through the M2 translation layer. Data needs mirror the proven
    # sweep_reclaim BTC/ETH strategies but on CME session hours.
    BotRequirements(
        bot_id="mbt_sweep_reclaim",
        requirements=(
            DataRequirement("bars", "MBT", "5m", critical=True),
            DataRequirement("bars", "MBT", "1h", critical=True),
            DataRequirement("bars", "MBT", "D", critical=True,
                note="regime + macro lens"),
            DataRequirement("correlation", "ETH", "1h", critical=False,
                note="ETH-BTC correlation as regime confirmation"),
            DataRequirement("correlation", "DXY", "1h", critical=False),
        ),
        sources_hint=("IBKR CME bars (via Client Portal Gateway)", "scripts/fetch_ibkr_crypto_bars.py"),
    ),
    BotRequirements(
        bot_id="met_sweep_reclaim",
        requirements=(
            DataRequirement("bars", "MET", "5m", critical=True),
            DataRequirement("bars", "MET", "1h", critical=True),
            DataRequirement("bars", "MET", "D", critical=True,
                note="regime + macro lens"),
            DataRequirement("correlation", "BTC", "1h", critical=False,
                note="BTC-ETH correlation as regime confirmation"),
            DataRequirement("correlation", "DXY", "1h", critical=False),
        ),
        sources_hint=("IBKR CME bars (via Client Portal Gateway)", "scripts/fetch_ibkr_crypto_bars.py"),
    ),

    # ── Wave-18 fleet additions ────────────────────────────────────
    # Stub requirements added 2026-05-03 to clear the registry/requirements
    # sync drift discovered during the live VPS paper-live cutover. Each
    # entry covers the bot's own symbol bars at the timeframes implied by
    # its strategy_kind in strategies/per_bot_registry.py. These are the
    # minimum critical bars needed for honest signals; enrichment (funding,
    # onchain, sentiment, correlations) can be promoted from optional to
    # critical bot-by-bot as research validates the dependency.

    # confluence_scorecard variants on BTC — sweep_reclaim sub-strategy
    BotRequirements(
        bot_id="btc_optimized",
        requirements=(
            DataRequirement("bars", "BTC", "5m", critical=True),
            DataRequirement("bars", "BTC", "1h", critical=True),
            DataRequirement("bars", "BTC", "D", critical=True,
                note="regime + macro lens"),
        ),
        sources_hint=("scripts/fetch_btc_bars.py (Coinbase spot bars)",),
    ),

    # MNQ confluence_scorecard sweep_reclaim sub-strategy
    BotRequirements(
        bot_id="mnq_sweep_reclaim",
        requirements=(
            DataRequirement("bars", "MNQ1", "5m", critical=True),
            DataRequirement("bars", "MNQ1", "1h", critical=True),
            DataRequirement("correlation", "ES1", "5m", critical=False,
                note="ES correlation is a primary MNQ regime driver"),
        ),
        sources_hint=("scripts/fetch_index_futures_bars.py",),
    ),

    # RSI mean-reversion variants
    BotRequirements(
        bot_id="rsi_mr_mnq",
        requirements=(
            DataRequirement("bars", "MNQ1", "5m", critical=True),
            DataRequirement("bars", "MNQ1", "1h", critical=True),
        ),
        sources_hint=("scripts/fetch_index_futures_bars.py",),
    ),
    BotRequirements(
        bot_id="rsi_mr_btc",
        requirements=(
            DataRequirement("bars", "BTC", "5m", critical=True),
            DataRequirement("bars", "BTC", "1h", critical=True),
        ),
        sources_hint=("scripts/fetch_btc_bars.py (Coinbase spot bars)",),
    ),

    # VWAP mean-reversion variants
    BotRequirements(
        bot_id="vwap_mr_mnq",
        requirements=(
            DataRequirement("bars", "MNQ1", "5m", critical=True),
            DataRequirement("bars", "MNQ1", "1h", critical=True),
        ),
        sources_hint=("scripts/fetch_index_futures_bars.py",),
    ),
    BotRequirements(
        bot_id="vwap_mr_btc",
        requirements=(
            DataRequirement("bars", "BTC", "5m", critical=True),
            DataRequirement("bars", "BTC", "1h", critical=True),
        ),
        sources_hint=("scripts/fetch_btc_bars.py (Coinbase spot bars)",),
    ),

    BotRequirements(
        bot_id="vwap_mr_nq",
        requirements=(
            DataRequirement("bars", "NQ1", "5m", critical=True),
            DataRequirement("bars", "NQ1", "1h", critical=True),
        ),
        sources_hint=("scripts/fetch_index_futures_bars.py",),
    ),

    # Volume-profile (POC/value-area) — needs daily for context
    BotRequirements(
        bot_id="volume_profile_mnq",
        requirements=(
            DataRequirement("bars", "MNQ1", "5m", critical=True),
            DataRequirement("bars", "MNQ1", "1h", critical=True),
            DataRequirement("bars", "MNQ1", "D", critical=True,
                note="daily POC anchors session value-area"),
        ),
        sources_hint=("scripts/fetch_index_futures_bars.py",),
    ),
    BotRequirements(
        bot_id="volume_profile_btc",
        requirements=(
            DataRequirement("bars", "BTC", "5m", critical=True),
            DataRequirement("bars", "BTC", "1h", critical=True),
            DataRequirement("bars", "BTC", "D", critical=True,
                note="daily POC anchors session value-area"),
        ),
        sources_hint=("scripts/fetch_btc_bars.py (Coinbase spot bars)",),
    ),

    # Gap-fill — needs daily to detect overnight gap
    BotRequirements(
        bot_id="gap_fill_mnq",
        requirements=(
            DataRequirement("bars", "MNQ1", "5m", critical=True),
            DataRequirement("bars", "MNQ1", "1h", critical=True),
            DataRequirement("bars", "MNQ1", "D", critical=True,
                note="daily prev-close anchors gap detection"),
        ),
        sources_hint=("scripts/fetch_index_futures_bars.py",),
    ),
    BotRequirements(
        bot_id="gap_fill_btc",
        requirements=(
            DataRequirement("bars", "BTC", "5m", critical=True),
            DataRequirement("bars", "BTC", "1h", critical=True),
            DataRequirement("bars", "BTC", "D", critical=True,
                note="daily prev-close anchors gap detection"),
        ),
        sources_hint=("scripts/fetch_btc_bars.py (Coinbase spot bars)",),
    ),

    # Cross-asset (correlation-driven entries)
    BotRequirements(
        bot_id="cross_asset_mnq",
        requirements=(
            DataRequirement("bars", "MNQ1", "5m", critical=True),
            DataRequirement("bars", "MNQ1", "1h", critical=True),
            DataRequirement("correlation", "ES1", "5m", critical=True,
                note="ES is the primary cross-asset reference for MNQ"),
            DataRequirement("correlation", "DXY", "5m", critical=False),
            DataRequirement("correlation", "VIX", "5m", critical=False),
        ),
        sources_hint=("scripts/fetch_index_futures_bars.py",
                      "scripts/fetch_market_context_bars.py"),
    ),
    BotRequirements(
        bot_id="cross_asset_btc",
        requirements=(
            DataRequirement("bars", "BTC", "5m", critical=True),
            DataRequirement("bars", "BTC", "1h", critical=True),
            DataRequirement("correlation", "ETH", "1h", critical=True,
                note="ETH-BTC is the primary intra-crypto cross-asset signal"),
            DataRequirement("correlation", "DXY", "1h", critical=False),
        ),
        sources_hint=("scripts/fetch_btc_bars.py (Coinbase spot bars)",
                      "scripts/fetch_market_context_bars.py"),
    ),

    # Funding-rate skew bot — funding is the actual edge
    BotRequirements(
        bot_id="funding_rate_btc",
        requirements=(
            DataRequirement("bars", "BTC", "1h", critical=True),
            DataRequirement("bars", "BTC", "D", critical=True,
                note="daily timeframe is the primary signal frame"),
            DataRequirement("funding", "BTC", "8h", critical=True,
                note="funding skew IS the edge; non-optional for this bot"),
        ),
        sources_hint=("scripts/fetch_btc_bars.py (Coinbase spot bars)",
                      "scripts/fetch_funding_rates.py (OKX/Binance funding)"),
    ),

    # ETH compression-breakout
    BotRequirements(
        bot_id="eth_compression",
        requirements=(
            DataRequirement("bars", "ETH", "5m", critical=True),
            DataRequirement("bars", "ETH", "1h", critical=True),
            DataRequirement("correlation", "BTC", "1h", critical=False,
                note="ETH-BTC correlation as regime confirmation"),
        ),
        sources_hint=("Coinbase ETH bars (scripts/fetch_crypto_bars_coinbase.py)",),
    ),

    # MNQ optimized — full-stack OHLCV + ES correlation
    BotRequirements(
        bot_id="mnq_futures_optimized",
        requirements=(
            DataRequirement("bars", "MNQ1", "5m", critical=True),
            DataRequirement("bars", "MNQ1", "1h", critical=True),
            DataRequirement("bars", "MNQ1", "D", critical=True,
                note="regime + walk-forward window"),
            DataRequirement("correlation", "ES1", "5m", critical=True,
                note="ES correlation is a primary MNQ price driver"),
            DataRequirement("correlation", "DXY", "5m", critical=False),
            DataRequirement("correlation", "VIX", "5m", critical=False),
        ),
        sources_hint=("scripts/fetch_index_futures_bars.py",
                      "scripts/fetch_market_context_bars.py"),
    ),

    # BTC crypto scalper — short-timeframe entry
    BotRequirements(
        bot_id="btc_crypto_scalp",
        requirements=(
            DataRequirement("bars", "BTC", "1m", critical=True,
                note="1m bars drive scalp entry timing"),
            DataRequirement("bars", "BTC", "5m", critical=True),
        ),
        sources_hint=("Coinbase 1m granularity (scripts/fetch_crypto_bars_coinbase.py)",),
    ),

    # SOL sweep scalp — pulls SOL bars at multiple timeframes
    BotRequirements(
        bot_id="sol_sweep_scalp",
        requirements=(
            DataRequirement("bars", "SOL", "5m", critical=True),
            DataRequirement("bars", "SOL", "1h", critical=True),
            DataRequirement("correlation", "BTC", "1h", critical=False,
                note="BTC-SOL correlation as regime confirmation"),
        ),
        sources_hint=("Coinbase SOL bars (scripts/fetch_crypto_bars_coinbase.py)",),
    ),

    # ETH sweep_reclaim — same shape as btc/sol sweep variants
    BotRequirements(
        bot_id="eth_sweep_reclaim",
        requirements=(
            DataRequirement("bars", "ETH", "5m", critical=True),
            DataRequirement("bars", "ETH", "1h", critical=True),
            DataRequirement("bars", "ETH", "D", critical=True,
                note="regime + macro lens"),
            DataRequirement("correlation", "BTC", "1h", critical=False,
                note="ETH-BTC correlation as regime confirmation"),
        ),
        sources_hint=("Coinbase ETH bars (scripts/fetch_crypto_bars_coinbase.py)",),
    ),
)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def get_requirements(bot_id: str) -> BotRequirements | None:
    for r in REQUIREMENTS:
        if r.bot_id == bot_id:
            return r
    return None


def all_requirements() -> list[BotRequirements]:
    return list(REQUIREMENTS)


def critical_requirements_for(bot_id: str) -> list[DataRequirement]:
    """Return only the ``critical=True`` requirements for a bot."""
    bot = get_requirements(bot_id)
    if bot is None:
        return []
    return [r for r in bot.requirements if r.critical]
