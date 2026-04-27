"""EVOLUTIONARY TRADING ALGO // bots.btc_hybrid.profile.

Profile loader for the BTC hybrid bot. The dataclass itself lives in
``eta_engine.bots.btc_hybrid.bot`` so the bot module stays the single
source of truth for the L2 state machine. This module just exposes:

* :data:`DEFAULT_BTC_PROFILE_PATH` -- the canonical path the
  optimization pipeline writes a tuned profile to. The file is
  optional: when it does not exist, the loader falls back to the
  hard-coded defaults baked into ``BtcHybridProfile``.
* :func:`load_btc_hybrid_profile` -- read a YAML or JSON profile from
  disk and instantiate :class:`BtcHybridProfile` with the override
  fields the file carries. Unknown keys are ignored so the loader
  forward-compatible with newer pipelines.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from eta_engine.bots.btc_hybrid.bot import BtcHybridProfile


DEFAULT_BTC_PROFILE_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent
    / "configs"
    / "btc_hybrid_profile.yaml"
)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _parse(text: str, suffix: str) -> dict[str, Any]:
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "PyYAML is required to load YAML profiles; "
                "install pyyaml or use a .json profile"
            ) from exc
        loaded = yaml.safe_load(text) or {}
    elif suffix == ".json":
        loaded = json.loads(text) if text.strip() else {}
    else:
        raise ValueError(f"unsupported profile suffix: {suffix!r}")
    if not isinstance(loaded, dict):
        raise ValueError(
            f"profile must deserialize to a mapping, got {type(loaded).__name__}"
        )
    return loaded


def load_btc_hybrid_profile(path: Path | str | None = None) -> BtcHybridProfile:
    """Load a tuned profile from disk, or return defaults.

    A missing file (including the canonical
    :data:`DEFAULT_BTC_PROFILE_PATH`) is treated as "use defaults" so
    the bot is always constructible without operator setup. Unknown
    keys in the file are ignored.
    """
    target = Path(path) if path is not None else DEFAULT_BTC_PROFILE_PATH
    text = _read_text(target)
    if text is None:
        return BtcHybridProfile()
    raw = _parse(text, target.suffix.lower())
    valid = {f.name for f in fields(BtcHybridProfile)}
    overrides = {k: v for k, v in raw.items() if k in valid}
    return BtcHybridProfile(**overrides)


__all__ = [
    "BtcHybridProfile",
    "DEFAULT_BTC_PROFILE_PATH",
    "load_btc_hybrid_profile",
]
