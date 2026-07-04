"""Background robot-mic listener — only forwards speech after the wake phrase."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from core.api_client import transcribe_audio
from core.wake_word import extract_wake_command

logger = logging.getLogger(__name__)


@dataclass
class WakeListenerState:
    running: bool = False
    last_transcript: str = ""
    last_command: str = ""
    last_reply: str = ""
    last_status: str = "idle"
    triggers: int = 0
    ignored: int = 0
    errors: int = 0


class WakeListener:
    """Poll the robot microphone and invoke the agent only on wake phrase."""

    def __init__(
        self,
        robot,
        *,
        on_command: Callable[[str], Awaitable[dict]],
        on_event: Callable[[dict], Awaitable[None]] | None = None,
        poll_interval_sec: float = 0.4,
        capture_duration_sec: float = 5.0,
        cooldown_sec: float = 3.0,
    ) -> None:
        self.robot = robot
        self.on_command = on_command
        self.on_event = on_event
        self.poll_interval_sec = poll_interval_sec
        self.capture_duration_sec = capture_duration_sec
        self.cooldown_sec = cooldown_sec
        self.state = WakeListenerState()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._busy = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _emit(self, payload: dict) -> None:
        if self.on_event is None:
            return
        try:
            await self.on_event(payload)
        except Exception:
            logger.exception("Wake listener event callback failed")

    async def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.state.running = True
        self.state.last_status = "listening"
        self._task = asyncio.create_task(self._loop())
        await self._emit({"type": "wake_listener", "status": "started"})
        logger.info("Wake listener started (phrase: hi loomo)")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.state.running = False
        self.state.last_status = "stopped"
        await self._emit({"type": "wake_listener", "status": "stopped"})
        logger.info("Wake listener stopped")

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while not self._stop.is_set():
                try:
                    await self._listen_once(loop)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.state.errors += 1
                    self.state.last_status = "error"
                    logger.exception("Wake listener cycle failed")
                await asyncio.sleep(self.poll_interval_sec)
        except asyncio.CancelledError:
            pass
        finally:
            self.state.running = False

    async def _listen_once(self, loop: asyncio.AbstractEventLoop) -> None:
        if self.robot.simulated:
            self.state.last_status = "simulated — mic unavailable"
            await self._emit(
                {
                    "type": "wake_listener",
                    "status": "simulated",
                    "message": "Mic unavailable in simulated mode",
                }
            )
            await asyncio.sleep(1.0)
            return

        if self._busy.locked():
            return

        async with self._busy:
            self.state.last_status = "capturing"
            await self._emit({"type": "wake_listener", "status": "capturing"})
            logger.info("Wake: capturing mic (%.1fs)", self.capture_duration_sec)
            wav = await self.robot.capture_audio(self.capture_duration_sec)
            if not wav:
                self.state.last_status = "listening (no audio)"
                await self._emit({"type": "wake_listener", "status": "no_audio"})
                logger.info("Wake: no audio from mic")
                return

            self.state.last_status = "transcribing"
            await self._emit(
                {
                    "type": "wake_listener",
                    "status": "transcribing",
                    "audio_bytes": len(wav),
                }
            )
            logger.info("Wake: transcribing %d bytes", len(wav))
            heard = await loop.run_in_executor(None, transcribe_audio, wav)
            heard = (heard or "").strip()
            self.state.last_transcript = heard

            if not heard:
                self.state.last_status = "listening (silent)"
                await self._emit(
                    {
                        "type": "wake_listener",
                        "status": "silent",
                        "audio_bytes": len(wav),
                    }
                )
                logger.info("Wake: silent — %d bytes but empty transcript", len(wav))
                return

            await self._emit(
                {"type": "wake_listener", "status": "heard", "heard": heard}
            )
            logger.info("Wake: heard %r", heard)

            command, reason = extract_wake_command(heard)
            if command is None:
                self.state.ignored += 1
                self.state.last_status = f"ignored ({reason})"
                logger.info("Wake: ignored %r — %s", heard, reason)
                await self._emit(
                    {
                        "type": "wake_listener",
                        "status": "ignored",
                        "heard": heard,
                        "reason": reason,
                    }
                )
                return

            self.state.triggers += 1
            self.state.last_command = command
            self.state.last_status = f"triggered: {command[:60]}"
            logger.info("Wake: TRIGGERED — heard=%r command=%r", heard, command)

            await self._emit(
                {
                    "type": "wake_listener",
                    "status": "triggered",
                    "heard": heard,
                    "command": command,
                }
            )

            self.state.last_status = "thinking"
            await self._emit(
                {
                    "type": "wake_listener",
                    "status": "thinking",
                    "heard": heard,
                    "command": command,
                }
            )
            logger.info("Wake: running agent for command=%r", command)
            result = await self.on_command(command)
            reply = (result.get("reply") or "").strip()
            self.state.last_reply = reply
            logger.info("Wake: agent replied %r", reply[:120] if reply else "")

            await self._emit(
                {
                    "type": "wake_listener",
                    "status": "replied",
                    "heard": heard,
                    "command": command,
                    "reply": reply,
                }
            )
            self.state.last_status = "listening"
            if self.cooldown_sec > 0:
                await asyncio.sleep(self.cooldown_sec)
