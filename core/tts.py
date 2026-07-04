"""TTS for robot audio streaming via the mylab OpenAI-compatible API."""

from __future__ import annotations

import logging

from core.api_client import _client, api_key

logger = logging.getLogger(__name__)


def text_to_speech(text: str, *, model: str = "xtts-v2") -> bytes | None:
    if not text.strip():
        return None

    try:
        client = _client()
        response = client.audio.speech.create(
            model=model,
            voice="alloy",
            input=text,
            response_format="wav",
        )
        return response.content
    except Exception:
        logger.exception("TTS request failed (model=%s, key=%s)", model, api_key()[:4] + "…")
        return None
