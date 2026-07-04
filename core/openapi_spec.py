"""Load robot OpenAPI specs for LLM context (Go1 dog, Loomo)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from core.robot_http import API_GO1, API_LOOMO, detect_robot_api

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"

SPEC_FILES: dict[str, str] = {
    API_GO1: "robot_openapi.json",
    API_LOOMO: "loomo_openapi.json",
    "go1": "robot_openapi.json",
    "loomo": "loomo_openapi.json",
    "dog": "robot_openapi.json",
}


def spec_filename(api: str, *, spec_override: str | None = None) -> str:
    if spec_override:
        return spec_override
    key = (api or "").strip().lower()
    return SPEC_FILES.get(key, SPEC_FILES[API_GO1])


@lru_cache(maxsize=8)
def load_openapi_text(api: str = API_GO1, spec_file: str | None = None) -> str:
    filename = spec_file or spec_filename(api)
    path = CONFIGS_DIR / filename
    if not path.is_file():
        return json.dumps({"error": f"OpenAPI spec missing at {path}"})
    return path.read_text(encoding="utf-8")


def _audio_note(api: str, host: str) -> str:
    if api == API_LOOMO:
        return f"""
Active robot: Loomo (Segway) at {host}
  speak: POST /api/cmd {{"cmd":"speak","text":"..."}}
  move:  POST /api/cmd {{"cmd":"move","linear":…,"angular":…}} — keeps moving until stop
  stop:  POST /api/cmd {{"cmd":"stop"}}
  mic:   GET /api/audio.wav (~2s 16kHz WAV)
Runtime tools handle motion and speech; use documented REST paths when planning raw HTTP.
"""
    return f"""
Active robot: Unitree Go1 (dog) at {host}
  connect:    POST /api/c2/connect
  move:       POST /api/c2/move — vx, vy, yaw_speed (-1..1 / -3..3), continuous until stop
  stop:       POST /api/c2/stop
  disconnect: POST /api/c2/disconnect
  audio:      wss://{host}/api/c2/audio — {{"mode":"talk"}}, PCM 48kHz mono int16, then {{"mode":"off"}}
  listen:     wss://{host}/api/c2/audio — {{"mode":"listen"}}
Go1 has no on-device TTS text API; speech may require WebSocket audio streaming.
"""


def robot_identity(api: str, base_url: str, *, label: str | None = None) -> str:
    host = (base_url or "").rstrip("/").replace("https://", "").replace("http://", "")
    name = label or ("Loomo" if api == API_LOOMO else "Go1 dog")
    return f"You are controlling the **{name}** robot ({api}) at {base_url or '(simulated)'}."


def system_context(
    robot_base_url: str,
    *,
    api: str | None = None,
    spec_file: str | None = None,
    label: str | None = None,
) -> str:
    resolved_api = api or detect_robot_api(robot_base_url)
    host = robot_base_url.rstrip("/").replace("https://", "").replace("http://", "")
    spec = load_openapi_text(resolved_api, spec_file)
    return (
        robot_identity(resolved_api, robot_base_url, label=label)
        + "\n\nRobot REST API OpenAPI 3.1 spec (use only documented paths and schemas):\n"
        + spec
        + "\n"
        + _audio_note(resolved_api, host)
    )
