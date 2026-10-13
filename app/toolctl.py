#!/usr/bin/env python3
"""Control plane for the Codex official and Local model router."""

from __future__ import annotations

import argparse
import base64
import copy
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import getpass
import hashlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

import keys_store

try:  # Windows 注册表自启；其他平台导入为 None
    import winreg  # type: ignore[import-not-found]
except ImportError:
    winreg = None


VERSION = "6"
BRIDGE_NAME = "responses-chat-completions"
LOCAL_UPSTREAM = "https://api.example.com/v1"
OFFICIAL_BASE_URL = "https://chatgpt.com/backend-api/codex"
LAUNCH_LABEL = "com.codexmultikey.official-local-router"
RUNTIME_NAME = "CodexOfficialLocalRouter"
WINDOWS_AUTOSTART_NAME = "CodexOfficialLocalRouter"
WINDOWS_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MODEL_PROVIDER_ID = "codex_official_local"
PROVIDER_CONFIG = {
    "name": "Codex Official + Local",
    "base_url": "http://127.0.0.1:8790/v1",
    "wire_api": "responses",
    "requires_openai_auth": True,
    "supports_websockets": False,
}
MANAGED_KEYS = ("model", "model_reasoning_effort", "model_provider", "openai_base_url", "model_catalog_json")
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
PROBE_EFFORTS = ("max", "xhigh", "high", "medium", "low", "minimal", "none")
MULTI_KEY_CONCURRENCY = 3
DISCOVERY_RETRY_BACKOFF_SECONDS = 0.5
MODALITIES = {"text", "image"}
CHAT_OPTIONS = {
    "temperature": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "top_p": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "explicit_tool_choice": lambda value: value in {"native", "required_single"},
}
log = logging.getLogger("codex_official_local_toolctl")
log.addHandler(logging.NullHandler())
_PROVIDER_UNCHANGED = object()
DISCOVERY_DEFAULTS = {
    "context_window": 200000,
    "input_modalities": ["text", "image"],
    "supports_image_detail_original": True,
    "chat_options": {"explicit_tool_choice": "native"},
}


class ValidationError(ValueError):
    pass


class ControlError(RuntimeError):
    pass


class GatewayUnavailable(ControlError):
    """可重试的上游失败：限流、5xx、超时或连不上。

    多 Key 并发拉取时只有这类失败才会退回串行重试，避免把参数写错、
    密钥失效这类确定性错误也重试一遍。
    """

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def is_windows() -> bool:
    return os.name == "nt"


def paths(home: Optional[Path] = None, package_dir: Optional[Path] = None) -> dict[str, Path]:
    home = home or Path.home()
    package_dir = package_dir or Path(__file__).resolve().parent
    if is_windows():
        # Windows 就地固定运行：运行文件、Key 和日志都留在包内.
        runtime = package_dir / "runtime"
    else:
        runtime = home / "Library" / "Application Support" / RUNTIME_NAME
    return {
        "home": home,
        "package": package_dir,
        "runtime": runtime,
        "logs": runtime / "logs",
        "manifest": runtime / "local-models.json",
        "catalog": runtime / "merged-models.json",
        "settings": runtime / "settings.json",
        "key": runtime / "local-api-key.secret",
        "keys": runtime / "keys.json",
        "config": home / ".codex" / "config.toml",
        "official_cache": home / ".codex" / "models_cache.json",
        "launchagent": home / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist",
        "backups": runtime / "backups",
    }


def configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "toolctl.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)


def _load_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"找不到{label}：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label}不是有效 JSON：第 {exc.lineno} 行") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label}顶层必须是对象")
    return value


def validate_manifest(data: dict) -> dict:
    version = data.get("version")
    if version not in (1, 2):
        raise ValidationError("local-models.json version 只支持 1 或 2")
    models = data.get("models")
    if not isinstance(models, list) or (not models and version == 1):
        raise ValidationError("Local 模型清单不能为空")
    agent_models = data.get("agent_models", [])
    if not isinstance(agent_models, list) or any(
        not isinstance(model, dict) or "upstream_model" not in model for model in agent_models
    ):
        raise ValidationError("agent_models 必须是模型列表")
    official_models = data.get("official_models", [])
    if (
        not isinstance(official_models, list)
        or any(not isinstance(slug, str) or not slug.strip() for slug in official_models)
        or len(official_models) != len(set(official_models))
    ):
        raise ValidationError("official_models 必须是不重复的模型 ID 列表")
    if "official_cache_path" in data and (
        not isinstance(data["official_cache_path"], str) or not data["official_cache_path"].strip()
    ):
        raise ValidationError("official_cache_path 必须是缓存文件路径")
    seen = set()
    required = {
        "slug", "display_name", "description", "priority", "context_window",
        "default_reasoning_level", "supported_reasoning_levels", "input_modalities",
        "supports_image_detail_original", "chat_options",
    }
    for index, model in enumerate(models + agent_models):
        if not isinstance(model, dict) or not required.issubset(model):
            raise ValidationError(f"第 {index + 1} 个模型缺少必要字段")
        slug = model["slug"]
        if not isinstance(slug, str) or (
            slug != "auto"
            and (not re.fullmatch(r"local/[a-z0-9][a-z0-9._/-]*", slug) or slug != slug.lower())
        ):
            raise ValidationError(f"非法 Local slug：{slug!r}")
        if slug in seen:
            raise ValidationError(f"Local slug 重复：{slug}")
        seen.add(slug)
        if not isinstance(model["display_name"], str) or not model["display_name"].strip() or not isinstance(model["description"], str):
            raise ValidationError(f"{slug} 的显示名或描述非法")
        upstream_model = model.get("upstream_model", slug)
        if (
            not isinstance(upstream_model, str)
            or not upstream_model.strip()
            or len(upstream_model) > 256
            or any(ord(char) < 32 for char in upstream_model)
        ):
            raise ValidationError(f"{slug} 的上游模型 ID 非法")
        if not isinstance(model["priority"], int) or isinstance(model["priority"], bool):
            raise ValidationError(f"{slug} 的 priority 必须是整数")
        if not isinstance(model["context_window"], int) or isinstance(model["context_window"], bool) or model["context_window"] <= 0:
            raise ValidationError(f"{slug} 的 context_window 必须是正整数")
        efforts = model["supported_reasoning_levels"]
        if not isinstance(efforts, list) or not efforts or any(effort not in EFFORTS for effort in efforts) or len(efforts) != len(set(efforts)):
            raise ValidationError(f"{slug} 的推理档位非法")
        if model["default_reasoning_level"] not in efforts:
            raise ValidationError(f"{slug} 的默认推理档位不在支持列表中")
        modalities = model["input_modalities"]
        if not isinstance(modalities, list) or not modalities or any(item not in MODALITIES for item in modalities) or len(modalities) != len(set(modalities)):
            raise ValidationError(f"{slug} 的输入模态非法")
        if not isinstance(model["supports_image_detail_original"], bool):
            raise ValidationError(f"{slug} 的图片能力字段非法")
        options = model["chat_options"]
        if not isinstance(options, dict) or any(key not in CHAT_OPTIONS or not CHAT_OPTIONS[key](value) for key, value in options.items()):
            raise ValidationError(f"{slug} 的 chat_options 非法")
        wire_api = model.get("wire_api")
        if wire_api is not None and wire_api != "responses":
            raise ValidationError(f"{slug} 的 wire_api 只支持 responses 或缺省")
    if version == 1:
        if data.get("upstream_base_url") != LOCAL_UPSTREAM:
            raise ValidationError("Local 上游地址不符合预期")
        log.info("manifest validated version=1 local_models=%d", len(models))
        return data
    sources = data.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise ValidationError("多 Key 模型清单必须包含 sources")
    for source_id, source in sources.items():
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValidationError("模型来源 ID 非法")
        if not isinstance(source, dict):
            raise ValidationError(f"模型来源 {source_id} 必须是对象")
        kind = source.get("kind")
        if kind == "official":
            continue
        if kind != "key":
            raise ValidationError(f"模型来源 {source_id} 的类型非法")
        if not isinstance(source.get("name"), str) or not source["name"].strip():
            raise ValidationError(f"模型来源 {source_id} 缺少名称")
        upstream = source.get("upstream_base_url")
        if not isinstance(upstream, str) or not upstream.startswith(("http://", "https://")):
            raise ValidationError(f"模型来源 {source_id} 的上游地址非法")
        api_key = source.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValidationError(f"模型来源 {source_id} 缺少密钥")
    for model in models + agent_models:
        source_id = model.get("source")
        source = sources.get(source_id) if isinstance(source_id, str) else None
        if not isinstance(source, dict) or source.get("kind") != "key":
            raise ValidationError(f"{model['slug']} 指向了不存在的模型来源")
    log.info(
        "manifest validated version=2 local_models=%d agent_models=%d sources=%d",
        len(models), len(agent_models), len(sources),
    )
    return data


def load_manifest(path: Path) -> dict:
    return validate_manifest(_load_json(path, "Local 模型清单"))


def _discovery_settings(policy: dict) -> dict:
    raw = policy.get("discovery", {})
    if not isinstance(raw, dict):
        raise ValidationError("discovery 必须是对象")
    excluded = raw.get("excluded_models", [])
    if (
        not isinstance(excluded, list)
        or any(not isinstance(item, str) or not item.strip() for item in excluded)
        or len(excluded) != len(set(excluded))
    ):
        raise ValidationError("discovery.excluded_models 必须是不重复的非空字符串列表")
    defaults = raw.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValidationError("discovery.defaults 必须是对象")
    unknown_defaults = set(defaults) - set(DISCOVERY_DEFAULTS)
    if unknown_defaults:
        raise ValidationError(f"discovery.defaults 包含未支持字段：{sorted(unknown_defaults)}")
    normalized_defaults = copy.deepcopy(DISCOVERY_DEFAULTS)
    normalized_defaults.update(copy.deepcopy(defaults))
    if (
        not isinstance(normalized_defaults["context_window"], int)
        or isinstance(normalized_defaults["context_window"], bool)
        or normalized_defaults["context_window"] <= 0
    ):
        raise ValidationError("discovery.defaults.context_window 必须是正整数")
    modalities = normalized_defaults["input_modalities"]
    if (
        not isinstance(modalities, list)
        or not modalities
        or any(item not in MODALITIES for item in modalities)
        or len(modalities) != len(set(modalities))
    ):
        raise ValidationError("discovery.defaults.input_modalities 非法")
    if not isinstance(normalized_defaults["supports_image_detail_original"], bool):
        raise ValidationError("discovery.defaults.supports_image_detail_original 必须是布尔值")
    options = normalized_defaults["chat_options"]
    if not isinstance(options, dict) or any(key not in CHAT_OPTIONS or not CHAT_OPTIONS[key](value) for key, value in options.items()):
        raise ValidationError("discovery.defaults.chat_options 非法")
    overrides = raw.get("model_overrides", {})
    if not isinstance(overrides, dict):
        raise ValidationError("discovery.model_overrides 必须是对象")
    allowed_override_fields = {"input_modalities", "supports_image_detail_original"}
    for upstream_model, override in overrides.items():
        if not isinstance(upstream_model, str) or not upstream_model.strip() or not isinstance(override, dict):
            raise ValidationError("model_overrides 的模型 ID 和配置必须有效")
        unknown = set(override) - allowed_override_fields
        if unknown:
            raise ValidationError(f"{upstream_model} 的 model_overrides 包含未支持字段：{sorted(unknown)}")
        if "input_modalities" in override:
            value = override["input_modalities"]
            if not isinstance(value, list) or not value or any(item not in MODALITIES for item in value) or len(value) != len(set(value)):
                raise ValidationError(f"{upstream_model} 的识图开关非法")
        if "supports_image_detail_original" in override and not isinstance(override["supports_image_detail_original"], bool):
            raise ValidationError(f"{upstream_model} 的原图开关非法")
    return {
        "excluded_models": list(excluded),
        "defaults": normalized_defaults,
        "model_overrides": copy.deepcopy(overrides),
    }


def validate_policy(data: dict) -> dict:
    validate_manifest(data)
    settings = _discovery_settings(data)
    upstream_ids = [model.get("upstream_model", model["slug"]) for model in data["models"]]
    if len(upstream_ids) != len(set(upstream_ids)):
        raise ValidationError("Local 模型策略包含重复的上游模型 ID")
    log.info(
        "discovery policy validated configured=%d excluded=%d overrides=%d image_default=%s",
        len(data["models"]), len(settings["excluded_models"]), len(settings["model_overrides"]),
        "image" in settings["defaults"]["input_modalities"],
    )
    return data


def load_policy(path: Path) -> dict:
    return validate_policy(_load_json(path, "Local 模型策略"))


def _gateway_json(
    method: str, path: str, local_key: str, payload: Optional[dict] = None,
    upstream_base_url: str = LOCAL_UPSTREAM,
) -> tuple[int, dict]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        upstream_base_url.rstrip("/") + path,
        data=body,
        method=method,
        headers={
            "Authorization": "Bearer " + local_key,
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except urllib.error.URLError as exc:
        # 超时与连不上都属于可重试失败，调用方可以退回串行重试。
        raise GatewayUnavailable(f"无法连接 Local 网关：{type(exc.reason).__name__}") from exc
    except (TimeoutError, OSError) as exc:
        raise GatewayUnavailable(f"无法连接 Local 网关：{type(exc).__name__}") from exc
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = {}
    return status, value if isinstance(value, dict) else {}


def gateway_requester(upstream_base_url: str):
    """绑定某个 Key 的上游地址，返回与 _gateway_json 同签名的请求函数。"""
    def request_json(method: str, path: str, local_key: str, payload: Optional[dict] = None):
        return _gateway_json(method, path, local_key, payload, upstream_base_url)

    return request_json


def fetch_gateway_catalog(local_key: str, request_json=None) -> dict:
    """拉取网关模型清单，同时带回每条记录声明的端点能力。

    返回 `{"models": [...], "endpoints": {模型 ID: 声明的端点类型列表或 None}}`；
    `supported_endpoint_types` 缺失或形状非法时记为 None，交给调用方实探兜底。
    """
    request_json = request_json or _gateway_json
    status, payload = request_json("GET", "/models", local_key, None)
    if status == 429 or status >= 500:
        raise GatewayUnavailable(f"Local 网关模型列表请求失败（HTTP {status}）", status)
    if status != 200:
        raise ControlError(f"Local 网关模型列表请求失败（HTTP {status}）")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ControlError("Local 网关 /models 返回结构无法识别")
    model_ids = []
    endpoints: dict[str, Optional[list[str]]] = {}
    for item in data:
        model_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 256 or any(ord(char) < 32 for char in model_id):
            raise ControlError("Local 网关 /models 包含非法模型 ID")
        if model_id not in model_ids:
            model_ids.append(model_id)
            declared = item.get("supported_endpoint_types")
            if isinstance(declared, list) and declared and all(isinstance(value, str) for value in declared):
                endpoints[model_id] = list(dict.fromkeys(declared))
            else:
                endpoints[model_id] = None
    if not model_ids:
        raise ControlError("Local 网关未返回任何模型")
    responses_capable = sum(1 for value in endpoints.values() if value and "openai-response" in value)
    log.info("gateway models fetched discovered=%d responses_capable=%d", len(model_ids), responses_capable)
    return {"models": model_ids, "endpoints": endpoints}


def fetch_gateway_models(local_key: str, request_json=None) -> list[str]:
    """旧调用方的薄封装：只要模型 ID 列表。"""
    model_ids = fetch_gateway_catalog(local_key, request_json)["models"]
    return model_ids


def _tool_probe_message(payload: dict) -> Optional[dict]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return None
    call = calls[0]
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not isinstance(function, dict):
        return None
    if function.get("name") != "codex_model_probe" or not isinstance(function.get("arguments"), str):
        return None
    try:
        arguments = json.loads(function["arguments"])
    except json.JSONDecodeError:
        return None
    if not isinstance(arguments, dict) or not isinstance(arguments.get("input"), str):
        return None
    return message


def _has_assistant_text(payload: dict) -> bool:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip() for part in content)
    return False


def probe_gateway_model(upstream_model: str, local_key: str, request_json=None) -> Optional[str]:
    request_json = request_json or _gateway_json
    tool = {
        "type": "function",
        "function": {
            "name": "codex_model_probe",
            "description": "Compatibility probe. Call this tool once.",
            "parameters": {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
                "additionalProperties": False,
            },
        },
    }
    base_messages = [{"role": "user", "content": "Call codex_model_probe with input ok, then wait for its result."}]
    for effort in PROBE_EFFORTS:
        first = {
            "model": upstream_model,
            "messages": copy.deepcopy(base_messages),
            "tools": [tool],
            "tool_choice": "required",
            "parallel_tool_calls": False,
            "max_tokens": 256,
        }
        if effort != "none":
            first["reasoning_effort"] = effort
        status, payload = request_json("POST", "/chat/completions", local_key, first)
        if status == 404:
            log.info("gateway model probe skipped model=%s reason=chat_endpoint_404", upstream_model)
            return None
        if status >= 500 or status in {401, 403, 429}:
            log.warning("gateway model probe skipped model=%s status=%d", upstream_model, status)
            return None
        assistant = _tool_probe_message(payload) if status == 200 else None
        if assistant is None:
            continue
        call_id = assistant["tool_calls"][0]["id"]
        second = copy.deepcopy(first)
        # 第二轮不再带 parallel_tool_calls：部分模型（如 gpt-5.6-terra）会因此返回空正文。
        second.pop("parallel_tool_calls", None)
        second["messages"] = base_messages + [assistant, {"role": "tool", "tool_call_id": call_id, "content": "probe-ok"}]
        second["tool_choice"] = "none"
        status, final_payload = request_json("POST", "/chat/completions", local_key, second)
        if status == 200 and _has_assistant_text(final_payload):
            log.info("gateway model probe passed model=%s reasoning=%s tool_roundtrip=true", upstream_model, effort)
            return effort
    log.info("gateway model probe skipped model=%s reason=no_compatible_tool_roundtrip", upstream_model)
    return None


PROBE_TOOL_NAME = "codex_model_probe"
PROBE_TOOL_DESCRIPTION = "Compatibility probe. Call this tool once."
PROBE_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {"input": {"type": "string"}},
    "required": ["input"],
    "additionalProperties": False,
}
PROBE_PROMPT = "Call codex_model_probe with input ok, then wait for its result."
CUSTOM_PROBE_TOOL_NAME = "codex_custom_probe"
CUSTOM_PROBE_TOOL_DESCRIPTION = "Compatibility probe for custom tools. Call this tool once."
CUSTOM_PROBE_PROMPT = "Call codex_custom_probe with input ok, then wait for its result."
# responses 探测的三态返回值：档位字符串=通过，None=明确不兼容，该哨兵=上游暂时不可用。
PROBE_UNAVAILABLE = "__probe_unavailable__"


def _responses_probe_payload(upstream_model: str, effort: str) -> dict:
    """custom + function 混合工具探测，复现 Codex 真实会话里的工具语法。"""
    payload = {
        "model": upstream_model,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": CUSTOM_PROBE_PROMPT}]}],
        "tools": [
            {"type": "custom", "name": CUSTOM_PROBE_TOOL_NAME, "description": CUSTOM_PROBE_TOOL_DESCRIPTION},
            {
                "type": "function",
                "name": PROBE_TOOL_NAME,
                "description": PROBE_TOOL_DESCRIPTION,
                "parameters": PROBE_TOOL_PARAMETERS,
            },
        ],
        "tool_choice": "required",
        "stream": False,
        "store": False,
    }
    if effort != "none":
        payload["reasoning"] = {"effort": effort}
    return payload


def _responses_custom_call(payload: dict) -> Optional[dict]:
    """探测通过的唯一标准：模型用 custom_tool_call 语法调用 custom 工具。"""
    output = payload.get("output")
    if not isinstance(output, list):
        return None
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "custom_tool_call":
            continue
        if item.get("name") != CUSTOM_PROBE_TOOL_NAME or not isinstance(item.get("input"), str):
            continue
        return item
    return None


def _responses_misused_custom_call(payload: dict) -> bool:
    """上游把 custom 工具写成 function_call 时命中；这种模型走直通会丢工具调用。"""
    output = payload.get("output")
    if not isinstance(output, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("type") == "function_call"
        and item.get("name") == CUSTOM_PROBE_TOOL_NAME
        for item in output
    )


def probe_gateway_responses(upstream_model: str, local_key: str, request_json=None) -> Optional[str]:
    """一轮式 /responses 探测：从高到低试推理档位，拿到 custom_tool_call 才算通过。

    同时声明 custom 与 function 工具，专门挡住把 custom 工具写成 function_call 的模型。
    上游限流/5xx 返回 PROBE_UNAVAILABLE，调用方必须保持原状，不能趁机降级。
    """
    request_json = request_json or _gateway_json
    for effort in PROBE_EFFORTS:
        status, payload = request_json(
            "POST", "/responses", local_key, _responses_probe_payload(upstream_model, effort),
        )
        if status in {401, 403, 429} or status >= 500:
            log.warning("gateway responses probe skipped model=%s status=%d reason=upstream_unavailable", upstream_model, status)
            return PROBE_UNAVAILABLE
        if status in {404, 405}:
            log.info("gateway responses probe skipped model=%s status=%d reason=responses_endpoint_missing", upstream_model, status)
            return None
        if status == 200 and _responses_custom_call(payload):
            log.info("gateway responses probe passed model=%s reasoning=%s tool_roundtrip=custom", upstream_model, effort)
            return effort
        if status == 200 and _responses_misused_custom_call(payload):
            log.warning(
                "gateway responses probe rejected model=%s reasoning=%s reason=custom_tool_called_as_function",
                upstream_model, effort,
            )
            return None
        if status not in {400, 422}:
            log.info("gateway responses probe skipped model=%s status=%d", upstream_model, status)
            return None
    log.info("gateway responses probe skipped model=%s reason=no_compatible_custom_tool_roundtrip", upstream_model)
    return None


def declared_wire_api(declared: Optional[list[str]]) -> Optional[str]:
    """按 /models 的 supported_endpoint_types 判断接线方式；None 表示声明缺失要实探。"""
    if declared is None:
        return None
    return "responses" if "openai-response" in declared else "chat"


def _probe_new_gateway_model(
    upstream_model: str, local_key: str, request_json, declared: Optional[list[str]],
) -> Optional[tuple[str, str]]:
    """新模型的接线判定：先按 /models 声明探测，声明和实探不符就换另一种接线兜底。

    只声明 openai 的模型在 Chat 上也可能带不了工具调用（上游要求改用 /responses），
    所以 Chat 探测失败后要补一次直通探测，否则这类模型会被误判成不兼容。
    """
    wire = declared_wire_api(declared)
    if wire != "chat":
        effort = probe_gateway_responses(upstream_model, local_key, request_json)
        if isinstance(effort, str) and effort != PROBE_UNAVAILABLE:
            return "responses", effort
        if effort == PROBE_UNAVAILABLE:
            log.warning("gateway model probe skipped model=%s reason=responses_upstream_unavailable", upstream_model)
            return None
        log.info("gateway model responses fallback model=%s reason=custom_tool_roundtrip_failed", upstream_model)
    effort = probe_gateway_model(upstream_model, local_key, request_json)
    if effort:
        return "chat", effort
    if wire == "chat":
        # 声明只支持 openai 的模型也可能只能在 /responses 上跑通工具调用，补一次兜底探测。
        log.info("gateway model responses retry model=%s reason=declared_chat_tool_roundtrip_failed", upstream_model)
        effort = probe_gateway_responses(upstream_model, local_key, request_json)
        if isinstance(effort, str) and effort != PROBE_UNAVAILABLE:
            return "responses", effort
        log.warning(
            "gateway model probe skipped model=%s reason=chat_and_responses_tool_roundtrip_failed",
            upstream_model,
        )
    return None


def responses_upgrade(
    upstream_model: str, local_key: str, request_json, declared: Optional[list[str]], record: dict,
    chat_only_cached: bool = False,
) -> Optional[dict]:
    """给已登记的旧记录补一次直通判定，返回要合并进记录的字段。

    旧 Chat 记录即使只声明 openai，也要在 Chat 工具探测失败后尝试 /responses。
    已标记 responses 的记录仅在明确不兼容且 Chat 可用时降级；暂时不可用时保持原状。
    """
    wire = declared_wire_api(declared)
    marked = record.get("wire_api") == "responses"
    if chat_only_cached and wire == "chat" and not marked:
        chat_unavailable = False

        def watched_request(method, path, key, payload=None):
            nonlocal chat_unavailable
            status, result = (request_json or _gateway_json)(method, path, key, payload)
            if status in {401, 403, 429} or status >= 500:
                chat_unavailable = True
            return status, result

        chat_effort = probe_gateway_model(upstream_model, local_key, watched_request)
        if chat_effort:
            log.info("gateway model cached wire kept wire=chat model=%s reason=chat_tool_roundtrip_passed", upstream_model)
            return None
        if chat_unavailable:
            log.warning("gateway model cached wire kept wire=chat model=%s reason=chat_upstream_unavailable", upstream_model)
            return None
        log.info("gateway model cached responses retry model=%s reason=chat_tool_roundtrip_failed", upstream_model)
        effort = probe_gateway_responses(upstream_model, local_key, request_json)
        if isinstance(effort, str) and effort != PROBE_UNAVAILABLE:
            log.info("gateway model cached wire upgraded wire=responses model=%s reasoning=%s", upstream_model, effort)
            return {
                "wire_api": "responses",
                "default_reasoning_level": effort,
                "supported_reasoning_levels": _reasoning_levels_through(effort),
            }
        log.warning(
            "gateway model cached wire kept wire=chat model=%s reason=%s",
            upstream_model, "responses_upstream_unavailable" if effort == PROBE_UNAVAILABLE else "responses_probe_failed",
        )
        return None
    if wire != "responses":
        return None
    effort = probe_gateway_responses(upstream_model, local_key, request_json)
    if isinstance(effort, str) and effort != PROBE_UNAVAILABLE:
        return {
            "wire_api": "responses",
            "default_reasoning_level": effort,
            "supported_reasoning_levels": _reasoning_levels_through(effort),
        }
    if effort == PROBE_UNAVAILABLE:
        log.warning("gateway model wire probe skipped model=%s reason=responses_upstream_unavailable", upstream_model)
        return None
    if not marked:
        log.warning("gateway model wire upgrade skipped model=%s reason=responses_probe_failed", upstream_model)
        return None
    chat_effort = probe_gateway_model(upstream_model, local_key, request_json)
    if chat_effort is None:
        log.warning("gateway model wire downgrade blocked model=%s reason=chat_probe_failed", upstream_model)
        return None
    log.warning("gateway model wire downgraded wire=chat model=%s reason=custom_tool_roundtrip_failed", upstream_model)
    return {
        "wire_api": None,
        "default_reasoning_level": chat_effort,
        "supported_reasoning_levels": _reasoning_levels_through(chat_effort),
    }


def _apply_wire_upgrade(model: dict, upgrade: Optional[dict]) -> None:
    """合并接线探测结果；回退 Chat 桥接时移除 wire_api 标记。"""
    if not upgrade:
        return
    model.update(upgrade)
    if model.get("wire_api") is None:
        model.pop("wire_api", None)


def _safe_model_slug(upstream_model: str, owners: dict[str, str]) -> str:
    """把上游模型 ID 收敛成合法 slug；同名写法不在这里处理。"""
    if upstream_model == "auto":
        candidate = "auto"
    elif re.fullmatch(r"local/[a-z0-9][a-z0-9._/-]*", upstream_model) and upstream_model == upstream_model.lower():
        candidate = upstream_model
    else:
        raw = upstream_model[6:] if upstream_model.lower().startswith("local/") else upstream_model
        normalized = re.sub(r"[^a-z0-9._/-]+", "-", raw.lower()).strip("./-") or "model"
        candidate = "local/" + normalized[:160].rstrip("./-")
        candidate += "-" + hashlib.sha256(upstream_model.encode("utf-8")).hexdigest()[:8]
    owner = owners.get(candidate)
    if owner is not None and owner != upstream_model:
        candidate += "-" + hashlib.sha256(upstream_model.encode("utf-8")).hexdigest()[:8]
    return candidate


def model_identity(value: str) -> str:
    """模型身份：忽略大小写与 / _ . - 和空格，用于识别同名模型。

    `GPT-5.6 Sol`、`gpt-5.6-sol`、`gpt5.6.sol` 归一化后都是 `gpt56sol`。
    """
    return re.sub(r"[\s/_.\-]+", "", str(value).lower())


def _apply_model_override(model: dict, upstream_model: str, settings: dict) -> dict:
    result = copy.deepcopy(model)
    result.update(copy.deepcopy(settings["model_overrides"].get(upstream_model, {})))
    return result


def _reasoning_levels_through(maximum: str) -> list[str]:
    return list(reversed(PROBE_EFFORTS[PROBE_EFFORTS.index(maximum):]))


def resolve_gateway_manifest(policy: dict, local_key: str, previous_manifest: Optional[dict] = None, request_json=None) -> tuple[dict, dict]:
    validate_policy(policy)
    settings = _discovery_settings(policy)
    catalog = fetch_gateway_catalog(local_key, request_json)
    discovered = catalog["models"]
    endpoints = catalog["endpoints"]
    discovered_set = set(discovered)
    excluded_set = set(settings["excluded_models"])
    configured = {model.get("upstream_model", model["slug"]): model for model in policy["models"]}
    cached = {}
    if previous_manifest:
        validate_manifest(previous_manifest)
        cached = {model.get("upstream_model", model["slug"]): model for model in previous_manifest["models"]}
    owners = {model["slug"]: upstream for upstream, model in configured.items()}
    owners.update({model["slug"]: upstream for upstream, model in cached.items() if model["slug"] not in owners})
    models = []
    included_upstream = set()
    added = 0
    skipped = []

    for source in (policy["models"], (previous_manifest or {}).get("models", [])):
        for model in source:
            upstream_model = model.get("upstream_model", model["slug"])
            if upstream_model not in discovered_set or upstream_model in excluded_set or upstream_model in included_upstream:
                continue
            resolved_model = copy.deepcopy(model)
            upgrade = responses_upgrade(
                upstream_model, local_key, request_json, endpoints.get(upstream_model), resolved_model,
            )
            if upgrade:
                _apply_wire_upgrade(resolved_model, upgrade)
                log.info("gateway model wire resolved wire=%s model=%s", resolved_model.get("wire_api") or "chat", upstream_model)
            resolved_model = _apply_model_override(resolved_model, upstream_model, settings)
            if upstream_model not in configured and resolved_model["default_reasoning_level"] in PROBE_EFFORTS:
                resolved_model["supported_reasoning_levels"] = _reasoning_levels_through(resolved_model["default_reasoning_level"])
            models.append(resolved_model)
            included_upstream.add(upstream_model)

    next_priority = min((model["priority"] for model in models), default=51) - 1
    for upstream_model in discovered:
        if upstream_model in excluded_set or upstream_model in included_upstream:
            continue
        probed = _probe_new_gateway_model(upstream_model, local_key, request_json, endpoints.get(upstream_model))
        if probed is None:
            skipped.append(upstream_model)
            continue
        wire_api, effort = probed
        slug = _safe_model_slug(upstream_model, owners)
        owners[slug] = upstream_model
        defaults = settings["defaults"]
        model = {
            "slug": slug,
            "display_name": upstream_model,
            "description": f"{upstream_model} via Local upstream",
            "priority": next_priority,
            "context_window": defaults["context_window"],
            "default_reasoning_level": effort,
            "supported_reasoning_levels": _reasoning_levels_through(effort),
            "input_modalities": copy.deepcopy(defaults["input_modalities"]),
            "supports_image_detail_original": defaults["supports_image_detail_original"],
            "chat_options": copy.deepcopy(defaults["chat_options"]),
        }
        if wire_api == "responses":
            model["wire_api"] = "responses"
        if slug != upstream_model:
            model["upstream_model"] = upstream_model
        models.append(_apply_model_override(model, upstream_model, settings))
        log.info("gateway model added wire=%s model=%s reasoning=%s", wire_api, upstream_model, effort)
        included_upstream.add(upstream_model)
        next_priority -= 1
        added += 1

    resolved = validate_manifest({
        "version": 1,
        "upstream_base_url": policy["upstream_base_url"],
        "models": models,
    })
    report = {
        "discovered": len(discovered),
        "included": len(models),
        "added": added,
        "excluded": [model_id for model_id in discovered if model_id in excluded_set],
        "skipped": skipped,
    }
    log.info(
        "gateway manifest resolved discovered=%d included=%d added=%d excluded=%d skipped=%d",
        report["discovered"], report["included"], report["added"], len(report["excluded"]), len(report["skipped"]),
    )
    return resolved, report


def load_runtime_keys(p: dict[str, Path]) -> dict:
    """读取多 Key 配置；旧版单 Key 文件会自动迁移成第一条记录。"""
    if p["keys"].exists():
        return keys_store.load_keys(p["keys"])
    legacy = ""
    try:
        legacy = p["key"].read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        legacy = ""
    except OSError as exc:
        log.warning("legacy key file unreadable kind=%s", type(exc).__name__)
    if legacy and "\n" not in legacy and "\r" not in legacy:
        entry = keys_store.make_key("默认 Key", legacy, upstream_base_url=LOCAL_UPSTREAM)
        keys_store.save_keys(p["keys"], {"version": 1, "keys": [entry], "model_priority": {}})
        log.info("legacy local key migrated into keys.json key_id=%s", entry["id"])
        return keys_store.load_keys(p["keys"])
    return keys_store.default_keys()


def _manifest_source_entry(key: dict) -> dict:
    return {
        "kind": "key",
        "name": key["name"],
        "upstream_base_url": key["upstream_base_url"],
        "api_key": key["api_key"],
    }


def _owned_models(sources: list[tuple[dict, list[str]]], priority_map: dict) -> tuple[dict, list[str]]:
    """决定每个上游模型最终由谁提供。

    顺序优先级固定为：显式指定 > Key 顺序（keys.json 的 order）> 官方。
    显式指定给官方、或指定给没有拉取到该模型的 Key 时，回退到 Key 顺序。
    """
    owners: dict[str, str] = {}
    official: list[str] = []
    for key, model_ids in sources:
        for model_id in model_ids:
            explicit = priority_map.get(model_id)
            if explicit == keys_store.OFFICIAL_SOURCE:
                if model_id not in official:
                    official.append(model_id)
                continue
            if explicit and explicit != key["id"]:
                continue
            owners.setdefault(model_id, key["id"])
    return owners, official


def _discover_key_models(key: dict, factory, excluded_set: set[str]) -> dict:
    """拉一个 Key 的模型列表，失败原因写进 entry['error']，不向上抛。

    `retryable` 为真表示这次失败值得退回串行再试一次（限流、5xx、超时）。
    """
    started = time.monotonic()
    outcome = {
        "key": key,
        "entry": {
            "id": key["id"], "name": key["name"], "discovered": 0, "selected": 0,
            "included": 0, "skipped": [], "error": None,
        },
        "selected": None,
        "request_json": None,
        "endpoints": {},
        "retryable": False,
    }
    try:
        request_json = factory(key["upstream_base_url"])
        catalog = fetch_gateway_catalog(key["api_key"], request_json)
        discovered = catalog["models"]
    except (ControlError, OSError, ValueError) as exc:
        outcome["entry"]["error"] = f"{type(exc).__name__}: {exc}"
        outcome["retryable"] = isinstance(exc, GatewayUnavailable)
        log.error(
            "multi key discovery failed key=%s kind=%s retryable=%s ms=%d",
            key["id"], type(exc).__name__, outcome["retryable"], int((time.monotonic() - started) * 1000),
        )
        return outcome
    selected = [
        model_id for model_id in keys_store.apply_selected_models(key, discovered)
        if model_id not in excluded_set
    ]
    outcome["entry"]["discovered"] = len(discovered)
    outcome["entry"]["selected"] = len(selected)
    outcome["selected"] = selected
    outcome["request_json"] = request_json
    outcome["endpoints"] = catalog["endpoints"]
    log.info(
        "multi key discovery ok key=%s discovered=%d selected=%d ms=%d",
        key["id"], len(discovered), len(selected), int((time.monotonic() - started) * 1000),
    )
    return outcome


def _discover_keys(enabled: list[dict], factory, excluded_set: set[str]) -> list[dict]:
    """并发拉取所有启用 Key 的模型列表，返回顺序与 enabled 一致。

    并发度固定 `MULTI_KEY_CONCURRENCY`；单个 Key 内部不做并发，避免打爆同一个上游。
    """
    if not enabled:
        return []
    workers = min(MULTI_KEY_CONCURRENCY, len(enabled))
    started = time.monotonic()
    if workers == 1:
        outcomes = [_discover_key_models(key, factory, excluded_set) for key in enabled]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_discover_key_models, key, factory, excluded_set) for key in enabled]
            outcomes = [future.result() for future in futures]
    log.info(
        "multi key discovery concurrent keys=%d workers=%d ms=%d",
        len(enabled), workers, int((time.monotonic() - started) * 1000),
    )
    return outcomes


def _retry_key_serially(outcome: dict, factory, excluded_set: set[str]) -> dict:
    """并发阶段撞上限流的 Key 单独串行重试一次；成功就换成重试结果。"""
    key = outcome["key"]
    log.info("multi key discovery retry serial key=%s reason=%s", key["id"], outcome["entry"]["error"])
    time.sleep(DISCOVERY_RETRY_BACKOFF_SECONDS)
    retried = _discover_key_models(key, factory, excluded_set)
    if retried["selected"] is None:
        log.error(
            "multi key discovery retry failed key=%s error=%s", key["id"], retried["entry"]["error"],
        )
        return retried
    log.info("multi key discovery retry ok key=%s discovered=%d", key["id"], retried["entry"]["discovered"])
    return retried


def resolve_multi_key_manifest(
    policy: dict, keys_data: dict, previous_manifest: Optional[dict] = None,
    request_json_factory=None,
) -> tuple[dict, dict]:
    """按多 Key 拉取模型并合并成 version 2 路由清单。

    发现与兼容性探测逻辑与单 Key 版本保持一致，只多了「按 Key 分别拉取」和
    「同名模型按优先级选来源」两步。
    """
    validate_policy(policy)
    settings = _discovery_settings(policy)
    enabled = keys_store.enabled_keys(keys_data)
    priority_map = keys_data.get("model_priority") or {}
    sources = {"official": {"kind": "official", "name": "ChatGPT 官方账号"}}
    for key in enabled:
        sources[key["id"]] = _manifest_source_entry(key)
    excluded_set = set(settings["excluded_models"])
    factory = request_json_factory or gateway_requester
    configured = {model.get("upstream_model", model["slug"]): model for model in policy["models"]}
    cached: dict[str, dict] = {}
    source_cached: dict[tuple[str, str], dict] = {}
    if previous_manifest:
        try:
            validate_manifest(previous_manifest)
            cached = {model.get("upstream_model", model["slug"]): model for model in previous_manifest["models"]}
            source_cached = {
                (model["source"], model.get("upstream_model", model["slug"])): model
                for model in previous_manifest["models"] + previous_manifest.get("agent_models", [])
                if model.get("source") in sources
                and (previous_manifest.get("sources") or {}).get(model["source"]) == sources[model["source"]]
            }
        except ValidationError:
            log.warning("previous runtime manifest ignored during multi key resolve")

    discovered_sources: list[tuple[dict, list[str], object, dict, dict]] = []
    key_reports: list[dict] = []
    for outcome in _discover_keys(enabled, factory, excluded_set):
        if outcome["retryable"]:
            outcome = _retry_key_serially(outcome, factory, excluded_set)
        key_reports.append(outcome["entry"])
        if outcome["selected"] is None:
            continue
        discovered_sources.append((
            outcome["key"], outcome["selected"], outcome["request_json"], outcome["entry"], outcome["endpoints"],
        ))

    if enabled and not discovered_sources:
        raise ControlError("所有 Key 的模型列表都拉取失败，请检查 Key、上游地址与网络后重试。")

    owners, official_models = _owned_models(
        [(key, models) for key, models, _r, _e, _p in discovered_sources], priority_map,
    )
    providers: dict[str, list[str]] = {}
    for key, models, _request_json, _entry, _endpoints in discovered_sources:
        for model_id in models:
            providers.setdefault(model_id, []).append(key["id"])
    conflicts = [
        {"model": model_id, "chosen": owners.get(model_id), "candidates": ids}
        for model_id, ids in sorted(providers.items()) if len(ids) > 1
    ]
    unresolved = [
        model_id for model_id, target in priority_map.items()
        if target != keys_store.OFFICIAL_SOURCE and target not in {key["id"] for key, _m, _r, _e, _p in discovered_sources}
    ]

    models: list[dict] = []
    agent_models: list[dict] = []
    used_slugs: dict[str, str] = {
        model["slug"]: model.get("upstream_model", model["slug"])
        for model in list(configured.values()) + list(cached.values())
    }
    for upstream_model in owners:
        slug = _safe_model_slug(upstream_model, used_slugs)
        used_slugs.setdefault(slug, upstream_model)
    next_priority = min((model["priority"] for model in configured.values()), default=51) - 1
    for key, discovered, request_json, entry, endpoints in discovered_sources:
        for upstream_model in discovered:
            included = owners.get(upstream_model) == key["id"]
            previous_model = source_cached.get((key["id"], upstream_model))
            if included and upstream_model in configured:
                resolved_model = copy.deepcopy(configured[upstream_model])
                upgrade = responses_upgrade(
                    upstream_model, key["api_key"], request_json, endpoints.get(upstream_model), resolved_model,
                )
                if upgrade:
                    _apply_wire_upgrade(resolved_model, upgrade)
                    log.info("multi key model wire resolved wire=%s model=%s", resolved_model.get("wire_api") or "chat", upstream_model)
                resolved_model = _apply_model_override(resolved_model, upstream_model, settings)
            elif previous_model is not None:
                resolved_model = copy.deepcopy(previous_model)
                resolved_model.pop("source", None)
                upgrade = responses_upgrade(
                    upstream_model, key["api_key"], request_json, endpoints.get(upstream_model), resolved_model,
                    chat_only_cached=True,
                )
                if upgrade:
                    _apply_wire_upgrade(resolved_model, upgrade)
                    log.info("multi key model wire resolved wire=%s model=%s", resolved_model.get("wire_api") or "chat", upstream_model)
                log.info("multi key model reused from previous manifest model=%s", upstream_model)
            else:
                probed = _probe_new_gateway_model(
                    upstream_model, key["api_key"], request_json, endpoints.get(upstream_model),
                )
                if probed is None:
                    entry["skipped"].append(upstream_model)
                    continue
                wire_api, effort = probed
                slug = _safe_model_slug(upstream_model, used_slugs)
                defaults = settings["defaults"]
                resolved_model = {
                    "slug": slug,
                    "display_name": upstream_model,
                    "description": f"{upstream_model} via Local upstream",
                    "priority": cached.get(upstream_model, {}).get("priority", next_priority) if included else next_priority,
                    "context_window": defaults["context_window"],
                    "default_reasoning_level": effort,
                    "supported_reasoning_levels": _reasoning_levels_through(effort),
                    "input_modalities": copy.deepcopy(defaults["input_modalities"]),
                    "supports_image_detail_original": defaults["supports_image_detail_original"],
                    "chat_options": copy.deepcopy(defaults["chat_options"]),
                }
                if wire_api == "responses":
                    resolved_model["wire_api"] = "responses"
                if slug != upstream_model:
                    resolved_model["upstream_model"] = upstream_model
                resolved_model = _apply_model_override(resolved_model, upstream_model, settings)
                log.info("multi key model added wire=%s model=%s reasoning=%s", wire_api, upstream_model, effort)
            if included and upstream_model not in configured and upstream_model not in cached:
                resolved_model["priority"] = next_priority
                next_priority -= 1
            if included:
                resolved_model["slug"] = (
                    configured.get(upstream_model, cached.get(upstream_model)) or {}
                ).get("slug", _safe_model_slug(upstream_model, used_slugs))
            else:
                alias = f"{upstream_model}-{key['id']}"
                slug = _safe_model_slug(alias, used_slugs)
                suffix = 0
                while slug in used_slugs:
                    suffix += 1
                    slug = _safe_model_slug(f"{alias}-{suffix}", used_slugs)
                resolved_model["slug"] = slug
            slug = resolved_model["slug"]
            if slug != upstream_model:
                resolved_model["upstream_model"] = upstream_model
            used_slugs.setdefault(slug, upstream_model)
            resolved_model["source"] = key["id"]
            if included:
                models.append(resolved_model)
                entry["included"] += 1
            else:
                agent_models.append(resolved_model)

    if not models and not agent_models and any(entry["skipped"] for entry in key_reports):
        raise ControlError("Key 上的模型都不兼容 Codex 对话/工具调用，没有可接入的模型。")

    resolved = validate_manifest({"version": 2, "sources": sources, "models": models, "agent_models": agent_models})
    report = {
        "mode": "multi-key",
        "keys": key_reports,
        "official": official_models,
        "conflicts": conflicts,
        "unresolved": unresolved,
        "discovered": sum(entry["discovered"] for entry in key_reports),
        "included": len(models),
    }
    log.info(
        "multi key manifest resolved keys=%d enabled=%d models=%d agent_models=%d conflicts=%d official=%d",
        len(key_reports), len(enabled), len(models), len(agent_models), len(conflicts), len(official_models),
    )
    return resolved, report


def validate_official_cache(cache: dict) -> list[dict]:
    models = cache.get("models")
    if not isinstance(models, list) or not models or any(not isinstance(item, dict) or not isinstance(item.get("slug"), str) for item in models):
        raise ValidationError("官方模型缓存结构无法识别")
    return models


def _official_client_version(p: dict[str, Path]) -> str:
    version_pattern = r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?"
    for name in ("ChatGPT", "Codex"):
        package = Path("/Applications") / (name + ".app") / "Contents/Resources/codex-cli/codex-package.json"
        if not package.exists():
            continue
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
            version = data.get("version") if isinstance(data, dict) else None
            if isinstance(version, str) and re.fullmatch(version_pattern, version):
                return version
        except (OSError, ValueError) as exc:
            log.warning("official client version package failed type=%s", type(exc).__name__)
    executable = shutil.which("codex")
    if executable:
        try:
            result = subprocess.run(
                [executable, "--version"], capture_output=True, text=True, errors="replace",
                timeout=5, **hidden_subprocess_kwargs(),
            )
            match = re.search(version_pattern, result.stdout) if result.returncode == 0 else None
            if match:
                return match.group()
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("official client version command failed type=%s", type(exc).__name__)
    try:
        cache = _load_json(p["official_cache"], "官方模型缓存")
        version = cache.get("client_version")
        if isinstance(version, str) and re.fullmatch(version_pattern, version):
            return version
    except (OSError, ValidationError) as exc:
        log.warning("official client version cache failed type=%s", type(exc).__name__)
    raise ControlError("无法确定 Codex 客户端版本，请先运行并登录 Codex 后重试")


def fetch_official_models(p: dict[str, Path]) -> dict:
    """拉取当前 ChatGPT 账号的完整 Codex 模型目录，不修改登录或缓存。"""
    try:
        auth = json.loads((p["official_cache"].parent / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("official model auth unreadable type=%s", type(exc).__name__)
        raise ControlError("无法读取 ChatGPT 官方登录信息，请先在 Codex 登录官方账号后重试") from None
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
    account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
    if (
        not isinstance(auth, dict)
        or auth.get("auth_mode") not in (None, "chatgpt", "chatgptAuthTokens")
        or not isinstance(access_token, str) or not access_token.strip()
        or any(ord(char) < 32 for char in access_token)
        or (account_id is not None and (
            not isinstance(account_id, str) or any(ord(char) < 32 for char in account_id)
        ))
    ):
        log.warning("official model auth invalid type=InvalidChatGPTAuth")
        raise ControlError("ChatGPT 官方账号登录信息不完整，请在 Codex 重新登录官方账号后重试")
    version = _official_client_version(p)
    request = urllib.request.Request(
        OFFICIAL_BASE_URL + "/models?" + urllib.parse.urlencode({"client_version": version}),
        method="GET",
        headers={
            "Authorization": "Bearer " + access_token,
            **({"ChatGPT-Account-Id": account_id} if account_id else {}),
            "Accept": "application/json",
            "originator": "codex_cli_rs",
            "User-Agent": "codex_cli_rs/" + version,
            "Cache-Control": "no-cache",
        },
    )
    started = time.monotonic()
    log.info("official model fetch started version=%s", version)
    try:
        context = ssl.create_default_context()
        if sys.platform == "darwin" and Path("/etc/ssl/cert.pem").is_file():
            context.load_verify_locations(cafile="/etc/ssl/cert.pem")
        with urllib.request.urlopen(request, timeout=45, context=context) as response:
            status = response.status
            raw = response.read()
            etag = response.headers.get("ETag")
    except urllib.error.HTTPError as exc:
        log.warning("official model fetch failed status=%d", exc.code)
        if exc.code in {401, 403}:
            raise ControlError("ChatGPT 官方登录已过期或无权限，请在 Codex 重新登录官方账号后重试") from None
        raise ControlError(f"ChatGPT 官方模型列表请求失败（HTTP {exc.code}）") from None
    except (urllib.error.URLError, OSError) as exc:
        log.warning("official model fetch failed type=%s", type(exc).__name__)
        raise ControlError(f"无法连接 ChatGPT 官方模型服务：{type(exc).__name__}") from None
    if status != 200:
        log.warning("official model fetch failed status=%d", status)
        raise ControlError(f"ChatGPT 官方模型列表请求失败（HTTP {status}）")
    try:
        cache = json.loads(raw.decode("utf-8"))
        if not isinstance(cache, dict):
            raise ValidationError("官方模型列表返回结构无法识别")
        models = validate_official_cache(cache)
        slugs = [model["slug"] for model in models]
        if any(not slug.strip() for slug in slugs) or len(slugs) != len(set(slugs)):
            raise ValidationError("官方模型列表包含空白或重复模型名")
    except (UnicodeDecodeError, ValueError) as exc:
        log.warning("official model response invalid type=%s", type(exc).__name__)
        raise ControlError("ChatGPT 官方模型列表返回结构无法识别，原模型列表未修改") from None
    cache.pop("identity", None)
    cache.update({
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "etag": etag,
        "client_version": version,
    })
    log.info(
        "official model fetch completed status=200 version=%s models=%d ms=%d",
        version, len(models), int((time.monotonic() - started) * 1000),
    )
    return cache


def _reasoning_level_shape(template: dict, efforts: list[str]) -> list:
    current = template.get("supported_reasoning_levels")
    if isinstance(current, list) and current and isinstance(current[0], dict):
        descriptions = {item.get("effort"): item.get("description", "") for item in current if isinstance(item, dict)}
        defaults = {
            "none": "No reasoning", "minimal": "Minimal reasoning", "low": "Fast responses with lighter reasoning",
            "medium": "Balances speed and reasoning depth", "high": "Greater reasoning depth for complex tasks",
            "xhigh": "Extra high reasoning depth", "max": "Maximum supported reasoning depth", "ultra": "Ultra reasoning depth",
        }
        return [{"effort": effort, "description": descriptions.get(effort) or defaults[effort]} for effort in efforts]
    return list(efforts)


def merge_catalog(official_cache: dict, manifest: dict) -> dict:
    validate_manifest(manifest)
    official_models = validate_official_cache(official_cache)
    local_slugs = {model["slug"] for model in manifest["models"]}
    # Key 条目与官方条目同名时只保留一条：按归一化身份把官方条目剔除，
    # 谁生效已经在 resolve_multi_key_manifest() 里按优先级决定。
    local_identities = {
        model_identity(model.get("upstream_model") or model["slug"]) for model in manifest["models"]
    }
    kept = []
    shadowed = []
    for model in official_models:
        if model["slug"] in local_slugs:
            continue
        if model_identity(model["slug"]) in local_identities:
            log.info("catalog dropped official model shadowed by same identity slug=%s", model["slug"])
            shadowed.append(model)
            continue
        kept.append(copy.deepcopy(model))
    # 官方条目被 Key 全部盖住时，仍用被盖住的条目当模板，避免整份目录构建失败。
    pool = kept if kept else [copy.deepcopy(model) for model in shadowed]
    if not kept and pool:
        log.warning("catalog template falls back to a shadowed official model count=%d", len(pool))
    template = next((model for model in pool if model.get("visibility", "list") == "list"), pool[0] if pool else None)
    if not template:
        raise ValidationError("官方模型缓存中没有可用于生成 Local 条目的模型")
    merged = copy.deepcopy(official_cache)
    merged["models"] = kept
    for local in manifest["models"]:
        item = copy.deepcopy(template)
        item.update({
            "slug": local["slug"],
            "display_name": local["display_name"],
            "description": local["description"],
            "priority": local["priority"],
            "default_reasoning_level": local["default_reasoning_level"],
            "supported_reasoning_levels": _reasoning_level_shape(template, local["supported_reasoning_levels"]),
            "input_modalities": list(local["input_modalities"]),
            "supports_image_detail_original": local["supports_image_detail_original"],
            "context_window": local["context_window"],
            "max_context_window": local["context_window"],
            "auto_compact_token_limit": int(local["context_window"] * 0.7),
            "visibility": "list",
            "supported_in_api": True,
        })
        merged["models"].append(item)
    slugs = [model["slug"] for model in merged["models"]]
    if len(slugs) != len(set(slugs)):
        raise ValidationError("合并模型目录存在重复 slug")
    log.info("catalog built official_models=%d local_models=%d total=%d", len(kept), len(manifest["models"]), len(slugs))
    return merged


def atomic_write(path: Path, data: bytes, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temp = Path(name)
    try:
        if mode is not None and hasattr(os, "fchmod"):  # Windows 无 fchmod
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        if mode is not None:
            try:
                path.chmod(mode)
            except OSError:
                log.warning("chmod unsupported path=%s", path)
    finally:
        if temp.exists():
            temp.unlink()


def write_local_key(path: Path, raw: str) -> None:
    value = raw.strip()
    if not value or "\n" in value or "\r" in value:
        raise ValidationError("Local Key 必须是非空单行文本")
    atomic_write(path, value.encode("utf-8"), 0o600)
    _restrict_key_acl(path)
    log.info("local key saved present=true length=%d", len(value))


def _restrict_key_acl(path: Path) -> None:
    """Windows 上用 icacls 收紧 Key 权限；POSIX 权限位已由 chmod 处理。"""
    if not is_windows():
        return
    user = os.environ.get("USERNAME")
    if not user:
        log.warning("key acl hardening skipped reason=missing-username")
        return
    try:
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True, text=True, errors="replace",
            **hidden_subprocess_kwargs(),
        )
    except OSError as exc:
        log.warning("key acl hardening failed error=%s", type(exc).__name__)
        return
    if result.returncode:
        log.warning("key acl hardening failed returncode=%d", result.returncode)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _patch_provider_section(source: str, provider) -> str:
    if provider is _PROVIDER_UNCHANGED:
        return source
    newline = "\r\n" if "\r\n" in source else "\n"
    lines = source.splitlines(keepends=True)
    header = re.compile(r"^\s*\[\s*model_providers\.codex_official_local\s*\]\s*(?:#.*)?$")
    result = []
    skipping = False
    for line in lines:
        if header.match(line.rstrip("\r\n")):
            skipping = True
            continue
        if skipping and re.match(r"^\s*\[", line):
            skipping = False
        if not skipping:
            result.append(line)
    while result and not result[-1].strip():
        result.pop()
    text = "".join(result)
    if provider is None:
        return text + (newline if text and not text.endswith(("\n", "\r")) else "")
    if not isinstance(provider, dict):
        raise ValueError("Provider config must be a dictionary or None")
    if text and not text.endswith(("\n", "\r")):
        text += newline
    if text:
        text += newline
    text += f"[model_providers.{MODEL_PROVIDER_ID}]{newline}"
    for key in ("name", "base_url", "wire_api", "requires_openai_auth", "supports_websockets"):
        value = provider[key]
        encoded = str(value).lower() if isinstance(value, bool) else _toml_string(value)
        text += f"{key} = {encoded}{newline}"
    return text


def patch_config(source: str, changes: dict[str, Optional[str]], provider=_PROVIDER_UNCHANGED) -> str:
    unknown = set(changes) - set(MANAGED_KEYS)
    if unknown:
        raise ValueError(f"Unsupported managed keys: {sorted(unknown)}")
    newline = "\r\n" if "\r\n" in source else "\n"
    lines = source.splitlines(keepends=True)
    first_table = next((index for index, line in enumerate(lines) if re.match(r"^\s*\[", line)), len(lines))
    top, rest = lines[:first_table], lines[first_table:]
    patterns = {key: re.compile(rf"^(\s*){re.escape(key)}\s*=") for key in MANAGED_KEYS}
    result = []
    consumed = set()
    for line in top:
        matched = next((key for key, pattern in patterns.items() if pattern.match(line)), None)
        if matched is None:
            result.append(line)
            continue
        if matched in changes and matched not in consumed:
            consumed.add(matched)
            value = changes[matched]
            if value is not None:
                result.append(f"{matched} = {_toml_string(value)}{newline}")
        elif matched not in changes:
            result.append(line)
    additions = [f"{key} = {_toml_string(changes[key])}{newline}" for key in MANAGED_KEYS if key in changes and changes[key] is not None and key not in consumed]
    if additions:
        if result and not result[-1].endswith(("\n", "\r")):
            result[-1] += newline
        result.extend(additions)
    return _patch_provider_section("".join(result + rest), provider)


def read_managed_config(source: str) -> dict[str, str]:
    values = {}
    for line in source.splitlines():
        if re.match(r"^\s*\[", line):
            break
        match = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.+?)\s*(?:#.*)?$", line)
        if match and match.group(1) in MANAGED_KEYS:
            try:
                value = json.loads(match.group(2))
            except json.JSONDecodeError:
                continue
            if isinstance(value, str):
                values[match.group(1)] = value
    return values


def read_provider_config(source: str) -> Optional[dict]:
    header = re.compile(r"^\s*\[\s*model_providers\.codex_official_local\s*\]\s*(?:#.*)?$")
    inside = False
    values = {}
    for line in source.splitlines():
        if header.match(line):
            inside = True
            continue
        if inside and re.match(r"^\s*\[", line):
            break
        if not inside:
            continue
        match = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.+?)\s*(?:#.*)?$", line)
        if not match:
            continue
        raw = match.group(2)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        values[match.group(1)] = value
    return values if inside else None


def backup_config(config_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = backup_dir / f"config-{stamp}.toml"
    shutil.copy2(config_path, backup)
    log.info("config backup created file=%s", backup.name)
    return backup


def write_config(config_path: Path, changes: dict[str, Optional[str]], backup_dir: Path, provider=_PROVIDER_UNCHANGED) -> Path:
    if not config_path.exists():
        raise ControlError(f"找不到 Codex 配置：{config_path}")
    backup = backup_config(config_path, backup_dir)
    original = config_path.read_text(encoding="utf-8")
    try:
        atomic_write(config_path, patch_config(original, changes, provider).encode("utf-8"))
        verify_config(config_path, changes, provider)
    except Exception:
        shutil.copy2(backup, config_path)
        log.exception("config patch failed; restored backup=%s", backup.name)
        raise
    log.info("config patched managed_keys=%s", ",".join(changes))
    return backup


def verify_config(config_path: Path, expected: dict[str, Optional[str]], expected_provider=_PROVIDER_UNCHANGED) -> None:
    source = config_path.read_text(encoding="utf-8")
    actual = read_managed_config(source)
    for key, value in expected.items():
        if value is None and key in actual:
            raise ControlError(f"配置复核失败：{key} 应已删除")
        if value is not None and actual.get(key) != value:
            raise ControlError(f"配置复核失败：{key} 未写入目标值")
    if expected_provider is not _PROVIDER_UNCHANGED:
        actual_provider = read_provider_config(source)
        if expected_provider is None and actual_provider is not None:
            raise ControlError("配置复核失败：工具 Provider 应已删除")
        if expected_provider is not None and actual_provider != expected_provider:
            raise ControlError("配置复核失败：工具 Provider 未写入目标值")
    log.info("config verified managed_keys=%s", ",".join(expected))


def _efforts(model: dict) -> list[str]:
    values = model.get("supported_reasoning_levels") or []
    return [item.get("effort") if isinstance(item, dict) else item for item in values]


def select_fallback(cache: dict, saved: Optional[dict] = None) -> tuple[str, str]:
    models = validate_official_cache(cache)
    visible = [model for model in models if model.get("visibility", "list") == "list" and not model["slug"].startswith("local/")]
    if not visible:
        raise ValidationError("官方模型缓存中没有可用官方模型")
    by_slug = {model["slug"]: model for model in visible}
    saved = saved or {}
    chosen = by_slug.get(saved.get("model")) or by_slug.get("gpt-5.6-terra") or max(visible, key=lambda model: model.get("priority", 0))
    supported = _efforts(chosen)
    preferred = saved.get("reasoning_effort")
    effort = preferred if preferred in supported else chosen.get("default_reasoning_level")
    if effort not in supported:
        effort = supported[0] if supported else "medium"
    return chosen["slug"], effort


def write_launchagent(path: Path, runtime_dir: Path) -> None:
    log_dir = runtime_dir / "logs"
    data = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [sys.executable, str(runtime_dir / "router.py"), "--runtime-dir", str(runtime_dir)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 7,
        "StandardOutPath": str(log_dir / "launchagent.stdout.log"),
        "StandardErrorPath": str(log_dir / "launchagent.stderr.log"),
        "ProcessType": "Background",
    }
    atomic_write(path, plistlib.dumps(data))
    result = subprocess.run(["plutil", "-lint", str(path)], capture_output=True, text=True)
    if result.returncode:
        path.unlink(missing_ok=True)
        raise ControlError("LaunchAgent plist 校验失败")
    log.info("launchagent generated label=%s", LAUNCH_LABEL)


class LaunchController:
    def __init__(self, plist_path: Path, runtime_dir: Path):
        self.plist_path = plist_path
        self.runtime_dir = runtime_dir
        self.uid = os.getuid()

    def _run(self, *args: str, check: bool = True):
        result = subprocess.run(list(args), capture_output=True, text=True)
        if check and result.returncode:
            raise ControlError(f"系统命令失败：{args[0]} {args[1] if len(args) > 1 else ''}")
        return result

    def start(self) -> bool:
        write_launchagent(self.plist_path, self.runtime_dir)
        domain = f"gui/{self.uid}"
        try:
            self._run("launchctl", "bootout", domain, str(self.plist_path), check=False)
            self._run("launchctl", "bootstrap", domain, str(self.plist_path))
            self._run("launchctl", "kickstart", "-k", f"{domain}/{LAUNCH_LABEL}")
        except Exception:
            self._run("launchctl", "bootout", domain, str(self.plist_path), check=False)
            self.plist_path.unlink(missing_ok=True)
            log.exception("launchagent start failed; partial state removed")
            raise
        log.info("launchagent loaded label=%s", LAUNCH_LABEL)
        return True

    def restart(self) -> None:
        self._run("launchctl", "kickstart", "-k", f"gui/{self.uid}/{LAUNCH_LABEL}")
        log.info("launchagent restarted label=%s", LAUNCH_LABEL)

    def stop(self) -> None:
        domain = f"gui/{self.uid}"
        self._run("launchctl", "bootout", domain, str(self.plist_path), check=False)
        self.plist_path.unlink(missing_ok=True)
        log.info("launchagent unloaded label=%s", LAUNCH_LABEL)

    def health(self, attempts: int = 20) -> dict:
        return http_health(attempts)

    def is_enabled(self) -> bool:
        return self.plist_path.exists()


def http_health(attempts: int = 20) -> dict:
    last = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8790/healthz", timeout=1) as response:
                return json.loads(response.read())
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last = exc
            time.sleep(0.25)
    raise ControlError(f"路由健康检查失败：{type(last).__name__}")


def _port_listening(port: int = 8790) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _run_powershell(script: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """用 -EncodedCommand(UTF-16LE + Base64) 执行脚本，绕开命令行与脚本文件的编码差异。

    stdout 的编码跟随系统 ANSI 代码页，因此调用方只应以退出码判定结果，
    不要依赖输出文本内容，避免不同代码页下中文乱码导致误判。
    """
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [shutil.which("powershell") or "powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        capture_output=True, text=True, errors="replace", timeout=timeout,
        **hidden_subprocess_kwargs(),
    )
    log.info("powershell bridge finished returncode=%s", result.returncode)
    return result


def windows_autostart_enabled() -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_AUTOSTART_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            value, _ = winreg.QueryValueEx(key, WINDOWS_AUTOSTART_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("windows autostart query failed error=%s", type(exc).__name__)
        return False


def _set_windows_autostart(command: str) -> None:
    if winreg is None:
        raise ControlError("当前环境缺少 Windows 注册表模块，无法注册登录自启")
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, WINDOWS_AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, WINDOWS_AUTOSTART_NAME, 0, winreg.REG_SZ, command)
    except OSError as exc:
        raise ControlError(f"写入 Windows 自启项失败：{type(exc).__name__}") from exc
    log.info("windows autostart written name=%s", WINDOWS_AUTOSTART_NAME)


def _remove_windows_autostart() -> None:
    if winreg is None:
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, WINDOWS_AUTOSTART_NAME)
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("windows autostart removal failed error=%s", type(exc).__name__)
        return
    log.info("windows autostart removed name=%s", WINDOWS_AUTOSTART_NAME)


def _windows_creation_flags() -> int:
    return getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def _windows_hidden_flags() -> int:
    """CREATE_NO_WINDOW for short lived console helpers.

    The admin service and the router run without a console (pythonw.exe), so
    Windows hands every console helper its own brand new console window unless
    this flag is set. That is the black box users see blinking on the desktop.
    """
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


_hidden_flags_logged = False


def hidden_subprocess_kwargs() -> dict:
    """Popen kwargs that keep helper processes invisible on Windows."""
    global _hidden_flags_logged
    if not is_windows():
        return {}
    flags = _windows_hidden_flags()
    if not _hidden_flags_logged:
        _hidden_flags_logged = True
        log.info("windows helper processes use CREATE_NO_WINDOW flags=%#x", flags)
    return {"creationflags": flags}


def _windows_listening_pids(port: int = 8790) -> list[int]:
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, errors="replace", timeout=30,
            **hidden_subprocess_kwargs(),
        )
    except OSError as exc:
        log.warning("netstat unavailable error=%s", type(exc).__name__)
        return []
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or "LISTEN" not in parts[3].upper():
            continue
        if parts[1].rsplit(":", 1)[-1] != str(port):
            continue
        try:
            pid = int(parts[4])
        except ValueError:
            continue
        if pid not in pids:
            pids.append(pid)
    return pids


def _windows_process_image(pid: int) -> str:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace", timeout=30,
            **hidden_subprocess_kwargs(),
        )
    except OSError as exc:
        log.warning("tasklist unavailable error=%s", type(exc).__name__)
        return ""
    lines = [line for line in (result.stdout or "").splitlines() if line.startswith('"')]
    if not lines:
        return ""
    return lines[0].split(",", 1)[0].strip().strip('"')


class WindowsController:
    """Windows 登录自启（注册表 Run 键）与路由进程管理。"""

    def __init__(self, runtime_dir: Path, python_exe: Optional[Path] = None):
        self.runtime_dir = runtime_dir
        self.package_dir = runtime_dir.parent
        self.python_exe = Path(python_exe or sys.executable)

    def _console_less_python(self) -> Path:
        pythonw = self.python_exe.with_name("pythonw.exe")
        return pythonw if pythonw.is_file() else self.python_exe

    def autostart_command(self) -> str:
        return f'"{self._console_less_python()}" "{self.package_dir / "toolctl.py"}" autostart'

    def is_enabled(self) -> bool:
        return windows_autostart_enabled()

    def start(self) -> bool:
        _set_windows_autostart(self.autostart_command())
        self.ensure_running()
        return True

    def restart(self) -> None:
        self._stop_router_processes()
        self._spawn_router()
        log.info("windows router restarted")

    def stop(self) -> None:
        self._stop_router_processes()
        _remove_windows_autostart()

    def health(self, attempts: int = 20) -> dict:
        return http_health(attempts)

    def ensure_running(self) -> None:
        if _port_listening():
            log.info("windows router already listening; skip spawn")
            return
        self._spawn_router()

    def _spawn_router(self) -> None:
        router_script = self.runtime_dir / "router.py"
        if not router_script.is_file():
            raise ControlError("路由文件缺失，请回界面点一次「启用路由」重建运行文件")
        log_dir = self.runtime_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "router.stdout.log").open("ab") as out, (log_dir / "router.stderr.log").open("ab") as err:
            process = subprocess.Popen(
                [str(self._console_less_python()), str(router_script), "--runtime-dir", str(self.runtime_dir)],
                cwd=str(self.runtime_dir),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                close_fds=True,
                creationflags=_windows_creation_flags(),
            )
        log.info("windows router spawned pid=%s", process.pid)

    def _stop_router_processes(self) -> None:
        for pid in _windows_listening_pids():
            image = _windows_process_image(pid)
            if not image:
                log.warning("windows port owner unresolved pid=%s", pid)
                continue
            if not image.lower().startswith("python"):
                raise ControlError(f"端口 8790 被其他程序占用（{image}），请先关闭该程序")
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"], capture_output=True, text=True, errors="replace", timeout=30,
                **hidden_subprocess_kwargs(),
            )
            log.info("windows router process stopped pid=%s returncode=%s", pid, result.returncode)
        for _ in range(20):
            if not _port_listening():
                return
            time.sleep(0.25)
        log.warning("windows port still listening after stop attempts")


def launch_controller(p: dict[str, Path]):
    if is_windows():
        return WindowsController(p["runtime"])
    return LaunchController(p["launchagent"], p["runtime"])


def enable_transaction(
    config_path: Path, backup_dir: Path, controller, changes: dict[str, Optional[str]],
    expected_model_count: int, provider=_PROVIDER_UNCHANGED,
) -> Path:
    original = config_path.read_bytes()
    started = False
    try:
        started = bool(controller.start())
        health = controller.health()
        if (
            health.get("status") != "ok"
            or health.get("version") != VERSION
            or health.get("transport") != "http-only"
            or health.get("bridge") != BRIDGE_NAME
            or health.get("local_model_count") != expected_model_count
        ):
            raise ControlError("路由健康检查结果不符合预期，Codex 配置未修改")
        backup = write_config(config_path, changes, backup_dir, provider)
        return backup
    except Exception:
        if config_path.read_bytes() != original:
            atomic_write(config_path, original)
            log.warning("enable failed; original config restored")
        if started:
            controller.stop()
        raise


def _check_platform_and_python() -> None:
    if not is_windows() and sys.platform != "darwin":
        raise ControlError("本工具只支持 macOS 和 Windows")
    if sys.version_info < (3, 9):
        raise ControlError("需要 Python 3.9 或更高版本")


def _zstd_candidates() -> tuple[Path, ...]:
    package_dir = Path(__file__).resolve().parent
    return (
        Path("/opt/homebrew/bin/zstd"),
        Path("/usr/local/bin/zstd"),
        package_dir / "zstd",
        package_dir / "zstd.exe",
        package_dir / "runtime" / "zstd",
        package_dir / "runtime" / "zstd.exe",
    )


def _bundled_zstd(source_dir: Path) -> Optional[Path]:
    """返回包内自带的 zstd 解码器（macOS 无后缀，Windows 用 .exe）。"""
    for name in ("zstd", "zstd.exe"):
        candidate = source_dir / name
        if candidate.is_file():
            return candidate
    return None


def _install_bundled_zstd(source_dir: Path, runtime_dir: Path) -> None:
    """把包内解码器复制到 runtime 目录，保证 router.py 启动时能找到。"""
    source = _bundled_zstd(source_dir)
    if source is None:
        return
    target = runtime_dir / ("zstd.exe" if is_windows() else "zstd")
    atomic_write(target, source.read_bytes(), 0o755)
    log.info("bundled zstd installed source=%s target=%s", source.name, target)


def _check_zstd_decoder() -> None:
    candidates = _zstd_candidates()
    if shutil.which("zstd") or any(path.is_file() and os.access(path, os.X_OK) for path in candidates):
        return
    try:
        import compression.zstd  # type: ignore[import-not-found]  # Python 3.14+
        return
    except ImportError:
        pass
    try:
        import zstandard  # type: ignore[import-not-found]
        return
    except ImportError as exc:
        raise ControlError("缺少 zstd 解码器；请先安装 zstd，Codex 配置未修改") from exc


def _check_port() -> None:
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", 8790)) == 0:
            raise ControlError("127.0.0.1:8790 已被占用；请先在界面点「停止路由」，或停掉旧工具的路由服务")


def _read_key(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ControlError("尚未设置上游 Key，请在界面「Key 与模型」页添加并保存") from exc
    if not value or "\n" in value or "\r" in value:
        raise ControlError("Local Key 文件无效，请重新设置")
    return value


def _write_json(path: Path, value: dict, mode: Optional[int] = None) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), mode)


def _prepare_runtime(source_dir: Path, p: dict[str, Path], manifest: dict, cache: dict) -> None:
    manifest = dict(
        manifest, official_models=[model["slug"] for model in validate_official_cache(cache)],
        official_cache_path=str(p["official_cache"].resolve()),
    )
    p["runtime"].mkdir(parents=True, exist_ok=True)
    p["logs"].mkdir(parents=True, exist_ok=True)
    router_source = source_dir / "router.py"
    atomic_write(p["runtime"] / "router.py", router_source.read_bytes(), router_source.stat().st_mode & 0o777)
    _install_bundled_zstd(source_dir, p["runtime"])
    # version 2 的清单内嵌各 Key 的密钥，必须和 keys.json 一样限制读取权限。
    manifest_mode = 0o600 if manifest.get("version") == 2 else None
    _write_json(p["manifest"], manifest, manifest_mode)
    _write_json(p["catalog"], merge_catalog(cache, manifest))
    log.info(
        "runtime prepared manifest_version=%s local_models=%d sources=%d",
        manifest.get("version"), len(manifest["models"]), len(manifest.get("sources") or {}),
    )


def _runtime_snapshot(p: dict[str, Path]) -> dict[Path, Optional[tuple[bytes, int]]]:
    snapshot = {}
    for path in (p["runtime"] / "router.py", p["manifest"], p["catalog"]):
        snapshot[path] = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
    return snapshot


def _restore_runtime(snapshot: dict[Path, Optional[tuple[bytes, int]]]) -> None:
    for path, saved in snapshot.items():
        if saved is None:
            path.unlink(missing_ok=True)
        else:
            data, mode = saved
            atomic_write(path, data, mode)
    log.warning("runtime files restored after failed update")


def _previous_runtime_manifest(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return load_manifest(path)
    except (ValidationError, OSError):
        log.warning("previous runtime manifest ignored because it is invalid")
        return None


def _settings_model_overrides(p: dict[str, Path]) -> dict:
    """读取界面保存在 settings.json 里的模型能力开关，非法条目直接丢弃。"""
    if not p["settings"].exists():
        return {}
    try:
        raw = _load_json(p["settings"], "工具设置")
    except (ValidationError, OSError):
        log.warning("tool settings unreadable; model overrides ignored")
        return {}
    raw_overrides = raw.get("model_overrides") if isinstance(raw, dict) else None
    if not isinstance(raw_overrides, dict):
        return {}
    result = {}
    for upstream_model, override in raw_overrides.items():
        if not isinstance(upstream_model, str) or not upstream_model.strip() or not isinstance(override, dict):
            log.warning("model override ignored because its shape is invalid")
            continue
        clean = {}
        modalities = override.get("input_modalities")
        if modalities is not None:
            if (
                not isinstance(modalities, list) or not modalities
                or any(item not in MODALITIES for item in modalities)
                or len(modalities) != len(set(modalities))
            ):
                log.warning("model override ignored upstream=%s reason=modalities", upstream_model)
                continue
            clean["input_modalities"] = list(modalities)
        if "supports_image_detail_original" in override:
            value = override["supports_image_detail_original"]
            if not isinstance(value, bool):
                log.warning("model override ignored upstream=%s reason=image_detail", upstream_model)
                continue
            clean["supports_image_detail_original"] = value
        if clean:
            result[upstream_model] = clean
    return result


def _resolve_runtime_manifest(source_dir: Path, p: dict[str, Path]) -> tuple[dict, dict]:
    policy = load_policy(source_dir / "local-models.json")
    overrides = _settings_model_overrides(p)
    if overrides:
        discovery = policy.setdefault("discovery", {})
        merged = dict(discovery.get("model_overrides") or {})
        merged.update(overrides)
        discovery["model_overrides"] = merged
        log.info("runtime model overrides merged from settings count=%d", len(overrides))
    keys_data = load_runtime_keys(p)
    return resolve_multi_key_manifest(policy, keys_data, _previous_runtime_manifest(p["manifest"]))


def _print_discovery_report(report: dict) -> None:
    if report.get("mode") == "multi-key":
        for entry in report["keys"]:
            if entry["error"]:
                print(f"Key「{entry['name']}」拉取失败：{entry['error']}")
                continue
            message = (
                f"Key「{entry['name']}」发现 {entry['discovered']} 个模型，"
                f"勾选 {entry['selected']} 个，已接入 {entry['included']} 个。"
            )
            if entry["skipped"]:
                message += " 不兼容 Codex 对话/工具调用，已跳过：" + "、".join(entry["skipped"]) + "。"
            print(message)
        if report["official"]:
            print("按优先级交回官方账号：" + "、".join(report["official"]) + "。")
        for conflict in report["conflicts"]:
            print(
                f"同名模型 {conflict['model']}：走 {conflict['chosen']}"
                f"（候选：{'、'.join(conflict['candidates'])}）。"
            )
        print(f"共接入 {report['included']} 个模型。")
        return
    message = f"网关发现 {report['discovered']} 个模型，已接入 {report['included']} 个，本次新增 {report['added']} 个。"
    if report["excluded"]:
        message += " 已屏蔽：" + "、".join(report["excluded"]) + "。"
    if report["skipped"]:
        message += " 不兼容 Codex 对话/工具调用，已跳过：" + "、".join(report["skipped"]) + "。"
    print(message)


def _restart_chatgpt() -> bool:
    if is_windows():
        return _restart_codex_windows()
    script = 'tell application "ChatGPT" to quit\ndelay 1\ntell application "ChatGPT" to activate'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode:
        raise ControlError("ChatGPT 自动重启失败，请手动重启 ChatGPT")
    log.info("ChatGPT restarted")
    return True


def _restart_codex_windows() -> bool:
    """尽力结束并重新打开 Codex 桌面应用；失败时只提示手动重启。"""
    for image in ("ChatGPT.exe", "Codex.exe"):
        try:
            subprocess.run(
                ["taskkill", "/IM", image, "/F"], capture_output=True, text=True, errors="replace", timeout=30,
                **hidden_subprocess_kwargs(),
            )
        except OSError as exc:
            log.warning("codex stop failed image=%s error=%s", image, type(exc).__name__)
    script = (
        "$apps = Get-StartApps | Where-Object { $_.Name -match 'Codex' -or $_.Name -match 'ChatGPT' } "
        "| Select-Object -First 1\n"
        "if ($apps) { Start-Process ('shell:AppsFolder\\' + $apps.AppID); exit 0 }\n"
        "exit 3\n"
    )
    try:
        result = _run_powershell(script)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("codex restart failed error=%s", type(exc).__name__)
        print("未能自动重启 Codex，请手动重新打开。")
        return False
    if result.returncode:
        log.warning(
            "codex restart incomplete returncode=%s stdout=%r",
            result.returncode, (result.stdout or "").strip()[:200],
        )
        print("未能自动重启 Codex，请手动重新打开。")
        return False
    log.info("Codex restarted")
    return True


def _is_unified(config_path: Path) -> bool:
    if not config_path.exists():
        return False
    source = config_path.read_text(encoding="utf-8")
    values = read_managed_config(source)
    provider = read_provider_config(source)
    return (
        values.get("model_provider") == MODEL_PROVIDER_ID
        and bool(values.get("model_catalog_json"))
        and provider == PROVIDER_CONFIG
    )


def command_set_key(p: dict[str, Path], stdin) -> None:
    write_local_key(p["key"], stdin.read())
    controller = launch_controller(p)
    if controller.is_enabled():
        controller.restart()
    print("Local Key 已安全保存。")


def command_enable(source_dir: Path, p: dict[str, Path]) -> None:
    _check_platform_and_python()
    _check_zstd_decoder()
    _check_port()
    manifest, report = _resolve_runtime_manifest(source_dir, p)
    cache = _load_json(p["official_cache"], "官方模型缓存")
    validate_official_cache(cache)
    current = read_managed_config(p["config"].read_text(encoding="utf-8")) if p["config"].exists() else {}
    if current.get("model", "").startswith("local/"):
        current = {}
    # 保留界面写入的模型能力开关，避免启用路由时把设置清掉。
    settings = _load_json(p["settings"], "工具设置") if p["settings"].exists() else {}
    if not isinstance(settings, dict):
        settings = {}
    settings["previous_model"] = current.get("model")
    settings["previous_reasoning_effort"] = current.get("model_reasoning_effort")
    _write_json(p["settings"], settings, 0o600)
    model, effort = select_fallback(cache, {"model": current.get("model"), "reasoning_effort": current.get("model_reasoning_effort")})
    snapshot = _runtime_snapshot(p)
    controller = launch_controller(p)
    changes = {
        "model": model,
        "model_reasoning_effort": effort,
        "model_provider": MODEL_PROVIDER_ID,
        "openai_base_url": None,
        "model_catalog_json": str(p["catalog"]),
    }
    try:
        _prepare_runtime(source_dir, p, manifest, cache)
        enable_transaction(
            p["config"], p["backups"], controller, changes, len(manifest["models"]), PROVIDER_CONFIG,
        )
    except Exception:
        _restore_runtime(snapshot)
        raise
    _restart_chatgpt()
    _print_discovery_report(report)
    print("已启用官方与 Local 模型。")


def command_stop(p: dict[str, Path]) -> None:
    launch_controller(p).stop()
    print("已停止 Local 路由服务。Codex 配置未修改，请继续执行“启用官方与Local模型”或“切回官方直连”。")


def command_refresh(source_dir: Path, p: dict[str, Path]) -> None:
    manifest, report = _resolve_runtime_manifest(source_dir, p)
    cache = _load_json(p["official_cache"], "官方模型缓存")
    validate_official_cache(cache)
    unified = _is_unified(p["config"])
    controller = launch_controller(p) if unified else None
    snapshot = _runtime_snapshot(p)
    try:
        _prepare_runtime(source_dir, p, manifest, cache)
        if controller:
            controller.restart()
            health = controller.health()
            if (
                health.get("status") != "ok"
                or health.get("version") != VERSION
                or health.get("transport") != "http-only"
                or health.get("bridge") != BRIDGE_NAME
                or health.get("local_model_count") != len(manifest["models"])
            ):
                raise ControlError("刷新后健康检查失败，已恢复原模型列表")
    except Exception:
        _restore_runtime(snapshot)
        if controller:
            try:
                controller.restart()
            except Exception:
                log.exception("failed to restart restored runtime")
        raise
    _print_discovery_report(report)
    if controller:
        _restart_chatgpt()
        print("模型列表已刷新，路由和 ChatGPT 已重启。")
    else:
        print("模型列表已刷新；当前为官方直连，未启动路由或修改 Codex 配置。")


def command_direct(p: dict[str, Path]) -> None:
    cache = _load_json(p["official_cache"], "官方模型缓存")
    settings = _load_json(p["settings"], "工具设置") if p["settings"].exists() else {}
    model, effort = select_fallback(cache, {"model": settings.get("previous_model"), "reasoning_effort": settings.get("previous_reasoning_effort")})
    changes = {"model": model, "model_reasoning_effort": effort, "model_provider": "openai", "openai_base_url": None, "model_catalog_json": None}
    write_config(p["config"], changes, p["backups"], None)
    _restart_chatgpt()
    launch_controller(p).stop()
    print(f"已切回官方直连，当前模型：{model}（{effort}）。")


def command_autostart(p: dict[str, Path]) -> None:
    """Windows 登录自启入口：端口空闲时才拉起路由，保证幂等。"""
    if not is_windows():
        raise ControlError("autostart 仅用于 Windows")
    controller = launch_controller(p)
    if not isinstance(controller, WindowsController):
        raise ControlError("autostart 仅用于 Windows")
    controller.ensure_running()


def _configure_stdio() -> None:
    """Windows 控制台/重定向下用 UTF-8 输出，避免中文提示触发编码异常。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:  # pythonw 等无控制台场景：丢弃输出
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _prompt_local_key() -> str:
    print("请输入 Local 模型 Key（输入内容不会显示）。")
    return getpass.getpass("Local Key: ")


def main(argv=None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="管理 Codex 官方与 Local 模型路由")
    parser.add_argument("command", choices=("set-key", "enable", "stop", "refresh", "direct", "autostart"))
    parser.add_argument("--stdin", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--package", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    source_dir = Path(__file__).resolve().parent
    p = paths(args.home, args.package)
    configure_logging(p["logs"])
    try:
        if args.command == "set-key":
            command_set_key(p, sys.stdin if args.stdin else io.StringIO(_prompt_local_key()))
        elif args.command == "enable":
            command_enable(source_dir, p)
        elif args.command == "stop":
            command_stop(p)
        elif args.command == "refresh":
            command_refresh(source_dir, p)
        elif args.command == "autostart":
            command_autostart(p)
        else:
            command_direct(p)
    except (ValidationError, ControlError, OSError) as exc:
        log.error("command failed command=%s error_type=%s error=%s", args.command, type(exc).__name__, exc)
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
