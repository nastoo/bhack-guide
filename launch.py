"""Entrypoint for the LLM robot navigation service."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path("/app") if Path("/app").exists() else Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

logging.basicConfig(level=logging.INFO)

AUDIO_TESTS = ("speak", "listen", "echo")
MOTION_TESTS = ("move", "patrol", "stop")
ALL_TESTS = AUDIO_TESTS + MOTION_TESTS


def _as_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _print_result(result) -> int:
    label = getattr(result, "mode", "test")
    print(f"\n=== {label} test ===")
    for step in result.steps:
        print(f"  • {step}")
    for field in ("heard", "spoken", "direction", "error"):
        val = getattr(result, field, "")
        if val:
            print(f"{field}: {val}")
    if getattr(result, "duration_sec", 0):
        print(f"duration: {result.duration_sec}s")
    print(f"result: {'OK' if result.ok else 'FAILED'}\n")
    return 0 if result.ok else 1


async def _run_test(args) -> int:
    from services.app import load_settings

    settings = load_settings()
    robot_cfg = settings.get("robot", {})
    api_cfg = settings.get("api", {})
    simulated = _as_bool(os.environ.get("ROBOT_SIMULATED", robot_cfg.get("simulated", True)))

    if args.test in AUDIO_TESTS:
        from core.audio_test import run_cli_test

        result = await run_cli_test(
            args.test,
            text=args.text,
            duration=args.duration,
            robot_base_url=robot_cfg.get("base_url", ""),
            simulated=simulated,
            robot_api=robot_cfg.get("api"),
            stt_model=api_cfg.get("stt_model"),
        )
        return _print_result(result)

    from core.motion_test import run_cli_motion_test

    result = await run_cli_motion_test(
        args.test,
        direction=args.direction,
        duration_sec=args.move_duration,
        linear_speed=args.linear_speed,
        angular_speed=args.angular_speed,
        robot_base_url=robot_cfg.get("base_url", ""),
        simulated=simulated,
        robot_api=robot_cfg.get("api"),
        settings=settings,
    )
    return _print_result(result)


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM robot navigation service")
    parser.add_argument(
        "--test",
        choices=ALL_TESTS,
        help="Run a hardware test and exit (no web server)",
    )
    parser.add_argument(
        "--text",
        default="Hello, this is a speaker test from the navigation service.",
        help="Text for --test speak",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=5.0,
        help="Mic seconds for --test listen|echo",
    )
    parser.add_argument(
        "--direction",
        default="forward",
        choices=("forward", "backward", "turn_left", "turn_right"),
        help="Direction for --test move",
    )
    parser.add_argument(
        "--move-duration",
        type=float,
        default=1.2,
        help="Move seconds for --test move (capped at 2.5s)",
    )
    parser.add_argument(
        "--linear-speed",
        type=float,
        default=0.25,
        help="Forward/back speed for --test move (capped at 0.35)",
    )
    parser.add_argument(
        "--angular-speed",
        type=float,
        default=0.55,
        help="Turn speed for --test move (capped at 0.8)",
    )
    args = parser.parse_args()

    if args.test:
        return asyncio.run(_run_test(args))

    from services.app import app, load_settings
    import uvicorn

    settings = load_settings()
    host = settings.get("service", {}).get("host", "0.0.0.0")
    port = int(os.environ.get("PORT", settings.get("service", {}).get("port", 8001)))
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
