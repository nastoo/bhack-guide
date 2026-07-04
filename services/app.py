"""LLM robot navigation service — GPS instructions via OpenAPI-planned HTTP calls."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.audio_test import AudioTestRunner
from core import gps_stream_client
from core.motion_test import MotionTestRunner, VALID_DIRECTIONS
from core import manual_route
from core.gps_motion import linear_speed_kmh, linear_speed_ms
from core.navigation_loop import NavigationLoop
from core.robot_http import LOOMO_CMD_PATH, RobotHttpClient
from core.robot_agent import DirectRobotAgent
from core import robot_profiles
from core.voice_agent import VoiceAgent
from core.wake_listener import WakeListener
from core.wake_word import extract_wake_command, set_wake_phrases, wake_phrases
from core.auth import setup_auth, user_from_websocket

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

STATIC_DIR = PROJECT_ROOT / "static"
SETTINGS_PATH = PROJECT_ROOT / "configs" / "settings.yaml"

navigation_loop: NavigationLoop | None = None
robot_client: RobotHttpClient | None = None
voice_agent: VoiceAgent | None = None
robot_agent: DirectRobotAgent | None = None
wake_listener: WakeListener | None = None
_route_task: asyncio.Task | None = None
_route_stop: asyncio.Event | None = None
_selected_robot: str = "loomo"
_main_loop: asyncio.AbstractEventLoop | None = None
_ws_clients: set[WebSocket] = set()


def _expand_env(value: str) -> str:
    if not isinstance(value, str):
        return value
    if value.startswith("${") and value.endswith("}"):
        inner = value[2:-1]
        if ":-" in inner:
            key, default = inner.split(":-", 1)
            return os.environ.get(key, default)
        return os.environ.get(inner, "")
    return value


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_settings() -> dict:
    data = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            return _expand_env(node)
        return node

    return walk(data)


def _apply_robot_selection(robot_id: str) -> dict:
    """Switch the shared client to go1 (dog) or loomo."""
    global _selected_robot
    if robot_client is None:
        raise RuntimeError("Robot client not ready")
    settings = load_settings()
    profile = robot_profiles.apply_profile(robot_client, robot_id, settings)
    _selected_robot = robot_id
    if robot_agent is not None:
        robot_agent.set_profile(robot_id, profile)
    logger.info(
        "Robot selected: %s (%s) → %s",
        profile.get("label"),
        profile.get("api"),
        profile.get("base_url"),
    )
    return profile


async def broadcast(payload: dict) -> None:
    if not _ws_clients:
        return
    message = json.dumps(payload)
    dead: list[WebSocket] = []
    for ws in list(_ws_clients):
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


async def _wake_debug(status: str, **fields) -> None:
    """Log wake-word pipeline steps to server logs and the dashboard."""
    detail = " ".join(f"{k}={v!r}" for k, v in fields.items() if v not in (None, ""))
    logger.info("Wake [%s]%s", status, f" {detail}" if detail else "")
    await broadcast({"type": "wake_listener", "status": status, **fields})


def _parse_manual_commands(
    *,
    route_text: str | None,
    steps: list[dict] | None,
    settings: dict,
    use_llm_fallback: bool = True,
) -> tuple[list, str]:
    if steps:
        return manual_route.parse_structured_steps(steps, settings=settings), "json"
    if route_text and route_text.strip():
        api_cfg = settings.get("api", {})
        return manual_route.parse_route_text(
            route_text,
            settings=settings,
            model=api_cfg.get("model", "chat-vl-large"),
            use_llm_fallback=use_llm_fallback,
        )
    raise ValueError("Provide route_text (one step per line) or steps (JSON array)")


async def _cancel_route_task() -> None:
    global _route_task, _route_stop
    if _route_stop is not None:
        _route_stop.set()
    if navigation_loop is not None:
        navigation_loop.stop()
    if _route_task is not None and not _route_task.done():
        _route_task.cancel()
        try:
            await _route_task
        except asyncio.CancelledError:
            pass
    if robot_client is not None:
        await robot_client.stop()
    _route_task = None
    _route_stop = None


async def _run_route_background(
    name: str,
    commands: list,
    *,
    total_distance: str,
    summary: str,
    speak: bool = True,
) -> None:
    global _route_stop
    _route_stop = asyncio.Event()

    async def on_step(index: int, total: int, command) -> None:
        await broadcast(
            {
                "type": "navigation_update",
                "active": True,
                "route_provider": "manual",
                "status": "navigating",
                "destination": name,
                "place_name": name,
                "step_index": index,
                "step_total": total,
                "current_instruction": command.label,
                "last_http": command.label,
                "last_speech": command.label if speak else "",
                "message": f"Step {index}/{total}: {command.label}",
                "total_distance": total_distance,
                "total_duration": f"{total} steps",
                "motion_mode": "distance",
            }
        )

    await broadcast(
        {
            "type": "navigation_update",
            "active": True,
            "route_provider": "manual",
            "status": "connecting",
            "destination": name,
            "place_name": name,
            "step_total": len(commands),
            "step_index": 0,
            "total_distance": total_distance,
            "total_duration": f"{len(commands)} steps",
            "message": f"Starting route ({len(commands)} steps)…",
            "last_speech": "",
            "motion_mode": "distance",
        }
    )

    try:
        if robot_agent is None:
            raise RuntimeError("Robot agent not ready")
        if speak:
            intro = f"Starting manual route. {len(commands)} steps, about {total_distance}."
            async with robot_agent.robot.motion_lock():
                await robot_agent.robot.connect()
                await robot_agent.robot._stream_speech_unlocked(intro)
        await robot_agent.run_route(
            commands,
            stop_event=_route_stop,
            on_step=on_step,
            speak=speak,
        )
        if _route_stop and _route_stop.is_set():
            final_status = "stopped"
            message = "Route stopped."
        else:
            final_status = "arrived"
            message = f"Route {name!r} complete."
            if speak:
                async with robot_agent.robot.motion_lock():
                    await robot_agent.robot._stream_speech_unlocked(f"Arrived at {name}.")
    except asyncio.CancelledError:
        final_status = "stopped"
        message = "Route cancelled."
    except Exception as exc:
        logger.exception("Robot route failed")
        final_status = "error"
        message = str(exc)

    await broadcast(
        {
            "type": "navigation_update",
            "active": False,
            "route_provider": "manual",
            "status": final_status,
            "destination": name,
            "place_name": name,
            "step_total": len(commands),
            "message": message,
            "last_speech": "",
            "motion_mode": "distance",
        }
    )


async def _start_forced_route(
    route_text: str,
    *,
    user_message: str = "",
    name: str = "Forced route",
) -> dict:
    """Parse and run a preset route, ignoring whatever the user asked."""
    global _route_task

    settings = load_settings()
    try:
        commands, parser = _parse_manual_commands(
            route_text=route_text,
            steps=None,
            settings=settings,
        )
    except ValueError as exc:
        return {"reply": str(exc), "steps": 0, "forced": True, "error": str(exc)}

    total_distance, summary = manual_route.summarize_route(commands)
    await _cancel_route_task()
    _route_task = asyncio.create_task(
        _run_route_background(
            name,
            commands,
            total_distance=total_distance,
            summary=summary,
            speak=True,
        )
    )

    preview = user_message.strip() or "your request"
    reply = (
        f"Force route enabled — running preset route ({len(commands)} steps, {total_distance}) "
        f"instead of: {preview!r}. Plan: {summary}"
    )
    return {
        "reply": reply,
        "steps": len(commands),
        "parser": parser,
        "summary": summary,
        "forced": True,
        "speed_kmh": linear_speed_kmh(settings),
        "speed_ms": round(linear_speed_ms(settings), 3),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    global navigation_loop, robot_client, voice_agent, robot_agent, wake_listener, _main_loop, _route_task, _selected_robot

    _main_loop = asyncio.get_running_loop()
    settings = load_settings()
    robot_cfg = settings.get("robot", {})
    api_cfg = settings.get("api", {})

    robot_client = RobotHttpClient(
        robot_cfg.get("base_url", ""),
        simulated=_as_bool(robot_cfg.get("simulated", True)),
        api=robot_cfg.get("api"),
    )
    _selected_robot = robot_profiles.default_robot_id(settings)
    initial_profile = _apply_robot_selection(_selected_robot)
    navigation_loop = NavigationLoop(
        settings,
        robot_client,
        model=api_cfg.get("model", "chat-vl-large"),
        on_update=broadcast,
    )
    voice_agent = VoiceAgent(
        navigation_loop,
        robot_client,
        loop=_main_loop,
        model=api_cfg.get("model", "chat-vl-large"),
    )
    robot_agent = DirectRobotAgent(
        robot_client,
        settings=settings,
        loop=_main_loop,
        model=api_cfg.get("model", "chat-vl-large"),
        robot_id=_selected_robot,
        profile=initial_profile,
    )

    async def _wake_on_command(command: str) -> dict:
        if voice_agent is None:
            return {"reply": "Voice agent not ready."}
        return await voice_agent.handle(command)

    wake_cfg = settings.get("wake_word") or {}
    set_wake_phrases(wake_cfg.get("phrases"))
    wake_listener = WakeListener(
        robot_client,
        on_command=_wake_on_command,
        on_event=broadcast,
        poll_interval_sec=float(wake_cfg.get("poll_interval_sec", 0.4)),
        capture_duration_sec=float(wake_cfg.get("capture_duration_sec", 5.0)),
        cooldown_sec=float(wake_cfg.get("cooldown_sec", 3.0)),
    )
    if _as_bool(wake_cfg.get("auto_start", False)) and not _as_bool(robot_cfg.get("simulated", True)):
        await wake_listener.start()

    from core import gps_navigation

    nav = settings.get("navigation") or {}
    if gps_navigation.live_gps_enabled(settings):
        gps_cfg = gps_stream_client.stream_settings(settings)
        logger.info(
            "Navigation motion: live GPS (experimental) via %s/api/location",
            gps_cfg["url"],
        )
    else:
        logger.info(
            "Navigation motion: distance-based (origin=%s, %s, heading=%s°, speed=%s km/h)",
            nav.get("origin_lat"),
            nav.get("origin_lng"),
            nav.get("origin_heading_deg", 0),
            nav.get("gps_speed_kmh", 3),
        )

    logger.info(
        "LLM navigation service started (robot=%s/%s, api=%s, simulated=%s, llm=%s)",
        _selected_robot,
        initial_profile.get("base_url"),
        robot_client.api,
        _as_bool(robot_cfg.get("simulated", True)),
        api_cfg.get("model", "chat-vl-large"),
    )
    yield
    if wake_listener is not None:
        await wake_listener.stop()
    await _cancel_route_task()
    if navigation_loop is not None:
        navigation_loop.stop()
    logger.info("LLM navigation service stopped")


app = FastAPI(title="LLM Robot Navigation", lifespan=lifespan)
auth_settings = setup_auth(app, load_settings())
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class NavigationStartRequest(BaseModel):
    destination: str


class ManualNavigationRequest(BaseModel):
    name: str = "Manual route"
    route_text: str | None = None
    steps: list[dict] | None = None
    use_llm_fallback: bool = True
    speak: bool = True


class AgentChatRequest(BaseModel):
    message: str
    force_route: bool = False
    route_text: str | None = None


class AgentListenRequest(BaseModel):
    duration: float = 5.0
    force_route: bool = False
    route_text: str | None = None
    require_wake_word: bool = True


class WakeListenerRequest(BaseModel):
    enabled: bool = True


class RobotSelectRequest(BaseModel):
    robot: str


class RobotChatRequest(BaseModel):
    message: str
    reset: bool = False
    force_route: bool = False
    route_text: str | None = None


class RobotRouteRequest(BaseModel):
    name: str = "Manual route"
    route_text: str | None = None
    steps: list[dict] | None = None
    use_llm_fallback: bool = True
    speak: bool = True


class TestSpeakRequest(BaseModel):
    text: str = "Hello, this is a speaker test."


class TestListenRequest(BaseModel):
    duration: float = 5.0


class TestEchoRequest(BaseModel):
    duration: float = 5.0
    prefix: str = "You said: "


class TestMoveRequest(BaseModel):
    direction: str = "forward"
    duration_sec: float = 1.2
    linear_speed: float = 0.25
    angular_speed: float = 0.55


def _audio_tester() -> AudioTestRunner:
    if robot_client is None:
        raise HTTPException(status_code=503, detail="Robot client not ready")
    settings = load_settings()
    api_cfg = settings.get("api", {})
    return AudioTestRunner(
        robot_client,
        stt_model=api_cfg.get("stt_model"),
    )


def _motion_tester() -> MotionTestRunner:
    if robot_client is None:
        raise HTTPException(status_code=503, detail="Robot client not ready")
    return MotionTestRunner(robot_client, load_settings())


def _test_response(result) -> dict:
    payload = {
        "ok": result.ok,
        "mode": result.mode,
        "steps": result.steps,
    }
    if getattr(result, "heard", ""):
        payload["heard"] = result.heard
    if getattr(result, "spoken", ""):
        payload["spoken"] = result.spoken
    if getattr(result, "direction", ""):
        payload["direction"] = result.direction
    if getattr(result, "duration_sec", 0):
        payload["duration_sec"] = result.duration_sec
    if getattr(result, "linear_speed", 0):
        payload["linear_speed"] = result.linear_speed
    if getattr(result, "angular_speed", 0):
        payload["angular_speed"] = result.angular_speed
    if getattr(result, "error", ""):
        payload["error"] = result.error
    return payload


@app.get("/")
def index():
    return RedirectResponse(url="/static/index.html")


@app.get("/test")
def test_page():
    return RedirectResponse(url="/static/test.html")


@app.get("/robot")
def robot_page():
    return RedirectResponse(url="/static/robot.html")


@app.get("/settings")
def settings_page():
    return RedirectResponse(url="/static/settings.html")


@app.get("/health")
def health():
    settings = load_settings()
    phone = None
    location_source = "hardcoded"
    if gps_stream_client.is_configured(settings):
        phone = gps_stream_client.fetch_latest(settings)
        if phone and gps_stream_client.is_live(phone, settings):
            location_source = "phone GPS (live)"
        else:
            nav = settings.get("navigation") or {}
            location_source = f"hardcoded origin ({nav.get('origin_label', 'configured origin')})"
    else:
        nav = settings.get("navigation") or {}
        location_source = f"hardcoded origin ({nav.get('origin_label', 'configured origin')})"

    profile = robot_profiles.get_profile(_selected_robot, settings) if robot_client else {}
    return {
        "status": "ok",
        "navigation_active": navigation_loop.state.active if navigation_loop else False,
        "robot_simulated": robot_client.simulated if robot_client else True,
        "robot_api": robot_client.api if robot_client else "go1",
        "robot_selected": _selected_robot,
        "robot_label": profile.get("label", _selected_robot),
        "robot_base_url": robot_client.base_url if robot_client else "",
        "phone_gps_configured": gps_stream_client.is_configured(settings),
        "phone_gps_live": bool(
            phone and gps_stream_client.is_live(phone, settings)
        ),
        "location_source": location_source,
    }


@app.get("/api/robots")
def api_robots_list():
    settings = load_settings()
    profiles = robot_profiles.list_profiles(settings)
    return {
        "ok": True,
        "selected": _selected_robot,
        "robots": profiles,
    }


@app.post("/api/robot/select")
async def api_robot_select(body: RobotSelectRequest):
    try:
        profile = _apply_robot_selection(body.robot)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "selected": _selected_robot,
        "robot": profile,
    }


@app.get("/api/gps/phone")
def api_gps_phone():
    """Proxy latest fix from the phone_gps service."""
    settings = load_settings()
    if not gps_stream_client.is_configured(settings):
        return {
            "ok": False,
            "configured": False,
            "message": "Set GPS_STREAM_URL to your phone_gps service (e.g. http://localhost:8080)",
        }
    fix = gps_stream_client.fetch_latest(settings)
    live = gps_stream_client.is_live(fix, settings)
    return {
        "ok": fix is not None,
        "configured": True,
        "live": live,
        "location": fix,
        "stream_url": gps_stream_client.stream_settings(settings)["url"],
    }


@app.get("/api/navigation/status")
def api_navigation_status():
    if navigation_loop is None:
        return {"status": "starting"}
    return navigation_loop.state.__dict__


@app.post("/api/navigation/start")
async def api_navigation_start(body: NavigationStartRequest):
    if navigation_loop is None:
        raise HTTPException(status_code=503, detail="Navigation not ready")
    destination = (body.destination or "").strip()
    if not destination:
        raise HTTPException(status_code=400, detail="destination is required")
    try:
        state = await navigation_loop.start(destination)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "navigation": state.__dict__}


@app.post("/api/robot/route")
async def api_robot_route(body: RobotRouteRequest):
    """Run a manual route using the same motion engine as /robot (no speech)."""
    global _route_task

    if robot_agent is None or robot_client is None:
        raise HTTPException(status_code=503, detail="Robot agent not ready")

    name = (body.name or "Manual route").strip() or "Manual route"
    settings = load_settings()

    try:
        commands, parser = _parse_manual_commands(
            route_text=body.route_text,
            steps=body.steps,
            settings=settings,
            use_llm_fallback=body.use_llm_fallback,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await _cancel_route_task()
    total_distance, summary = manual_route.summarize_route(commands)
    _route_task = asyncio.create_task(
        _run_route_background(
            name,
            commands,
            total_distance=total_distance,
            summary=summary,
            speak=body.speak,
        )
    )

    return {
        "ok": True,
        "parser": parser,
        "summary": summary,
        "total_distance": total_distance,
        "steps": len(commands),
        "speak": body.speak,
        "message": (
            "Route started — speak each step, then move."
            if body.speak
            else "Route started — motion only."
        ),
    }


@app.post("/api/robot/route/stop")
async def api_robot_route_stop():
    await _cancel_route_task()
    return {"ok": True}


@app.post("/api/navigation/manual")
async def api_navigation_manual(body: ManualNavigationRequest):
    """Legacy alias — forwards to /api/robot/route."""
    return await api_robot_route(
        RobotRouteRequest(
            name=body.name,
            route_text=body.route_text,
            steps=body.steps,
            use_llm_fallback=body.use_llm_fallback,
            speak=body.speak,
        )
    )


@app.post("/api/navigation/stop")
async def api_navigation_stop():
    await _cancel_route_task()
    if navigation_loop is not None:
        navigation_loop.stop()
    return {"ok": True}


@app.post("/api/agent/chat")
async def api_agent_chat(body: AgentChatRequest):
    if voice_agent is None:
        raise HTTPException(status_code=503, detail="Voice agent not ready")
    if body.force_route and body.route_text and body.route_text.strip():
        result = await _start_forced_route(body.route_text, user_message=body.message)
        return {"ok": True, "heard": body.message, **result}
    result = await voice_agent.handle(body.message)
    return {"ok": True, **result}


@app.post("/api/robot/chat")
async def api_robot_chat(body: RobotChatRequest):
    if robot_agent is None:
        raise HTTPException(status_code=503, detail="Robot agent not ready")
    if body.force_route and body.route_text and body.route_text.strip():
        if body.reset:
            robot_agent.reset_history()
        result = await _start_forced_route(body.route_text, user_message=body.message)
        return {"ok": True, **result}
    result = await robot_agent.handle(body.message, reset=body.reset)
    return {"ok": True, **result}


@app.post("/api/robot/reset")
async def api_robot_reset():
    if robot_agent is None:
        raise HTTPException(status_code=503, detail="Robot agent not ready")
    robot_agent.reset_history()
    return {"ok": True}


@app.post("/api/agent/listen")
async def api_agent_listen(body: AgentListenRequest):
    if voice_agent is None or robot_client is None:
        raise HTTPException(status_code=503, detail="Voice agent not ready")
    if robot_client.simulated:
        raise HTTPException(
            status_code=501,
            detail="Voice listen requires ROBOT_SIMULATED=false. Use /api/agent/chat for text.",
        )

    await _wake_debug("capturing", duration=body.duration, source="listen_once")
    wav = await robot_client.capture_audio(body.duration)
    if not wav:
        await _wake_debug("no_audio", source="listen_once")
        return {"ok": False, "heard": "", "reply": "I did not hear anything."}

    from core.api_client import transcribe_audio

    await _wake_debug("transcribing", audio_bytes=len(wav), source="listen_once")
    heard = await asyncio.get_event_loop().run_in_executor(None, transcribe_audio, wav)
    heard = (heard or "").strip()
    if not heard:
        await _wake_debug("silent", audio_bytes=len(wav), source="listen_once")
        return {"ok": False, "heard": "", "reply": "I did not hear anything."}

    await _wake_debug("heard", heard=heard, source="listen_once")

    command = heard
    wake_reason = ""
    if body.require_wake_word:
        command, wake_reason = extract_wake_command(heard)
        if command is None:
            await _wake_debug(
                "ignored",
                heard=heard,
                reason=wake_reason,
                source="listen_once",
            )
            return {
                "ok": True,
                "heard": heard,
                "reply": f'Say "Hi Loomo" first — e.g. "Hi Loomo, take me to the station". ({wake_reason})',
                "wake_triggered": False,
                "wake_reason": wake_reason,
            }

    await _wake_debug(
        "triggered",
        heard=heard,
        command=command,
        source="listen_once",
    )

    if body.force_route and body.route_text and body.route_text.strip():
        result = await _start_forced_route(body.route_text, user_message=command)
        await _wake_debug(
            "replied",
            heard=heard,
            command=command,
            reply=result.get("reply", ""),
            source="listen_once",
        )
        return {"ok": True, "heard": heard, "command": command, "wake_triggered": True, **result}
    result = await voice_agent.handle(command)
    await _wake_debug(
        "replied",
        heard=heard,
        command=command,
        reply=result.get("reply", ""),
        source="listen_once",
    )
    return {"ok": True, "command": command, "wake_triggered": True, **result}


async def _process_local_mic_upload(
    audio_bytes: bytes,
    filename: str,
    *,
    require_wake_word: bool,
    run_agent: bool,
) -> dict:
    """Transcribe browser mic audio and optionally run Hi Loomo wake + agent."""
    if not audio_bytes:
        await _wake_debug("no_audio", source="local_mic")
        return {"ok": False, "heard": "", "reply": "No audio uploaded.", "source": "local_mic"}

    from core.api_client import transcribe_audio

    await _wake_debug("transcribing", audio_bytes=len(audio_bytes), source="local_mic")
    heard = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: transcribe_audio(audio_bytes, filename=filename),
    )
    heard = (heard or "").strip()
    if not heard:
        await _wake_debug("silent", audio_bytes=len(audio_bytes), source="local_mic")
        return {
            "ok": False,
            "heard": "",
            "reply": "Whisper returned empty.",
            "source": "local_mic",
        }

    await _wake_debug("heard", heard=heard, source="local_mic")

    if not require_wake_word:
        result = {"ok": True, "heard": heard, "source": "local_mic", "wake_triggered": None}
        if run_agent:
            if voice_agent is None:
                raise HTTPException(status_code=503, detail="Voice agent not ready")
            agent_result = await voice_agent.handle(heard)
            result.update(agent_result)
            result["wake_triggered"] = True
            result["command"] = heard
        return result

    command, wake_reason = extract_wake_command(heard)
    if command is None:
        await _wake_debug("ignored", heard=heard, reason=wake_reason, source="local_mic")
        return {
            "ok": True,
            "heard": heard,
            "reply": f'Say "Hi Loomo" first — e.g. "Hi Loomo, take me to the station". ({wake_reason})',
            "wake_triggered": False,
            "wake_reason": wake_reason,
            "source": "local_mic",
        }

    await _wake_debug("triggered", heard=heard, command=command, source="local_mic")

    if not run_agent:
        return {
            "ok": True,
            "heard": heard,
            "command": command,
            "wake_triggered": True,
            "reply": f'Wake phrase OK — command: "{command}"',
            "source": "local_mic",
        }

    if voice_agent is None:
        raise HTTPException(status_code=503, detail="Voice agent not ready")
    agent_result = await voice_agent.handle(command)
    await _wake_debug(
        "replied",
        heard=heard,
        command=command,
        reply=agent_result.get("reply", ""),
        source="local_mic",
    )
    return {
        "ok": True,
        "heard": heard,
        "command": command,
        "wake_triggered": True,
        "source": "local_mic",
        **agent_result,
    }


@app.get("/api/agent/wake")
def api_agent_wake_status():
    if wake_listener is None:
        raise HTTPException(status_code=503, detail="Wake listener not ready")
    state = wake_listener.state
    return {
        "ok": True,
        "running": wake_listener.running,
        "last_transcript": state.last_transcript,
        "last_command": state.last_command,
        "last_reply": state.last_reply,
        "last_status": state.last_status,
        "triggers": state.triggers,
        "ignored": state.ignored,
        "errors": state.errors,
        "phrase": wake_phrases()[0] if wake_phrases() else "hi loomo",
        "phrases": list(wake_phrases()),
    }


@app.post("/api/agent/wake")
async def api_agent_wake_toggle(body: WakeListenerRequest):
    if wake_listener is None or robot_client is None:
        raise HTTPException(status_code=503, detail="Wake listener not ready")
    if robot_client.simulated:
        raise HTTPException(
            status_code=501,
            detail='Always-on wake word requires ROBOT_SIMULATED=false. Say "Hi Loomo, …" on the robot mic.',
        )
    if body.enabled:
        await wake_listener.start()
    else:
        await wake_listener.stop()
    return {"ok": True, "running": wake_listener.running}


@app.get("/api/test")
def api_test_info():
    return {
        "modes": {
            "speak": f"POST /api/test/speak — Loomo POST {LOOMO_CMD_PATH} speak",
            "listen": "POST /api/test/listen — robot mic → Whisper",
            "local_mic": "POST /api/test/local-mic — your computer mic → Whisper (test only)",
            "echo": f"POST /api/test/echo — mic → Whisper → {LOOMO_CMD_PATH} speak",
            "move": f"POST /api/test/move — short nudge ({', '.join(VALID_DIRECTIONS)})",
            "patrol": "POST /api/test/patrol — small in-room loop (4 nudges)",
            "stop": "POST /api/test/stop — emergency stop",
        },
        "cli": {
            "speak": "python launch.py --test speak --text 'Hello'",
            "listen": "python launch.py --test listen --duration 5",
            "echo": "python launch.py --test echo --duration 5",
            "move": "python launch.py --test move --direction forward --move-duration 1.2",
            "patrol": "python launch.py --test patrol",
            "stop": "python launch.py --test stop",
        },
        "requires": "ROBOT_SIMULATED=false for hardware tests",
    }


@app.post("/api/test/speak")
async def api_test_speak(body: TestSpeakRequest):
    result = await _audio_tester().test_speak(body.text)
    return _test_response(result)


@app.post("/api/test/listen")
async def api_test_listen(body: TestListenRequest):
    result = await _audio_tester().test_listen(body.duration)
    return _test_response(result)


@app.post("/api/test/local-mic")
async def api_test_local_mic(
    audio: UploadFile = File(...),
    require_wake_word: bool = Form(default=True),
    run_agent: bool = Form(default=False),
):
    """Test Whisper + Hi Loomo using audio recorded in the browser (no robot mic)."""
    audio_bytes = await audio.read()
    filename = audio.filename or "local-mic.webm"
    return await _process_local_mic_upload(
        audio_bytes,
        filename,
        require_wake_word=require_wake_word,
        run_agent=run_agent,
    )


@app.post("/api/test/echo")
async def api_test_echo(body: TestEchoRequest):
    result = await _audio_tester().test_echo(body.duration, prefix=body.prefix)
    return _test_response(result)


@app.post("/api/test/move")
async def api_test_move(body: TestMoveRequest):
    result = await _motion_tester().test_move(
        body.direction,
        duration_sec=body.duration_sec,
        linear_speed=body.linear_speed,
        angular_speed=body.angular_speed,
    )
    return _test_response(result)


@app.post("/api/test/patrol")
async def api_test_patrol():
    result = await _motion_tester().test_patrol()
    return _test_response(result)


@app.post("/api/test/stop")
async def api_test_stop():
    result = await _motion_tester().test_stop()
    return _test_response(result)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if auth_settings.enabled and not user_from_websocket(websocket, auth_settings):
        await websocket.close(code=1008, reason="Not authenticated")
        return
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        if navigation_loop is not None:
            await websocket.send_text(
                json.dumps({"type": "navigation_update", **navigation_loop.state.__dict__})
            )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)


if __name__ == "__main__":
    settings = load_settings()
    host = settings.get("service", {}).get("host", "0.0.0.0")
    port = int(os.environ.get("PORT", settings.get("service", {}).get("port", 8001)))
    uvicorn.run(app, host=host, port=port)
