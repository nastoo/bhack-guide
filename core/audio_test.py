"""Standalone tests for robot mic → Whisper → speak pipeline."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field

from core.api_client import stt_language, transcribe_audio
from core.robot_http import LOOMO_CMD_PATH, RobotHttpClient

logger = logging.getLogger(__name__)


@dataclass
class AudioTestResult:
    ok: bool
    mode: str
    heard: str = ""
    spoken: str = ""
    steps: list[str] = field(default_factory=list)
    error: str = ""


class AudioTestRunner:
    """Test audio I/O without running full GPS navigation."""

    def __init__(
        self,
        robot: RobotHttpClient,
        *,
        stt_model: str | None = None,
    ) -> None:
        self.robot = robot
        self.stt_model = stt_model or os.environ.get("NAV_STT_MODEL", "whisper-3-large")

    def _require_live_robot(self) -> None:
        if self.robot.simulated:
            raise RuntimeError(
                "Audio hardware test requires ROBOT_SIMULATED=false and a reachable ROBOT_BASE_URL"
            )

    async def test_speak(self, text: str) -> AudioTestResult:
        """POST /api/cmd speak on Loomo (on-device speech)."""
        text = (text or "").strip()
        if not text:
            return AudioTestResult(ok=False, mode="speak", error="text is empty")

        steps = ["connect"]
        try:
            self._require_live_robot()
        except RuntimeError as exc:
            return AudioTestResult(ok=False, mode="speak", error=str(exc), steps=steps)

        if not await self.robot.connect():
            return AudioTestResult(ok=False, mode="speak", error="robot connect failed", steps=steps)

        try:
            steps.append(f"POST {LOOMO_CMD_PATH} speak")
            await self.robot.stream_speech(text)
            steps.append("done")
            return AudioTestResult(ok=True, mode="speak", spoken=text, steps=steps)
        except Exception as exc:
            logger.exception("test_speak failed")
            return AudioTestResult(ok=False, mode="speak", spoken=text, error=str(exc), steps=steps)
        finally:
            await self.robot.disconnect()

    async def test_listen(self, duration: float = 5.0) -> AudioTestResult:
        """Robot mic → Whisper transcription only."""
        steps = ["connect"]
        try:
            self._require_live_robot()
        except RuntimeError as exc:
            return AudioTestResult(ok=False, mode="listen", error=str(exc), steps=steps)

        if not await self.robot.connect():
            return AudioTestResult(ok=False, mode="listen", error="robot connect failed", steps=steps)

        try:
            capture_note = (
                "GET /api/audio.wav (~2s)"
                if self.robot.api == "loomo"
                else f"go1 websocket listen ({duration:.1f}s)"
            )
            steps.append(capture_note)
            wav = await self.robot.capture_audio(duration)
            if not wav:
                return AudioTestResult(
                    ok=False,
                    mode="listen",
                    error="no audio captured from robot mic",
                    steps=steps,
                )

            steps.append(f"whisper ({self.stt_model}, lang={stt_language()})")
            loop = asyncio.get_event_loop()
            heard = await loop.run_in_executor(None, transcribe_audio, wav)
            heard = (heard or "").strip()
            if not heard:
                return AudioTestResult(
                    ok=False,
                    mode="listen",
                    error="whisper returned empty transcript",
                    steps=steps,
                )

            steps.append("done")
            return AudioTestResult(ok=True, mode="listen", heard=heard, steps=steps)
        except Exception as exc:
            logger.exception("test_listen failed")
            return AudioTestResult(ok=False, mode="listen", error=str(exc), steps=steps)
        finally:
            await self.robot.disconnect()

    async def test_echo(self, duration: float = 5.0, *, prefix: str = "You said: ") -> AudioTestResult:
        """Robot mic → Whisper → POST /api/cmd speak."""
        steps = ["connect"]
        try:
            self._require_live_robot()
        except RuntimeError as exc:
            return AudioTestResult(ok=False, mode="echo", error=str(exc), steps=steps)

        if not await self.robot.connect():
            return AudioTestResult(ok=False, mode="echo", error="robot connect failed", steps=steps)

        try:
            capture_note = (
                "GET /api/audio.wav (~2s)"
                if self.robot.api == "loomo"
                else f"go1 websocket listen ({duration:.1f}s)"
            )
            steps.append(capture_note)
            wav = await self.robot.capture_audio(duration)
            if not wav:
                return AudioTestResult(
                    ok=False,
                    mode="echo",
                    error="no audio captured from robot mic",
                    steps=steps,
                )

            steps.append(f"whisper ({self.stt_model}, lang={stt_language()})")
            loop = asyncio.get_event_loop()
            heard = await loop.run_in_executor(None, transcribe_audio, wav)
            heard = (heard or "").strip()
            if not heard:
                return AudioTestResult(
                    ok=False,
                    mode="echo",
                    error="whisper returned empty transcript",
                    steps=steps,
                )

            spoken = f"{prefix}{heard}".strip()
            steps.append(f"POST {LOOMO_CMD_PATH} speak")
            await self.robot.stream_speech(spoken)
            steps.append("done")
            return AudioTestResult(ok=True, mode="echo", heard=heard, spoken=spoken, steps=steps)
        except Exception as exc:
            logger.exception("test_echo failed")
            return AudioTestResult(ok=False, mode="echo", error=str(exc), steps=steps)
        finally:
            await self.robot.disconnect()


async def run_cli_test(
    mode: str,
    *,
    text: str = "Hello, this is an audio test.",
    duration: float = 5.0,
    robot_base_url: str,
    simulated: bool,
    robot_api: str | None = None,
    stt_model: str | None = None,
    tts_model: str | None = None,
) -> AudioTestResult:
    del tts_model  # TTS not used — Loomo speak API only
    robot = RobotHttpClient(robot_base_url, simulated=simulated, api=robot_api)
    runner = AudioTestRunner(robot, stt_model=stt_model)

    if mode == "speak":
        return await runner.test_speak(text)
    if mode == "listen":
        return await runner.test_listen(duration)
    if mode == "echo":
        return await runner.test_echo(duration)
    raise ValueError(f"Unknown test mode: {mode}. Use speak, listen, or echo.")
