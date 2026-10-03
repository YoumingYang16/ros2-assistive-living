"""Read-only installation diagnostics; does not record audio or contact models."""
from __future__ import annotations
import importlib.util
import os
import platform
from pathlib import Path
import shutil
import sqlite3
import sys


def collect_diagnostics(config=None, *, probe_ros=False):
    modules = {}
    for name in ("rclpy", "nav2_msgs", "voice_patrol_interfaces", "vosk", "sounddevice"):
        try:
            modules[name] = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            modules[name] = False
    ros = {"installed": modules["rclpy"], "loaded": None, "integration_verified": False}
    if probe_ros:
        try:
            import rclpy
            ros["loaded"] = True
        except Exception as exc:
            ros.update(loaded=False, error_type=type(exc).__name__, error=str(exc)[:1500])
    model = os.environ.get("VOICE_PATROL_VOSK_MODEL", "")
    provider = os.environ.get("VOICE_PATROL_MODEL_PROVIDER", "none")
    from . import __version__
    return {"application": "行知 Voice Patrol", "version": __version__, "python": platform.python_version(),
            "platform": platform.platform(), "sqlite": sqlite3.sqlite_version,
            "modules": modules, "ros": ros,
            "commands": {name: bool(shutil.which(name)) for name in ("ros2", "colcon", "docker", "podman")},
            "voice": {"offline_model_configured": bool(model), "offline_model_directory_exists": bool(model and Path(model).is_dir()),
                      "microphone_tested": False},
            "model": {"provider": provider, "model_name_configured": bool(os.environ.get("VOICE_PATROL_MODEL")),
                      "credential_values_hidden": True},
            "config": {"locations": len(config.get("locations", {})), "routes": len(config.get("patrol_routes", {}))} if config else None}
