"""Parse user-provided routes into distance-based motion commands."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from core import gps_navigation
from core.route_motion import MotionCommand, _forward_label, _rotate_label, _turn_label

logger = logging.getLogger(__name__)

_DIST = r"(\d+(?:\.\d+)?)\s*(?:m(?:et(?:er|re)?s?)?)?"


def _lang(settings: dict | None) -> str:
    return gps_navigation.language(settings)


def _forward(distance_m: float, line: str, *, lang: str) -> MotionCommand:
    return MotionCommand(
        action="forward",
        label=_forward_label(distance_m, lang),
        distance_m=distance_m,
        source_instruction=line,
    )


def _backward(distance_m: float, line: str, *, lang: str) -> MotionCommand:
    label = (
        f"{int(round(distance_m))} Meter rückwärts"
        if lang == "de"
        else f"Go backward for {int(round(distance_m))} m"
    )
    return MotionCommand(
        action="forward",
        label=label,
        distance_m=-distance_m,
        source_instruction=line,
    )


def parse_line(line: str, *, lang: str = "en") -> MotionCommand | None:
    """Parse one route step line into a motion command."""
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None

    lower = line.lower()

    forward_patterns = (
        rf"^(?:forward|straight|ahead)\s+{_DIST}$",
        rf"^go\s+straight\s+(?:for\s+)?{_DIST}$",
        rf"^go\s+forward\s+(?:for\s+)?{_DIST}$",
        rf"^(?:go|walk|move)\s+(?:straight\s+)?(?:for\s+)?{_DIST}$",
        rf"^{_DIST}\s+(?:straight|forward|ahead)$",
        rf"^(?:walk|move)\s+{_DIST}\s+(?:straight|forward)$",
    )
    for pattern in forward_patterns:
        match = re.match(pattern, lower)
        if match:
            return _forward(float(match.group(1)), line, lang=lang)

    backward_patterns = (
        rf"^(?:backward|back|reverse)\s+{_DIST}$",
        rf"^(?:go|walk|move)\s+backward\s+(?:for\s+)?{_DIST}$",
    )
    for pattern in backward_patterns:
        match = re.match(pattern, lower)
        if match:
            return _backward(float(match.group(1)), line, lang=lang)

    match = re.match(
        r"^(?:rotate|face)\s+(\d+(?:\.\d+)?)\s*(?:°|deg(?:rees?)?)?\s+(left|right)$",
        lower,
    )
    if match:
        angle = float(match.group(1))
        if match.group(2) == "right":
            angle = -angle
        return MotionCommand(
            action="rotate",
            label=_rotate_label(angle, lang),
            angle_deg=angle,
            source_instruction=line,
        )

    match = re.match(
        r"^u[-\s]?turn(?:\s+to)?(?:\s+the)?\s+(left|right)$",
        lower,
    )
    if match:
        action = "uturn_left" if match.group(1) == "left" else "uturn_right"
        return MotionCommand(
            action=action,
            label=_turn_label(action, lang),
            source_instruction=line,
        )

    match = re.match(
        r"^turn(?:\s+to)?(?:\s+the)?\s+(left|right)(?:\s+(\d+(?:\.\d+)?)\s*(?:°|deg(?:rees?)?)?)?$",
        lower,
    )
    if match:
        direction = match.group(1)
        degrees = match.group(2)
        if degrees:
            angle = float(degrees) * (1.0 if direction == "left" else -1.0)
            return MotionCommand(
                action="rotate",
                label=_rotate_label(angle, lang),
                angle_deg=angle,
                source_instruction=line,
            )
        action = "turn_left" if direction == "left" else "turn_right"
        return MotionCommand(
            action=action,
            label=_turn_label(action, lang),
            source_instruction=line,
        )

    if lower in ("left", "turn left", "turn to the left", "make a left turn"):
        return MotionCommand(
            action="turn_left",
            label=_turn_label("turn_left", lang),
            source_instruction=line,
        )

    if lower in ("right", "turn right", "turn to the right", "make a right turn"):
        return MotionCommand(
            action="turn_right",
            label=_turn_label("turn_right", lang),
            source_instruction=line,
        )

    raise ValueError(f"Could not parse route line: {line!r}")


def _extract_json_array(text: str) -> list:
    text = (text or "").strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        return json.loads(match.group(0))
    raise ValueError("LLM did not return a JSON step array")


def parse_route_llm(
    text: str,
    *,
    settings: dict | None = None,
    model: str = "chat-vl-large",
) -> list[MotionCommand]:
    """Use the LLM to turn free-form route text into motion steps."""
    from core.api_client import _client

    lang = _lang(settings)
    client = _client()
    prompt = (
        "Convert this robot walking route into a JSON array of steps. "
        "Use only these actions: forward, backward, turn_left, turn_right, uturn_left, "
        "uturn_right, rotate.\n"
        "- forward/backward: include distance_m (metres)\n"
        "- rotate: include angle_deg (positive=left, negative=right) OR degrees+direction\n"
        "- turns without angle: just action turn_left or turn_right\n"
        "Return ONLY a JSON array, no markdown.\n\n"
        f"Route:\n{text.strip()}"
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You output valid JSON arrays only."},
            {"role": "user", "content": prompt},
        ],
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        temperature=0.1,
    )
    raw = response.choices[0].message.content or "[]"
    steps = _extract_json_array(raw)
    commands = parse_structured_steps(steps, settings=settings)
    logger.info("LLM parsed manual route (%d steps) from: %r", len(commands), text[:80])
    return commands


def parse_route_text(
    text: str,
    *,
    settings: dict | None = None,
    model: str | None = None,
    use_llm_fallback: bool = True,
) -> tuple[list[MotionCommand], str]:
    """Parse a multi-line manual route. Returns (commands, parser_used)."""
    lang = _lang(settings)
    commands: list[MotionCommand] = []
    errors: list[str] = []

    for line in (text or "").splitlines():
        try:
            cmd = parse_line(line, lang=lang)
            if cmd is not None:
                commands.append(cmd)
        except ValueError as exc:
            errors.append(str(exc))

    if commands and not errors:
        return commands, "text"

    if use_llm_fallback and model:
        try:
            return parse_route_llm(text, settings=settings, model=model), "llm"
        except Exception as exc:
            logger.exception("LLM manual route parse failed")
            if errors:
                raise ValueError(errors[0]) from exc
            raise ValueError(f"Could not parse route: {exc}") from exc

    if errors:
        raise ValueError(errors[0])
    raise ValueError("Manual route has no steps")


def parse_structured_step(raw: dict[str, Any], *, lang: str = "en") -> MotionCommand:
    """Parse one JSON step object."""
    action = str(raw.get("action") or "").strip().lower().replace("-", "_")
    if not action:
        raise ValueError("Each step needs an action")

    if action in ("forward", "straight", "ahead"):
        distance_m = float(raw.get("distance_m") or raw.get("distance") or 0)
        if distance_m <= 0:
            raise ValueError("forward steps need distance_m > 0")
        return MotionCommand(
            action="forward",
            label=str(raw.get("label") or _forward_label(distance_m, lang)),
            distance_m=distance_m,
            source_instruction=str(raw.get("instruction") or f"forward {distance_m}m"),
        )

    if action in ("backward", "back", "reverse"):
        distance_m = abs(float(raw.get("distance_m") or raw.get("distance") or 0))
        if distance_m <= 0:
            raise ValueError("backward steps need distance_m > 0")
        label = str(raw.get("label") or (
            f"{int(round(distance_m))} Meter rückwärts"
            if lang == "de"
            else f"Go backward for {int(round(distance_m))} m"
        ))
        return MotionCommand(
            action="forward",
            label=label,
            distance_m=-distance_m,
            source_instruction=str(raw.get("instruction") or f"backward {distance_m}m"),
        )

    if action in ("turn_left", "left"):
        return MotionCommand(
            action="turn_left",
            label=str(raw.get("label") or _turn_label("turn_left", lang)),
            source_instruction=str(raw.get("instruction") or "turn left"),
        )

    if action in ("turn_right", "right"):
        return MotionCommand(
            action="turn_right",
            label=str(raw.get("label") or _turn_label("turn_right", lang)),
            source_instruction=str(raw.get("instruction") or "turn right"),
        )

    if action in ("uturn_left", "uturn_right"):
        return MotionCommand(
            action=action,
            label=str(raw.get("label") or _turn_label(action, lang)),
            source_instruction=str(raw.get("instruction") or action.replace("_", " ")),
        )

    if action in ("rotate", "turn"):
        angle = float(raw.get("angle_deg") or raw.get("degrees") or raw.get("deg") or 0)
        direction = str(raw.get("direction") or "").strip().lower()
        if direction == "right" and angle > 0:
            angle = -angle
        elif direction == "left" and angle < 0:
            angle = abs(angle)
        if abs(angle) < 1:
            raise ValueError("rotate steps need angle_deg")
        return MotionCommand(
            action="rotate",
            label=str(raw.get("label") or _rotate_label(angle, lang)),
            angle_deg=angle,
            source_instruction=str(raw.get("instruction") or f"rotate {angle}°"),
        )

    raise ValueError(f"Unknown manual route action: {action!r}")


def parse_structured_steps(
    steps: list[dict[str, Any]],
    *,
    settings: dict | None = None,
) -> list[MotionCommand]:
    lang = _lang(settings)
    if not steps:
        raise ValueError("Manual route has no steps")
    return [parse_structured_step(step, lang=lang) for step in steps]


def summarize_route(commands: list[MotionCommand]) -> tuple[str, str]:
    """Return (total_distance_label, step_summary)."""
    total_m = sum(abs(c.distance_m) for c in commands if c.action == "forward")
    dist_label = f"{int(round(total_m))} m" if total_m else "—"
    summary = " → ".join(cmd.label for cmd in commands)
    return dist_label, summary
