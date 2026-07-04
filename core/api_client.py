"""LLM and speech API client for the navigation service (OpenAI-compatible mylab endpoint)."""

from __future__ import annotations

import io
import os

from openai import OpenAI

MYLAB_BASE_URL = "https://models.mylab.th-luebeck.dev/v1"
MYLAB_API_KEY = "none"
MYLAB_DEFAULT_MODEL = "chat-vl-large"

# Available chat models on mylab (openai-completions API):
MYLAB_CHAT_MODELS = (
    "chat-vl-large",    # Qwen 3.5 (397B)
    "chat-vl-xlarge",   # Mistral Large (675B)
    "gpt-oss-120b",     # GPT OSS 120B XL
    "chat-vl-fast",     # Gemma 4 31B
)


def api_base_url() -> str:
    return os.environ.get("NAV_API_BASE_URL", MYLAB_BASE_URL)


def api_key() -> str:
    return os.environ.get("NAV_API_KEY", MYLAB_API_KEY)


def default_model() -> str:
    return os.environ.get("NAV_LLM_MODEL", MYLAB_DEFAULT_MODEL)


def stt_model() -> str:
    return os.environ.get("NAV_STT_MODEL", "whisper-3-large")


def stt_language() -> str:
    """ISO-639-1 code for Whisper (e.g. en). Empty disables language hint."""
    return os.environ.get("NAV_STT_LANGUAGE", "en").strip().lower() or "en"


def _client() -> OpenAI:
    return OpenAI(base_url=api_base_url(), api_key=api_key())


def chat_completion(messages: list[dict], *, model: str | None = None, **kwargs) -> str:
    client = _client()
    response = client.chat.completions.create(
        model=model or default_model(),
        messages=messages,
        **kwargs,
    )
    return response.choices[0].message.content or ""


def transcribe_audio(
    audio_data: bytes,
    filename: str = "audio.wav",
    *,
    language: str | None = None,
) -> str:
    client = _client()
    model = stt_model()
    lang = (language or stt_language()).strip().lower() or None
    audio_file = io.BytesIO(audio_data)
    audio_file.name = filename
    kwargs: dict = {"model": model, "file": audio_file}
    if lang:
        kwargs["language"] = lang
    response = client.audio.transcriptions.create(**kwargs)
    return response.text
