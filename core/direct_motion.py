"""Shared distance-based motion — same logic as /robot DirectRobotAgent."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from core.gps_motion import linear_speed_ms
from core.route_motion import TURN_ACTIONS, MotionCommand, motion_config

if TYPE_CHECKING:
    from core.robot_http import RobotHttpClient

logger = logging.getLogger(__name__)

_GAIT = {"gait_type": 1, "foot_raise_height": 0.08}


def _angular_speed(settings: dict | None) -> float:
    nav = (settings or {}).get("navigation") or {}
    return max(0.3, min(0.8, float(nav.get("gps_angular_speed", 0.55))))


def _turn_duration_for_degrees(settings: dict | None, degrees: float) -> float:
    nav = (settings or {}).get("navigation") or {}
    turn_90_sec = float(nav.get("turn_duration_sec", 3.0))
    return (abs(degrees) / 90.0) * turn_90_sec


def _max_forward_duration(settings: dict | None) -> float:
    cfg = motion_config(settings)
    return cfg["max_forward_duration_sec"]


async def _hold_and_stop(
    robot: RobotHttpClient,
    body: dict,
    duration_sec: float,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    if duration_sec <= 0:
        return
    if stop_event and stop_event.is_set():
        return

    async with robot.motion_lock():
        await robot.connect()
        hold = asyncio.create_task(robot._hold_move(body, duration_sec))
        try:
            while not hold.done():
                if stop_event and stop_event.is_set():
                    await robot._stop_unlocked()
                    return
                await asyncio.sleep(0.15)
            await hold
        finally:
            await robot._stop_unlocked()


async def execute_command(
    robot: RobotHttpClient,
    settings: dict,
    command: MotionCommand,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run one motion step using _hold_move — matches /robot move_straight and turn."""
    if stop_event and stop_event.is_set():
        return

    speed_ms = linear_speed_ms(settings)
    angular = _angular_speed(settings)
    max_forward = _max_forward_duration(settings)

    if command.action == "forward":
        distance = abs(command.distance_m)
        vx = max(0.05, min(speed_ms, 1.0))
        duration = min(distance / vx if vx > 0 else 0.0, max_forward)
        if command.distance_m < 0:
            vx = -vx
        body = {"vx": vx, "vy": 0.0, "yaw_speed": 0.0, **_GAIT}
        logger.info(
            "Direct motion: %s %.1fm %.1fs (%.2f m/s) — %s",
            "backward" if command.distance_m < 0 else "forward",
            distance,
            duration,
            abs(vx),
            command.source_instruction[:60],
        )
        await _hold_and_stop(robot, body, duration, stop_event=stop_event)
        return

    if command.action == "rotate":
        angle = float(command.angle_deg or 0)
        if abs(angle) < 1:
            return
        duration = max(
            0.5,
            min(
                _turn_duration_for_degrees(settings, abs(angle)),
                motion_config(settings)["uturn_duration_sec"] * 2,
            ),
        )
        sign = 1.0 if angle > 0 else -1.0
        yaw_speed = sign * angular * 3.0
        body = {"vx": 0.0, "vy": 0.0, "yaw_speed": yaw_speed, **_GAIT}
        logger.info(
            "Direct motion: rotate %.0f° %.1fs — %s",
            angle,
            duration,
            command.source_instruction[:60],
        )
        await _hold_and_stop(robot, body, duration, stop_event=stop_event)
        return

    if command.action in TURN_ACTIONS:
        cfg = motion_config(settings)
        duration = (
            cfg["uturn_duration_sec"]
            if command.action.startswith("uturn")
            else cfg["turn_duration_sec"]
        )
        sign = 1.0 if "left" in command.action else -1.0
        yaw_speed = sign * angular * 3.0
        body = {"vx": 0.0, "vy": 0.0, "yaw_speed": yaw_speed, **_GAIT}
        logger.info(
            "Direct motion: %s %.1fs — %s",
            command.action,
            duration,
            command.source_instruction[:60],
        )
        await _hold_and_stop(robot, body, duration, stop_event=stop_event)
        return

    logger.warning("Unknown motion command: %s", command.action)
