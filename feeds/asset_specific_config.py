"""Asset-Specific Configuration — per-ticker sage schools, alpha sniper pairs, edge layers.

No more one-size-fits-all. Each asset gets exactly the tools that matter for its market.

Equity Index (MNQ/NQ/MES/M2K/MYM):
  Technical analysis focus: order flow, market profile, VWAP, volume profile
  Key intermarket: ES (S&P correlation), VIX (volatility regime), YM (Dow)
  Edge: RTH session gate essential (09:30-16:00 ET)

Commodities (GC/CL/NG):
  Macro + supply/demand: trend following, volatility regime, risk management
  Key intermarket: DXY (dollar), TNX (yields), correlated commodities
  Edge: Wide stops (macro swings), vol sizing critical

FX/Rates (6E/ZN):
  Macro + carry: support_resistance, trend_following, risk_management
  Key intermarket: DXY, yield spreads, other FX pairs
  Edge: Tighter stops (range-bound), session gate for London/NY overlap

Crypto (BTC/ETH/SOL):
  24/7 markets: wyckoff, smc_ict, trend_following, vpa
  Key intermarket: DXY, SPX, on-chain metrics
  Edge: No session gate (24/7), crypto vol sizing
"""

# Sage schools available (the 22) — we assign subsets per asset
_EQUITY_SCHOOLS = frozenset({
    "dow_theory", "wyckoff", "trend_following", "vpa",
    "market_profile", "smc_ict", "order_flow", "support_resistance",
    "volatility_regime", "risk_management",
})

_COMMODITY_SCHOOLS = frozenset({
    "trend_following", "volatility_regime", "risk_management",
    "support_resistance", "dow_theory", "wyckoff",
    "vpa", "market_profile",
})

_FX_SCHOOLS = frozenset({
    "support_resistance", "trend_following", "risk_management",
    "dow_theory", "volatility_regime", "order_flow",
})

_CRYPTO_SCHOOLS = frozenset({
    "wyckoff", "smc_ict", "trend_following", "vpa",
    "order_flow", "support_resistance", "volatility_regime",
    "risk_management",
})

# Alpha sniper intermarket pairs per asset
_INTERMARKET_PAIRS = {
    "MNQ": ["ES", "NQ", "VIX"],
    "NQ":  ["ES", "MNQ", "VIX"],
    "MES": ["ES", "MNQ", "VIX"],
    "M2K": ["ES", "YM", "VIX"],
    "MYM": ["ES", "M2K", "VIX"],
    "GC":  ["SI", "DXY", "TNX"],
    "CL":  ["DXY", "RB", "NG"],
    "NG":  ["CL", "DXY"],
    "6E":  ["DXY", "ZN", "GBP"],
    "ZN":  ["6E", "DXY", "ZB"],
    "BTC": ["DXY", "SPX", "ETH"],
    "ETH": ["BTC", "DXY", "SPX"],
    "SOL": ["BTC", "ETH", "SPX"],
    "MBT": ["BTC", "DXY", "SPX"],
    "MET": ["ETH", "BTC", "SPX"],
}

# Edge layer presets per asset class
_EDGE_PRESETS = {
    "equity": {
        "enable_session_gate": True,
        "is_crypto": False,
        "strategy_mode": "trend",
        "enable_structural_stops": True,
        "structural_lookback": 10,
        "structural_buffer_mult": 0.25,
        "enable_vol_sizing": True,
        "vol_regime_lookback": 78,
        "enable_exhaustion_gate": False,
        "enable_absorption_gate": False,
        "enable_drift_boost": False,
    },
    "commodity": {
        "enable_session_gate": False,
        "is_crypto": False,
        "strategy_mode": "both",
        "enable_structural_stops": True,
        "structural_lookback": 14,
        "structural_buffer_mult": 0.35,
        "enable_vol_sizing": True,
        "vol_regime_lookback": 100,
        "enable_exhaustion_gate": False,
        "enable_absorption_gate": True,
        "absorption_vol_z_min": 1.2,
        "absorption_range_z_max": 0.5,
        "enable_drift_boost": False,
    },
    "fx": {
        "enable_session_gate": False,
        "is_crypto": False,
        "strategy_mode": "both",
        "enable_structural_stops": True,
        "structural_lookback": 20,
        "structural_buffer_mult": 0.20,
        "enable_vol_sizing": True,
        "vol_regime_lookback": 100,
        "enable_exhaustion_gate": False,
        "enable_absorption_gate": False,
        "enable_drift_boost": False,
    },
    "crypto": {
        "enable_session_gate": False,
        "is_crypto": True,
        "strategy_mode": "both",
        "enable_structural_stops": True,
        "structural_lookback": 24,
        "structural_buffer_mult": 0.50,
        "enable_vol_sizing": True,
        "vol_regime_lookback": 168,
        "enable_exhaustion_gate": False,
        "enable_absorption_gate": False,
        "enable_drift_boost": False,
    },
}

# Asset class mapping (ticker root → class)
_ASSET_CLASS = {
    "MNQ": "equity", "NQ": "equity", "MES": "equity",
    "M2K": "equity", "MYM": "equity", "YM": "equity",
    "GC": "commodity", "MGC": "commodity", "CL": "commodity",
    "MCL": "commodity", "NG": "commodity",
    "6E": "fx", "EUR": "fx", "ZN": "fx",
    "BTC": "crypto", "ETH": "crypto", "SOL": "crypto",
    "MBT": "crypto", "MET": "crypto",
}


def get_schools_for_symbol(symbol: str) -> frozenset:
    """Return the right sage schools for this symbol."""
    sym = symbol.upper().rstrip("1")
    cls = _ASSET_CLASS.get(sym, "equity")
    return {
        "equity": _EQUITY_SCHOOLS,
        "commodity": _COMMODITY_SCHOOLS,
        "fx": _FX_SCHOOLS,
        "crypto": _CRYPTO_SCHOOLS,
    }.get(cls, _EQUITY_SCHOOLS)


def get_intermarket_for_symbol(symbol: str) -> list[str]:
    """Return the right intermarket tickers for alpha sniper."""
    sym = symbol.upper().rstrip("1")
    return _INTERMARKET_PAIRS.get(sym, [])


def get_edge_preset_for_symbol(symbol: str) -> dict:
    """Return the right edge layer config for this symbol."""
    sym = symbol.upper().rstrip("1")
    cls = _ASSET_CLASS.get(sym, "equity")
    return dict(_EDGE_PRESETS[cls])


def get_asset_class(symbol: str) -> str:
    """Return the asset class for a symbol."""
    sym = symbol.upper().rstrip("1")
    return _ASSET_CLASS.get(sym, "equity")
