"""Ask an LLM to map GPS navigation text into robot HTTP calls."""

from __future__ import annotations

import json
import logging
import re

from core.api_client import _client
from core.gps_motion import DEFAULT_SPEED_KMH, linear_speed_ms
from core.openapi_spec import system_context
from core.robot_http import HttpCall, NavigationPlan

logger = logging.getLogger(__name__)

DEFAULT_FALLBACK_SPEED_MS = DEFAULT_SPEED_KMH / 3.6


def _planner_system(linear_speed_ms: float) -> str:
    return f"""You are a robot navigation controller. You receive turn-by-turn GPS walking instructions
and must produce robot motion via the Go1 dashboard REST API documented in the OpenAPI spec.

Rules:
- Always call POST /api/c2/connect once at route start (handled separately); for each step plan motion only.
- Use POST /api/c2/move with vx (forward), vy (strafe), yaw_speed (turn; positive=left, negative=right).
  Forward speed: vx ≈ {linear_speed_ms:.2f} m/s ({linear_speed_ms * 3.6:.1f} km/h). yaw_speed 0.8-1.2 for turns.
  Always end with POST /api/c2/stop or set hold_sec on move.
- Parse the GPS text: "turn left" -> positive yaw_speed; "turn right" -> negative; "straight"/"continue" -> positive vx.
- Estimate hold_sec from distance hints (e.g. 100 m at {linear_speed_ms * 3.6:.1f} km/h). Cap hold_sec at 8.0.
- speech: short natural phrase for the user (what to do now or at the next stop). Keep under 25 words.
- Return ONLY valid JSON, no markdown fences.

Output schema:
{{
  "speech": "string",
  "http_calls": [
    {{"method": "POST", "path": "/api/c2/move", "body": {{"vx": {linear_speed_ms:.2f}, "vy": 0, "yaw_speed": 0, "gait_type": 1, "foot_raise_height": 0.08}}, "hold_sec": 3.0}},
    {{"method": "POST", "path": "/api/c2/stop", "body": {{}}}}
  ]
}}
"""


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def _fallback_plan(
    instruction: str,
    distance: str = "",
    *,
    linear_speed_ms: float = DEFAULT_FALLBACK_SPEED_MS,
) -> NavigationPlan:
    lower = instruction.lower()
    speech = instruction
    if distance:
        speech = f"{instruction}. About {distance}."

    if "left" in lower and "turn" in lower:
        return NavigationPlan(
            speech=speech,
            http_calls=[
                HttpCall(
                    "POST",
                    "/api/c2/move",
                    {"vx": 0.0, "vy": 0.0, "yaw_speed": 1.0, "gait_type": 1, "foot_raise_height": 0.08},
                    hold_sec=2.0,
                ),
                HttpCall("POST", "/api/c2/stop", {}),
            ],
        )
    if "right" in lower and "turn" in lower:
        return NavigationPlan(
            speech=speech,
            http_calls=[
                HttpCall(
                    "POST",
                    "/api/c2/move",
                    {"vx": 0.0, "vy": 0.0, "yaw_speed": -1.0, "gait_type": 1, "foot_raise_height": 0.08},
                    hold_sec=2.0,
                ),
                HttpCall("POST", "/api/c2/stop", {}),
            ],
        )
    return NavigationPlan(
        speech=speech,
        http_calls=[
            HttpCall(
                "POST",
                "/api/c2/move",
                {
                    "vx": linear_speed_ms,
                    "vy": 0.0,
                    "yaw_speed": 0.0,
                    "gait_type": 1,
                    "foot_raise_height": 0.08,
                },
                hold_sec=3.0,
            ),
            HttpCall("POST", "/api/c2/stop", {}),
        ],
    )


def _parse_plan(
    data: dict,
    instruction: str,
    distance: str,
    *,
    linear_speed_ms: float,
) -> NavigationPlan:
    if not data:
        return _fallback_plan(instruction, distance, linear_speed_ms=linear_speed_ms)

    speech = str(data.get("speech") or instruction).strip()
    calls: list[HttpCall] = []
    for item in data.get("http_calls") or []:
        if not isinstance(item, dict):
            continue
        method = str(item.get("method", "POST")).upper()
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        body = item.get("body")
        if body is not None and not isinstance(body, dict):
            body = {}
        hold_sec = float(item.get("hold_sec") or 0.0)
        calls.append(HttpCall(method, path, body, hold_sec=max(0.0, min(hold_sec, 8.0))))

    if not calls:
        return _fallback_plan(instruction, distance, linear_speed_ms=linear_speed_ms)
    return NavigationPlan(speech=speech, http_calls=calls)


def plan_navigation_step(
    *,
    instruction: str,
    distance: str,
    step_index: int,
    step_total: int,
    robot_base_url: str,
    model: str,
    route_context: str = "",
    settings: dict | None = None,
    robot_api: str | None = None,
    spec_file: str | None = None,
    robot_label: str | None = None,
) -> NavigationPlan:
    """Turn one GPS instruction into speech + HTTP motion plan via LLM."""
    instruction = (instruction or "").strip()
    speed_ms = linear_speed_ms(settings)
    if not instruction:
        return NavigationPlan(speech="Continue straight.", http_calls=[])

    try:
        client = _client()
    except RuntimeError:
        logger.warning("No LLM API key — using heuristic planner")
        return _fallback_plan(instruction, distance, linear_speed_ms=speed_ms)

    user_content = (
        f"Route context: {route_context}\n"
        f"Step {step_index} of {step_total}\n"
        f"GPS instruction: {instruction}\n"
        f"Distance: {distance or 'unknown'}\n"
        "Produce JSON motion plan and spoken guidance."
    )

    messages = [
        {
            "role": "system",
            "content": _planner_system(speed_ms)
            + "\n\n"
            + system_context(
                robot_base_url,
                api=robot_api,
                spec_file=spec_file,
                label=robot_label,
            ),
        },
        {"role": "user", "content": user_content},
    ]

    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            temperature=0.2,
        )
        raw = response.choices[0].message.content or ""
        data = _extract_json(raw)
        plan = _parse_plan(data, instruction, distance, linear_speed_ms=speed_ms)
        logger.info(
            "LLM plan step %d/%d: speech=%r calls=%d speed=%.1f km/h",
            step_index,
            step_total,
            plan.speech[:60],
            len(plan.http_calls),
            speed_ms * 3.6,
        )
        return plan
    except Exception:
        logger.exception("LLM navigation planning failed — using fallback")
        return _fallback_plan(instruction, distance, linear_speed_ms=speed_ms)
