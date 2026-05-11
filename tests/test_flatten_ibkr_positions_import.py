from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path


def test_flatten_ibkr_positions_imports_under_python314() -> None:
    module = importlib.import_module("eta_engine.scripts.flatten_ibkr_positions")

    assert hasattr(module, "flatten_ibkr_positions")


def test_flatten_ibkr_positions_imports_in_plain_subprocess() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib; "
                "module = importlib.import_module('eta_engine.scripts.flatten_ibkr_positions'); "
                "assert hasattr(module, 'flatten_ibkr_positions')"
            ),
        ],
        check=False,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[2],
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_flatten_ibkr_positions_main_reports_connection_refused(monkeypatch, capsys) -> None:
    module = importlib.import_module("eta_engine.scripts.flatten_ibkr_positions")

    def _raise_connection_refused(**_kwargs):
        raise ConnectionRefusedError(1225, "remote computer refused the network connection")

    monkeypatch.setattr(module, "flatten_ibkr_positions", _raise_connection_refused)

    rc = module.main(["--host", "127.0.0.1", "--port", "4002", "--client-id", "907", "--no-global-cancel"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["status"] == "connection_failed"
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == 4002
    assert payload["order_action_attempted"] is False
