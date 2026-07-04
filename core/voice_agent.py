"""LLM voice/text agent — controls navigation via tool calls only."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from core import tool_loop
from core.api_client import _client

if TYPE_CHECKING:
    from core.navigation_loop import NavigationLoop
    from core.robot_http import RobotHttpClient

logger = logging.getLogger(__name__)


class VoiceAgent:
    """Turn natural language into navigation actions via LLM tool calls."""

    def __init__(
        self,
        navigation_loop: NavigationLoop,
        robot: RobotHttpClient,
        *,
        loop: asyncio.AbstractEventLoop,
        model: str,
    ) -> None:
        self.navigation_loop = navigation_loop
        self.robot = robot
        self.loop = loop
        self.model = model

    def _run_async(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=120)

    def _tools(self) -> list[tool_loop.Tool]:
        def start_navigation(destination: str) -> str:
            try:
                state = self._run_async(self.navigation_loop.start(destination))
                return (
                    f"Started navigation to {state.place_name or destination}. "
                    f"Route: {state.total_distance}, {state.total_duration}. Status: {state.status}."
                )
            except Exception as exc:
                return f"Could not start navigation: {exc}"

        def stop_navigation() -> str:
            self.navigation_loop.stop()
            self._run_async(self.navigation_loop.robot.stop())
            return "Navigation stopped."

        def navigation_status() -> str:
            state = self.navigation_loop.state
            if not state.active:
                return "No active navigation. Ask where they want to go."
            return (
                f"Navigating to {state.place_name or state.destination}. "
                f"Step {state.step_index}/{state.step_total}: {state.current_instruction}. "
                f"Last speech: {state.last_speech}"
            )

        return [
            tool_loop.Tool(
                name="start_navigation",
                description=(
                    "Start GPS turn-by-turn navigation to a place name or address. "
                    "The LLM plans robot HTTP calls (move/stop) and streams spoken guidance."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "destination": {
                            "type": "string",
                            "description": "Place name or address",
                        }
                    },
                    "required": ["destination"],
                },
                fn=lambda destination: start_navigation(destination),
            ),
            tool_loop.Tool(
                name="stop_navigation",
                description="Stop navigation immediately.",
                parameters={"type": "object", "properties": {}, "required": []},
                fn=lambda: stop_navigation(),
            ),
            tool_loop.Tool(
                name="navigation_status",
                description="Get current navigation status.",
                parameters={"type": "object", "properties": {}, "required": []},
                fn=lambda: navigation_status(),
            ),
        ]

    def _system_prompt(self) -> str:
        return (
            "You are a friendly guide dog helping a person walk outdoors. "
            "When they ask to go somewhere, call start_navigation with the destination. "
            "When they ask to stop, call stop_navigation. "
            "Keep replies to one or two short natural sentences."
        )

    async def handle(self, user_text: str) -> dict:
        user_text = (user_text or "").strip()
        if not user_text:
            return {"heard": "", "reply": "I did not catch that."}

        try:
            client = _client()
        except RuntimeError as exc:
            return {"heard": user_text, "reply": str(exc)}

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: tool_loop.run(
                user_text,
                self._tools(),
                client=client,
                model=self.model,
                system=self._system_prompt(),
                max_steps=4,
            ),
        )

        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._maybe_speak(result.final_text),
        )

        return {
            "heard": user_text,
            "reply": result.final_text,
            "steps": result.steps,
        }

    def _maybe_speak(self, text: str) -> None:
        # Replies show in the UI only — speaking on Loomo blocks motion commands.
        return
