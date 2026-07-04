"""Execute robot HTTP calls and stream audio (Go1 WebSocket or Loomo REST)."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import struct
import wave
from dataclasses import dataclass, field

import aiohttp

logger = logging.getLogger(__name__)

API_GO1 = "go1"
API_LOOMO = "loomo"

# LoomoAgent behind the mylab proxy (see curl example)
LOOMO_CMD_PATH = "/api/cmd"
LOOMO_STATUS_PATH = "/api/"
LOOMO_AUDIO_WAV_PATH = "/api/audio.wav"


def detect_robot_api(base_url: str, explicit: str | None = None) -> str:
    """Return go1 (Unitree dashboard) or loomo (LoomoAgent REST)."""
    if explicit and explicit.strip().lower() not in ("", "auto"):
        kind = explicit.strip().lower()
        if kind in (API_GO1, "robodog", "go1", "dog"):
            return API_GO1
        if kind in (API_LOOMO, "segway"):
            return API_LOOMO
        return kind

    host = (base_url or "").lower()
    if "loomo" in host or "segway" in host:
        return API_LOOMO
    return API_GO1


@dataclass
class HttpCall:
    method: str
    path: str
    body: dict | None = None
    hold_sec: float = 0.0


@dataclass
class NavigationPlan:
    speech: str
    http_calls: list[HttpCall] = field(default_factory=list)


class RobotHttpClient:
    """Talk to Go1 dashboard or LoomoAgent via their public HTTP/WebSocket APIs."""

    def __init__(
        self,
        base_url: str,
        *,
        simulated: bool = False,
        api: str | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.simulated = simulated or not self.base_url
        self.api = detect_robot_api(
            self.base_url,
            api or os.environ.get("ROBOT_API"),
        )
        self._motion_abort = asyncio.Event()
        self._connected = False
        self._motion_lock = asyncio.Lock()

    def motion_lock(self) -> asyncio.Lock:
        """Serialize robot commands (move/speak/stop) — shared with /robot agent."""
        return self._motion_lock

    async def connect(self) -> bool:
        if self.simulated:
            self._connected = True
            logger.info("[SIM HTTP] connect (%s)", self.api)
            return True

        if self.api == API_LOOMO:
            status = await self._get_json(LOOMO_STATUS_PATH)
            if status is None:
                status = await self._get_json("/")
            self._connected = status is not None
            if self._connected:
                logger.info("Loomo connected at %s", self.base_url)
            return self._connected

        result = await self.request("POST", "/api/c2/connect", {})
        self._connected = result is not None
        return self._connected

    async def disconnect(self) -> None:
        if self.api == API_LOOMO:
            await self._loomo_cmd({"cmd": "stop"})
        else:
            await self.request("POST", "/api/c2/disconnect", {})
        self._connected = False

    async def stop(self) -> None:
        async with self._motion_lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self) -> None:
        self._motion_abort.set()
        if self.api == API_LOOMO:
            await self._loomo_cmd({"cmd": "stop"})
        else:
            await self.request("POST", "/api/c2/stop", {})

    async def set_motion(
        self,
        *,
        vx: float = 0.0,
        vy: float = 0.0,
        yaw_speed: float = 0.0,
    ) -> None:
        """Send a single move command without stopping afterward (for GPS loops)."""
        vx = max(-1.0, min(1.0, vx))
        vy = max(-1.0, min(1.0, vy))
        yaw_speed = max(-3.0, min(3.0, yaw_speed))

        if self.simulated:
            logger.debug("[SIM MOTION] vx=%.2f vy=%.2f yaw=%.2f", vx, vy, yaw_speed)
            return

        if self.api == API_LOOMO:
            linear = vx
            angular = max(-1.0, min(1.0, yaw_speed / 3.0))
            await self._loomo_cmd({"cmd": "move", "linear": linear, "angular": angular})
            return

        await self.request(
            "POST",
            "/api/c2/move",
            {
                "vx": vx,
                "vy": vy,
                "yaw_speed": yaw_speed,
                "gait_type": 1,
                "foot_raise_height": 0.08,
            },
        )

    async def _loomo_cmd(self, payload: dict) -> dict | None:
        return await self.request("POST", LOOMO_CMD_PATH, payload)

    async def request(self, method: str, path: str, body: dict | None = None) -> dict | None:
        path = path if path.startswith("/") else f"/{path}"
        if self.simulated:
            logger.info("[SIM HTTP] %s %s %s", method, path, body or {})
            await asyncio.sleep(0.05)
            return {}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method,
                    f"{self.base_url}{path}",
                    json=body if method.upper() != "GET" else None,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        try:
                            return await resp.json(content_type=None)
                        except Exception:
                            return {}
                    logger.warning("Robot HTTP %s %s -> %s", method, path, resp.status)
        except Exception:
            logger.exception("Robot HTTP failed %s %s", method, path)
        return None

    async def _get_json(self, path: str) -> dict | None:
        path = path if path.startswith("/") else f"/{path}"
        if self.simulated:
            logger.info("[SIM HTTP] GET %s", path)
            return {"status": "ok"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}{path}",
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status == 200:
                        ct = resp.content_type or ""
                        if "json" in ct:
                            return await resp.json()
                    logger.warning("Robot GET %s -> %s", path, resp.status)
        except Exception:
            logger.exception("Robot GET failed %s", path)
        return None

    async def _get_bytes(self, path: str) -> bytes | None:
        path = path if path.startswith("/") else f"/{path}"
        if self.simulated:
            logger.info("[SIM HTTP] GET %s (binary)", path)
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}{path}",
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    logger.warning("Robot GET %s -> %s", path, resp.status)
        except Exception:
            logger.exception("Robot GET failed %s", path)
        return None

    async def execute_plan(self, plan: NavigationPlan) -> None:
        """Run motion HTTP calls and speech concurrently."""
        speech_task = asyncio.create_task(self.stream_speech(plan.speech))
        motion_task = asyncio.create_task(self._execute_motion(plan.http_calls))
        await asyncio.gather(speech_task, motion_task)

    async def stream_speech(self, text: str) -> None:
        """Speak on the robot. Loomo: POST /api/cmd speak. Go1: not implemented without TTS."""
        text = (text or "").strip()
        if not text:
            return

        async with self._motion_lock:
            await self._stream_speech_unlocked(text)

    async def _stream_speech_unlocked(self, text: str) -> None:
        if self.simulated:
            logger.info("[SIM SPEAK] (%s) %s", self.api, text)
            await asyncio.sleep(min(1.5, 0.2 + len(text) * 0.03))
            return

        if self.api == API_LOOMO:
            await self._loomo_speak(text)
            return

        logger.warning(
            "Go1 speech streaming is not enabled (no TTS). Text: %s",
            text[:80],
        )

    async def _loomo_speak(self, text: str) -> None:
        """Loomo on-device speech: POST /api/cmd {"cmd":"speak","text":"..."}."""
        result = await self._loomo_cmd({"cmd": "speak", "text": text})
        if result is None:
            logger.error("Loomo speak failed for: %s", text[:80])
            return
        wait = min(12.0, 0.4 + len(text.split()) * 0.45)
        logger.info("Loomo speaking via %s (%.1fs): %s", LOOMO_CMD_PATH, wait, text[:80])
        await asyncio.sleep(wait)

    async def stream_wav(self, audio_bytes: bytes) -> None:
        """Stream WAV bytes to Go1 speaker via WebSocket (not used on Loomo)."""
        if self.simulated:
            logger.info("[SIM STREAM] %d bytes of audio", len(audio_bytes))
            await asyncio.sleep(min(1.5, len(audio_bytes) / 48000 / 2))
            return

        if self.api == API_LOOMO:
            logger.warning("Loomo uses POST %s speak — pass text to stream_speech()", LOOMO_CMD_PATH)
            return

        await self._go1_play_wav(audio_bytes)

    async def _execute_motion(self, calls: list[HttpCall]) -> None:
        self._motion_abort.clear()
        for call in calls:
            if self._motion_abort.is_set():
                break
            method = call.method.upper()
            path = call.path

            if method == "POST" and path.rstrip("/") == "/api/c2/move" and call.hold_sec > 0:
                await self._hold_move(call.body or {}, call.hold_sec)
                continue

            await self.request(method, path, call.body)
            if call.hold_sec > 0:
                await asyncio.sleep(call.hold_sec)

        await self.stop()

    async def _hold_move(self, body: dict, duration_sec: float) -> None:
        # stop() sets this flag; clear before each new motion (see _execute_motion).
        self._motion_abort.clear()
        if self.api == API_LOOMO:
            await self._loomo_hold_move(body, duration_sec)
            return

        step = 0.1
        elapsed = 0.0
        vx = float(body.get("vx", 0.0))
        vy = float(body.get("vy", 0.0))
        yaw_speed = float(body.get("yaw_speed", 0.0))
        payload = {
            "vx": max(-1.0, min(1.0, vx)),
            "vy": max(-1.0, min(1.0, vy)),
            "yaw_speed": max(-3.0, min(3.0, yaw_speed)),
            "gait_type": int(body.get("gait_type", 1)),
            "foot_raise_height": float(body.get("foot_raise_height", 0.08)),
        }

        while elapsed < duration_sec:
            if self._motion_abort.is_set():
                break
            remaining = duration_sec - elapsed
            scale = 1.0 if remaining >= 0.5 else max(0.2, remaining / 0.5)
            scaled = {
                **payload,
                "vx": max(-1.0, min(1.0, payload["vx"] * scale)),
                "vy": max(-1.0, min(1.0, payload["vy"] * scale)),
                "yaw_speed": max(-3.0, min(3.0, payload["yaw_speed"] * scale)),
            }
            result = await self.request("POST", "/api/c2/move", scaled)
            if result is None and not self.simulated:
                break
            await asyncio.sleep(min(step, remaining))
            elapsed += step

    async def _loomo_hold_move(self, body: dict, duration_sec: float) -> None:
        """One POST /api/cmd move — Loomo keeps going until stop or a new command."""
        vx = float(body.get("vx", 0.0))
        yaw = float(body.get("yaw_speed", 0.0))
        linear = max(-1.0, min(1.0, vx))
        angular = max(-1.0, min(1.0, yaw / 3.0))

        if self.simulated:
            logger.info(
                "[SIM LOOMO] POST %s move linear=%.2f angular=%.2f (%.1fs then stop)",
                LOOMO_CMD_PATH,
                linear,
                angular,
                duration_sec,
            )
            await asyncio.sleep(min(duration_sec, 0.3))
            return

        result = await self._loomo_cmd({"cmd": "move", "linear": linear, "angular": angular})
        if result is None:
            logger.error("Loomo move failed linear=%.2f angular=%.2f", linear, angular)
            return

        logger.info(
            "Loomo moving linear=%.2f angular=%.2f for %.1fs (single %s, then stop)",
            linear,
            angular,
            duration_sec,
            LOOMO_CMD_PATH,
        )
        deadline = asyncio.get_event_loop().time() + duration_sec
        while True:
            if self._motion_abort.is_set():
                break
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.25, remaining))

    async def nudge(
        self,
        direction: str,
        *,
        duration_sec: float = 1.2,
        linear_speed: float = 0.25,
        angular_speed: float = 0.55,
    ) -> None:
        """Short safe move for in-room testing, then stop."""
        direction = (direction or "forward").strip().lower().replace("-", "_")
        duration_sec = max(0.2, min(duration_sec, 3.0))
        linear_speed = max(0.05, min(abs(linear_speed), 0.35))
        angular_speed = max(0.3, min(abs(angular_speed), 0.8))

        linear = 0.0
        angular = 0.0
        if direction == "forward":
            linear = linear_speed
        elif direction == "backward":
            linear = -linear_speed
        elif direction in ("turn_left", "left"):
            angular = angular_speed
        elif direction in ("turn_right", "right"):
            angular = -angular_speed
        else:
            raise ValueError(
                f"Unknown direction {direction!r}. "
                "Use forward, backward, turn_left, turn_right."
            )

        body = {"vx": linear, "vy": 0.0, "yaw_speed": angular * 3.0}
        await self._hold_move(body, duration_sec)
        await self.stop()

    async def _go1_play_wav(self, audio_bytes: bytes) -> None:
        pcm_bytes = _wav_to_pcm_48k(audio_bytes)
        ws_url = (
            self.base_url.replace("https://", "wss://").replace("http://", "ws://")
            + "/api/c2/audio"
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    ws_url,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as ws:
                    try:
                        await asyncio.wait_for(ws.receive(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                    await ws.send_str('{"mode":"talk"}')
                    await asyncio.sleep(0.1)
                    for i in range(0, len(pcm_bytes), 4096):
                        await ws.send_bytes(pcm_bytes[i : i + 4096])
                    duration_sec = len(pcm_bytes) / (48000 * 2)
                    await asyncio.sleep(max(0.3, duration_sec))
                    await ws.send_str('{"mode":"off"}')
        except Exception:
            logger.exception("Go1 audio WebSocket stream failed")

    async def capture_audio(self, duration: float = 5.0) -> bytes | None:
        """Capture microphone audio from the robot."""
        if self.simulated:
            logger.info("[SIM LISTEN] %.1fs", duration)
            await asyncio.sleep(min(duration, 0.3))
            return None

        if self.api == API_LOOMO:
            return await self._loomo_capture_audio()

        return await self._go1_capture_audio(duration)

    async def _loomo_capture_audio(self) -> bytes | None:
        """Loomo: GET /api/audio.wav (~2s 16kHz mono WAV)."""
        data = await self._get_bytes(LOOMO_AUDIO_WAV_PATH)
        if not data:
            logger.warning("Loomo %s returned no data", LOOMO_AUDIO_WAV_PATH)
        return data

    async def _go1_capture_audio(self, duration: float) -> bytes | None:
        ws_url = (
            self.base_url.replace("https://", "wss://").replace("http://", "ws://")
            + "/api/c2/audio"
        )
        pcm_chunks: list[bytes] = []
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    ws_url,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as ws:
                    try:
                        await asyncio.wait_for(ws.receive(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                    await ws.send_str('{"mode":"listen"}')
                    deadline = asyncio.get_event_loop().time() + duration
                    while asyncio.get_event_loop().time() < deadline:
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=0.5)
                            if msg.type == aiohttp.WSMsgType.BINARY:
                                pcm_chunks.append(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                                break
                        except asyncio.TimeoutError:
                            continue
                    await ws.send_str('{"mode":"off"}')
        except Exception:
            logger.exception("Go1 audio WebSocket listen failed")
            return None

        if not pcm_chunks:
            return None

        pcm_data = b"".join(pcm_chunks)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(48000)
            wf.writeframes(pcm_data)
        return buf.getvalue()


def _wav_to_pcm_48k(audio_bytes: bytes) -> bytes:
    buf = io.BytesIO(audio_bytes)
    with wave.open(buf, "rb") as wf:
        src_rate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    if src_rate == 48000:
        return pcm
    samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
    ratio = 48000 / src_rate
    out_len = int(len(samples) * ratio)
    resampled = []
    for i in range(out_len):
        src_i = i / ratio
        lo = int(src_i)
        hi = min(lo + 1, len(samples) - 1)
        frac = src_i - lo
        val = int(samples[lo] * (1 - frac) + samples[hi] * frac)
        resampled.append(max(-32768, min(32767, val)))
    return struct.pack(f"<{len(resampled)}h", *resampled)
