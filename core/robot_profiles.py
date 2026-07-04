"""Named robot profiles (Go1 dog, Loomo) — URLs, API kind, OpenAPI spec file."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from core.robot_http import API_GO1, API_LOOMO, RobotHttpClient

if TYPE_CHECKING:
    pass

CONFIGS_DIR = "configs"

DEFAULT_PROFILES: dict[str, dict] = {
    "go1": {
        "label": "Dog (Go1)",
        "api": API_GO1,
        "base_url": "https://go1.cphs.mylab.th-luebeck.de",
        "spec": "robot_openapi.json",
    },
    "loomo": {
        "label": "Loomo",
        "api": API_LOOMO,
        "base_url": "https://loomo.cphs.mylab.th-luebeck.de",
        "spec": "loomo_openapi.json",
    },
}


def _robot_settings(settings: dict | None) -> dict:
    return (settings or {}).get("robots") or {}


def list_profiles(settings: dict | None = None) -> dict[str, dict]:
    """Return {id: {label, api, base_url, spec}} for each configured robot."""
    cfg = _robot_settings(settings)
    profiles_cfg = cfg.get("profiles") or {}
    out: dict[str, dict] = {}
    for robot_id, defaults in DEFAULT_PROFILES.items():
        override = profiles_cfg.get(robot_id) or {}
        out[robot_id] = {
            "id": robot_id,
            "label": str(override.get("label") or defaults["label"]),
            "api": str(override.get("api") or defaults["api"]),
            "base_url": str(
                override.get("base_url")
                or os.environ.get(f"ROBOT_{robot_id.upper()}_URL")
                or defaults["base_url"]
            ).rstrip("/"),
            "spec": str(override.get("spec") or defaults["spec"]),
        }
    return out


def default_robot_id(settings: dict | None = None) -> str:
    cfg = _robot_settings(settings)
    env = os.environ.get("ROBOT_DEFAULT", "").strip().lower()
    if env in list_profiles(settings):
        return env
    default = str(cfg.get("default") or "loomo").strip().lower()
    profiles = list_profiles(settings)
    if default in profiles:
        return default
    # Legacy single-robot config
    legacy_api = str((settings or {}).get("robot", {}).get("api") or "auto").lower()
    if legacy_api in (API_GO1, "go1", "dog"):
        return "go1"
    if legacy_api in (API_LOOMO, "loomo"):
        return "loomo"
    legacy_url = str((settings or {}).get("robot", {}).get("base_url") or "").lower()
    if "loomo" in legacy_url or "segway" in legacy_url:
        return "loomo"
    if "go1" in legacy_url:
        return "go1"
    return "loomo"


def get_profile(robot_id: str, settings: dict | None = None) -> dict:
    profiles = list_profiles(settings)
    key = (robot_id or "").strip().lower()
    if key not in profiles:
        raise ValueError(f"Unknown robot {robot_id!r}. Choose: {', '.join(profiles)}")
    return profiles[key]


def apply_profile(
    robot: RobotHttpClient,
    robot_id: str,
    settings: dict | None = None,
    *,
    simulated: bool | None = None,
) -> dict:
    """Point the shared HTTP client at a robot profile."""
    profile = get_profile(robot_id, settings)
    robot_cfg = (settings or {}).get("robot") or {}
    if simulated is None:
        sim_env = os.environ.get("ROBOT_SIMULATED")
        if sim_env is not None:
            simulated = str(sim_env).strip().lower() in ("1", "true", "yes", "on")
        else:
            simulated = bool(robot_cfg.get("simulated", True))

    robot.base_url = profile["base_url"]
    robot.api = profile["api"]
    robot.simulated = simulated or not profile["base_url"]
    robot._connected = False
    return profile
