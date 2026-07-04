"""GPS-guided robot motion: move and turn until a Maps step waypoint is reached."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from typing import TYPE_CHECKING

from core import gps_stream_client

if TYPE_CHECKING:
    from core.gps_navigation import RouteStep
    from core.robot_http import RobotHttpClient

logger = logging.getLogger(__name__)


def _navigation_settings(settings: dict | None) -> dict:
    return (settings or {}).get("navigation") or {}


_EARTH_RADIUS_M = 6_371_000
DEFAULT_SPEED_KMH = 3.0
MAX_SPEED_KMH = 6.0


def _kmh_to_ms(kmh: float) -> float:
    return kmh / 3.6


def linear_speed_kmh(settings: dict | None) -> float:
    """Configured forward speed in km/h (default 3)."""
    nav = _navigation_settings(settings)
    env_kmh = os.environ.get("GPS_SPEED_KMH", "").strip()
    if env_kmh:
        kmh = float(env_kmh)
    elif nav.get("gps_speed_kmh") is not None:
        kmh = float(nav["gps_speed_kmh"])
    elif os.environ.get("GPS_LINEAR_SPEED") or nav.get("gps_linear_speed"):
        # Legacy m/s setting — still supported
        ms = float(os.environ.get("GPS_LINEAR_SPEED") or nav["gps_linear_speed"])
        kmh = ms * 3.6
    else:
        kmh = DEFAULT_SPEED_KMH
    return max(0.5, min(MAX_SPEED_KMH, kmh))


def linear_speed_ms(settings: dict | None) -> float:
    """Forward speed in m/s for robot vx / Loomo linear."""
    return _kmh_to_ms(linear_speed_kmh(settings))


def motion_settings(settings: dict | None) -> dict:
    nav = _navigation_settings(settings)
    speed_kmh = linear_speed_kmh(settings)
    speed_ms = _kmh_to_ms(speed_kmh)
    return {
        "arrival_radius_m": max(
            3.0,
            float(
                os.environ.get("GPS_ARRIVAL_RADIUS_M")
                or nav.get("gps_arrival_radius_m")
                or 10
            ),
        ),
        "turn_tolerance_deg": max(
            10.0,
            float(
                os.environ.get("GPS_TURN_TOLERANCE_DEG")
                or nav.get("gps_turn_tolerance_deg")
                or 30
            ),
        ),
        "step_timeout_sec": max(
            15.0,
            float(
                os.environ.get("GPS_STEP_TIMEOUT_SEC")
                or nav.get("gps_step_timeout_sec")
                or 120
            ),
        ),
        "poll_interval_sec": max(
            0.25,
            float(
                os.environ.get("GPS_POLL_INTERVAL_SEC")
                or nav.get("gps_poll_interval_sec")
                or 0.5
            ),
        ),
        "linear_speed": max(0.05, min(1.0, speed_ms)),
        "linear_speed_kmh": speed_kmh,
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
    }


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lng2 - lng1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
        dlambda
    )
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def angle_diff_deg(from_deg: float, to_deg: float) -> float:
    """Signed shortest turn from `from_deg` to `to_deg` (-180 … 180)."""
    return (to_deg - from_deg + 540) % 360 - 180


def _turn_hint(instruction: str) -> str | None:
    lower = (instruction or "").lower()
    if "u-turn" in lower or "uturn" in lower:
        return "uturn"
    if "turn left" in lower or "left onto" in lower or "bear left" in lower:
        return "left"
    if "turn right" in lower or "right onto" in lower or "bear right" in lower:
        return "right"
    return None


def is_available(settings: dict | None) -> bool:
    """True when phone_gps is configured and returning a live fix."""
    if not gps_stream_client.is_configured(settings):
        return False
    fix = gps_stream_client.fetch_latest(settings, quiet=True)
    return gps_stream_client.is_live(fix, settings)


def _live_fix(settings: dict | None) -> dict | None:
    fix = gps_stream_client.fetch_latest(settings, quiet=True)
    if fix and gps_stream_client.is_live(fix, settings):
        return fix
    return None


async def execute_gps_step(
    robot: RobotHttpClient,
    settings: dict,
    step: RouteStep,
    *,
    stop_event: asyncio.Event,
) -> None:
    """Drive toward step.end_lat/lng using live phone GPS; stop at waypoint or timeout."""
    cfg = motion_settings(settings)
    target_lat, target_lng = step.end_lat, step.end_lng
    instruction = step.instruction
    turn_hint = _turn_hint(instruction)
    start = time.time()
    last_lat: float | None = None
    last_lng: float | None = None
    last_log = 0.0

    logger.info(
        "GPS motion start: target=(%.5f, %.5f) arrival=%.0fm speed=%.1f km/h — %s",
        target_lat,
        target_lng,
        cfg["arrival_radius_m"],
        cfg["linear_speed_kmh"],
        instruction[:80],
    )

    while not stop_event.is_set():
        elapsed = time.time() - start
        if elapsed > cfg["step_timeout_sec"]:
            logger.warning(
                "GPS motion timeout after %.0fs (target %.5f, %.5f)",
                elapsed,
                target_lat,
                target_lng,
            )
            break

        fix = _live_fix(settings)
        if not fix:
            logger.warning("GPS fix lost during step — stopping robot motion")
            break

        lat = float(fix["lat"])
        lng = float(fix["lng"])
        dist = haversine_m(lat, lng, target_lat, target_lng)

        if dist <= cfg["arrival_radius_m"]:
            logger.info(
                "GPS waypoint reached: %.1fm from target (%.5f, %.5f)",
                dist,
                target_lat,
                target_lng,
            )
            break

        desired = bearing_deg(lat, lng, target_lat, target_lng)
        heading = fix.get("heading")
        vx, yaw_speed = 0.0, 0.0

        if heading is not None:
            diff = angle_diff_deg(float(heading), desired)
            if abs(diff) > cfg["turn_tolerance_deg"]:
                sign = 1.0 if diff > 0 else -1.0
                if turn_hint == "left":
                    sign = abs(sign)
                elif turn_hint == "right":
                    sign = -abs(sign)
                yaw_speed = sign * cfg["angular_speed"] * 3.0
            else:
                vx = cfg["linear_speed"]
                yaw_speed = max(-1.0, min(1.0, diff / 45.0 * cfg["angular_speed"] * 3.0))
        elif last_lat is not None and last_lng is not None:
            travel = bearing_deg(last_lat, last_lng, lat, lng)
            diff = angle_diff_deg(travel, desired)
            if abs(diff) > cfg["turn_tolerance_deg"]:
                sign = 1.0 if diff > 0 else -1.0
                if turn_hint == "left":
                    sign = abs(sign)
                elif turn_hint == "right":
                    sign = -abs(sign)
                yaw_speed = sign * cfg["angular_speed"] * 3.0
            else:
                vx = cfg["linear_speed"]
        elif turn_hint in ("left", "right", "uturn"):
            sign = 1.0 if turn_hint == "left" else -1.0
            if turn_hint == "uturn":
                sign = 1.0
            yaw_speed = sign * cfg["angular_speed"] * 3.0
        else:
            vx = cfg["linear_speed"]

        now = time.time()
        if now - last_log >= 2.0:
            logger.info(
                "GPS motion: pos=(%.5f, %.5f) dist=%.1fm bearing=%.0f° vx=%.2f yaw=%.2f",
                lat,
                lng,
                dist,
                desired,
                vx,
                yaw_speed,
            )
            last_log = now

        last_lat, last_lng = lat, lng
        await robot.set_motion(vx=vx, vy=0.0, yaw_speed=yaw_speed)
        await asyncio.sleep(cfg["poll_interval_sec"])

    await robot.stop()
    logger.info("GPS motion stopped for step: %s", instruction[:60])
