"""Google Maps turn-by-turn walking routes for GPS navigation."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_GOOGLE_MAPS_BASE = "https://maps.googleapis.com/maps/api"

_simulated_lat: float | None = None
_simulated_lng: float | None = None


@dataclass
class Place:
    name: str
    address: str
    lat: float
    lng: float
    place_id: str


@dataclass
class RouteStep:
    instruction: str
    distance: str
    distance_m: float
    duration: str
    maneuver: str
    end_lat: float
    end_lng: float


@dataclass
class RoutePlan:
    place: Place
    steps: list[RouteStep]
    total_distance: str
    total_duration: str
    origin_label: str
    origin_lat: float
    origin_lng: float
    origin_heading_deg: float = 0.0
    motion_commands: list[Any] = field(default_factory=list)
    motion_mode: str = "distance"
    route_provider: str = "google_maps"


def navigation_settings(settings: dict | None = None) -> dict:
    if settings is None:
        return {}
    return settings.get("navigation") or {}


def simulate_progress_enabled(settings: dict) -> bool:
    nav = navigation_settings(settings)
    enabled = bool(nav.get("simulate_progress", False))
    env = os.environ.get("NAVIGATION_SIMULATE_PROGRESS")
    if env is not None:
        enabled = env.strip().lower() in ("1", "true", "yes", "on")
    return enabled


def live_gps_enabled(settings: dict) -> bool:
    """When true, use phone_gps for origin + GPS-guided step motion (experimental)."""
    nav = navigation_settings(settings)
    env = os.environ.get("NAVIGATION_USE_LIVE_GPS")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    return bool(nav.get("use_live_gps", False))


def origin(settings: dict) -> tuple[float, float, str]:
    nav = navigation_settings(settings)
    lat = float(nav.get("origin_lat", 53.8659))
    lng = float(nav.get("origin_lng", 10.6866))
    label = str(nav.get("origin_label", "configured origin"))
    return lat, lng, label


def origin_heading_deg(settings: dict) -> float:
    """Compass bearing the robot faces at the configured origin (0°=north, 90°=east)."""
    nav = navigation_settings(settings)
    env = os.environ.get("NAV_ORIGIN_HEADING_DEG", "").strip()
    if env:
        return float(env) % 360
    return float(nav.get("origin_heading_deg", 0)) % 360


def step_pause_seconds(settings: dict) -> float:
    nav = navigation_settings(settings)
    return max(0.0, float(nav.get("step_pause_seconds", 2.0)))


def language(settings: dict) -> str:
    nav = navigation_settings(settings)
    lang = str(nav.get("language", "en")).strip().lower() or "en"
    env = os.environ.get("NAVIGATION_LANGUAGE")
    if env is not None and env.strip():
        lang = env.strip().lower()
    return lang.split("-")[0]


def guidance_phrases(lang: str) -> dict[str, str]:
    if lang == "de":
        return {
            "intro": "Route von {origin} nach {dest}. Gesamt {dist}, etwa {duration} zu Fuß.",
            "intro_speech": "Navigation nach {dest}. {dist}, {duration}.",
            "destination": " Ziel: {address}.",
            "step": "Schritt {i}: {instruction}",
            "arrived_speech": "Ziel {dest} erreicht.",
        }
    return {
        "intro": "Route from {origin} to {dest}. Total {dist}, about {duration} on foot.",
        "intro_speech": "Navigating to {dest}. {dist}, {duration}.",
        "destination": " Destination: {address}.",
        "step": "Step {i}: {instruction}",
        "arrived_speech": "Arrived at {dest}.",
    }


def route_intro_speech(
    place_name: str,
    total_distance: str,
    total_duration: str,
    lang: str,
) -> str:
    """Short phrase for robot TTS at route start."""
    phrases = guidance_phrases(lang)
    return phrases["intro_speech"].format(
        dest=place_name,
        dist=total_distance or "?",
        duration=total_duration or "?",
    )


from core import gps_stream_client


def phone_gps_enabled(settings: dict) -> bool:
    return gps_stream_client.is_configured(settings)


def get_robot_location(settings: dict) -> tuple[float, float, str]:
    global _simulated_lat, _simulated_lng
    origin_lat, origin_lng, origin_label = origin(settings)

    if not live_gps_enabled(settings):
        logger.info(
            "Using configured origin for route planning (%.5f, %.5f, %r)",
            origin_lat,
            origin_lng,
            origin_label,
        )
        if simulate_progress_enabled(settings):
            if _simulated_lat is not None and _simulated_lng is not None:
                return _simulated_lat, _simulated_lng, f"simulated ({origin_label})"
        return origin_lat, origin_lng, f"configured origin ({origin_label})"

    cfg = gps_stream_client.stream_settings(settings)
    if gps_stream_client.is_configured(settings):
        logger.info("Fetching live location from phone_gps at %s/api/location", cfg["url"])
        phone = gps_stream_client.get_phone_location(settings)
        if phone is not None:
            lat, lng, label = phone
            logger.info("Using live phone GPS: (%.5f, %.5f) — %s", lat, lng, label)
            return phone
        logger.warning(
            "Phone GPS unavailable — falling back to configured origin "
            "(%.5f, %.5f, label=%r)",
            origin_lat,
            origin_lng,
            origin_label,
        )
    else:
        logger.warning(
            "GPS_STREAM_URL not configured — using configured origin "
            "(%.5f, %.5f, label=%r)",
            origin_lat,
            origin_lng,
            origin_label,
        )

    if not simulate_progress_enabled(settings):
        return origin_lat, origin_lng, f"configured origin ({origin_label})"

    if _simulated_lat is None or _simulated_lng is None:
        return origin_lat, origin_lng, f"configured origin ({origin_label})"

    logger.info(
        "Using simulated progress location: (%.5f, %.5f)",
        _simulated_lat,
        _simulated_lng,
    )
    return _simulated_lat, _simulated_lng, f"simulated ({origin_label})"


def reset_simulated_location(settings: dict) -> tuple[float, float, str]:
    global _simulated_lat, _simulated_lng
    _simulated_lat = None
    _simulated_lng = None
    origin_lat, origin_lng, origin_label = origin(settings)
    logger.info("Simulated location reset to origin: %s (%.5f, %.5f)", origin_label, origin_lat, origin_lng)
    return origin_lat, origin_lng, origin_label


def advance_simulated_location(lat: float, lng: float) -> None:
    global _simulated_lat, _simulated_lng
    _simulated_lat = lat
    _simulated_lng = lng
    logger.info("Simulated location advanced to (%.5f, %.5f)", lat, lng)


def _get_api_key(settings: dict) -> str:
    maps_cfg = settings.get("google_maps") or {}
    key = maps_cfg.get("api_key", "")
    if not key or str(key).startswith("${"):
        key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    return key or ""


def _maps_get(path: str, params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    url = f"{_GOOGLE_MAPS_BASE}/{path}?{query}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Google Maps HTTP {e.code}: {body[:200]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Google Maps request failed: {e.reason}") from e


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def find_place(
    query: str,
    api_key: str,
    bias_lat: float,
    bias_lng: float,
    language: str,
) -> Place:
    data = _maps_get(
        "place/textsearch/json",
        {
            "query": query,
            "key": api_key,
            "location": f"{bias_lat},{bias_lng}",
            "radius": 50000,
            "language": language,
        },
    )
    status = data.get("status", "UNKNOWN")
    if status != "OK":
        raise RuntimeError(f"Place search failed: {status} — {data.get('error_message', '')}")

    results = data.get("results") or []
    if not results:
        raise RuntimeError(f"No place found for '{query}'.")

    top = results[0]
    location = top["geometry"]["location"]
    return Place(
        name=top.get("name", query),
        address=top.get("formatted_address", ""),
        lat=location["lat"],
        lng=location["lng"],
        place_id=top.get("place_id", ""),
    )


def get_route(
    origin_lat: float,
    origin_lng: float,
    place: Place,
    api_key: str,
    language: str,
) -> tuple[list[RouteStep], str, str]:
    """Fetch a walking route from Google Maps Directions API."""
    destination = f"place_id:{place.place_id}" if place.place_id else f"{place.lat},{place.lng}"
    logger.info(
        "Google Maps Directions: walking route from (%.5f, %.5f) to %s",
        origin_lat,
        origin_lng,
        place.name,
    )
    data = _maps_get(
        "directions/json",
        {
            "origin": f"{origin_lat},{origin_lng}",
            "destination": destination,
            "mode": "walking",
            "language": language,
            "key": api_key,
        },
    )
    status = data.get("status", "UNKNOWN")
    if status != "OK":
        raise RuntimeError(f"Directions failed: {status} — {data.get('error_message', '')}")

    routes = data.get("routes") or []
    if not routes:
        raise RuntimeError("No route found.")

    leg = routes[0]["legs"][0]
    total_distance = leg.get("distance", {}).get("text", "?")
    total_duration = leg.get("duration", {}).get("text", "?")

    steps: list[RouteStep] = []
    for step in leg.get("steps", []):
        end = step.get("end_location", {})
        dist = step.get("distance") or {}
        steps.append(
            RouteStep(
                instruction=_strip_html(step.get("html_instructions", "")),
                distance=dist.get("text", ""),
                distance_m=float(dist.get("value") or 0),
                duration=step.get("duration", {}).get("text", ""),
                maneuver=str(step.get("maneuver") or ""),
                end_lat=end.get("lat", origin_lat),
                end_lng=end.get("lng", origin_lng),
            )
        )
    logger.info(
        "Google Maps returned %d walking steps (%s, %s)",
        len(steps),
        total_distance,
        total_duration,
    )
    return steps, total_distance, total_duration


def plan_route(destination: str, settings: dict) -> RoutePlan:
    """Plan navigation: Google Maps route → step-by-step motion commands."""
    destination = (destination or "").strip()
    if not destination:
        raise ValueError("destination text is empty")

    api_key = _get_api_key(settings)
    if not api_key:
        raise RuntimeError("GOOGLE_MAPS_API_KEY is not configured")

    lang = language(settings)
    origin_lat, origin_lng, location_label = get_robot_location(settings)
    heading = origin_heading_deg(settings)
    place = find_place(destination, api_key, origin_lat, origin_lng, lang)
    steps, total_distance, total_duration = get_route(
        origin_lat, origin_lng, place, api_key, lang
    )
    from core import route_motion

    logger.info(
        "Robot start pose: (%.5f, %.5f) heading=%.0f° (%s)",
        origin_lat,
        origin_lng,
        heading,
        location_label,
    )
    logger.info("Converting %d Google Maps steps to robot motion commands", len(steps))
    motion_commands = route_motion.convert_google_maps_steps(
        steps,
        lang,
        settings,
        origin_lat=origin_lat,
        origin_lng=origin_lng,
        origin_heading_deg=heading,
    )
    motion_mode = "gps" if live_gps_enabled(settings) else "distance"

    logger.info(
        "Route ready (provider=google_maps): %d Maps steps → %d motion commands (%s mode)",
        len(steps),
        len(motion_commands),
        motion_mode,
    )
    for index, cmd in enumerate(motion_commands, start=1):
        logger.info("  Motion %d: %s", index, cmd.label)

    return RoutePlan(
        place=place,
        steps=steps,
        motion_commands=motion_commands,
        total_distance=total_distance,
        total_duration=total_duration,
        origin_label=location_label,
        origin_lat=origin_lat,
        origin_lng=origin_lng,
        origin_heading_deg=heading,
        motion_mode=motion_mode,
        route_provider="google_maps",
    )
