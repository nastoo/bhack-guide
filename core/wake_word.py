"""Wake phrase detection for Siri-style voice control."""

from __future__ import annotations

import re

# Common Whisper mis-hearings for "hi loomo".
WAKE_VARIANTS = (
    "hi loomo",
    "hey loomo",
    "hi lomo",
    "hey lomo",
    "hi loom o",
    "hey loom o",
    "high loomo",
    "high lomo",
)

_WAKE_PATTERN = re.compile(
    r"\b(?:hi|hey|high)\s+lo(?:o)?m(?:o)?\b",
    re.IGNORECASE,
)


def normalize_transcript(text: str) -> str:
    """Lowercase and collapse whitespace for matching."""
    text = (text or "").strip().lower()
    text = re.sub(r"[^\w\säöüß\-']", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def extract_wake_command(transcript: str) -> tuple[str | None, str]:
    """Return (command, reason) after wake phrase, or (None, reason) if not triggered."""
    raw = (transcript or "").strip()
    if not raw:
        return None, "empty transcript"

    normalized = normalize_transcript(raw)

    for phrase in WAKE_VARIANTS:
        idx = normalized.find(phrase)
        if idx >= 0:
            command = normalized[idx + len(phrase) :].strip(" ,.-:;")
            if command:
                return command, "wake phrase matched"
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
