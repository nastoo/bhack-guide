"""Fetch live GPS fixes from the phone_gps stream service."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def stream_settings(settings: dict | None) -> dict:
    nav = (settings or {}).get("navigation") or {}
    url = (
        os.environ.get("GPS_STREAM_URL")
        or nav.get("gps_stream_url")
        or ""
    ).strip().rstrip("/")
    token = (os.environ.get("GPS_TOKEN") or nav.get("gps_token") or "").strip()
    access_password = (
        os.environ.get("GPS_ACCESS_PASSWORD")
        or os.environ.get("ACCESS_PASSWORD")
        or nav.get("gps_access_password")
        or ""
    ).strip()
    max_age = float(
        os.environ.get("GPS_MAX_AGE_SEC")
        or nav.get("gps_max_age_sec")
        or 20
    )
    return {
        "url": url,
        "token": token,
        "access_password": access_password,
        "max_age_sec": max(5.0, max_age),
    }


def is_configured(settings: dict | None) -> bool:
    return bool(stream_settings(settings)["url"])


def _request_headers(cfg: dict) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if cfg["access_password"]:
        headers["Authorization"] = f"Bearer {cfg['access_password']}"
    return headers


def fetch_latest(settings: dict | None, *, quiet: bool = False) -> dict | None:
    """GET {GPS_STREAM_URL}/api/location → latest fix or None."""
    cfg = stream_settings(settings)
    base = cfg["url"]
    if not base:
        if not quiet:
            logger.debug("Phone GPS: GPS_STREAM_URL not configured")
        return None

    url = f"{base}/api/location"
    req = urllib.request.Request(url, headers=_request_headers(cfg))
    if cfg["token"]:
        req.add_header("X-GPS-Token", cfg["token"])

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if not quiet:
            logger.warning(
                "Phone GPS HTTP %s from %s: %s",
                e.code,
                url,
                e.reason,
            )
        return None
    except urllib.error.URLError as e:
        if not quiet:
            logger.warning("Phone GPS unreachable at %s: %s", url, e.reason)
        return None
    except Exception:
        if not quiet:
            logger.warning("Phone GPS fetch failed for %s", url, exc_info=True)
        return None

    if not data.get("ok"):
        if not quiet:
            message = data.get("message") or "no fix available"
            logger.info("Phone GPS: %s (url=%s)", message, url)
        return None

    location = data.get("location")
    if not isinstance(location, dict):
        if not quiet:
            logger.warning("Phone GPS: invalid location payload from %s", url)
        return None

    try:
        lat = float(location["lat"])
        lng = float(location["lng"])
    except (KeyError, TypeError, ValueError):
        if not quiet:
            logger.warning("Phone GPS: missing lat/lng in response from %s", url)
        return None

    if not quiet:
        accuracy = location.get("accuracy")
        received_at = location.get("received_at")
        age = time.time() - float(received_at) if received_at is not None else None
        if accuracy is not None:
            logger.info(
                "Phone GPS fix: lat=%.5f lng=%.5f accuracy=%.0fm age=%s",
                lat,
                lng,
                float(accuracy),
                f"{age:.1f}s" if age is not None else "unknown",
            )
        else:
            logger.info(
                "Phone GPS fix: lat=%.5f lng=%.5f age=%s",
                lat,
                lng,
                f"{age:.1f}s" if age is not None else "unknown",
            )
    return location


def is_live(fix: dict | None, settings: dict | None) -> bool:
    if not fix:
        return False
    status = fix.get("status") if isinstance(fix.get("status"), dict) else {}
    if status.get("live") is True:
        return True
    received_at = fix.get("received_at")
    if received_at is None:
        return True
    age = time.time() - float(received_at)
    return age <= stream_settings(settings)["max_age_sec"]


def get_phone_location(settings: dict | None) -> tuple[float, float, str] | None:
    """Return (lat, lng, label) from phone stream if live, else None."""
    cfg = stream_settings(settings)
    if not cfg["url"]:
        return None

    fix = fetch_latest(settings)
    if not fix:
        return None

    if not is_live(fix, settings):
        received_at = fix.get("received_at")
        if received_at is not None:
            age = time.time() - float(received_at)
            logger.warning(
                "Phone GPS fix stale (age %.1fs > max %.1fs) — will fall back",
                age,
                cfg["max_age_sec"],
            )
        else:
            logger.warning("Phone GPS fix not live — will fall back")
        return None

    try:
        lat = float(fix["lat"])
        lng = float(fix["lng"])
    except (KeyError, TypeError, ValueError):
        logger.warning("Phone GPS fix missing coordinates — will fall back")
        return None

    accuracy = fix.get("accuracy")
    label = "phone GPS"
    if accuracy is not None:
        label = f"phone GPS (±{float(accuracy):.0f}m)"
    return lat, lng, label
