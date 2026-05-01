"""AVENGERS CONSOLE — Streamlit control panel for the four AIs.

Four personas, one fleet, one dashboard:
* JARVIS  — deterministic admin (no LLM). Shows recent audit entries
* BATMAN  — Opus-tier adversarial persona
* ALFRED  — Sonnet-tier default persona
* ROBIN   — Haiku-tier grunt persona
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st

_PERSONA_BADGE: dict[str, dict[str, str]] = {
    "JARVIS": {"role": "Admin / Policy Engine", "tier": "deterministic", "color": "#9aa0a6", "tag": "admin"},
    "BATMAN": {"role": "Adversarial Auditor", "tier": "Opus 4.6", "color": "#e8714a", "tag": "adversarial"},
    "ALFRED": {"role": "Default Operator", "tier": "Sonnet 4.6", "color": "#4da6ff", "tag": "routine"},
    "ROBIN": {"role": "Fast Grunt", "tier": "Haiku 4.5", "color": "#80e27e", "tag": "grunt"},
}

AVENGERS_JOURNAL = Path.home() / ".jarvis" / "avengers_journal.jsonl"
COST_RATIO = {"opus": 15.0, "sonnet": 3.0, "haiku": 1.0}


def _resolve_persona_from_args() -> str:
    """Precedence: --persona CLI flag > ?persona= query param > JARVIS."""
    for i, arg in enumerate(sys.argv):
        if arg == "--persona" and i + 1 < len(sys.argv):
            return sys.argv[i + 1].lower()
    try:
        qp = st.query_params.get("persona")
        if isinstance(qp, str) and qp.strip():
            return qp.strip().lower()
    except Exception:
        pass
    return "jarvis"


def main() -> None:
    st.set_page_config(page_title="Avengers Console", layout="wide")
    persona_key = _resolve_persona_from_args()
    badge = _PERSONA_BADGE.get(persona_key.upper(), _PERSONA_BADGE["JARVIS"])

    st.markdown(
        f"<h1>Avengers Console <span style='color:{badge['color']}'>({badge['tag']})</span></h1>",
        unsafe_allow_html=True,
    )
    st.caption(f"Persona: **{persona_key.upper()}** — {badge['role']} ({badge['tier']})")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Recent Audit Feed")
        if AVENGERS_JOURNAL.exists():
            lines = AVENGERS_JOURNAL.read_text().strip().split("\n")
            for line in lines[-20:]:
                st.code(line[:200], language="json")
        else:
            st.info("No journal entries yet.")

    with col2:
        st.subheader("Fleet Status")
        for pid, info in _PERSONA_BADGE.items():
            st.markdown(
                f"- **{pid}** ({info['role']}) — {info['tier']}",
                unsafe_allow_html=True,
            )

    st.divider()
    st.caption(f"Refreshed {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")


if __name__ == "__main__":
    main()
