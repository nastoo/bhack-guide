"""Distance-based route execution: Google Maps steps → forward / turn motion."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.gps_motion import angle_diff_deg, bearing_deg, linear_speed_ms

if TYPE_CHECKING:
    from core.gps_navigation import RouteStep
    from core.robot_http import RobotHttpClient


def _navigation_settings(settings: dict | None) -> dict:
    return (settings or {}).get("navigation") or {}

logger = logging.getLogger(__name__)

TURN_ACTIONS = frozenset({"turn_left", "turn_right", "uturn_left", "uturn_right"})


@dataclass
class MotionCommand:
    action: str  # forward | turn_left | turn_right | uturn_* | rotate
    label: str
    distance_m: float = 0.0
    source_instruction: str = ""
    maps_step_index: int = 0
    angle_deg: float = 0.0  # signed: + = left (counter-clockwise), for rotate


def motion_config(settings: dict | None) -> dict:
    nav = _navigation_settings(settings)
    return {
        "min_forward_m": max(
            1.0,
            float(
                os.environ.get("NAV_MIN_FORWARD_M")
                or nav.get("min_forward_m")
                or 3.0
            ),
        ),
        "turn_duration_sec": max(
            1.0,
            float(
                os.environ.get("NAV_TURN_DURATION_SEC")
                or nav.get("turn_duration_sec")
                or 3.0
            ),
        ),
        "uturn_duration_sec": max(
            2.0,
            float(
                os.environ.get("NAV_UTURN_DURATION_SEC")
                or nav.get("uturn_duration_sec")
                or 6.0
            ),
        ),
        "max_forward_duration_sec": max(
            30.0,
            float(
                os.environ.get("NAV_MAX_FORWARD_DURATION_SEC")
                or nav.get("max_forward_duration_sec")
                or 300.0
            ),
        ),
        "angular_speed": max(
            0.2,
            min(
                0.8,
                float(
                    os.environ.get("GPS_ANGULAR_SPEED")
                    or nav.get("gps_angular_speed")
                    or 0.55
                ),
            ),
        ),
        "heading_tolerance_deg": max(
            5.0,
            float(
                os.environ.get("NAV_HEADING_TOLERANCE_DEG")
                or nav.get("heading_tolerance_deg")
                or 15
            ),
        ),
    }


def _parse_turn(maneuver: str, instruction: str) -> str | None:
    m = (maneuver or "").lower().replace("-", "_")
    if m:
        if "uturn" in m or "u_turn" in m:
            return "uturn_left" if "left" in m else "uturn_right"
        if "left" in m:
            return "turn_left"
        if "right" in m:
            return "turn_right"

    lower = (instruction or "").lower()
    if "u-turn" in lower or "uturn" in lower:
        return "uturn_left" if "left" in lower else "uturn_right"
    if "turn left" in lower or "left onto" in lower or "bear left" in lower:
        return "turn_left"
    if "turn right" in lower or "right onto" in lower or "bear right" in lower:
        return "turn_right"
    return None


def _forward_label(distance_m: float, lang: str) -> str:
    d = int(round(distance_m))
    if lang == "de":
        return f"{d} Meter geradeaus fahren"
    return f"Go forward for {d} m"


def _turn_label(action: str, lang: str) -> str:
    if lang == "de":
        labels = {
            "turn_left": "Links abbiegen",
            "turn_right": "Rechts abbiegen",
            "uturn_left": "Wenden nach links",
            "uturn_right": "Wenden nach rechts",
        }
    else:
        labels = {
            "turn_left": "Turn left",
            "turn_right": "Turn right",
            "uturn_left": "Make a U-turn to the left",
            "uturn_right": "Make a U-turn to the right",
        }
    return labels.get(action, action.replace("_", " "))


def _rotate_label(angle_deg: float, lang: str) -> str:
    deg = int(round(abs(angle_deg)))
    if lang == "de":
        if angle_deg > 0:
            return f"{deg}° nach links drehen, Richtung Route"
        return f"{deg}° nach rechts drehen, Richtung Route"
    if angle_deg > 0:
        return f"Turn {deg}° left to face the route"
    return f"Turn {deg}° right to face the route"


def _heading_after_turn(action: str, heading_deg: float) -> float:
    if action == "turn_left":
        return (heading_deg - 90) % 360
    if action == "turn_right":
        return (heading_deg + 90) % 360
    if action == "uturn_left":
        return (heading_deg - 180) % 360
    if action == "uturn_right":
        return (heading_deg + 180) % 360
    if action == "rotate":
        return heading_deg  # caller updates separately
    return heading_deg


def _align_to_bearing_command(
    current_heading_deg: float,
    target_bearing_deg: float,
    lang: str,
    settings: dict | None,
    *,
    source_instruction: str = "Face the route",
) -> MotionCommand | None:
    cfg = motion_config(settings)
    diff = angle_diff_deg(current_heading_deg, target_bearing_deg)
    if abs(diff) <= cfg["heading_tolerance_deg"]:
        logger.info(
            "Heading OK: current=%.0f° target=%.0f° (diff=%.0f°)",
            current_heading_deg,
            target_bearing_deg,
            diff,
        )
        return None
    logger.info(
        "Heading align: current=%.0f° → target=%.0f° (rotate %.0f° %s)",
        current_heading_deg,
        target_bearing_deg,
        abs(diff),
        "left" if diff > 0 else "right",
    )
    return MotionCommand(
        action="rotate",
        label=_rotate_label(diff, lang),
        angle_deg=diff,
        source_instruction=source_instruction,
        maps_step_index=0,
    )


def convert_google_maps_steps(
    steps: list[Any],
    lang: str = "en",
    settings: dict | None = None,
    *,
    origin_lat: float | None = None,
    origin_lng: float | None = None,
    origin_heading_deg: float | None = None,
) -> list[MotionCommand]:
    """Turn Google Maps Directions API steps into robot motion commands.

    Each Maps step (instruction + distance + maneuver) becomes one or more of:
    - Turn left / Turn right / U-turn
    - Go forward for N meters
    """
    cfg = motion_config(settings)
    min_forward = cfg["min_forward_m"]
    commands: list[MotionCommand] = []

    nav = _navigation_settings(settings)
    heading = (
        origin_heading_deg
        if origin_heading_deg is not None
        else float(nav.get("origin_heading_deg", 0)) % 360
    )

    if steps and origin_lat is not None and origin_lng is not None:
        first = steps[0]
        route_bearing = bearing_deg(
            origin_lat,
            origin_lng,
            float(first.end_lat),
            float(first.end_lng),
        )
        align = _align_to_bearing_command(
            heading,
            route_bearing,
            lang,
            settings,
            source_instruction="Initial alignment to route",
        )
        if align:
            commands.append(align)
            heading = route_bearing

    for step_index, step in enumerate(steps, start=1):
        step_cmds: list[MotionCommand] = []
        instruction = str(getattr(step, "instruction", "") or "")
        maneuver = str(getattr(step, "maneuver", "") or "")
        distance_text = str(getattr(step, "distance", "") or "")

        turn = _parse_turn(maneuver, instruction)
        if turn:
            step_cmds.append(
                MotionCommand(
                    action=turn,
                    label=_turn_label(turn, lang),
                    source_instruction=instruction,
                    maps_step_index=step_index,
                )
            )
            heading = _heading_after_turn(turn, heading)

        distance_m = max(0.0, float(getattr(step, "distance_m", 0) or 0))
        if distance_m >= min_forward:
            step_cmds.append(
                MotionCommand(
                    action="forward",
                    label=_forward_label(distance_m, lang),
                    distance_m=distance_m,
                    source_instruction=instruction,
                    maps_step_index=step_index,
                )
            )
            if step_index == 1 and origin_lat is not None and origin_lng is not None:
                prev_lat, prev_lng = origin_lat, origin_lng
            elif step_index > 1:
                prev = steps[step_index - 2]
                prev_lat, prev_lng = float(prev.end_lat), float(prev.end_lng)
            else:
                prev_lat = prev_lng = None
            if prev_lat is not None:
                heading = bearing_deg(
                    prev_lat, prev_lng, float(step.end_lat), float(step.end_lng)
                )

        if step_cmds:
            logger.info(
                "Google Maps step %d → %s | %r (%s) [heading≈%.0f°]",
                step_index,
                " + ".join(c.label for c in step_cmds),
                instruction[:80],
                distance_text or f"{distance_m:.0f} m",
                heading,
            )
            commands.extend(step_cmds)
        else:
            logger.info(
                "Google Maps step %d → (skipped, <%sm) | %r",
                step_index,
                min_forward,
                instruction[:80],
            )

    return commands


def steps_to_commands(
    steps: list[Any],
    lang: str = "en",
    settings: dict | None = None,
) -> list[MotionCommand]:
    """Alias for convert_google_maps_steps."""
    return convert_google_maps_steps(steps, lang, settings)


def summarize_commands(commands: list[MotionCommand]) -> str:
    return " → ".join(cmd.label for cmd in commands)


async def _run_hold_interruptible(
    robot: RobotHttpClient,
    body: dict,
    duration_sec: float,
    *,
    stop_event: asyncio.Event,
) -> None:
    """Hold motion for duration_sec; stop early if navigation is cancelled."""
    if stop_event.is_set() or duration_sec <= 0:
        return

    hold = asyncio.create_task(robot._hold_move(body, duration_sec))
    try:
        while not hold.done():
            if stop_event.is_set():
                await robot.stop()
                return
            await asyncio.sleep(0.15)
        await hold
    finally:
        await robot.stop()


async def execute_command(
    robot: RobotHttpClient,
    settings: dict,
    command: MotionCommand,
    *,
    stop_event: asyncio.Event,
) -> None:
    """Run one distance-based motion command (timed forward or turn)."""
    if stop_event.is_set():
        return

    cfg = motion_config(settings)
    speed_ms = linear_speed_ms(settings)
    angular = cfg["angular_speed"]
    gait = {"gait_type": 1, "foot_raise_height": 0.08}

    if command.action == "forward":
        distance = abs(command.distance_m)
        duration = distance / speed_ms if speed_ms > 0 else 0.0
        duration = min(duration, cfg["max_forward_duration_sec"])
        vx = speed_ms if command.distance_m >= 0 else -speed_ms
        direction = "forward" if command.distance_m >= 0 else "backward"
        logger.info(
            "Distance motion: %s %.0fm at %.1f km/h (%.1fs) — %s",
            direction,
            distance,
            speed_ms * 3.6,
            duration,
            command.source_instruction[:60],
        )
        body = {"vx": vx, "vy": 0.0, "yaw_speed": 0.0, **gait}
        await _run_hold_interruptible(robot, body, duration, stop_event=stop_event)
        return

    if command.action == "rotate":
        angle = float(command.angle_deg or 0)
        if abs(angle) < 1:
            return
        duration = cfg["turn_duration_sec"] * (abs(angle) / 90.0)
        duration = max(0.5, min(duration, cfg["uturn_duration_sec"] * 2))
        sign = 1.0 if angle > 0 else -1.0
        yaw_speed = sign * angular * 3.0
        logger.info(
            "Distance motion: rotate %.0f° for %.1fs — %s",
            angle,
            duration,
            command.source_instruction[:60],
        )
        body = {"vx": 0.0, "vy": 0.0, "yaw_speed": yaw_speed, **gait}
        await _run_hold_interruptible(robot, body, duration, stop_event=stop_event)
        return

    if command.action in TURN_ACTIONS:
        if command.action.startswith("uturn"):
            duration = cfg["uturn_duration_sec"]
        else:
            duration = cfg["turn_duration_sec"]

        sign = 1.0 if "left" in command.action else -1.0
        yaw_speed = sign * angular * 3.0
        logger.info(
            "Distance motion: %s for %.1fs — %s",
            command.action,
            duration,
            command.source_instruction[:60],
        )
        body = {"vx": 0.0, "vy": 0.0, "yaw_speed": yaw_speed, **gait}
        await _run_hold_interruptible(robot, body, duration, stop_event=stop_event)
        return

    logger.warning("Unknown motion command action: %s", command.action)
