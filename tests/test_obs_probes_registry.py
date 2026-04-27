"""Unit tests for ``eta_engine.obs.probes`` registry semantics."""

from __future__ import annotations

import contextlib
import importlib
import sys

import pytest

from eta_engine.obs import probes


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    """Replace the package-level registry with an empty dict for each test
    so the production probes don't leak into our assertions.

    Uses the public clear/restore helpers exposed by the probes package.

    NOTE on test-order isolation:
      ``discover_probes()`` calls ``importlib.import_module(full)`` for
      each probe module. Python's import machinery is idempotent --
      once a probe module is in ``sys.modules``, its module-level
      ``@register_probe(...)`` calls have already fired and re-import
      is a no-op. So when ``test_discover_imports_real_probe_modules``
      runs after another test that imported a probe module, the @-call
      side effect is gone and ``discover_probes()`` returns an empty
      registry even though the imports "succeeded."

      Fix: in this fixture, evict every probe submodule from
      ``sys.modules`` before yielding so the test gets a clean
      sub-import path. After the test, restore the cleared modules
      (rebuilt by ``discover_probes`` if it ran) and re-merge the
      production registry so other tests that depend on it stay sane.
    """
    saved = probes.get_registry()
    probes.clear_registry_for_test()
    # Evict any cached probe submodules so module-level @register_probe
    # decorators re-run on next import.
    pkg_prefix = "eta_engine.obs.probes."
    evicted = [name for name in list(sys.modules) if name.startswith(pkg_prefix)]
    for name in evicted:
        del sys.modules[name]
    yield
    # Re-import any probes the test evicted but didn't reload, so that
    # downstream tests that imported probe symbols at module level
    # (e.g. ``from eta_engine.obs.probes.foo import _x``) keep
    # working.
    for name in evicted:
        # Defensive: never let a re-import break the next test.
        with contextlib.suppress(Exception):
            importlib.import_module(name)
    probes.clear_registry_for_test()
    # Restore production registry
    for rp_name, rp in saved.items():
        probes._REGISTRY[rp_name] = rp  # noqa: SLF001 — test scope


def test_register_probe_adds_to_registry() -> None:
    @probes.register_probe(name="pp", category="test", severity="advisory")
    def _p() -> probes.ProbeResult:
        return probes.ProbeResult("pp", "pass", "ok")

    reg = probes.get_registry()
    assert "pp" in reg
    assert reg["pp"].category == "test"
    assert reg["pp"].severity == "advisory"
    assert reg["pp"].fn().status == "pass"


def test_register_probe_rejects_duplicate_name() -> None:
    @probes.register_probe(name="dup", category="test")
    def _a() -> probes.ProbeResult:
        return probes.ProbeResult("dup", "pass", "")

    with pytest.raises(ValueError, match="dup.*already registered"):

        @probes.register_probe(name="dup", category="test")
        def _b() -> probes.ProbeResult:
            return probes.ProbeResult("dup", "pass", "")


def test_severity_defaults_to_important() -> None:
    @probes.register_probe(name="ds")
    def _p() -> probes.ProbeResult:
        return probes.ProbeResult("ds", "pass", "")

    assert probes.get_registry()["ds"].severity == "important"


def test_discover_imports_real_probe_modules() -> None:
    """End-to-end: discover_probes walks obs/probes/*.py and the production
    probe names appear in the registry."""
    reg = probes.discover_probes()
    expected = {
        "python_version",
        "dependencies",
        "config_loadable",
        "roadmap_state_fresh",
        "broker_dormancy",
        "preflight",
        "firm_bridge",
        "firm_runtime_shim",
        "obs_paths",
        "dashboard_importable",
        "kill_switch_state",
    }
    actual = set(reg)
    missing = expected - actual
    assert not missing, f"discover missed probes: {missing}"
