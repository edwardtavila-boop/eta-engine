"""
EVOLUTIONARY TRADING ALGO  //  data
=======================
Market data ingestion, cache catalog, cleaning, slippage, on-chain + social.
"""

from eta_engine.data.bybit_ws import BybitWSCapture
from eta_engine.data.cleaning import (
    detect_duplicates,
    detect_gaps,
    fill_gaps,
    remove_outliers_mad,
    validate_bar,
)
from eta_engine.data.databento_client import DataBentoClient
from eta_engine.data.models import (
    DataIntegrityReport,
    DatasetManifest,
    DatasetRef,
    DataSource,
)
from eta_engine.data.onchain_blockscout import BlockscoutClient
from eta_engine.data.parquet_loader import ParquetLoader
from eta_engine.data.sentiment_lunarcrush import LunarCrushClient
from eta_engine.data.slippage_model import SlippageModel

__all__ = [
    "BlockscoutClient",
    "BybitWSCapture",
    "DataBentoClient",
    "DataIntegrityReport",
    "DataSource",
    "DatasetManifest",
    "DatasetRef",
    "LunarCrushClient",
    "ParquetLoader",
    "SlippageModel",
    "detect_duplicates",
    "detect_gaps",
    "fill_gaps",
    "remove_outliers_mad",
    "validate_bar",
]
