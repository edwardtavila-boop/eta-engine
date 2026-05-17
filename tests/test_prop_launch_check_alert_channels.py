from __future__ import annotations

import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from eta_engine.scripts import prop_launch_check as mod  # noqa: E402


def test_check_alert_channels_uses_canonical_secret_files(tmp_path: Path, monkeypatch) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "telegram_bot_token.txt").write_text(
        "123456789:ABCDEFGHIJKLMNOPQRSTUV_abcdefghi",
        encoding="utf-8",
    )
    (secrets_dir / "telegram_chat_id.txt").write_text("-1001234567890", encoding="utf-8")
    monkeypatch.setenv("ETA_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("ETA_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ETA_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("ETA_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ETA_GENERIC_WEBHOOK_URL", raising=False)

    channels = mod._check_alert_channels()

    assert channels == {
        "telegram": True,
        "discord": False,
        "generic": False,
    }
