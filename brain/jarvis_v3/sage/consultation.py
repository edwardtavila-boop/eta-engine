"""JARVIS sage consultation entry point.

Builds the registry of every shipped school + provides ``consult_sage(ctx)``
which runs all schools and aggregates them into a SageReport.
"""
from __future__ import annotations

import logging

from eta_engine.brain.jarvis_v3.sage.base import (
    MarketContext,
    SageReport,
    SchoolBase,
    SchoolVerdict,
)
from eta_engine.brain.jarvis_v3.sage.confluence import aggregate
from eta_engine.brain.jarvis_v3.sage.schools.dow_theory import DowTheorySchool
from eta_engine.brain.jarvis_v3.sage.schools.elliott_wave import ElliottWaveSchool
from eta_engine.brain.jarvis_v3.sage.schools.fibonacci import FibonacciSchool
from eta_engine.brain.jarvis_v3.sage.schools.gann import GannSchool
from eta_engine.brain.jarvis_v3.sage.schools.market_profile import MarketProfileSchool
from eta_engine.brain.jarvis_v3.sage.schools.neowave import NEoWaveSchool
from eta_engine.brain.jarvis_v3.sage.schools.order_flow import OrderFlowSchool
from eta_engine.brain.jarvis_v3.sage.schools.risk_management import RiskManagementSchool
from eta_engine.brain.jarvis_v3.sage.schools.smc_ict import SmcIctSchool
from eta_engine.brain.jarvis_v3.sage.schools.support_resistance import SupportResistanceSchool
from eta_engine.brain.jarvis_v3.sage.schools.trend_following import TrendFollowingSchool
from eta_engine.brain.jarvis_v3.sage.schools.vpa import VPASchool
from eta_engine.brain.jarvis_v3.sage.schools.weis_wyckoff import WeisWyckoffSchool
from eta_engine.brain.jarvis_v3.sage.schools.wyckoff import WyckoffSchool

logger = logging.getLogger(__name__)


SCHOOLS: dict[str, SchoolBase] = {
    s.NAME: s for s in (
        DowTheorySchool(),
        WyckoffSchool(),
        ElliottWaveSchool(),
        FibonacciSchool(),
        GannSchool(),
        SupportResistanceSchool(),
        TrendFollowingSchool(),
        VPASchool(),
        MarketProfileSchool(),
        SmcIctSchool(),
        OrderFlowSchool(),
        RiskManagementSchool(),
        NEoWaveSchool(),
        WeisWyckoffSchool(),
    )
}


def consult_sage(
    ctx: MarketContext,
    *,
    enabled: set[str] | None = None,
) -> SageReport:
    """Consult every (or a filtered subset) of the schools.

    ``enabled``: if not None, only these school NAMEs are consulted.
    Useful for unit tests + ablation studies.
    """
    verdicts: dict[str, SchoolVerdict] = {}
    for name, school in SCHOOLS.items():
        if enabled is not None and name not in enabled:
            continue
        try:
            v = school.analyze(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("school %s raised: %s", name, exc)
            continue
        verdicts[name] = v
    return aggregate(verdicts, SCHOOLS, entry_side=ctx.side)
