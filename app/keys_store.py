#!/usr/bin/env python3
"""Multi-key store for the Codex multi-key router.

Only the standard library is used so the same file runs on macOS and Windows.
Every read/write is explicit UTF-8 (BOM tolerated on read, never written).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger("codex_multi_key_store")
log.addHandler(logging.NullHandler())

DEFAULT_UPSTREAM = "https://api.example.com/v1"
OFFICIAL_SOURCE = "official"
KEY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_NAME = 64
MAX_API_KEY = 512


class KeysError(ValueError):
    """Raised when keys.json cannot be validated."""


def default_keys() -> dict:
    return {"version": 1, "keys": [], "model_priority": {}}


def new_key_id(existing) -> str:
    taken = set(existing or ())
    while True:
        candidate = "k" + uuid.uuid4().hex[:10]
        if candidate not in taken:
            return candidate


def normalize_upstream(value) -> str:
    if not isinstance(value, str):
        raise KeysError("上游地址必须是文本")
    text = value.strip().rstrip("/")
    if not text:
        raise KeysError("上游地址不能为空")
    if not (text.startswith("http://") or text.startswith("https://")):
        raise KeysError("上游地址必须以 http:// 或 https:// 开头")
    if len(text) > 512 or any(char.isspace() or ord(char) < 32 for char in text):
        raise KeysError("上游地址含有非法字符")
    return text


def make_key(name, api_key, upstream_base_url=None, enabled=True,
             order=0, selected_models=None, key_id=None, existing=()) -> dict:
    return validate_keys({
        "version": 1,
        "keys": [{
            "id": key_id or new_key_id(existing),
            "name": name,
            "api_key": api_key,
            "upstream_base_url": upstream_base_url or DEFAULT_UPSTREAM,
            "enabled": enabled,
            "order": order,
            "selected_models": selected_models,
        }],
        "model_priority": {},
    })["keys"][0]


def _validate_selected_models(name: str, value) -> Optional[list]:
    if value is None:
        return None
    if not isinstance(value, list):
        raise KeysError(f"{name} 的模型勾选必须是数组或留空")
    cleaned = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise KeysError(f"{name} 的模型勾选含有非法条目")
        text = item.strip()
        if len(text) > 256:
            raise KeysError(f"{name} 的模型名过长")
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def validate_keys(data: dict) -> dict:
    if not isinstance(data, dict):
        raise KeysError("配置文件顶层必须是对象")
    if data.get("version") != 1:
        raise KeysError("keys.json 的 version 只支持 1")
    raw_keys = data.get("keys")
    if not isinstance(raw_keys, list):
        raise KeysError("keys 必须是数组")

    keys = []
    seen_ids = set()
    seen_names = set()
    for index, item in enumerate(raw_keys):
        position = f"第 {index + 1} 个 Key"
        if not isinstance(item, dict):
            raise KeysError(f"{position} 必须是对象")
        key_id = item.get("id")
        if not isinstance(key_id, str) or not KEY_ID_PATTERN.fullmatch(key_id):
            raise KeysError(f"{position} 的 id 非法")
        if key_id in seen_ids:
            raise KeysError(f"Key id 重复：{key_id}")
        seen_ids.add(key_id)
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise KeysError(f"{position} 的名称不能为空")
        name = name.strip()
        if len(name) > MAX_NAME:
            raise KeysError(f"{position} 的名称过长")
        if name in seen_names:
            raise KeysError(f"Key 名称重复：{name}")
        seen_names.add(name)
        api_key = item.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            raise KeysError(f"{name} 的密钥不能为空")
        api_key = api_key.strip()
        if len(api_key) > MAX_API_KEY or any(ord(char) < 32 for char in api_key):
            raise KeysError(f"{name} 的密钥含有非法字符")
        upstream = normalize_upstream(item.get("upstream_base_url", DEFAULT_UPSTREAM))
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise KeysError(f"{name} 的启用状态必须是布尔值")
        order = item.get("order", index)
        if not isinstance(order, int) or isinstance(order, bool):
            raise KeysError(f"{name} 的排序值必须是整数")
        selected = _validate_selected_models(name, item.get("selected_models"))
        keys.append({
            "id": key_id,
            "name": name,
            "api_key": api_key,
            "upstream_base_url": upstream,
            "enabled": enabled,
            "order": order,
            "selected_models": selected,
        })

    keys.sort(key=lambda entry: (entry["order"], entry["name"]))
    for position, entry in enumerate(keys):
        entry["order"] = position

    raw_priority = data.get("model_priority")
    if raw_priority is None:
        raw_priority = {}
    if not isinstance(raw_priority, dict):
        raise KeysError("model_priority 必须是对象")
    priority = {}
    for model_name, target in raw_priority.items():
        if not isinstance(model_name, str) or not model_name.strip():
            raise KeysError("model_priority 的模型名非法")
        if target != OFFICIAL_SOURCE and target not in seen_ids:
            raise KeysError(f"模型 {model_name.strip()} 指向了不存在的 Key")
        priority[model_name.strip()] = target
    return {"version": 1, "keys": keys, "model_priority": priority}


def load_keys(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        log.info("keys file missing path=%s using defaults", path)
        return default_keys()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise KeysError(f"keys.json 不是有效 JSON：第 {exc.lineno} 行") from exc
    except OSError as exc:
        raise KeysError(f"无法读取 keys.json：{type(exc).__name__}") from exc
    validated = validate_keys(raw)
    log.info("keys loaded path=%s keys=%d priority=%d", path, len(validated["keys"]), len(validated["model_priority"]))
    return validated


def save_keys(path: Path, data: dict) -> dict:
    path = Path(path)
    validated = validate_keys(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(validated, ensure_ascii=False, indent=2) + "\n"
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    log.info("keys saved path=%s keys=%d priority=%d", path, len(validated["keys"]), len(validated["model_priority"]))
    return validated


def ordered_keys(data: dict) -> list:
    return sorted(data.get("keys") or [], key=lambda entry: entry["order"])


def enabled_keys(data: dict) -> list:
    return [entry for entry in ordered_keys(data) if entry["enabled"]]


def key_index(data: dict) -> dict:
    return {entry["id"]: entry for entry in data.get("keys") or []}


def mask_api_key(api_key: str) -> str:
    text = (api_key or "").strip()
    if len(text) <= 12:
        return "*" * len(text)
    return text[:5] + "*" * 6 + text[-4:]


def selected_models_for(key: dict) -> Optional[set]:
    selected = key.get("selected_models")
    if selected is None:
        return None
    return set(selected)


def apply_selected_models(key: dict, models) -> list:
    allowed = selected_models_for(key)
    if allowed is None:
        return list(models)
    return [model for model in models if model in allowed]
