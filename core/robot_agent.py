"""Direct LLM control of the robot — natural language → move / speak / stop."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from core import direct_motion
from core.api_client import _client
from core.gps_motion import linear_speed_kmh, linear_speed_ms
from core.openapi_spec import system_context
from core.route_motion import MotionCommand

if TYPE_CHECKING:
    from core.robot_http import RobotHttpClient

logger = logging.getLogger(__name__)

DEFAULT_LIMITS = {
    "max_distance_m": 200.0,
    "max_duration_sec": 300.0,
    "max_turn_deg": 360.0,
}


class DirectRobotAgent:
    """Turn natural language into direct robot actions via LLM tool calls."""

    def __init__(
        self,
        robot: RobotHttpClient,
        *,
        settings: dict,
        loop: asyncio.AbstractEventLoop,
        model: str,
        robot_id: str = "loomo",
        profile: dict | None = None,
    ) -> None:
        self.robot = robot
        self.settings = settings
        self.loop = loop
        self.model = model
        self._history: list[dict] = []
        self._robot_id = robot_id
        self._profile: dict = profile or {}

    def set_profile(self, robot_id: str, profile: dict) -> None:
        self._robot_id = robot_id
        self._profile = profile

    def _limits(self) -> dict:
        cfg = (self.settings.get("robot_agent") or {}).copy()
        out = dict(DEFAULT_LIMITS)
        for key in out:
            if key in cfg:
                out[key] = float(cfg[key])
        return out

    def _angular_speed(self) -> float:
        nav = self.settings.get("navigation") or {}
        return max(0.3, min(0.8, float(nav.get("gps_angular_speed", 0.55))))

    def _turn_duration_for_degrees(self, degrees: float) -> float:
        nav = self.settings.get("navigation") or {}
        turn_90_sec = float(nav.get("turn_duration_sec", 3.0))
        return (abs(degrees) / 90.0) * turn_90_sec

    def reset_history(self) -> None:
        self._history = []

    def _tool_specs(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "move_straight",
                    "description": (
                        "Move the robot forward or backward for a given distance in metres. "
                        "Duration is computed as distance / configured speed — e.g. 50 m at 3 km/h ≈ 60 s."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "distance_m": {
                                "type": "number",
                                "description": "Distance in metres",
                            },
                            "direction": {
                                "type": "string",
                                "enum": ["forward", "backward"],
                                "description": "Travel direction",
                            },
                        },
                        "required": ["distance_m"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "turn",
                    "description": "Turn the robot left or right by a number of degrees.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "degrees": {
                                "type": "number",
                                "description": "Turn angle in degrees (e.g. 90 for a quarter turn)",
                            },
                            "direction": {
                                "type": "string",
                                "enum": ["left", "right"],
                                "description": "Turn direction",
                            },
                        },
                        "required": ["degrees", "direction"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "speak",
                    "description": "Speak text on the robot's speaker.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "What to say"},
                        },
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "stop",
                    "description": "Emergency stop — halt all motion immediately.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "robot_status",
                    "description": "Get robot connection mode and configured motion speeds.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
        ]

    async def _move_straight(self, distance_m: float, direction: str = "forward") -> str:
        limits = self._limits()
        speed_ms = linear_speed_ms(self.settings)
        speed_kmh = linear_speed_kmh(self.settings)
        direction = (direction or "forward").strip().lower()
        if direction not in ("forward", "backward"):
            return "direction must be forward or backward"

        try:
            distance_m = float(distance_m)
        except (TypeError, ValueError):
            return "distance_m must be a number"

        distance_m = max(0.1, min(distance_m, limits["max_distance_m"]))
        vx = max(0.05, min(speed_ms, 1.0))
        duration = min(distance_m / vx, limits["max_duration_sec"])
        actual_m = vx * duration
        if direction == "backward":
            vx = -vx

        body = {
            "vx": vx,
            "vy": 0.0,
            "yaw_speed": 0.0,
            "gait_type": 1,
            "foot_raise_height": 0.08,
        }

        async with self.robot.motion_lock():
            await self.robot.connect()
            await self.robot._hold_move(body, duration)
            await self.robot._stop_unlocked()

        return (
            f"Moved {direction} {actual_m:.1f} m "
            f"({duration:.1f} s at {abs(vx):.2f} m/s, {speed_kmh:.1f} km/h)."
        )

    async def _turn(self, degrees: float, direction: str = "left") -> str:
        limits = self._limits()
        direction = (direction or "left").strip().lower()
        if direction not in ("left", "right"):
            return "direction must be left or right"

        try:
            degrees = float(degrees)
        except (TypeError, ValueError):
            return "degrees must be a number"

        degrees = max(1.0, min(abs(degrees), limits["max_turn_deg"]))
        duration = min(self._turn_duration_for_degrees(degrees), limits["max_duration_sec"])
        angular = self._angular_speed()
        yaw_speed = angular * 3.0 * (1.0 if direction == "left" else -1.0)
        body = {
            "vx": 0.0,
            "vy": 0.0,
            "yaw_speed": yaw_speed,
            "gait_type": 1,
            "foot_raise_height": 0.08,
        }

        async with self.robot.motion_lock():
            await self.robot.connect()
            await self.robot._hold_move(body, duration)
            await self.robot._stop_unlocked()

        return f"Turned {direction} {degrees:.0f}° ({duration:.1f} s)."

    async def _speak(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return "Nothing to speak."
        async with self.robot.motion_lock():
            await self.robot._stream_speech_unlocked(text)
        return f"Spoke: {text[:80]}"

    async def _stop(self) -> str:
        async with self.robot.motion_lock():
            await self.robot._stop_unlocked()
        return "Robot stopped."

    async def run_route(
        self,
        commands: list[MotionCommand],
        *,
        stop_event: asyncio.Event | None = None,
        on_step: Callable[[int, int, MotionCommand], Awaitable[None] | None] | None = None,
        speak: bool = True,
    ) -> list[str]:
        """Execute a parsed manual route — speak each step first, then move."""
        results: list[str] = []
        total = len(commands)
        for index, command in enumerate(commands, start=1):
            if stop_event and stop_event.is_set():
                break
            if on_step is not None:
                maybe = on_step(index, total, command)
                if asyncio.iscoroutine(maybe):
                    await maybe
            try:
                if speak and command.label.strip():
                    async with self.robot.motion_lock():
                        await self.robot._stream_speech_unlocked(command.label)
                if stop_event and stop_event.is_set():
                    break
                await direct_motion.execute_command(
                    self.robot,
                    self.settings,
                    command,
                    stop_event=stop_event,
                )
                results.append(command.label)
            except Exception as exc:
                logger.exception("Route step %d failed: %s", index, command.label)
                results.append(f"failed: {exc}")
                break
        return results

    def _robot_status(self) -> str:
        limits = self._limits()
        speed_ms = linear_speed_ms(self.settings)
        speed_kmh = linear_speed_kmh(self.settings)
        sim = "simulated" if self.robot.simulated else "live"
        label = self._profile.get("label") or self._robot_id
        return (
            f"Robot: {label} ({self.robot.api}), mode: {sim}, "
            f"base: {self.robot.base_url or '(none)'}. "
            f"Forward speed: {speed_kmh:.1f} km/h ({speed_ms:.2f} m/s). "
            f"Max move: {limits['max_distance_m']:.0f} m / {limits['max_duration_sec']:.0f} s."
        )

    def _openapi_context(self) -> str:
        return system_context(
            self.robot.base_url,
            api=self._profile.get("api") or self.robot.api,
            spec_file=self._profile.get("spec"),
            label=self._profile.get("label"),
        )

    async def _run_tool(self, name: str, args: dict) -> str:
        try:
            if name == "move_straight":
                return await self._move_straight(**args)
            if name == "turn":
                return await self._turn(**args)
            if name == "speak":
                return await self._speak(**args)
            if name == "stop":
                return await self._stop()
            if name == "robot_status":
                return self._robot_status()
            return f"Unknown tool: {name}"
        except Exception as exc:
            logger.exception("Robot tool %s failed", name)
            await self.robot.stop()
            return f"Tool {name} failed: {exc}"

    def _system_prompt(self) -> str:
        speed_kmh = linear_speed_kmh(self.settings)
        speed_ms = linear_speed_ms(self.settings)
        limits = self._limits()
        label = self._profile.get("label") or self._robot_id
        tools_note = (
            "You control the robot via tools (move_straight, turn, speak, stop). "
            "Use the OpenAPI spec below for this robot's REST API.\n"
            f"- Active robot: {label} ({self.robot.api}) at {self.robot.base_url or 'simulated'}\n"
            f"- Forward speed: {speed_kmh:.1f} km/h ({speed_ms:.2f} m/s). "
            "For distance moves use move_straight — duration is computed automatically.\n"
            f"- Example: 'go straight for 50 m' → move_straight(distance_m=50, direction='forward').\n"
            f"- Max single move: {limits['max_distance_m']:.0f} m or {limits['max_duration_sec']:.0f} s.\n"
            "- For compound commands, call tools in sequence (e.g. move then turn).\n"
            "- Confirm what you did in one short sentence. Be concise."
        )
        return tools_note + "\n\n" + self._openapi_context()

    def _goal_with_history(self, user_text: str) -> str:
        if not self._history:
            return user_text
        lines = []
        for msg in self._history[-8:]:
            role = msg.get("role", "user")
            content = (msg.get("content") or "").strip()
            if content:
                lines.append(f"{role}: {content}")
        lines.append(f"user: {user_text}")
        return "Recent conversation:\n" + "\n".join(lines) + "\n\nRespond to the latest user message."

    async def _llm_create(self, client, messages: list[dict], *, tools: bool = True) -> object:
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }
        if tools:
            kwargs["tools"] = self._tool_specs()
        return await asyncio.to_thread(client.chat.completions.create, **kwargs)

    async def _run_tool_loop(self, goal: str, client) -> tuple[str, int]:
        base: list[dict] = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": goal},
        ]
        tool_msgs: list[dict] = []
        steps = 0
        max_steps = 6

        while steps < max_steps:
            resp = await self._llm_create(client, base + tool_msgs[-6:])
            msg = resp.choices[0].message
            calls = getattr(msg, "tool_calls", None) or []
            if not calls:
                return (msg.content or "").strip(), steps

            steps += 1
            call = calls[0]
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                tool_msgs.append({"role": "assistant", "content": f"(invalid tool call {name})"})
                tool_msgs.append({"role": "user", "content": "Answer the user directly in one short sentence."})
                continue

            observation = await self._run_tool(name, args)
            tool_msgs.append({"role": "assistant", "content": f"Called {name}({args})"})
            tool_msgs.append({"role": "user", "content": f"Result: {observation}"})

        final = await self._llm_create(
            client,
            base + tool_msgs[-6:] + [{"role": "user", "content": "Reply briefly to the user."}],
            tools=False,
        )
        return (final.choices[0].message.content or "").strip(), steps

    async def handle(self, user_text: str, *, reset: bool = False) -> dict:
        user_text = (user_text or "").strip()
        if reset:
            self.reset_history()

        if not user_text:
            return {"reply": "Say something — e.g. 'go straight for 50 metres'."}

        try:
            client = _client()
        except RuntimeError as exc:
            return {"reply": str(exc)}

        goal = self._goal_with_history(user_text)
        reply, steps = await self._run_tool_loop(goal, client)

        self._history.append({"role": "user", "content": user_text})
        self._history.append({"role": "assistant", "content": reply})

        return {
            "reply": reply,
            "steps": steps,
            "speed_kmh": linear_speed_kmh(self.settings),
            "speed_ms": round(linear_speed_ms(self.settings), 3),
        }
