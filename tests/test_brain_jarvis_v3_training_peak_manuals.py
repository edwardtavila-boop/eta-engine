from __future__ import annotations

import pytest

from eta_engine.brain.jarvis_v3.training.peak_manuals import (
    PEAK_MANUALS,
    manual_for,
    render_manual,
)


def test_peak_manuals_have_complete_operator_contracts() -> None:
    assert set(PEAK_MANUALS) == {"JARVIS", "BATMAN", "ALFRED", "ROBIN"}

    for manual in PEAK_MANUALS.values():
        assert manual.identity
        assert manual.strengths
        assert manual.anti_patterns
        assert manual.invocation


def test_manual_for_is_case_insensitive_and_rejects_unknown_persona() -> None:
    assert manual_for("alfred").persona == "ALFRED"

    with pytest.raises(KeyError, match="no peak manual"):
        manual_for("not-a-persona")


def test_render_manual_includes_signature_examples_and_boundaries() -> None:
    rendered = render_manual("BATMAN")

    assert rendered.startswith("=== BATMAN :: PEAK MANUAL")
    assert "OUTPUT SIGNATURE:" in rendered
    assert "PEAK EXAMPLES" in rendered
    assert "ANTI-PATTERNS" in rendered
    assert rendered.endswith("=" * 60)
