"""Tests for eta_engine.scripts.setup_tradovate_secrets.

The interactive prompts are exercised by monkeypatching input / getpass.getpass
so no real stdin is needed and no plaintext is passed via argv.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eta_engine.scripts import setup_tradovate_secrets as sts

if TYPE_CHECKING:
    import pytest


# --------------------------------------------------------------------------- #
# --check
# --------------------------------------------------------------------------- #


def test_cmd_check_returns_1_when_all_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sts.SECRETS, "get", lambda k, required=False: None)  # noqa: ARG005
    rc = sts.cmd_check()
    assert rc == 1
    out = capsys.readouterr().out
    assert "5/5 missing" in out


def test_cmd_check_returns_0_when_all_present(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sts.SECRETS, "get", lambda k, required=False: "x")  # noqa: ARG005
    rc = sts.cmd_check()
    assert rc == 0
    out = capsys.readouterr().out
    assert "All 5 Tradovate secrets present" in out


def test_cmd_check_returns_1_when_some_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {"TRADOVATE_USERNAME": "u", "TRADOVATE_PASSWORD": "p"}

    def fake_get(key: str, required: bool = False) -> str | None:  # noqa: ARG001
        return values.get(key)

    monkeypatch.setattr(sts.SECRETS, "get", fake_get)
    assert sts.cmd_check() == 1


def test_prop_account_fields_prefix_credentials_and_account_id() -> None:
    fields = sts.fields_for_prop_account("blusky_50k")
    assert [field[0] for field in fields] == [
        "BLUSKY_TRADOVATE_ACCOUNT_ID",
        "BLUSKY_TRADOVATE_USERNAME",
        "BLUSKY_TRADOVATE_PASSWORD",
        "BLUSKY_TRADOVATE_APP_ID",
        "BLUSKY_TRADOVATE_APP_SECRET",
        "BLUSKY_TRADOVATE_CID",
    ]


def test_launch_50k_phase1_fields_are_account_scoped() -> None:
    fields = sts.fields_for_prop_account("blusky_launch_50k_phase1")

    assert [field[0] for field in fields] == [
        "BLUSKY_LAUNCH_50K_TRADOVATE_ACCOUNT_ID",
        "BLUSKY_LAUNCH_50K_TRADOVATE_USERNAME",
        "BLUSKY_LAUNCH_50K_TRADOVATE_PASSWORD",
        "BLUSKY_LAUNCH_50K_TRADOVATE_APP_ID",
        "BLUSKY_LAUNCH_50K_TRADOVATE_APP_SECRET",
        "BLUSKY_LAUNCH_50K_TRADOVATE_CID",
    ]


def test_cmd_check_supports_prop_account_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fields = sts.fields_for_prop_account("blusky_50k")
    present = {field[0] for field in fields}
    monkeypatch.setattr(
        sts.SECRETS,
        "get",
        lambda k, required=False: "x" if k in present else None,  # noqa: ARG005
    )

    rc = sts.cmd_check(fields=fields, title="BluSky Tradovate prop secret status")

    assert rc == 0
    out = capsys.readouterr().out
    assert "BluSky Tradovate prop secret status" in out
    assert "BLUSKY_TRADOVATE_ACCOUNT_ID" in out
    assert "All 6 Tradovate secrets present" in out


# --------------------------------------------------------------------------- #
# --reset
# --------------------------------------------------------------------------- #


def test_cmd_reset_attempts_delete_on_all_keys(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(sts, "_delete", lambda k: deleted.append(k))
    rc = sts.cmd_reset()
    assert rc == 0
    # All 5 keys attempted
    assert len(deleted) == 5
    out = capsys.readouterr().out
    assert "Clearing Tradovate secrets" in out


def test_cmd_reset_tolerates_missing_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_delete` should swallow any exception so reset is idempotent."""
    # Simulate a missing keyring module entirely.

    def boom(k: str) -> None:
        raise RuntimeError("keyring not installed")

    # Patch the internal function that the top-level one wraps.
    monkeypatch.setattr(sts, "_delete", lambda k: None)  # outer _delete is safe
    # The real _delete() is its own function; let's also exercise it directly
    # with a broken keyring to confirm it doesn't raise.
    # (This is an integration-level smoke check of the except path.)
    import eta_engine.scripts.setup_tradovate_secrets as mod

    # Replace the keyring import inside _delete with something that raises
    class _BoomKeyring:
        @staticmethod
        def delete_password(*a: object, **kw: object) -> None:  # noqa: ARG004
            raise RuntimeError("nope")

    monkeypatch.setitem(
        __import__("sys").modules,
        "keyring",
        _BoomKeyring,
    )
    # Should not raise
    mod._delete("TRADOVATE_USERNAME")


# --------------------------------------------------------------------------- #
# interactive cmd (non-interactive via monkeypatch)
# --------------------------------------------------------------------------- #


def test_interactive_stores_all_five_when_user_fills_everything(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Nothing stored initially.
    stored: dict[str, str] = {}

    def fake_get(key: str, required: bool = False) -> str | None:  # noqa: ARG001
        return stored.get(key)

    def fake_set(key: str, val: str, scope: str = "keyring") -> None:  # noqa: ARG001
        stored[key] = val

    monkeypatch.setattr(sts.SECRETS, "get", fake_get)
    monkeypatch.setattr(sts.SECRETS, "set", fake_set)

    answers = iter(
        [
            "trader@example.com",  # USERNAME
            "pw-1",  # PASSWORD (getpass)
            "",  # APP_ID (takes default "EtaEngine")
            "app-sec-xyz",  # APP_SECRET (getpass)
            "12345",  # CID
        ]
    )
    # Overwrite prompts only fire for already-stored keys; none here.
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(sts.getpass, "getpass", lambda prompt="": next(answers))

    rc = sts.cmd_interactive()
    assert rc == 0
    assert stored["TRADOVATE_USERNAME"] == "trader@example.com"
    assert stored["TRADOVATE_PASSWORD"] == "pw-1"
    assert stored["TRADOVATE_APP_ID"] == "EtaEngine"  # from default
    assert stored["TRADOVATE_APP_SECRET"] == "app-sec-xyz"
    assert stored["TRADOVATE_CID"] == "12345"
    out = capsys.readouterr().out
    assert "Present overall: 5/5" in out


def test_interactive_skips_stored_keys_when_user_says_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: dict[str, str] = {
        "TRADOVATE_USERNAME": "existing-u",
        "TRADOVATE_PASSWORD": "existing-p",
        "TRADOVATE_APP_ID": "existing-aid",
        "TRADOVATE_APP_SECRET": "existing-sec",
        "TRADOVATE_CID": "existing-cid",
    }

    def fake_get(key: str, required: bool = False) -> str | None:  # noqa: ARG001
        return stored.get(key)

    def fake_set(key: str, val: str, scope: str = "keyring") -> None:  # noqa: ARG001
        stored[key] = val

    monkeypatch.setattr(sts.SECRETS, "get", fake_get)
    monkeypatch.setattr(sts.SECRETS, "set", fake_set)

    # User answers "n" to every "overwrite?" prompt. Should touch nothing.
    answers = iter(["n", "n", "n", "n", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    called: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        sts.getpass,
        "getpass",
        lambda prompt="": called.append(("getpass",)) or "",
    )
    rc = sts.cmd_interactive()
    assert rc == 0  # 5/5 still present
    assert called == []  # no password prompt fired
    assert stored["TRADOVATE_USERNAME"] == "existing-u"


def test_interactive_overwrites_when_user_says_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: dict[str, str] = {"TRADOVATE_USERNAME": "old-u"}

    def fake_get(key: str, required: bool = False) -> str | None:  # noqa: ARG001
        return stored.get(key)

    def fake_set(key: str, val: str, scope: str = "keyring") -> None:  # noqa: ARG001
        stored[key] = val

    monkeypatch.setattr(sts.SECRETS, "get", fake_get)
    monkeypatch.setattr(sts.SECRETS, "set", fake_set)

    # For the stored USERNAME: "y" then new value. For others: value.
    input_answers = iter(
        [
            "y",  # overwrite USERNAME?
            "new-u",  # new username
            "EtaEngine",  # APP_ID (default)
            "12345",  # CID
        ]
    )
    getpass_answers = iter(
        [
            "new-p",  # PASSWORD
            "new-sec",  # APP_SECRET
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(input_answers))
    monkeypatch.setattr(sts.getpass, "getpass", lambda prompt="": next(getpass_answers))

    rc = sts.cmd_interactive()
    assert rc == 0
    assert stored["TRADOVATE_USERNAME"] == "new-u"
    assert stored["TRADOVATE_PASSWORD"] == "new-p"
    assert stored["TRADOVATE_APP_SECRET"] == "new-sec"


def test_interactive_returns_1_when_user_skips_required_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: dict[str, str] = {}

    def fake_get(key: str, required: bool = False) -> str | None:  # noqa: ARG001
        return stored.get(key)

    def fake_set(key: str, val: str, scope: str = "keyring") -> None:  # noqa: ARG001
        stored[key] = val

    monkeypatch.setattr(sts.SECRETS, "get", fake_get)
    monkeypatch.setattr(sts.SECRETS, "set", fake_set)

    # User leaves everything blank -> default only fills APP_ID.
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr(sts.getpass, "getpass", lambda prompt="": "")

    rc = sts.cmd_interactive()
    assert rc == 1  # not 5/5
    # APP_ID has a default, so it's the only one stored
    assert stored == {"TRADOVATE_APP_ID": "EtaEngine"}


def test_interactive_reports_keyring_set_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sts.SECRETS, "get", lambda k, required=False: None)  # noqa: ARG005

    def boom(key: str, val: str, scope: str = "keyring") -> None:  # noqa: ARG001
        raise RuntimeError("keyring backend error")

    monkeypatch.setattr(sts.SECRETS, "set", boom)

    answers = iter(["trader@example.com", "pw-1", "EtaEngine", "sec", "1"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(sts.getpass, "getpass", lambda prompt="": next(answers))

    rc = sts.cmd_interactive()
    assert rc == 2
    out = capsys.readouterr().out
    assert "keyring set failed" in out


# --------------------------------------------------------------------------- #
# main() routing
# --------------------------------------------------------------------------- #


def test_main_routes_check_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    monkeypatch.setattr(sts, "cmd_check", lambda: called.append("check") or 42)
    monkeypatch.setattr("sys.argv", ["setup_tradovate_secrets", "--check"])
    assert sts.main() == 42
    assert called == ["check"]


def test_main_routes_reset_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    monkeypatch.setattr(sts, "cmd_reset", lambda: called.append("reset") or 7)
    monkeypatch.setattr("sys.argv", ["setup_tradovate_secrets", "--reset"])
    assert sts.main() == 7
    assert called == ["reset"]


def test_main_defaults_to_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    monkeypatch.setattr(sts, "cmd_interactive", lambda: called.append("i") or 3)
    monkeypatch.setattr("sys.argv", ["setup_tradovate_secrets"])
    assert sts.main() == 3
    assert called == ["i"]
