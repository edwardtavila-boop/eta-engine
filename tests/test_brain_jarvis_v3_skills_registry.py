from __future__ import annotations

from eta_engine.brain.jarvis_v3.skills_registry import (
    SkillDescriptor,
    SkillRegistry,
    SkillTier,
    default_registry,
)


def test_default_skills_registry_scopes_status_research_and_high_risk_skills() -> None:
    registry = default_registry()

    assert registry.get("bot-status").tier is SkillTier.LOW  # type: ignore[union-attr]
    assert registry.get("firm:the-firm").tier is SkillTier.HIGH  # type: ignore[union-attr]
    assert registry.can_invoke("bot-status", "watchdog.autopilot").allowed is True
    assert registry.can_invoke("firm:the-firm", "bot.mnq").allowed is False


def test_skill_registry_wildcard_and_prefix_allowlists() -> None:
    registry = SkillRegistry()
    registry.register(
        SkillDescriptor(
            name="diagnostics",
            tier=SkillTier.LOW,
            allowed_subsystems=["bot.*", "operator.edward"],
        )
    )

    assert registry.can_invoke("diagnostics", "bot.mnq").allowed is True
    assert registry.can_invoke("diagnostics", "bot").allowed is True
    denied = registry.can_invoke("diagnostics", "firm.pm")
    assert denied.allowed is False
    assert "not in allowlist" in denied.reason


def test_skill_registry_save_load_and_missing_default(tmp_path) -> None:
    path = tmp_path / "skills.json"
    registry = SkillRegistry()
    registry.register(SkillDescriptor(name="safe-view", tier=SkillTier.LOW))
    registry.save(path)

    loaded = SkillRegistry.load(path)
    missing_default = SkillRegistry.load(tmp_path / "missing.json")

    assert loaded.names() == ["safe-view"]
    assert loaded.by_tier(SkillTier.LOW)[0].name == "safe-view"
    assert "bot-status" in missing_default.names()
