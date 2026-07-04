"""GPS navigation loop: Maps route → motion commands → robot HTTP + audio."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from core import gps_navigation
from core import gps_motion
from core import gps_stream_client
from core import direct_motion
from core import manual_route
from core import route_motion
from core.robot_http import RobotHttpClient

logger = logging.getLogger(__name__)

UpdateCallback = Callable[[dict], Awaitable[None] | None]


@dataclass
class NavigationState:
    active: bool = False
    destination: str = ""
    place_name: str = ""
    place_address: str = ""
    step_index: int = 0
    step_total: int = 0
    current_instruction: str = ""
    last_speech: str = ""
    last_http: str = ""
    status: str = "idle"
    message: str = "Ready for GPS navigation"
    origin_label: str = ""
    origin_heading_deg: float = 0.0
    total_distance: str = ""
    total_duration: str = ""
    motion_mode: str = "distance"
    route_provider: str = "google_maps"
    maps_step_total: int = 0
    phone_lat: float | None = None
    phone_lng: float | None = None
    phone_live: bool = False
    location_source: str = "unknown"


class NavigationLoop:
    """Plan a Google Maps walking route and execute motion commands on the robot."""

    def __init__(
        self,
        settings: dict,
        robot_client: RobotHttpClient,
        *,
        model: str,
        on_update: UpdateCallback | None = None,
    ) -> None:
        self.settings = settings
        self.robot = robot_client
        self.model = model
        self.on_update = on_update
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._state = NavigationState()

    @property
    def state(self) -> NavigationState:
        return self._state

    def stop(self) -> None:
        self._stop_event.set()

    async def start(self, destination: str) -> NavigationState:
        return await self._begin(destination, mode="gps")

    async def start_manual(
        self,
        name: str,
        commands: list[route_motion.MotionCommand],
    ) -> NavigationState:
        if not commands:
            raise ValueError("Manual route has no steps")
        return await self._begin(name, mode="manual", commands=commands)

    async def _begin(
        self,
        destination: str,
        *,
        mode: str,
        commands: list[route_motion.MotionCommand] | None = None,
    ) -> NavigationState:
        if self._task and not self._task.done():
            self.stop()
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        self._stop_event = asyncio.Event()
        self._state = NavigationState(active=True, destination=destination, status="planning")
        if mode == "manual" and commands:
            total_distance, _ = manual_route.summarize_route(commands)
            self._state.route_provider = "manual"
            self._state.motion_mode = "distance"
            self._state.place_name = destination
            self._state.step_total = len(commands)
            self._state.total_distance = total_distance
            self._state.total_duration = f"{len(commands)} steps"
        await self._emit()
        if mode == "manual":
            self._task = asyncio.create_task(self._run_manual(destination, commands or []))
        else:
            self._task = asyncio.create_task(self._run(destination))
        return self.state

    async def _run_manual(
        self,
        name: str,
        commands: list[route_motion.MotionCommand],
    ) -> None:
        total_distance, summary = manual_route.summarize_route(commands)

        self._state.place_name = name
        self._state.place_address = ""
        self._state.total_distance = total_distance
        self._state.total_duration = f"{len(commands)} steps"
        self._state.motion_mode = "distance"
        self._state.route_provider = "manual"
        self._state.maps_step_total = 0
        self._state.step_total = len(commands)
        self._state.status = "connecting"
        await self._emit()

        if not await self.robot.connect():
            self._state.status = "error"
            self._state.message = "Could not connect to robot controller"
            self._state.active = False
            await self._emit()
            return

        logger.info("Manual route: %s | %s", intro := f"{name} — {len(commands)} steps, {total_distance}", summary)
        self._state.message = f"Running manual route ({len(commands)} steps)…"
        await self._emit()

        await self._run_manual_commands(commands)

        if not self._stop_event.is_set():
            self._state.status = "arrived"
            self._state.message = f"Manual route {name!r} complete."
        else:
            self._state.status = "stopped"
            self._state.message = "Navigation stopped"

        async with self.robot.motion_lock():
            await self.robot._stop_unlocked()
        self._state.active = False
        await self._emit()

    async def _run(self, destination: str) -> None:
        lang = gps_navigation.language(self.settings)
        phrases = gps_navigation.guidance_phrases(lang)
        use_live_gps = gps_navigation.live_gps_enabled(self.settings)

        try:
            route = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: gps_navigation.plan_route(destination, self.settings),
            )
        except asyncio.CancelledError:
            await self.robot.stop()
            raise
        except Exception as exc:
            logger.exception("Route planning failed")
            self._state.status = "error"
            self._state.message = str(exc)
            self._state.active = False
            await self._emit()
            return

        self._state.place_name = route.place.name
        self._state.place_address = route.place.address
        self._state.origin_label = route.origin_label
        self._state.origin_heading_deg = route.origin_heading_deg
        self._state.total_distance = route.total_distance
        self._state.total_duration = route.total_duration
        self._state.motion_mode = route.motion_mode
        self._state.route_provider = route.route_provider
        self._state.maps_step_total = len(route.steps)

        use_gps_motion = False
        if use_live_gps:
            use_gps_motion = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: gps_motion.is_available(self.settings),
            )
            if use_gps_motion:
                self._state.motion_mode = "gps"
                self._state.step_total = len(route.steps)
            else:
                logger.warning(
                    "NAVIGATION_USE_LIVE_GPS is on but phone GPS is not live — "
                    "using distance-based motion commands"
                )
                self._state.motion_mode = "distance"

        if not use_gps_motion:
            self._state.step_total = len(route.motion_commands)

        self._state.status = "connecting"
        await self._emit()

        if not await self.robot.connect():
            self._state.status = "error"
            self._state.message = "Could not connect to robot controller"
            self._state.active = False
            await self._emit()
            return

        intro_log = phrases["intro"].format(
            origin=route.origin_label,
            dest=route.place.name,
            dist=route.total_distance,
            duration=route.total_duration,
        )
        if route.place.address:
            intro_log += phrases["destination"].format(address=route.place.address)
        if not use_gps_motion:
            summary = route_motion.summarize_commands(route.motion_commands)
            intro_log += f" Motion plan: {summary}."
        logger.info("Route intro: %s", intro_log)

        intro = gps_navigation.route_intro_speech(
            route.place.name,
            route.total_distance,
            route.total_duration,
            lang,
        )
        await self._execute_speech_only(intro, action_label="intro")

        if use_gps_motion:
            await self._run_gps_steps(route)
        else:
            await self._run_distance_commands(route.motion_commands)

        if not self._stop_event.is_set():
            self._state.status = "arrived"
            self._state.message = f"Arrived at {route.place.name}."
            await self._execute_speech_only(
                phrases["arrived_speech"].format(dest=route.place.name),
                action_label="arrived",
            )
        else:
            await self.robot.stop()
            self._state.status = "stopped"
            self._state.message = "Navigation stopped"

        await self.robot.disconnect()

        self._state.active = False
        await self._emit()

    async def _run_manual_commands(self, commands: list[route_motion.MotionCommand]) -> None:
        """Execute manual route steps — motion only, same path as /robot."""
        for index, command in enumerate(commands, start=1):
            if self._stop_event.is_set():
                break

            self._state.step_index = index
            self._state.current_instruction = command.label
            self._state.status = "navigating"
            self._state.last_speech = command.label
            self._state.last_http = (
                f"rotate {command.angle_deg:.0f}°"
                if command.action == "rotate"
                else (
                    f"{'backward' if command.distance_m < 0 else 'forward'} {abs(command.distance_m):.0f}m"
                    if command.action == "forward"
                    else command.action
                )
            )
            self._state.message = command.label
            await self._emit()

            logger.info("Manual step %d/%d: %s", index, len(commands), command.label)
            await direct_motion.execute_command(
                self.robot,
                self.settings,
                command,
                stop_event=self._stop_event,
            )

    async def _run_distance_commands(self, commands: list[route_motion.MotionCommand]) -> None:
        for index, command in enumerate(commands, start=1):
            if self._stop_event.is_set():
                break

            self._state.step_index = index
            self._state.current_instruction = command.label
            self._state.status = "navigating"
            self._state.last_speech = command.label
            self._state.last_http = (
                f"rotate {command.angle_deg:.0f}°"
                if command.action == "rotate"
                else (
                    f"{'backward' if command.distance_m < 0 else 'forward'} {abs(command.distance_m):.0f}m"
                    if command.action == "forward"
                    else command.action
                )
            )
            self._state.message = command.label
            await self._emit()

            logger.info("Navigation step_%d (distance): %s", index, command.label)

            speech_task = asyncio.create_task(self.robot.stream_speech(command.label))
            motion_task = asyncio.create_task(
                route_motion.execute_command(
                    self.robot,
                    self.settings,
                    command,
                    stop_event=self._stop_event,
                )
            )
            await asyncio.gather(speech_task, motion_task)

    async def _run_gps_steps(self, route: gps_navigation.RoutePlan) -> None:
        """Experimental: live phone GPS drives move/turn per Maps step."""
        for index, step in enumerate(route.steps, start=1):
            if self._stop_event.is_set():
                break

            self._state.step_index = index
            self._state.current_instruction = step.instruction
            self._state.status = "navigating"
            self._state.last_speech = step.instruction
            self._state.last_http = f"GPS → ({step.end_lat:.5f}, {step.end_lng:.5f})"
            self._state.message = step.instruction
            await self._emit()

            logger.info("Navigation step_%d (gps): %s", index, step.instruction[:80])

            speech_task = asyncio.create_task(self.robot.stream_speech(step.instruction))
            motion_task = asyncio.create_task(
                gps_motion.execute_gps_step(
                    self.robot,
                    self.settings,
                    step,
                    stop_event=self._stop_event,
                )
            )
            await asyncio.gather(speech_task, motion_task)

    async def _execute_speech_only(self, speech: str, *, action_label: str) -> None:
        self._state.last_speech = speech
        self._state.last_http = "(speech only)"
        self._state.message = speech
        await self._emit()

        if self._stop_event.is_set():
            return

        await self.robot.stream_speech(speech)
        logger.info("Navigation %s: speech only", action_label)

    async def _refresh_phone_state(self) -> None:
        if not gps_navigation.live_gps_enabled(self.settings):
            self._state.phone_live = False
            if not gps_stream_client.is_configured(self.settings):
                self._state.location_source = "configured origin (live GPS off)"
            else:
                self._state.location_source = "configured origin (live GPS off)"
            return

        if not gps_stream_client.is_configured(self.settings):
            self._state.phone_live = False
            self._state.location_source = "configured origin (GPS_STREAM_URL not set)"
            return

        cfg = gps_stream_client.stream_settings(self.settings)
        fix = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: gps_stream_client.fetch_latest(self.settings, quiet=True),
        )
        if fix and gps_stream_client.is_live(fix, self.settings):
            self._state.phone_lat = float(fix["lat"])
            self._state.phone_lng = float(fix["lng"])
            self._state.phone_live = True
            self._state.location_source = "phone GPS (live)"
            logger.debug(
                "Phone GPS live: (%.5f, %.5f) from %s",
                self._state.phone_lat,
                self._state.phone_lng,
                cfg["url"],
            )
        else:
            self._state.phone_live = False
            origin_lat, origin_lng, origin_label = gps_navigation.origin(self.settings)
            self._state.location_source = f"configured origin ({origin_label})"

    async def _emit(self) -> None:
        await self._refresh_phone_state()
        if self.on_update is None:
            return
        payload = {"type": "navigation_update", **self._state.__dict__}
        result = self.on_update(payload)
        if asyncio.iscoroutine(result):
            await result
