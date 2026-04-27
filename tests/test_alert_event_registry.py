"""
Registry-pinning test for ``dispatcher.send(EVENT, ...)`` call sites.

Walks the production codebase (excludes ``tests/`` and ``__pycache__``)
for every call of the form ``dispatcher.send("event_name", ...)`` and
asserts that ``event_name`` is registered under
``routing.events`` in ``configs/alerts.yaml``.

Why this exists
---------------
The v0.1.63 R1 Red Team review (B2) caught that ``broker_equity_drift``
was being dispatched but had no entry in ``alerts.yaml``. The
:class:`AlertDispatcher` silently logs unknown events to
``docs/alerts_log.jsonl`` and returns ``DispatchResult(level="unknown",
channels=[], delivered=[], blocked=["unknown event '...'"])`` -- no
Pushover, no email, no SMS. So in production the operator received
zero notifications for that event. A re-audit also surfaced six other
events with the same gap (``boot_refused``, ``kill_switch_latched``,
``apex_preempt``, ``consistency_status``, ``runtime_start``,
``runtime_stop``, ``bot_error``).

Without a CI gate, this class of bug recurs every time an engineer
adds a new ``dispatcher.send(...)`` call without remembering to update
the YAML. The test below makes the omission impossible to merge.

What this test does NOT enforce
--------------------------------
* It does not check that ``alerts.yaml`` entries point at valid
  channels -- :class:`AlertDispatcher` already raises on bad channel
  names at construction.
* It does not check that the level is correct (``info`` vs ``warn`` vs
  ``critical``) -- that's a judgement call per event, not a structural
  invariant.
* It does not flag *registered-but-unused* events. Those are forward-
  looking entries (e.g. ``bot_entry`` is in YAML but no code path
  emits it yet). Removing them silently would break the next hookup.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# Match ``dispatcher.send("event_name", ...)`` -- works for both bare
# ``dispatcher`` (as in ``apply_verdict``) and ``self.dispatcher``
# (as in ``ApexRuntime``) because we only need the call-target attribute
# match, not the receiver. snake_case event name only -- the dispatcher
# rejects non-snake-case at runtime.
_DISPATCHER_SEND_RE = re.compile(
    r'dispatcher\.send\(\s*["\']([a-z_][a-z0-9_]*)["\']',
)


def _called_events() -> dict[str, list[Path]]:
    """Walk production .py files, return {event_name: [callers]}."""
    out: dict[str, list[Path]] = {}
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith("tests/") or "__pycache__" in rel:
            continue
        text = p.read_text(encoding="utf-8")
        for m in _DISPATCHER_SEND_RE.finditer(text):
            out.setdefault(m.group(1), []).append(p)
    return out


def _registered_events() -> set[str]:
    cfg = yaml.safe_load(
        (ROOT / "configs" / "alerts.yaml").read_text(encoding="utf-8"),
    )
    return set(cfg.get("routing", {}).get("events", {}).keys())


def test_every_dispatched_event_is_registered_in_alerts_yaml() -> None:
    """
    Every event dispatched by production code MUST be registered in
    ``configs/alerts.yaml``. Unregistered events are silently dropped.
    """
    called = _called_events()
    registered = _registered_events()
    missing = sorted(set(called.keys()) - registered)
    assert missing == [], (
        "These events are dispatched in production code but are missing "
        "from configs/alerts.yaml -- AlertDispatcher will silently drop "
        "them and the operator will get NO Pushover / email / SMS. "
        f"Add a routing.events entry for each: {missing}.\n"
        + "\n".join(f"  {ev:<30s} -> {', '.join(p.relative_to(ROOT).as_posix() for p in called[ev])}" for ev in missing)
    )


def test_no_event_is_dispatched_under_a_non_snake_case_name() -> None:
    """
    Defensive: the regex above only matches snake_case. If a future
    refactor introduces ``dispatcher.send("bot-entry", ...)`` (kebab)
    or ``dispatcher.send("BotEntry", ...)`` (camel), the regex skips it
    and our registry test passes spuriously.

    This test re-walks for the loose pattern and rejects.
    """
    loose = re.compile(r'dispatcher\.send\(\s*["\']([^"\']+)["\']')
    bad: list[tuple[str, str]] = []
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith("tests/") or "__pycache__" in rel:
            continue
        text = p.read_text(encoding="utf-8")
        for m in loose.finditer(text):
            ev = m.group(1)
            if not re.fullmatch(r"[a-z_][a-z0-9_]*", ev):
                bad.append((rel, ev))
    assert bad == [], (
        f"Non-snake-case event names dispatched: {bad}. "
        "AlertDispatcher matches strictly on the YAML key, so any drift "
        "from snake_case becomes an unrouted event."
    )
