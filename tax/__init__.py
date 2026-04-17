"""
EVOLUTIONARY TRADING ALGO  //  tax
======================
Tax event tracking, cost-basis lot matching, Koinly export, Section 1256 reporting.
"""

from eta_engine.tax.cost_basis import CostBasisCalculator, Lot
from eta_engine.tax.koinly_exporter import KOINLY_LABEL_VOCAB, KoinlyExporter
from eta_engine.tax.models import (
    AccountTier,
    EventType,
    InstrumentType,
    TaxableEvent,
    TaxReport,
)
from eta_engine.tax.section_1256_reporter import (
    OpenFuturesPosition,
    Section1256Reporter,
)

__all__ = [
    "KOINLY_LABEL_VOCAB",
    "AccountTier",
    "CostBasisCalculator",
    "EventType",
    "InstrumentType",
    "KoinlyExporter",
    "Lot",
    "OpenFuturesPosition",
    "Section1256Reporter",
    "TaxReport",
    "TaxableEvent",
]
