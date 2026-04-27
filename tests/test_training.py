"""
Tests for brain/jarvis_v3/training/* -- peak manuals, skills catalog,
MCP awareness, collaboration, eval harness, curriculum.
"""

from __future__ import annotations

import pytest

from eta_engine.brain.jarvis_v3.training import (
    collaboration,
    curriculum,
    eval_harness,
    mcp_awareness,
    peak_manuals,
    skills_catalog,
)

# ---------------------------------------------------------------------------
# peak_manuals
# ---------------------------------------------------------------------------


class TestPeakManuals:
    def test_all_four_personas_present(self):
        for p in ("JARVIS", "BATMAN", "ALFRED", "ROBIN"):
            assert p in peak_manuals.PEAK_MANUALS
            m = peak_manuals.manual_for(p)
            assert m.persona == p
            assert len(m.identity) > 20
            assert len(m.strengths) >= 3

    def test_batman_has_adversarial_doctrine(self):
        m = peak_manuals.manual_for("BATMAN")
        assert "ADVERSARIAL_HONESTY" in m.doctrine

    def test_robin_anti_patterns_forbid_padding(self):
        m = peak_manuals.manual_for("ROBIN")
        combined = " ".join(m.anti_patterns).lower()
        assert "here is" in combined or "pad" in combined

    def test_render_manual_has_all_sections(self):
        rendered = peak_manuals.render_manual("BATMAN")
        assert "IDENTITY:" in rendered
        assert "STRENGTHS" in rendered
        assert "ANTI-PATTERNS" in rendered
        assert "DOCTRINE YOU UPHOLD" in rendered

    def test_missing_persona_raises(self):
        with pytest.raises(KeyError):
            peak_manuals.manual_for("UNKNOWN")


# ---------------------------------------------------------------------------
# skills_catalog
# ---------------------------------------------------------------------------


class TestSkillsCatalog:
    def test_each_persona_has_skills(self):
        for p in ("BATMAN", "ALFRED", "ROBIN"):
            assert len(skills_catalog.skills_for(p)) >= 3

    def test_can_handle_known_skill(self):
        from eta_engine.brain.model_policy import TaskCategory

        assert skills_catalog.can_handle("BATMAN", TaskCategory.RED_TEAM_SCORING)
        assert skills_catalog.can_handle("ALFRED", TaskCategory.DOC_WRITING)
        assert skills_catalog.can_handle("ROBIN", TaskCategory.COMMIT_MESSAGE)

    def test_persona_routing(self):
        from eta_engine.brain.model_policy import TaskCategory

        assert skills_catalog.persona_for_category(TaskCategory.RED_TEAM_SCORING) == "BATMAN"
        assert skills_catalog.persona_for_category(TaskCategory.DOC_WRITING) == "ALFRED"
        assert skills_catalog.persona_for_category(TaskCategory.COMMIT_MESSAGE) == "ROBIN"

    def test_categories_rollup(self):
        r = skills_catalog.categories_by_persona()
        assert "BATMAN" in r and len(r["BATMAN"]) >= 3
        assert "ROBIN" in r and len(r["ROBIN"]) >= 3


# ---------------------------------------------------------------------------
# mcp_awareness
# ---------------------------------------------------------------------------


class TestMCPAwareness:
    def test_batman_has_pine_analyze(self):
        mcps = mcp_awareness.mcps_for("BATMAN")
        assert any(m.tool == "pine_analyze" for m in mcps)

    def test_jarvis_has_no_mcps(self):
        assert mcp_awareness.mcps_for("JARVIS") == []

    def test_robin_has_read_file(self):
        assert any(m.tool == "read_file" for m in mcp_awareness.mcps_for("ROBIN"))

    def test_render_mcp_block_non_empty(self):
        block = mcp_awareness.render_mcp_block("ALFRED")
        assert "ALFRED" in block
        assert "::" in block  # server::tool markers

    def test_render_mcp_block_jarvis_explains(self):
        block = mcp_awareness.render_mcp_block("JARVIS").lower()
        assert "no mcp access" in block


# ---------------------------------------------------------------------------
# collaboration
# ---------------------------------------------------------------------------


class TestCollaboration:
    def test_kill_switch_highest_priority(self):
        protos = collaboration.PROTOCOLS
        # The KILL_SWITCH protocol should be highest priority (10)
        max_p = max(r.priority for r in protos)
        assert max_p == 10
        kill = [r for r in protos if r.priority == 10]
        assert any("kill" in r.when.lower() for r in kill)

    def test_protocols_sorted_by_priority_desc(self):
        rules = collaboration.rules_applicable(set())
        priorities = [r.priority for r in rules]
        assert priorities == sorted(priorities, reverse=True)

    def test_render_protocols_lists_all(self):
        block = collaboration.render_protocols()
        assert "COLLABORATION PROTOCOLS" in block
        assert "WHEN:" in block
        assert "WHO:" in block


# ---------------------------------------------------------------------------
# curriculum
# ---------------------------------------------------------------------------


class TestCurriculum:
    def test_each_persona_has_exercises(self):
        counts = curriculum.count_per_persona()
        for p in ("BATMAN", "ALFRED", "ROBIN"):
            assert counts[p] >= 3

    def test_exercises_have_skill_match(self):
        for ex in curriculum.EXERCISES:
            assert skills_catalog.can_handle(ex.persona, ex.skill), (
                f"Exercise {ex.id} targets skill {ex.skill.value} but {ex.persona} doesn't list it"
            )

    def test_tier_filter(self):
        adv = curriculum.exercises_for("BATMAN", tier="advanced")
        assert all(e.tier == "advanced" for e in adv)


# ---------------------------------------------------------------------------
# eval_harness
# ---------------------------------------------------------------------------


class TestEvalHarness:
    def test_grade_perfect_robin_response(self):
        r = eval_harness.grade_exercise(
            persona="ROBIN",
            exercise_id="ROB-001",
            skill_category="COMMIT_MESSAGE",
            response="fix(avengers): clean SIGTERM exit in heartbeat loop",
            typical_tokens=80,
        )
        assert r.format_ok
        assert r.score >= 0.9
        assert r.anti_pattern_hits == []

    def test_grade_padded_robin_response(self):
        r = eval_harness.grade_exercise(
            persona="ROBIN",
            exercise_id="ROB-001",
            skill_category="COMMIT_MESSAGE",
            response="Sure! Here is the commit message: fix(avengers): stuff",
            typical_tokens=80,
        )
        assert len(r.anti_pattern_hits) >= 1
        assert r.score < 0.9

    def test_grade_proper_alfred_response(self):
        response = (
            "## Plan\n"
            "- Step 1\n- Step 2\n- Step 3\n\n"
            "## Deliverable\n```python\ndef foo(): pass\n```\n\n"
            "## Check\nRun pytest."
        )
        r = eval_harness.grade_exercise(
            persona="ALFRED",
            exercise_id="ALF-001",
            skill_category="TEST_RUN",
            response=response,
            typical_tokens=600,
        )
        assert r.format_ok
        assert r.score >= 0.8

    def test_grade_alfred_missing_section(self):
        r = eval_harness.grade_exercise(
            persona="ALFRED",
            exercise_id="ALF-001",
            skill_category="TEST_RUN",
            response="## Plan\n- do stuff\n## Deliverable\ncode",
            typical_tokens=600,
        )
        assert any("Check" in h for h in r.anti_pattern_hits)

    def test_grade_proper_batman_response(self):
        response = (
            "## Thesis\nThe proposal is X.\n"
            "## Attack Vectors\n- a\n- b\n"
            "## Evidence Check\nall survive\n"
            "## Mitigations\n- m1\n"
            "## Verdict\nITERATE -- needs more evidence"
        )
        r = eval_harness.grade_exercise(
            persona="BATMAN",
            exercise_id="BAT-001",
            skill_category="RED_TEAM_SCORING",
            response=response,
            typical_tokens=1200,
        )
        assert r.format_ok

    def test_aggregate_report_empty(self):
        rep = eval_harness.aggregate_report("ROBIN", [])
        assert rep.n_exercises == 0
        assert "no exercises" in rep.recommendation

    def test_aggregate_report_peak(self):
        results = [
            eval_harness.grade_exercise(
                "ROBIN",
                "t1",
                "TEST",
                "fix(x): y",
                50,
            )
            for _ in range(5)
        ]
        rep = eval_harness.aggregate_report("ROBIN", results)
        assert rep.mean_score > 0.8
        assert rep.n_passed >= 4


# ---------------------------------------------------------------------------
# integration with claude_layer.prompts
# ---------------------------------------------------------------------------


class TestPromptIntegration:
    def test_persona_prompts_embed_peak_manual(self):
        """BULL/BEAR/SKEPTIC/HISTORIAN prefixes should now include BATMAN's peak manual."""
        from eta_engine.brain.jarvis_v3.claude_layer.prompts import (
            PERSONA_PREFIXES,
        )

        for name in ("BULL", "BEAR", "SKEPTIC", "HISTORIAN"):
            prefix = PERSONA_PREFIXES[name]
            assert "PEAK MANUAL" in prefix
            assert "BATMAN" in prefix
            assert "COLLABORATION PROTOCOLS" in prefix
