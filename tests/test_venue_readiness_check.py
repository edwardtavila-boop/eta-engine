from __future__ import annotations

import json
from pathlib import Path

from eta_engine.scripts import venue_readiness_check as mod


def test_check_venues_empty_when_venues_dir_missing(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(mod, "VENUES_DIR", tmp_path / "missing_venues")

    assert mod.check_venues() == []


def test_check_venues_classifies_connectors(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    venues_dir = tmp_path / "venues"
    venues_dir.mkdir()
    (venues_dir / "ibkr_connector.py").write_text("# ibkr\n", encoding="utf-8")
    (venues_dir / "tastytrade_adapter.py").write_text("# tasty\n", encoding="utf-8")
    (venues_dir / "tradovate_probe.py").write_text("# tradovate\n", encoding="utf-8")
    monkeypatch.setattr(mod, "VENUES_DIR", venues_dir)

    venues = mod.check_venues()
    by_name = {venue.venue: venue for venue in venues}

    assert by_name["IBKR"].status == "READY"
    assert by_name["IBKR"].live_supported is True
    assert by_name["Tastytrade"].status == "READY"
    assert by_name["PaperSim"].path == mod.PAPERSIM_LABEL
    assert by_name["Tradovate"].status == "DORMANT"
    assert by_name["Tradovate"].path.endswith("tradovate_probe.py")


def test_main_json_prints_payload(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    rows = [
        mod.VenueStatus("IBKR", True, True, True, "READY", "ibkr_connector.py"),
        mod.VenueStatus("PaperSim", True, True, False, "READY", mod.PAPERSIM_LABEL),
    ]
    monkeypatch.setattr(mod, "check_venues", lambda: rows)

    rc = mod.main(["--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["venues"][0]["venue"] == "IBKR"
    assert payload["venues"][1]["path"] == mod.PAPERSIM_LABEL


def test_main_text_output_uses_ascii_copy(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    rows = [
        mod.VenueStatus("IBKR", False, False, False, "MISSING", mod.EMPTY_MARKER),
        mod.VenueStatus("PaperSim", True, True, False, "READY", mod.PAPERSIM_LABEL),
    ]
    monkeypatch.setattr(mod, "check_venues", lambda: rows)

    rc = mod.main([])

    assert rc == 0
    output = capsys.readouterr().out
    assert "Venue" in output
    assert mod.EMPTY_MARKER in output
    assert "1 venue(s) ready for paper trading" in output
    assert "â" not in output
