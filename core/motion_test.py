"""Safe short-distance motion tests for in-room robot checks."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from core.robot_http import LOOMO_CMD_PATH, RobotHttpClient

logger = logging.getLogger(__name__)

VALID_DIRECTIONS = ("forward", "backward", "turn_left", "turn_right")

# Small capped defaults — tweak in settings.yaml under motion_test
DEFAULTS = {
    "duration_sec": 1.2,
    "linear_speed": 0.25,
    "angular_speed": 0.55,
    "max_duration_sec": 2.5,
    "max_linear_speed": 0.35,
    "max_angular_speed": 0.8,
}

# Gentle room pattern: short forward + turn, repeat (stays in ~1–2 m area)
PATROL_STEPS = (
    ("forward", 1.0, 0.22, 0.55),
    ("turn_left", 0.7, 0.22, 0.55),
    ("forward", 1.0, 0.22, 0.55),
    ("turn_left", 0.7, 0.22, 0.55),
)


@dataclass
class MotionTestResult:
    ok: bool
    mode: str
    direction: str = ""
    duration_sec: float = 0.0
    linear_speed: float = 0.0
    angular_speed: float = 0.0
    steps: list[str] = field(default_factory=list)
    error: str = ""


def _limits(settings: dict | None) -> dict:
    cfg = (settings or {}).get("motion_test") or {}
    out = dict(DEFAULTS)
    for key in out:
        if key in cfg:
            out[key] = float(cfg[key])
    return out


def _clamp(params: dict, limits: dict) -> tuple[float, float, float]:
    duration = max(0.2, min(float(params.get("duration_sec", limits["duration_sec"])), limits["max_duration_sec"]))
    linear = max(0.05, min(float(params.get("linear_speed", limits["linear_speed"])), limits["max_linear_speed"]))
    angular = max(0.3, min(float(params.get("angular_speed", limits["angular_speed"])), limits["max_angular_speed"]))
    return duration, linear, angular


class MotionTestRunner:
    """Run bounded nudge moves — not full navigation."""

    def __init__(self, robot: RobotHttpClient, settings: dict | None = None) -> None:
        self.robot = robot
        self.limits = _limits(settings)

    def _require_live_robot(self) -> None:
        if self.robot.simulated:
            raise RuntimeError(
                "Motion test requires ROBOT_SIMULATED=false and a reachable ROBOT_BASE_URL"
            )

    async def test_move(
        self,
        direction: str,
        *,
        duration_sec: float | None = None,
        linear_speed: float | None = None,
        angular_speed: float | None = None,
    ) -> MotionTestResult:
        direction = (direction or "forward").strip().lower().replace("-", "_")
        if direction not in VALID_DIRECTIONS:
            return MotionTestResult(
                ok=False,
                mode="move",
                direction=direction,
                error=f"Invalid direction. Use one of: {', '.join(VALID_DIRECTIONS)}",
            )

        duration, linear, angular = _clamp(
            {
                "duration_sec": duration_sec if duration_sec is not None else self.limits["duration_sec"],
                "linear_speed": linear_speed if linear_speed is not None else self.limits["linear_speed"],
                "angular_speed": angular_speed if angular_speed is not None else self.limits["angular_speed"],
            },
            self.limits,
        )

        steps = ["connect"]
        try:
            self._require_live_robot()
        except RuntimeError as exc:
            return MotionTestResult(ok=False, mode="move", direction=direction, error=str(exc), steps=steps)

        if not await self.robot.connect():
            return MotionTestResult(
                ok=False,
                mode="move",
                direction=direction,
                error="robot connect failed",
                steps=steps,
            )

        try:
            steps.append(
                f"{direction} — one POST {LOOMO_CMD_PATH} move, "
                f"wait {duration:.1f}s (linear={linear:.2f}, angular={angular:.2f}), then stop"
                if self.robot.api == "loomo"
                else f"{direction} {duration:.1f}s via POST /api/c2/move"
            )
            await self.robot.nudge(
                direction,
                duration_sec=duration,
                linear_speed=linear,
                angular_speed=angular,
            )
            steps.append("stop")
            steps.append("done")
            return MotionTestResult(
                ok=True,
                mode="move",
                direction=direction,
                duration_sec=duration,
                linear_speed=linear,
                angular_speed=angular,
                steps=steps,
            )
        except Exception as exc:
            logger.exception("test_move failed")
            await self.robot.stop()
            return MotionTestResult(
                ok=False,
                mode="move",
                direction=direction,
                duration_sec=duration,
                error=str(exc),
                steps=steps,
            )
        finally:
            await self.robot.disconnect()

    async def test_patrol(self) -> MotionTestResult:
        """Small in-room loop: two short forwards with two quarter turns."""
        steps = ["connect", "patrol sequence (4 nudges, capped speeds)"]
        try:
            self._require_live_robot()
        except RuntimeError as exc:
            return MotionTestResult(ok=False, mode="patrol", error=str(exc), steps=steps)

        if not await self.robot.connect():
            return MotionTestResult(ok=False, mode="patrol", error="robot connect failed", steps=steps)

        try:
            for index, (direction, duration, linear, angular) in enumerate(PATROL_STEPS, start=1):
                duration, linear, angular = _clamp(
                    {
                        "duration_sec": duration,
                        "linear_speed": linear,
                        "angular_speed": angular,
                    },
                    self.limits,
                )
                steps.append(f"{index}. {direction} {duration:.1f}s")
                await self.robot.nudge(
                    direction,
                    duration_sec=duration,
                    linear_speed=linear,
                    angular_speed=angular,
                )
                await asyncio.sleep(0.35)

            steps.append("done")
            return MotionTestResult(ok=True, mode="patrol", steps=steps)
        except Exception as exc:
            logger.exception("test_patrol failed")
            await self.robot.stop()
            return MotionTestResult(ok=False, mode="patrol", error=str(exc), steps=steps)
        finally:
            await self.robot.disconnect()

    async def test_stop(self) -> MotionTestResult:
        steps = ["connect", "emergency stop"]
        try:
            self._require_live_robot()
        except RuntimeError as exc:
            return MotionTestResult(ok=False, mode="stop", error=str(exc), steps=steps)

        if not await self.robot.connect():
            return MotionTestResult(ok=False, mode="stop", error="robot connect failed", steps=steps)

        try:
            await self.robot.stop()
            steps.append("done")
            return MotionTestResult(ok=True, mode="stop", steps=steps)
        except Exception as exc:
            return MotionTestResult(ok=False, mode="stop", error=str(exc), steps=steps)
        finally:
            await self.robot.disconnect()


async def run_cli_motion_test(
    mode: str,
    *,
    direction: str = "forward",
    duration_sec: float | None = None,
    linear_speed: float | None = None,
    angular_speed: float | None = None,
    robot_base_url: str,
    simulated: bool,
    robot_api: str | None = None,
    settings: dict | None = None,
) -> MotionTestResult:
    robot = RobotHttpClient(robot_base_url, simulated=simulated, api=robot_api)
    runner = MotionTestRunner(robot, settings)

    if mode == "move":
        return await runner.test_move(
            direction,
            duration_sec=duration_sec,
            linear_speed=linear_speed,
            angular_speed=angular_speed,
        )
    if mode == "patrol":
        return await runner.test_patrol()
    if mode == "stop":
        return await runner.test_stop()
    raise ValueError(f"Unknown motion test mode: {mode}. Use move, patrol, or stop.")
