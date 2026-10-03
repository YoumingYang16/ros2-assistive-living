"""Load and validate the JSON configuration shared by mock and ROS adapters.

Schema (all distances are metres, yaw is radians, and durations are seconds):
``locations`` maps identifiers to ``label, aliases, x, y, yaw``; labels, aliases
and identifiers must resolve unambiguously. ``patrol_routes`` maps identifiers
to ``label, waypoints, dwell_seconds`` and must include ``default``. Durations
``navigation_timeout`` and ``inspection_timeout`` are positive finite numbers;
``max_retries`` is an integer in 0..3. ``object_names`` defines the bounded
inspection vocabulary. ``mock`` provides travel/inspection delays and a map
of location identifiers to observed object names. Unknown fields are rejected
so misspelled settings cannot silently change operation.

Optional ``ros`` selects the navigation action, pose/inspection topics, map
frame and cancellation timeout (0.1..30 seconds). Topic/action names may be
absolute or relative and contain slash-separated ROS identifier tokens.

``load_config(None)`` finds the bundled default in the source tree or the ROS
package share directory; explicitly supplied paths always take precedence.
"""
from __future__ import annotations

import copy
from importlib import metadata
import json
import math
from pathlib import Path
import re
import sys
from typing import Any
import unicodedata


class ConfigError(ValueError):
    """The configuration cannot safely or unambiguously be used."""


def _number(value: Any, name: str, *, low: float | None = None, high: float | None = None,
            positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} 必须是有限数字")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ConfigError(f"{name} 必须是有限数字")
    if positive and value <= 0:
        raise ConfigError(f"{name} 必须大于 0")
    if low is not None and value < low or high is not None and value > high:
        raise ConfigError(f"{name} 超出允许范围 {low}..{high}")
    return float(value)


def _mapping(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} 必须是对象")
    return value


def _keys(value: dict, allowed: set[str], required: set[str], name: str) -> None:
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise ConfigError(f"{name} 包含未知字段: {', '.join(sorted(unknown))}")
    if missing:
        raise ConfigError(f"{name} 缺少字段: {', '.join(sorted(missing))}")


def _name(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 40:
        raise ConfigError(f"{name} 必须是 1..40 个字符的字符串")
    if unicodedata.normalize("NFKC", value) != value:
        raise ConfigError(f"{name} 请使用标准字符，避免全角字符导致名称混淆")
    if re.search(r"[\s，,。.!！?？、;；]", value) or any(
        token in value for token in ("然后", "接着", "之后", "最后", "并", "不", "别", "禁止", "无需")
    ):
        raise ConfigError(f"{name} 不得包含空白、标点或指令连接词")
    return value


def validate_config(data: Any) -> dict:
    """Return an independent normalized copy, raising ConfigError on errors."""
    config = copy.deepcopy(_mapping(data, "config"))
    required = {"locations", "patrol_routes", "navigation_timeout", "inspection_timeout", "max_retries", "mock"}
    _keys(config, required | {"version", "object_names", "ros"}, required, "config")
    if config.get("version", 1) != 1 or isinstance(config.get("version", 1), bool):
        raise ConfigError("仅支持配置 version=1")
    config["version"] = 1
    for key in ("navigation_timeout", "inspection_timeout"):
        config[key] = _number(config[key], key, positive=True, high=3600)
    retries = config["max_retries"]
    if type(retries) is not int or not 0 <= retries <= 3:
        raise ConfigError("max_retries 必须是 0..3 的整数")
    locations = _mapping(config["locations"], "locations")
    if not locations or "home" not in locations or len(locations) > 100:
        raise ConfigError("locations 必须包含 home，且地点总数应为 1..100")
    names: dict[str, str] = {}
    for identifier, location in locations.items():
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", identifier):
            raise ConfigError("地点 ID 只能使用英文字母、数字及下划线，且以字母开头")
        location = _mapping(location, f"locations.{identifier}")
        _keys(location, {"label", "aliases", "x", "y", "yaw"}, {"label", "x", "y", "yaw"}, identifier)
        label = _name(location["label"], f"{identifier}.label")
        aliases = location.setdefault("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > 20:
            raise ConfigError(f"{identifier}.aliases 必须是最多 20 项的列表")
        local_names = set()
        for alias in [label, *aliases]:
            alias = _name(alias, f"{identifier}.aliases")
            if alias in local_names:
                raise ConfigError(f"地点 {identifier} 重复使用名称 {alias}")
            local_names.add(alias)
        for alias in local_names | {identifier}:
            if identifier != "home" and alias in {"起点", "基地", "充电点"}:
                raise ConfigError(f"{alias} 是 home 的保留名称")
            if alias in names and names[alias] != identifier:
                raise ConfigError(f"地点名称 {alias} 同时对应 {names[alias]} 和 {identifier}")
            names[alias] = identifier
        for axis in ("x", "y", "yaw"):
            location[axis] = _number(location[axis], f"{identifier}.{axis}")
    routes = _mapping(config["patrol_routes"], "patrol_routes")
    if not routes or "default" not in routes or len(routes) > 30:
        raise ConfigError("patrol_routes 必须包含 default，且路线总数应为 1..30")
    route_names: dict[str, str] = {}
    for identifier, route in routes.items():
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", identifier):
            raise ConfigError("巡逻路线 ID 格式无效")
        route = _mapping(route, f"patrol_routes.{identifier}")
        _keys(route, {"label", "waypoints", "dwell_seconds"}, {"label", "waypoints", "dwell_seconds"}, identifier)
        label = _name(route["label"], f"{identifier}.label")
        for name in {identifier, label}:
            if identifier != "default" and name in {"默认路线", "默认巡逻路线"}:
                raise ConfigError(f"{name} 是 default 的保留路线名称")
            if name in route_names and route_names[name] != identifier:
                raise ConfigError(f"巡逻路线名称重复: {name}")
            if name in names:
                raise ConfigError(f"路线名称与地点名称冲突: {name}")
            route_names[name] = identifier
        points = route["waypoints"]
        if not isinstance(points, list) or not 1 <= len(points) <= 30:
            raise ConfigError(f"{identifier}.waypoints 必须包含 1..30 个地点")
        for point in points:
            if not isinstance(point, str) or point not in locations:
                raise ConfigError(f"路线 {identifier} 引用了未知地点: {point}")
        route["dwell_seconds"] = _number(route["dwell_seconds"], f"{identifier}.dwell_seconds", low=0, high=300)
    mock = _mapping(config["mock"], "mock")
    _keys(mock, {"travel_seconds", "inspection_seconds", "objects"}, {"travel_seconds", "inspection_seconds", "objects"}, "mock")
    for key, timeout in (("travel_seconds", "navigation_timeout"), ("inspection_seconds", "inspection_timeout")):
        mock[key] = _number(mock[key], f"mock.{key}", low=0, high=300)
        if mock[key] >= config[timeout]:
            raise ConfigError(f"mock.{key} 必须小于 {timeout}")
    observations = _mapping(mock["objects"], "mock.objects")
    object_names = config.setdefault("object_names", ["水杯", "人", "椅子", "桌子", "箱子", "背包", "灭火器"])
    if not isinstance(object_names, list) or not 1 <= len(object_names) <= 100:
        raise ConfigError("object_names 必须包含 1..100 个物体名称")
    checked_objects = [_name(item, "object_names") for item in object_names]
    if len(set(checked_objects)) != len(checked_objects):
        raise ConfigError("object_names 不得重复")
    for location, objects in observations.items():
        if location not in locations:
            raise ConfigError(f"mock.objects 引用了未知地点: {location}")
        if not isinstance(objects, list) or len(objects) > 100:
            raise ConfigError(f"mock.objects.{location} 必须是最多 100 项的列表")
        for item in objects:
            if not isinstance(item, str) or item not in object_names:
                raise ConfigError(f"mock.objects.{location} 包含未在 object_names 声明的物体: {item}")
        if len(objects) != len(set(objects)):
            raise ConfigError(f"mock.objects.{location} 包含重复物体名称")
    if "ros" in config:
        ros = _mapping(config["ros"], "ros")
        ros_names = {"navigation_action", "map_frame", "pose_topic", "inspection_request_topic", "inspection_result_topic", "inspection_action", "skill_action", "capabilities_service"}
        _keys(ros, ros_names | {"cancel_timeout", "perception_backend", "service_discovery_timeout", "pose_stale_seconds"}, set(), "ros")
        if not isinstance(ros.get("perception_backend", "action"), str) or ros.get("perception_backend", "action") not in {"action", "json"}:
            raise ConfigError("ros.perception_backend 只能是 action 或 json")
        for key in ("service_discovery_timeout", "pose_stale_seconds"):
            if key in ros:
                ros[key] = _number(ros[key], f"ros.{key}", low=0.1, high=60)
        for name in ros_names & ros.keys():
            value = ros[name]
            if (not isinstance(value, str) or not 1 <= len(value) <= 200
                    or not re.fullmatch(r"/?[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)*", value)):
                raise ConfigError(f"ros.{name} 必须是有效的 ROS 名称（长度 1..200）")
        if "cancel_timeout" in ros:
            ros["cancel_timeout"] = _number(ros["cancel_timeout"], "ros.cancel_timeout", low=0.1, high=30)
        request = ros.get("inspection_request_topic", "/voice_patrol/inspection/request")
        result = ros.get("inspection_result_topic", "/voice_patrol/inspection/result")
        if request.lstrip("/") == result.lstrip("/"):
            raise ConfigError("检查请求和结果必须使用不同的 ROS topic")
    return config


def _default_path() -> Path:
    module = Path(__file__).resolve()
    # Source checkout, package_data installation, then pip --target installation.
    for candidate in (
        module.parents[1] / "config" / "default.json",
        module.parent / "config" / "default.json",
        module.parents[1] / "share" / "robot_voice_patrol" / "config" / "default.json",
    ):
        if candidate.is_file():
            return candidate
    try:
        from ament_index_python.packages import get_package_share_directory
        candidate = Path(get_package_share_directory("robot_voice_patrol")) / "config" / "default.json"
        if candidate.is_file():
            return candidate
    except (ImportError, LookupError):
        pass
    candidate = Path(sys.prefix) / "share" / "robot_voice_patrol" / "config" / "default.json"
    if candidate.is_file():
        return candidate
    # Wheel metadata preserves data_files paths, including per-user installs
    # whose share directory is outside sys.prefix.
    try:
        installed = metadata.distribution("robot_voice_patrol")
        for entry in installed.files or []:
            if entry.as_posix().endswith("share/robot_voice_patrol/config/default.json"):
                candidate = Path(installed.locate_file(entry))
                if candidate.is_file():
                    return candidate
    except metadata.PackageNotFoundError:
        pass
    raise ConfigError("找不到默认配置，请通过 --config 指定 JSON 配置文件")


def _unique_json_pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"JSON 字段重复: {key}")
        result[key] = value
    return result


def load_config(path: str | Path | None = None) -> dict:
    """Load JSON (UTF-8, optionally BOM) and validate the complete schema."""
    selected = Path(path).expanduser() if path is not None else _default_path()
    try:
        with selected.open("r", encoding="utf-8-sig") as handle:
            value = json.load(handle, object_pairs_hook=_unique_json_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取配置 {selected}: {exc}") from exc
    return validate_config(value)
