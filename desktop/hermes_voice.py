"""
JARVIS / Hermes voice front-end (T15) — desktop orchestrator.

Runs on the OPERATOR'S DESKTOP (not the VPS). Listens for the
"Hey JARVIS" wake word via local speech recognition, captures the
following utterance, posts it to Hermes API on 127.0.0.1:8642 (via the
existing SSH tunnel to the VPS), and speaks Hermes's reply.

Architecture
------------

Three loose subsystems, all local-only on the desktop:

  1. Wake-word watcher (vad_loop)
       Uses simple energy + silence VAD on a continuous microphone
       stream. When energy crosses a threshold AND the trigger phrase
       is detected by whisper, the wake event fires.

  2. Hermes round-trip (chat_once)
       POST to /v1/chat/completions through the local tunnel. Reads
       the full reply (no streaming in v1 — adds 2-3s latency but keeps
       the orchestration simple).

  3. TTS playback (speak)
       Uses Windows SAPI by default (zero install). Operator can swap
       to piper-tts via PIPER_VOICE_PATH env var for higher quality.

Setup
-----

Required:
  pip install sounddevice numpy openai-whisper

Optional (better TTS):
  pip install piper-tts
  set PIPER_VOICE_PATH=C:\\path\\to\\voice.onnx

Run:
  python -m eta_engine.desktop.hermes_voice

The script blocks until Ctrl+C or operator says "stop listening".

Configuration via env vars
--------------------------

  HERMES_VOICE_API_URL    = "http://127.0.0.1:8642"
  HERMES_VOICE_API_KEY    = "<API_SERVER_KEY>"
  HERMES_VOICE_MODEL      = "deepseek-v4-pro"
  HERMES_VOICE_WAKE_WORD  = "hey jarvis"
  HERMES_VOICE_TIMEOUT_S  = "45"
  HERMES_VOICE_VAD_THRESHOLD = "0.02"
  HERMES_VOICE_LOG_PATH   = "<workspace>/logs/eta_engine/hermes_voice.log"

Cost
----

Whisper-tiny (CPU) is ~free; Hermes round-trips at DeepSeek-V4-Pro
prices (~$0.05 per voice query). Operator can use whisper-base for
better accuracy at ~3× CPU cost.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from eta_engine.scripts import workspace_roots

logger = logging.getLogger("eta_engine.desktop.hermes_voice")


DEFAULT_API_URL = "http://127.0.0.1:8642"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_WAKE_WORD = "hey jarvis"
DEFAULT_TIMEOUT_S = 45
DEFAULT_VAD_THRESHOLD = 0.02
DEFAULT_LOG_PATH = workspace_roots.ETA_HERMES_VOICE_LOG_PATH
SAMPLE_RATE = 16000
CHUNK_SECONDS = 0.5
MAX_UTTERANCE_SECONDS = 12
SILENCE_TO_END_SECONDS = 1.0


@dataclass(frozen=True)
class VoiceConfig:
    api_url: str
    api_key: str
    model: str
    wake_word: str
    timeout_s: int
    vad_threshold: float
    log_path: Path

    @classmethod
    def from_env(cls) -> VoiceConfig:
        return cls(
            api_url=os.environ.get("HERMES_VOICE_API_URL", DEFAULT_API_URL),
            api_key=os.environ.get(
                "HERMES_VOICE_API_KEY",
                os.environ.get("API_SERVER_KEY", ""),
            ),
            model=os.environ.get("HERMES_VOICE_MODEL", DEFAULT_MODEL),
            wake_word=os.environ.get("HERMES_VOICE_WAKE_WORD", DEFAULT_WAKE_WORD).lower(),
            timeout_s=int(os.environ.get("HERMES_VOICE_TIMEOUT_S", DEFAULT_TIMEOUT_S)),
            vad_threshold=float(
                os.environ.get("HERMES_VOICE_VAD_THRESHOLD", DEFAULT_VAD_THRESHOLD),
            ),
            log_path=Path(os.environ.get("HERMES_VOICE_LOG_PATH", str(DEFAULT_LOG_PATH))),
        )


# ---------------------------------------------------------------------------
# Hermes round-trip
# ---------------------------------------------------------------------------


def chat_once(cfg: VoiceConfig, text: str) -> str:
    """Send ``text`` to Hermes, return the reply string (or an error message).

    Errors are returned as strings (not raised) so the voice loop keeps
    running. The operator hears the error spoken back rather than seeing
    a silent failure.
    """
    if not text.strip():
        return "I didn't catch that."
    body = json.dumps(
        {
            "model": cfg.model,
            "messages": [{"role": "user", "content": text.strip()}],
            "max_tokens": 512,
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{cfg.api_url}/v1/chat/completions",
        data=body,
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    if cfg.api_key:
        req.add_header("Authorization", f"Bearer {cfg.api_key}")
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            return str(payload["choices"][0]["message"]["content"]).strip()
    except urllib.error.HTTPError as exc:
        return f"Hermes returned HTTP {exc.code}. Check the API key."
    except (urllib.error.URLError, OSError) as exc:
        return f"Cannot reach Hermes: {exc}"
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        return f"Hermes returned an unexpected response: {exc}"


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------


def speak(text: str) -> None:
    """Speak ``text`` aloud using piper-tts if PIPER_VOICE_PATH is set,
    otherwise fall back to Windows SAPI.

    Never raises — voice output is best-effort. If everything fails the
    text is printed so the operator can read it.
    """
    if not text:
        return
    piper_voice = os.environ.get("PIPER_VOICE_PATH")
    if piper_voice and Path(piper_voice).exists():
        try:
            import subprocess  # local: only spawn when actually using piper

            subprocess.run(
                ["piper", "--model", piper_voice, "--output_raw"],
                input=text.encode("utf-8"),
                check=False,
                timeout=30,
                capture_output=True,
            )
            return
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("piper TTS failed, falling back to SAPI: %s", exc)

    # Windows SAPI fallback (no install required)
    try:
        import comtypes.client  # type: ignore[import-not-found]

        voice = comtypes.client.CreateObject("SAPI.SpVoice")
        voice.Speak(text)
        return
    except Exception:  # noqa: BLE001 — SAPI is best-effort
        pass

    # Last resort: print
    print(f"[hermes-voice] {text}")


# ---------------------------------------------------------------------------
# Wake word + transcription
# ---------------------------------------------------------------------------


def _try_import_audio_stack() -> tuple[object, object, object]:
    """Import sounddevice + numpy + whisper. Returns the modules tuple
    or raises a clear ImportError if any are missing.
    """
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as exc:
        raise ImportError(
            "hermes_voice requires `sounddevice` and `numpy`. Install with: pip install sounddevice numpy",
        ) from exc
    try:
        import whisper  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "hermes_voice requires openai-whisper for local STT. Install with: pip install openai-whisper",
        ) from exc
    return sd, np, whisper


def _record_until_silence(sd_mod: object, np_mod: object, cfg: VoiceConfig) -> bytes:
    """Record from the default mic until SILENCE_TO_END_SECONDS of silence
    OR MAX_UTTERANCE_SECONDS elapses. Returns int16 mono PCM bytes.
    """
    chunk_size = int(SAMPLE_RATE * CHUNK_SECONDS)
    silence_chunks_needed = int(SILENCE_TO_END_SECONDS / CHUNK_SECONDS)
    max_chunks = int(MAX_UTTERANCE_SECONDS / CHUNK_SECONDS)

    audio_chunks: list = []
    silence_run = 0

    with sd_mod.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
    ) as stream:
        for _ in range(max_chunks):
            chunk, _overflow = stream.read(chunk_size)
            audio_chunks.append(chunk)
            # Simple energy-based VAD
            rms = float(
                np_mod.sqrt(
                    np_mod.mean(np_mod.square(chunk.astype("float32"))),
                )
            )
            normalized = rms / 32768.0
            if normalized < cfg.vad_threshold:
                silence_run += 1
                if silence_run >= silence_chunks_needed:
                    break
            else:
                silence_run = 0

    if not audio_chunks:
        return b""
    return np_mod.concatenate(audio_chunks).tobytes()


def _transcribe(whisper_mod: object, np_mod: object, pcm_bytes: bytes) -> str:
    """Transcribe int16 mono PCM to text via whisper-tiny.

    Loads the model once (module-global cached); subsequent calls reuse.
    """
    global _CACHED_WHISPER
    if "_CACHED_WHISPER" not in globals() or _CACHED_WHISPER is None:
        _CACHED_WHISPER = whisper_mod.load_model("tiny")
    audio = np_mod.frombuffer(pcm_bytes, dtype="int16").astype("float32") / 32768.0
    result = _CACHED_WHISPER.transcribe(audio, language="en", fp16=False)
    return str(result.get("text", "")).strip()


_CACHED_WHISPER = None


def vad_loop(cfg: VoiceConfig) -> None:
    """Main loop. Listens, transcribes, dispatches.

    Operator can say "stop listening" or Ctrl+C to exit.
    """
    sd, np, whisper = _try_import_audio_stack()
    logger.info("hermes_voice: listening for wake word %r", cfg.wake_word)
    speak("Hermes voice online.")

    while True:
        # Capture a short rolling window. If silence dominates, no action.
        pcm = _record_until_silence(sd, np, cfg)
        if not pcm:
            time.sleep(0.1)
            continue
        text = _transcribe(whisper, np, pcm).lower().strip()
        if not text:
            continue
        logger.info("hermes_voice heard: %r", text)
        if "stop listening" in text:
            speak("Stopping.")
            return
        if cfg.wake_word in text:
            # Strip wake phrase from the front of the command
            command = text.split(cfg.wake_word, 1)[-1].strip(" ,:.")
            if not command:
                speak("Yes?")
                # Capture follow-up utterance
                pcm = _record_until_silence(sd, np, cfg)
                command = _transcribe(whisper, np, pcm).strip() if pcm else ""
            if not command:
                continue
            logger.info("hermes_voice sending to Hermes: %r", command)
            reply = chat_once(cfg, command)
            logger.info("hermes_voice reply: %r", reply)
            speak(reply)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler(sys.stderr))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--once", help="Single-shot: send this text to Hermes and exit", default=None)
    args = parser.parse_args(argv)

    cfg = VoiceConfig.from_env()
    _setup_logging(cfg.log_path)

    if not cfg.api_key:
        logger.warning("HERMES_VOICE_API_KEY not set; Hermes calls will likely return 401")

    if args.once is not None:
        reply = chat_once(cfg, args.once)
        speak(reply)
        return 0

    try:
        vad_loop(cfg)
        return 0
    except KeyboardInterrupt:
        speak("Bye.")
        return 0
    except ImportError as exc:
        logger.error("missing dependency: %s", exc)
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
