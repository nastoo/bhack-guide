"""Wake phrase detection for Siri-style voice control."""

from __future__ import annotations

import re

# Default triggers — overridden by wake_word.phrases in settings.yaml.
DEFAULT_WAKE_VARIANTS = (
    "hi loomo",
    "hey loomo",
    "hello loomo",
    "ok loomo",
    "okay loomo",
    "hi lomo",
    "hey lomo",
    "hello lomo",
    "hi loom o",
    "hey loom o",
    "high loomo",
    "high lomo",
)

_WAKE_VARIANTS: tuple[str, ...] = DEFAULT_WAKE_VARIANTS

_WAKE_PATTERN = re.compile(
    r"\b(?:hi|hey|high|hello|ok|okay)\s+lo(?:o)?m(?:o)?\b",
    re.IGNORECASE,
)


def normalize_transcript(text: str) -> str:
    """Lowercase and collapse whitespace for matching."""
    text = (text or "").strip().lower()
    text = re.sub(r"[^\w\säöüß\-']", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def set_wake_phrases(phrases: list[str] | tuple[str, ...] | None) -> None:
    """Load wake triggers from settings (longest phrases match first)."""
    global _WAKE_VARIANTS
    if not phrases:
        _WAKE_VARIANTS = DEFAULT_WAKE_VARIANTS
        return

    seen: set[str] = set()
    normalized: list[str] = []
    for phrase in phrases:
        clean = normalize_transcript(str(phrase))
        if clean and clean not in seen:
            seen.add(clean)
            normalized.append(clean)

    _WAKE_VARIANTS = tuple(sorted(normalized, key=len, reverse=True)) or DEFAULT_WAKE_VARIANTS


def wake_phrases() -> tuple[str, ...]:
    return _WAKE_VARIANTS


def extract_wake_command(transcript: str) -> tuple[str | None, str]:
    """Return (command, reason) after wake phrase, or (None, reason) if not triggered."""
    raw = (transcript or "").strip()
    if not raw:
        return None, "empty transcript"

    normalized = normalize_transcript(raw)

    for phrase in _WAKE_VARIANTS:
        idx = normalized.find(phrase)
        if idx >= 0:
            command = normalized[idx + len(phrase) :].strip(" ,.-:;")
            if command:
                return command, f"wake phrase matched ({phrase!r})"
            return None, "wake phrase heard but no command followed"

    match = _WAKE_PATTERN.search(normalized)
    if match:
        command = normalized[match.end() :].strip(" ,.-:;")
        if command:
            return command, "wake phrase matched (regex)"
        return None, "wake phrase heard but no command followed"

    return None, "no wake phrase"


def wake_word_detected(transcript: str) -> bool:
    command, _ = extract_wake_command(transcript)
    return command is not None
