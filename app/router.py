#!/usr/bin/env python3
"""Local router that keeps official and explicitly registered local models separate."""

from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import time
from typing import Iterable, Optional, Union
from urllib.parse import urlsplit, urlunsplit
import uuid
import zlib

try:
    from compression import zstd as _stdlib_zstd
except ImportError:  # Python < 3.14
    _stdlib_zstd = None

try:
    import zstandard as _third_party_zstd
except ImportError:
    _third_party_zstd = None


VERSION = "6"
BRIDGE_NAME = "responses-chat-completions"
IMAGEGEN_ALIAS = "codex_router_imagegen"
IMAGE_TURN_TTL_SECONDS = 30 * 60
THREAD_ROUTE_TTL_SECONDS = 24 * 60 * 60
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8790
OFFICIAL_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_UPSTREAM = "https://api.example.com/v1"
LOCAL_KEY_FILE = "local-api-key.secret"
MANIFEST_FILE = "local-models.json"
REASONING_PREFIX = "codex-official-local-reasoning-v1:"
LEGACY_REASONING_PREFIX = "codex-gateway-reasoning-v1:"
MAX_REASONING_CARRIER = 4 * 1024 * 1024
TIMEOUT_SECONDS = 240
TITLE_TIMEOUT_SECONDS = 20
TITLE_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
ZSTD_CANDIDATES = (
    Path("/opt/homebrew/bin/zstd"),
    Path("/usr/local/bin/zstd"),
    # 包内自带的解码器：与 router.py 同目录，或位于包根 runtime/ 的上一级
    Path(__file__).resolve().parent / "zstd",
    Path(__file__).resolve().parent / "zstd.exe",
    Path(__file__).resolve().parent.parent / "zstd",
    Path(__file__).resolve().parent.parent / "zstd.exe",
)
CHAT_REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_details")
LOCAL_REASONING_LANGUAGE = "zh-CN"
LOCAL_REASONING_LANGUAGE_INSTRUCTION = (
    "所有通过 reasoning 或 reasoning_content 字段输出的推理标题和正文必须使用简体中文。"
    "代码、命令、SQL、路径、字段名、标识符和专有名词保持原样。"
    "本指令只约束推理文字；最终回答的语言继续遵循用户要求。"
)
VALID_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
IMAGE_OMITTED_TEXT = "[图片已省略：当前模型未开启图片输入]"
RECOVERY_MESSAGE = (
    "Your previous attempt stopped after planning. Continue the task now. "
    "Emit the next available tool call, or provide a complete final answer if no tool is needed. "
    "Do not stop after planning."
)
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

log = logging.getLogger("codex_official_local_router")
log.addHandler(logging.NullHandler())


@dataclass
class PreparedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    request_id: str = ""


@dataclass
class UpstreamResponse:
    status: int
    headers: dict[str, str]
    body: Union[bytes, Iterable[bytes]]
    reason: str = ""


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ModelRoutes:
    """短期保存 Codex 标识与本地模型的绑定。"""

    def __init__(self, ttl_seconds: int = IMAGE_TURN_TTL_SECONDS) -> None:
        self._routes: dict[str, tuple[str, float, Optional[str]]] = {}
        self._lock = threading.Lock()
        self._ttl_seconds = ttl_seconds

    def remember(self, turn_id: str, model: str, fingerprint: Optional[str] = None) -> None:
        now = time.monotonic()
        with self._lock:
            self._routes = {
                key: value for key, value in self._routes.items()
                if now - value[1] < self._ttl_seconds
            }
            if len(self._routes) >= 1024:
                oldest = min(self._routes, key=lambda key: self._routes[key][1])
                del self._routes[oldest]
            self._routes[turn_id] = (model, now, fingerprint)

    def fingerprint_for(self, turn_id: str) -> Optional[str]:
        with self._lock:
            route = self._routes.get(turn_id)
            return route[2] if route else None

    def model_for(self, turn_id: str) -> Optional[str]:
        with self._lock:
            route = self._routes.get(turn_id)
            if not route:
                return None
            if time.monotonic() - route[1] >= self._ttl_seconds:
                del self._routes[turn_id]
                return None
            return route[0]

    def forget(self, turn_id: str) -> None:
        with self._lock:
            self._routes.pop(turn_id, None)


class ImageTurnRoutes(ModelRoutes):
    """将图片请求的 turn ID 绑定到发起调用的本地模型。"""


IMAGE_TURN_ROUTES = ImageTurnRoutes()
TITLE_SESSION_ROUTES = ModelRoutes()
TITLE_CALL_ROUTES = ModelRoutes()
THREAD_MODEL_ROUTES = ModelRoutes(THREAD_ROUTE_TTL_SECONDS)


def configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "router.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)


def _validated_slugs(models) -> None:
    if not isinstance(models, list):
        raise ValueError("Local model manifest is invalid")
    slugs = [item.get("slug") for item in models if isinstance(item, dict)]
    if len(slugs) != len(models) or len(slugs) != len(set(slugs)):
        raise ValueError("Local model manifest contains invalid or duplicate slugs")
    if any(
        not isinstance(slug, str)
        or (slug != "auto" and (not slug.startswith("local/") or slug != slug.lower()))
        for slug in slugs
    ):
        raise ValueError("Local model manifest contains an unsafe slug")


def _validated_sources(sources) -> None:
    """v2 manifest 每个来源要么是官方账号，要么是一个带密钥的上游。"""
    if not isinstance(sources, dict) or not sources:
        raise ValueError("Local model manifest has no sources")
    for source_id, source in sources.items():
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("Local model manifest contains an invalid source id")
        if not isinstance(source, dict):
            raise ValueError("Local model manifest contains an invalid source")
        if source.get("kind") == "official":
            continue
        if source.get("kind") != "key":
            raise ValueError("Local model manifest contains an unknown source kind")
        upstream = source.get("upstream_base_url")
        if not isinstance(upstream, str) or not upstream.startswith(("http://", "https://")):
            raise ValueError("Local model manifest contains an invalid upstream")
        api_key = source.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Local model manifest contains an empty api key")


def load_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    # The control plane performs detailed validation before installation. These
    # checks prevent a damaged runtime copy from widening routing boundaries.
    version = data.get("version")
    if version == 1:
        _validated_slugs(data.get("models"))
        if data.get("upstream_base_url") != DEFAULT_UPSTREAM:
            raise ValueError("Unexpected Local upstream")
        return data
    if version != 2:
        raise ValueError("Local model manifest is invalid")
    _validated_slugs(data.get("models"))
    agent_models = data.get("agent_models", [])
    if not isinstance(agent_models, list):
        raise ValueError("Local model manifest contains invalid agent models")
    if any(
        not isinstance(item, dict) or not isinstance(item.get("upstream_model"), str)
        or not item["upstream_model"].strip() for item in agent_models
    ):
        raise ValueError("Local model manifest contains invalid agent upstream models")
    _validated_slugs(data["models"] + agent_models)
    if "official_models" in data and (
        not isinstance(data["official_models"], list)
        or any(not isinstance(model, str) or not model.strip() for model in data["official_models"])
        or len(data["official_models"]) != len(set(data["official_models"]))
    ):
        raise ValueError("Local model manifest contains invalid official models")
    if "official_cache_path" in data and (
        not isinstance(data["official_cache_path"], str) or not data["official_cache_path"].strip()
    ):
        raise ValueError("Local model manifest contains an invalid official cache path")
    _validated_sources(data.get("sources"))
    sources = data["sources"]
    for item in data["models"] + agent_models:
        source = sources.get(item.get("source"))
        if not isinstance(source, dict) or source.get("kind") != "key":
            raise ValueError("Local model manifest references an unavailable source")
    return data


def read_local_key(path: Path) -> Optional[str]:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return value if value and "\n" not in value and "\r" not in value else None


def health_payload(manifest: dict, local_key: Optional[str]) -> dict:
    sources = manifest.get("sources") or {}
    key_sources = [item for item in sources.values() if isinstance(item, dict) and item.get("kind") == "key"]
    return {
        "status": "ok",
        "version": VERSION,
        "transport": "http-only",
        "bridge": BRIDGE_NAME,
        "local_model_count": len(manifest["models"]),
        "local_key_present": bool(local_key) or any(item.get("api_key") for item in key_sources),
        "source_count": len(key_sources),
    }


def zstd_decoder_available() -> bool:
    return bool(
        _stdlib_zstd
        or _third_party_zstd
        or shutil.which("zstd")
        or any(_zstd_candidate_usable(path) for path in ZSTD_CANDIDATES)
    )


def _zstd_candidate_usable(path: Path) -> bool:
    if not path.is_file():
        return False
    return os.name == "nt" or os.access(path, os.X_OK)


_hidden_flags_logged = False


def _is_windows() -> bool:
    return os.name == "nt"


def _hidden_console_kwargs() -> dict:
    """CREATE_NO_WINDOW keeps a helper from flashing a black console box.

    The router runs under pythonw.exe on Windows, so every console helper it
    spawns (zstd here) would otherwise open its own console window.
    """
    global _hidden_flags_logged
    if not _is_windows():
        return {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    if not _hidden_flags_logged:
        _hidden_flags_logged = True
        log.info("zstd helper uses CREATE_NO_WINDOW flags=%#x", flags)
    return {"creationflags": flags}


def _decompress_zstd(body: bytes) -> bytes:
    if _stdlib_zstd:
        return _stdlib_zstd.decompress(body)
    if _third_party_zstd:
        return _third_party_zstd.ZstdDecompressor().decompress(body)
    executable = shutil.which("zstd") or next(
        (str(path) for path in ZSTD_CANDIDATES if _zstd_candidate_usable(path)),
        None,
    )
    if not executable:
        raise ValueError("zstd decoder is unavailable")
    result = subprocess.run(
        [executable, "-d", "-q", "-c"], input=body, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=TIMEOUT_SECONDS, check=False,
        **_hidden_console_kwargs(),
    )
    if result.returncode:
        raise ValueError("invalid zstd request body")
    return result.stdout


def _decoded_json(body: bytes, headers: dict[str, str]) -> dict:
    encoding = next((v for k, v in headers.items() if k.lower() == "content-encoding"), "").strip().lower()
    if encoding == "gzip":
        body = gzip.decompress(body)
    elif encoding == "zstd":
        body = _decompress_zstd(body)
    elif encoding and encoding != "identity":
        raise ValueError("unsupported content encoding")
    value = json.loads(body.decode("utf-8")) if body else {}
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object")
    return value


def _model_record(manifest: dict, slug: str) -> Optional[dict]:
    return next((item for item in manifest["models"] + manifest.get("agent_models", []) if item["slug"] == slug), None)


def _model_identity(value: str) -> str:
    return re.sub(r"[\s/_.\-]+", "", value.lower())


def _model_source(manifest: dict, record: dict) -> tuple[Optional[str], str, Optional[str]]:
    """返回模型应走的上游地址、来源标识和该来源自己的密钥。

    v1 manifest 只有一个全局上游和一个全局 Key，来源固定为 local。
    """
    if manifest.get("version") == 1:
        return manifest.get("upstream_base_url"), "local", None
    sources = manifest.get("sources") or {}
    source_id = str(record.get("source") or "")
    source = sources.get(source_id)
    if not isinstance(source, dict):
        return None, source_id, None
    return source.get("upstream_base_url"), source_id, source.get("api_key")


def _pack_reasoning(model: str, fields: dict) -> Optional[str]:
    clean = {key: value for key, value in fields.items() if value not in (None, "", [], {})}
    if not clean:
        return None
    raw = json.dumps({"version": 1, "kind": "chat", "model": model, "fields": clean}, ensure_ascii=False, separators=(",", ":")).encode()
    return REASONING_PREFIX + base64.urlsafe_b64encode(zlib.compress(raw, 6)).decode("ascii")


def _unpack_reasoning(value: object, model: str) -> dict:
    if not isinstance(value, str):
        return {}
    prefix = next((item for item in (REASONING_PREFIX, LEGACY_REASONING_PREFIX) if value.startswith(item)), None)
    if not prefix:
        return {}
    try:
        compressed = base64.urlsafe_b64decode(value[len(prefix):].encode("ascii"))
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_REASONING_CARRIER + 1) + decoder.flush()
        if len(raw) > MAX_REASONING_CARRIER or decoder.unconsumed_tail:
            return {}
        payload = json.loads(raw)
    except (ValueError, TypeError, zlib.error):
        return {}
    if (
        payload.get("version") != 1
        or payload.get("model") != model
        or payload.get("kind", "chat") != "chat"
        or not isinstance(payload.get("fields"), dict)
    ):
        return {}
    return {key: value for key, value in payload["fields"].items() if key in CHAT_REASONING_FIELDS}


def _merge_reasoning_details(existing: object, incoming: object) -> object:
    if existing in (None, "", [], {}):
        return incoming
    if isinstance(existing, list) and isinstance(incoming, list):
        result = list(existing)
        for value in incoming:
            if value not in result:
                result.append(value)
        return result
    return incoming


@dataclass(frozen=True)
class ToolSpec:
    kind: str
    name: str
    namespace: tuple[str, ...]
    upstream_name: str
    description: str
    parameters: dict


def _tool_alias(kind: str, namespace: tuple[str, ...], name: str, used: set[str]) -> str:
    if VALID_TOOL_NAME.fullmatch(name) and name not in used:
        return name
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "tool"
    digest = hashlib.sha256(f"{kind}:{'/'.join(namespace)}:{name}".encode()).hexdigest()[:10]
    room = 64 - len(digest) - 1
    candidate = f"{base[:room]}_{digest}"
    counter = 1
    while candidate in used:
        suffix = f"_{counter}"
        candidate = f"{base[:room - len(suffix)]}_{digest}{suffix}"
        counter += 1
    return candidate


class ToolRegistry:
    """Lossless mapping between Codex Responses tools and Chat Completions functions."""

    def __init__(self):
        self.specs: list[ToolSpec] = []
        self.by_upstream: dict[str, ToolSpec] = {}
        self.by_original: dict[tuple[str, tuple[str, ...], str], ToolSpec] = {}
        self.history_only: dict[tuple[str, tuple[str, ...], str], ToolSpec] = {}
        self._definitions: dict[tuple[str, tuple[str, ...], str], str] = {}
        self.namespace_count = 0
        self.additional_group_count = 0
        self.tool_search_count = 0
        self.web_search_count = 0
        self.web_search_history_count = 0
        self.web_search_choice_count = 0

    @classmethod
    def from_body(cls, body: dict) -> "ToolRegistry":
        registry = cls()
        top_level = body.get("tools")
        if top_level is not None and not isinstance(top_level, list):
            raise ProtocolError("invalid_tools", "tools 必须是数组。")
        for tool in top_level or []:
            registry._walk(tool, (), ())
        request_input = body.get("input")
        if isinstance(request_input, list):
            for item in request_input:
                if not isinstance(item, dict) or item.get("type") != "additional_tools":
                    continue
                registry.additional_group_count += 1
                tools = item.get("tools")
                if not isinstance(tools, list):
                    raise ProtocolError("invalid_additional_tools", "additional_tools.tools 必须是数组。")
                for tool in tools:
                    registry._walk(tool, (), ())
        return registry

    def _walk(self, tool: object, namespace: tuple[str, ...], namespace_descriptions: tuple[str, ...]) -> None:
        if not isinstance(tool, dict):
            raise ProtocolError("invalid_tool", "工具定义必须是对象。")
        kind = str(tool.get("type") or "")
        if kind == "web_search":
            # Chat 桥接没有内置网页搜索执行端，暂时跳过，避免阻断普通对话。
            self.web_search_count += 1
            return
        if kind == "tool_search":
            # Chat has no deferred discovery endpoint; additional_tools below
            # are already expanded into ordinary function definitions.
            self.tool_search_count += 1
            return
        if kind == "namespace":
            name = tool.get("name")
            children = tool.get("tools")
            if not isinstance(name, str) or not name.strip() or not isinstance(children, list):
                raise ProtocolError("invalid_tool_namespace", "namespace 必须包含名称和 tools 数组。")
            self.namespace_count += 1
            description = str(tool.get("description") or "").strip()
            descriptions = namespace_descriptions + ((description,) if description else ())
            for child in children:
                self._walk(child, namespace + (name.strip(),), descriptions)
            return
        if kind not in {"function", "custom"}:
            raise ProtocolError("unsupported_tool_type", f"Local 桥接不支持工具类型：{kind or 'missing'}。")
        source = tool.get("function") if kind == "function" and isinstance(tool.get("function"), dict) else tool
        name = source.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProtocolError("invalid_tool_name", "工具名称不能为空。")
        name = name.strip()
        description = str(source.get("description") or "").strip()
        parameters = source.get("parameters")
        input_format = source.get("format")
        if kind == "function":
            if parameters is None:
                parameters = {"type": "object", "properties": {}}
            if not isinstance(parameters, dict):
                raise ProtocolError("invalid_tool_schema", f"工具 {name} 的 parameters 必须是对象。")
        else:
            parameters = {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
                "additionalProperties": False,
            }
            extra = "Pass the complete free-form tool input in the `input` string."
            if input_format not in (None, "", {}):
                extra += " Original input format: " + json.dumps(input_format, ensure_ascii=False, separators=(",", ":"))
            description = (description + "\n" + extra).strip()
        if namespace:
            context = "Namespace: " + ".".join(namespace) + "."
            inherited = " ".join(value for value in namespace_descriptions if value)
            description = " ".join(value for value in (context, inherited, description) if value)
        # 宿主会在历史中追加新版说明；只把参数和输入语法的变化视为定义冲突。
        definition = json.dumps(
            {"kind": kind, "name": name, "parameters": source.get("parameters"), "format": input_format},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        identity = (kind, namespace, name)
        existing = self.by_original.get(identity)
        if existing:
            if self._definitions[identity] != definition:
                log.warning(
                    "桥接工具定义冲突 kind=%s namespace=%r name=%r old_schema=%s new_schema=%s",
                    kind, ".".join(namespace) or "-", name,
                    hashlib.sha256(self._definitions[identity].encode()).hexdigest()[:12],
                    hashlib.sha256(definition.encode()).hexdigest()[:12],
                )
                raise ProtocolError("duplicate_tool_name", f"工具名称 {name} 有冲突，无法安全映射。")
            if existing.description == description:
                return
            spec = ToolSpec(kind, name, namespace, existing.upstream_name, description, parameters)
            self.specs[self.specs.index(existing)] = spec
            self.by_upstream[spec.upstream_name] = spec
            self.by_original[identity] = spec
            log.info(
                "桥接工具说明已更新 kind=%s namespace=%r name=%r old_chars=%d new_chars=%d",
                kind, ".".join(namespace) or "-", name, len(existing.description), len(description),
            )
            return
        used = set(self.by_upstream)
        upstream_name = _tool_alias(kind, namespace, name, used)
        spec = ToolSpec(kind, name, namespace, upstream_name, description, parameters)
        self.specs.append(spec)
        self.by_upstream[upstream_name] = spec
        self.by_original[identity] = spec
        self._definitions[identity] = definition

    def metrics(self) -> dict[str, int]:
        return {
            "additional_groups": self.additional_group_count,
            "namespaces": self.namespace_count,
            "functions": sum(spec.kind == "function" for spec in self.specs),
            "custom": sum(spec.kind == "custom" for spec in self.specs),
            "history_only": len(self.history_only),
            "total": len(self.specs),
        }

    def chat_tools(self, specs: Optional[list[ToolSpec]] = None) -> list[dict]:
        result = []
        for spec in self.specs if specs is None else specs:
            function = {"name": spec.upstream_name, "parameters": spec.parameters}
            if spec.description:
                function["description"] = spec.description
            result.append({"type": "function", "function": function})
        return result

    def original(self, kind: str, name: str, namespace: Optional[str] = None) -> Optional[ToolSpec]:
        if namespace is not None:
            if not isinstance(namespace, str) or not namespace:
                raise ProtocolError("invalid_tool_namespace", "工具 namespace 必须是非空字符串。")
            return self.by_original.get((kind, tuple(namespace.split(".")), name))
        matches = [
            spec for (spec_kind, _spec_namespace, spec_name), spec in self.by_original.items()
            if spec_kind == kind and spec_name == name
        ]
        if len(matches) > 1:
            raise ProtocolError("ambiguous_tool_name", f"工具 {name} 存在多个 namespace，必须明确指定。")
        return matches[0] if matches else None

    def history_spec(self, kind: str, name: str, namespace: object = None) -> ToolSpec:
        if namespace is not None and (not isinstance(namespace, str) or not namespace):
            raise ProtocolError("invalid_tool_history", f"历史工具调用的 namespace 与工具 {name} 不匹配。")
        spec = self.original(kind, name, namespace)
        if spec:
            return spec

        # Compaction requests may replay completed calls without making those
        # tools available for the new response. Keep their aliases separate so
        # history remains readable but the model cannot call them again.
        history_namespace = tuple(namespace.split(".")) if namespace else ()
        identity = (kind, history_namespace, name)
        existing = self.history_only.get(identity)
        if existing:
            return existing
        used = set(self.by_upstream)
        used.update(value.upstream_name for value in self.history_only.values())
        parameters = (
            {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
                "additionalProperties": False,
            }
            if kind == "custom"
            else {"type": "object", "properties": {}}
        )
        spec = ToolSpec(kind, name, history_namespace, _tool_alias(kind, history_namespace, name, used), "", parameters)
        self.history_only[identity] = spec
        return spec

    def decode_call(self, upstream_name: str, arguments: str) -> tuple[ToolSpec, str]:
        spec = self.by_upstream.get(upstream_name)
        if not spec:
            raise ProtocolError("unknown_tool_call", "Local 模型返回了未注册的工具调用。")
        return spec, self.decode_arguments(spec, arguments)

    def decode_arguments(self, spec: ToolSpec, arguments: str) -> str:
        """校验并解包某次工具调用的参数；函数工具原样返回。"""
        if spec.kind == "function":
            return arguments
        try:
            wrapper = json.loads(arguments)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProtocolError("invalid_custom_tool_input", "Local 模型返回了无效的自定义工具参数。") from exc
        if not isinstance(wrapper, dict) or set(wrapper) != {"input"} or not isinstance(wrapper.get("input"), str):
            raise ProtocolError("invalid_custom_tool_input", "Local 模型返回了无效的自定义工具参数。")
        return wrapper["input"]

    @staticmethod
    def _call_match_key(name: str) -> str:
        """工具名纠偏键：忽略大小写与符号，并去掉 `_` + 10 位十六进制别名后缀。"""
        text = re.sub(r"_[0-9a-f]{10}$", "", str(name or "").strip().lower())
        return re.sub(r"[^a-z0-9]+", "", text)

    def recover_call(self, upstream_name: str) -> Optional[ToolSpec]:
        """模型写错工具名时的纠偏：归一化后唯一命中才采用。"""
        key = self._call_match_key(upstream_name)
        if not key:
            return None
        matches: list[ToolSpec] = []
        for candidate, spec in self.by_upstream.items():
            if self._call_match_key(candidate) == key and spec not in matches:
                matches.append(spec)
        for (_kind, _namespace, name), spec in self.by_original.items():
            if self._call_match_key(name) == key and spec not in matches:
                matches.append(spec)
        if len(matches) == 1:
            log.info("local bridge recovered tool name original=%s matched=%s", upstream_name, matches[0].upstream_name)
            return matches[0]
        if len(matches) > 1:
            log.warning("local bridge tool name ambiguous original=%s candidates=%d", upstream_name, len(matches))
        return None


def _content_parts(parts: object, allow_images: bool = True) -> object:
    if not isinstance(parts, list):
        return parts
    result = []
    for part in parts:
        if not isinstance(part, dict):
            result.append({"type": "text", "text": str(part)})
            continue
        kind = part.get("type")
        if kind in {"input_text", "output_text", "text"} and isinstance(part.get("text"), str):
            result.append({"type": "text", "text": part["text"]})
        elif kind in {"input_image", "image", "image_url"}:
            if not allow_images:
                # 模型未开启图片输入时用文字占位，避免上游直接 4xx 断流。
                result.append({"type": "text", "text": IMAGE_OMITTED_TEXT})
                continue
            value = part.get("image_url") or part.get("url")
            if isinstance(value, dict):
                value = value.get("url")
            if isinstance(value, str):
                result.append({"type": "image_url", "image_url": {"url": value}})
        elif kind == "refusal" and part.get("refusal"):
            result.append({"type": "text", "text": str(part["refusal"])})
        else:
            result.append({"type": "text", "text": json.dumps(part, ensure_ascii=False)})
    if len(result) == 1 and result[0]["type"] == "text":
        return result[0]["text"]
    return result


def _agent_message_text(item: dict) -> str:
    parts = item.get("content")
    if not isinstance(parts, list):
        raise ProtocolError("invalid_agent_message", "Agent 消息缺少 content 数组。")
    values = []
    for part in parts:
        if not isinstance(part, dict):
            values.append(str(part))
            continue
        kind = part.get("type")
        if kind in {"input_text", "output_text", "text"} and isinstance(part.get("text"), str):
            values.append(part["text"])
        elif kind == "encrypted_content":
            raise ProtocolError(
                "encrypted_agent_message_unsupported",
                "Chat 桥接无法读取加密的代理任务或报告，请使用同一来源的原生 Responses 模型。原消息未删除。",
            )
        else:
            converted = _content_parts([part], allow_images=False)
            if isinstance(converted, str):
                values.append(converted)
            elif converted:
                values.append(json.dumps(converted, ensure_ascii=False, separators=(",", ":")))
    text = "\n".join(value for value in values if value)
    if not text:
        raise ProtocolError("invalid_agent_message", "Agent 消息没有可转发的正文。")
    return text


def _normalize_agent_messages(body_obj: dict) -> tuple[dict, int]:
    """把 Codex 内部 agent_message 改成标准 Responses 用户消息。"""
    request_input = body_obj.get("input")
    if not isinstance(request_input, list):
        return body_obj, 0
    result = copy.deepcopy(body_obj)
    rewrites = 0
    normalized = []
    for item in result["input"]:
        if isinstance(item, dict) and item.get("type") == "agent_message" and not any(
            isinstance(part, dict) and part.get("type") == "encrypted_content"
            for part in (item.get("content") if isinstance(item.get("content"), list) else [])
        ):
            normalized.append({
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": _agent_message_text(item)}],
            })
            rewrites += 1
        else:
            normalized.append(item)
    result["input"] = normalized
    return result, rewrites


def _tool_output_content(output: object, allow_images: bool = True) -> object:
    if isinstance(output, list) and any(
        isinstance(part, dict) and part.get("type") in {"input_image", "image", "image_url"}
        for part in output
    ):
        return _content_parts(output, allow_images)
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False, separators=(",", ":"))


def _item_to_message(
    item: object, registry: ToolRegistry, calls: dict[str, ToolSpec], allow_images: bool = True,
) -> Optional[dict]:
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    if kind == "message":
        role = "system" if item.get("role") == "developer" else str(item.get("role") or "user")
        content = _content_parts(item.get("content") or [], allow_images)
        return {"role": role, "content": content} if content not in (None, [], "") else None
    if kind == "agent_message":
        return {"role": "user", "content": _agent_message_text(item)}
    if kind in {"function_call", "custom_tool_call"}:
        tool_kind = "custom" if kind == "custom_tool_call" else "function"
        call_id = str(item.get("call_id") or item.get("id") or "call_" + uuid.uuid4().hex[:12])
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProtocolError("invalid_tool_history", "历史工具调用缺少名称。")
        name = name.strip()
        if call_id in calls:
            raise ProtocolError("duplicate_call_id", "历史工具调用包含重复 call_id。")
        spec = registry.history_spec(tool_kind, name, item.get("namespace"))
        calls[call_id] = spec
        if tool_kind == "custom":
            raw_input = item.get("input", "")
            if not isinstance(raw_input, str):
                raise ProtocolError("invalid_custom_tool_history", "custom_tool_call.input 必须是字符串。")
            arguments = json.dumps({"input": raw_input}, ensure_ascii=False, separators=(",", ":"))
        else:
            arguments = item.get("arguments", "")
            if not isinstance(arguments, str):
                raise ProtocolError("invalid_function_tool_history", "function_call.arguments 必须是字符串。")
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": call_id, "type": "function", "function": {
                "name": spec.upstream_name, "arguments": arguments,
            },
        }]}
    if kind in {"function_call_output", "custom_tool_call_output"}:
        expected_kind = "custom" if kind == "custom_tool_call_output" else "function"
        call_id = str(item.get("call_id") or "")
        prior = calls.get(call_id)
        expected_namespace = ".".join(prior.namespace) if prior and prior.namespace else None
        if (
            not call_id or not prior or prior.kind != expected_kind
            or (item.get("name") is not None and item.get("name") != prior.name)
            or (item.get("namespace") is not None and item.get("namespace") != expected_namespace)
        ):
            raise ProtocolError("unmatched_tool_output", "工具返回结果无法与历史调用配对。")
        return {"role": "tool", "tool_call_id": call_id, "content": _tool_output_content(item.get("output"), allow_images)}
    raise ProtocolError("unsupported_input_type", f"Local 桥接不支持输入项目类型：{kind or 'missing'}。")


def _messages(body: dict, model: str, registry: ToolRegistry, allow_images: bool = True) -> list[dict]:
    messages = []
    calls: dict[str, ToolSpec] = {}
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    request_input = body.get("input")
    if isinstance(request_input, str):
        messages.append({"role": "user", "content": request_input})
    elif isinstance(request_input, list):
        pending_reasoning = None
        for item in request_input:
            if isinstance(item, dict) and item.get("type") == "web_search_call":
                registry.web_search_history_count += 1
                continue
            if isinstance(item, dict) and item.get("type") == "reasoning":
                pending_reasoning = _unpack_reasoning(item.get("encrypted_content"), model)
                if not pending_reasoning:
                    text = _reasoning_text([item])
                    pending_reasoning = {"reasoning_content": text} if text else {}
                continue
            if isinstance(item, dict) and item.get("type") in {"additional_tools", "tool_search_call", "tool_search_output"}:
                continue
            message = _item_to_message(item, registry, calls, allow_images)
            if not message:
                continue
            if pending_reasoning and message.get("role") == "assistant":
                message.update(pending_reasoning)
            pending_reasoning = None
            if message.get("role") == "assistant" and message.get("tool_calls") and messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls"):
                messages[-1]["tool_calls"].extend(message["tool_calls"])
            else:
                messages.append(message)
    messages = messages or [{"role": "user", "content": ""}]
    return messages


def _log_message_sequence(messages: list[dict]) -> None:
    sequence = []
    pending = []
    violations = []
    for index, message in enumerate(messages):
        role = message.get("role")
        calls = message.get("tool_calls") or []
        if role == "assistant" and calls:
            if pending:
                violations.append(f"idx={index} assistant while {len(pending)} tools pending")
            pending = [str(call.get("id") or "") for call in calls]
            sequence.append(f"a(TC{len(calls)})")
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if not pending:
                violations.append(f"idx={index} tool with no pending call")
            elif call_id != pending[0]:
                violations.append(f"idx={index} tool_call_id order mismatch")
            else:
                pending.pop(0)
            sequence.append("t")
        else:
            if pending:
                violations.append(f"idx={index} role={role} while {len(pending)} tools pending")
                pending = []
            sequence.append(str(role or "-")[:1])
    if pending:
        violations.append(f"EOF with {len(pending)} tools pending")
    log.info("local bridge message sequence count=%d roles=%s", len(messages), " ".join(sequence))
    if violations:
        log.warning("local bridge message sequence invalid details=%s", "; ".join(violations))
        raise ProtocolError("invalid_tool_history", "历史工具调用与返回结果的顺序不完整或不一致。")


def _input_type_counts(items: object) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items if isinstance(items, list) else []:
        item_type = str(item.get("type") or "unknown") if isinstance(item, dict) else "invalid"
        counts[item_type] = counts.get(item_type, 0) + 1
    return counts


def _has_tool_outputs(body: dict) -> bool:
    return any(
        isinstance(item, dict) and item.get("type") in {"function_call_output", "custom_tool_call_output"}
        for item in body.get("input") or []
    ) if isinstance(body.get("input"), list) else False


def _has_full_context(body: dict) -> bool:
    request_input = body.get("input")
    return isinstance(request_input, list) and any(
        isinstance(item, dict) and item.get("type") == "message" for item in request_input
    )


def _choice_reference(value: object) -> tuple[str, str, Optional[str]]:
    if not isinstance(value, dict):
        raise ProtocolError("invalid_tool_choice", "tool_choice 工具引用必须是对象。")
    kind = str(value.get("type") or "")
    nested = value.get(kind)
    name = value.get("name") or (nested.get("name") if isinstance(nested, dict) else None)
    if kind not in {"function", "custom"} or not isinstance(name, str) or not name:
        raise ProtocolError("invalid_tool_choice", "tool_choice 只能指定 function 或 custom 工具。")
    namespace = value.get("namespace") or (nested.get("namespace") if isinstance(nested, dict) else None)
    if namespace is not None and (not isinstance(namespace, str) or not namespace):
        raise ProtocolError("invalid_tool_choice", "tool_choice namespace 必须是非空字符串。")
    return kind, name, namespace


def _translate_tool_choice(
    choice: object, registry: ToolRegistry, explicit_behavior: str,
) -> tuple[list[ToolSpec], Optional[object]]:
    specs = list(registry.specs)
    if choice is None:
        return specs, None
    if isinstance(choice, str) and choice in {"auto", "none", "required"}:
        if choice == "required" and not specs:
            if registry.web_search_count:
                registry.web_search_choice_count += 1
                return specs, None
            raise ProtocolError("invalid_tool_choice", "没有可用工具时不能使用 required。")
        return specs, choice
    if not isinstance(choice, dict):
        raise ProtocolError("invalid_tool_choice", "tool_choice 结构无效。")
    if choice.get("type") == "web_search":
        registry.web_search_choice_count += 1
        return specs, None
    if choice.get("type") == "allowed_tools":
        allowed = choice.get("tools")
        mode = choice.get("mode")
        if not isinstance(allowed, list) or mode not in {"auto", "required"}:
            raise ProtocolError("invalid_tool_choice", "allowed_tools 必须包含 tools 和 auto/required mode。")
        if not allowed:
            raise ProtocolError("invalid_tool_choice", "allowed_tools 不能为空。")
        selected = []
        for value in allowed:
            if isinstance(value, dict) and value.get("type") == "web_search":
                registry.web_search_choice_count += 1
                continue
            kind, name, namespace = _choice_reference(value)
            spec = registry.original(kind, name, namespace)
            if not spec:
                raise ProtocolError("unknown_tool_choice", f"tool_choice 指定了未注册工具：{name}。")
            if spec not in selected:
                selected.append(spec)
        if not selected:
            return selected, None
        return selected, mode
    kind, name, namespace = _choice_reference(choice)
    spec = registry.original(kind, name, namespace)
    if not spec:
        raise ProtocolError("unknown_tool_choice", f"tool_choice 指定了未注册工具：{name}。")
    if explicit_behavior == "required_single":
        return [spec], "required"
    return specs, {"type": "function", "function": {"name": spec.upstream_name}}


def _chat_diagnostics(chat: dict) -> dict:
    images = 0
    tool_images = 0
    for message in chat.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        count = sum(1 for part in content if isinstance(part, dict) and part.get("type") == "image_url")
        images += count
        if message.get("role") == "tool":
            tool_images += count
    return {
        "messages": len(chat.get("messages") or []),
        "tools": len(chat.get("tools") or []),
        "images": images,
        "tool_images": tool_images,
    }


def build_chat_request(body: dict, model_record: dict, registry: Optional[ToolRegistry] = None) -> dict:
    client_model = model_record["slug"]
    upstream_model = model_record.get("upstream_model", client_model)
    registry = registry or ToolRegistry.from_body(body)
    # 缺省（含老清单）按支持图片处理，界面关闭「图片输入」后才会走文字占位。
    allow_images = "image" in (model_record.get("input_modalities") or ["text", "image"])
    messages = _messages(body, client_model, registry, allow_images)
    first_non_system = next(
        (index for index, message in enumerate(messages) if message.get("role") != "system"),
        len(messages),
    )
    messages.insert(first_non_system, {"role": "system", "content": LOCAL_REASONING_LANGUAGE_INSTRUCTION})
    _log_message_sequence(messages)
    chat = {"model": upstream_model, "messages": messages, "stream": bool(body.get("stream"))}
    if chat["stream"]:
        chat["stream_options"] = {"include_usage": True}
    if isinstance(body.get("max_output_tokens"), int) and body["max_output_tokens"] > 0:
        chat["max_tokens"] = body["max_output_tokens"]
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        chat["reasoning_effort"] = reasoning["effort"]
    options = model_record.get("chat_options") or {}
    selected, choice = _translate_tool_choice(body.get("tool_choice"), registry, str(options.get("explicit_tool_choice") or "native"))
    tools = registry.chat_tools(selected)
    if tools:
        chat["tools"] = tools
        if choice is not None:
            chat["tool_choice"] = choice
        if isinstance(body.get("parallel_tool_calls"), bool):
            chat["parallel_tool_calls"] = body["parallel_tool_calls"]
    elif choice == "none":
        chat["tool_choice"] = "none"
    for name in ("temperature", "top_p"):
        if name in options:
            chat[name] = options[name]
    return chat


def _sse(event: str, payload: dict) -> bytes:
    return ("event: " + event + "\ndata: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode()


class SSEBuffer:
    def __init__(self):
        self.buffer = ""
        self.event = ""
        self.data = []

    def feed(self, chunk: bytes) -> list[dict]:
        self.buffer += chunk.decode("utf-8", "replace")
        events = []
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            line = line[:-1] if line.endswith("\r") else line
            if not line:
                if self.event or self.data:
                    events.append({"event": self.event, "data": "\n".join(self.data)})
                self.event, self.data = "", []
            elif line.startswith("event:"):
                self.event = line[6:].strip()
            elif line.startswith("data:"):
                self.data.append(line[5:].lstrip())
        return events

    def finish(self) -> list[dict]:
        events = self.feed(b"\n")
        if self.event or self.data:
            events.append({"event": self.event, "data": "\n".join(self.data)})
            self.event, self.data = "", []
        return events


def _response_template(response_id: str, model: str, created: Optional[int] = None) -> dict:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created or int(time.time()),
        "status": "in_progress",
        "background": False,
        "completed_at": None,
        "content_filters": None,
        "model": model,
        "output": [],
        "error": None,
        "frequency_penalty": 0.0,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "max_tool_calls": None,
        "moderation": None,
        "parallel_tool_calls": True,
        "presence_penalty": 0.0,
        "previous_response_id": None,
        "prompt_cache_key": None,
        "prompt_cache_retention": None,
        "reasoning": {"effort": None, "summary": None},
        "safety_identifier": None,
        "service_tier": "default",
        "store": False,
        "temperature": 1.0,
        "text": {"format": {"type": "text"}, "verbosity": None},
        "tool_choice": "auto",
        "tools": [],
        "top_logprobs": 0,
        "top_p": 1.0,
        "truncation": "disabled",
        "usage": None,
        "user": None,
        "metadata": {},
    }


def _response_usage(usage: object, reasoning_text: str = "") -> dict:
    usage = usage if isinstance(usage, dict) else {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    return {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": _estimate_tokens(reasoning_text)},
        "total_tokens": int(usage.get("total_tokens") or prompt + completion),
    }


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    return max(1, cjk + (len(text) - cjk + 3) // 4)


def _sum_usage(*values: dict) -> dict:
    result = _response_usage({})
    for value in values:
        result["input_tokens"] += int(value.get("input_tokens") or 0)
        result["output_tokens"] += int(value.get("output_tokens") or 0)
        result["total_tokens"] += int(value.get("total_tokens") or 0)
        result["input_tokens_details"]["cached_tokens"] += int((value.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        result["output_tokens_details"]["reasoning_tokens"] += int((value.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)
    return result


class ChatStreamBridge:
    def __init__(
        self, model: str, registry: Optional[ToolRegistry] = None, response_id: Optional[str] = None, sequence_start: int = 0,
        output_index_start: int = 0, emit_lifecycle: bool = True, created: Optional[int] = None,
    ):
        self.model = model
        self.registry = registry or ToolRegistry()
        self.response_id = response_id or "resp_" + uuid.uuid4().hex
        self.created = created or int(time.time())
        self.sequence = sequence_start
        self.next_output_index = output_index_start
        self.emit_lifecycle = emit_lifecycle
        self.output: list[dict] = []
        self.usage = _response_usage({})
        self.finish_reason = None
        self.failure: Optional[dict[str, str]] = None
        self.metrics = {
            "events": 0, "content_chars": 0, "reasoning_chars": 0,
            "tool_calls": 0, "tool_downgrades": 0, "done": False,
        }

    @property
    def actionable(self) -> bool:
        return self.metrics["content_chars"] > 0 or self.metrics["tool_calls"] > 0

    @property
    def has_reasoning(self) -> bool:
        return any(item.get("type") == "reasoning" for item in self.output)

    def _event(self, event_type: str, **fields) -> dict:
        payload = {"type": event_type, **fields, "sequence_number": self.sequence}
        self.sequence += 1
        return payload

    def fail(self, code: str, message: str) -> None:
        if not self.failure:
            self.failure = {"code": code, "message": message}

    def final_response(self, output: Optional[list[dict]] = None, usage: Optional[dict] = None, failed: bool = False) -> dict:
        response = _response_template(self.response_id, self.model, self.created)
        response["completed_at"] = int(time.time())
        response["output"] = list(self.output if output is None else output)
        response["usage"] = self.usage if usage is None else usage
        if failed or self.failure:
            response["status"] = "failed"
            failure = self.failure or {
                "code": "server_error",
                "message": "Local 模型连续两次只返回思考，未产生正文或工具调用。",
            }
            response["error"] = {"code": failure["code"], "message": failure["message"]}
        elif self.finish_reason == "length":
            response["status"] = "incomplete"
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        else:
            response["status"] = "completed"
        return response

    def events(self, chunks: Iterable[bytes]):
        if self.emit_lifecycle:
            base = _response_template(self.response_id, self.model, self.created)
            yield self._event("response.created", response=dict(base))
            yield self._event("response.in_progress", response=dict(base))
        items = {}
        reasoning = None
        message = None
        tools = {}
        upstream_usage = {}

        def open_reasoning():
            nonlocal reasoning
            if reasoning is not None:
                return []
            index = self.next_output_index
            self.next_output_index += 1
            reasoning = {"id": "rs_" + uuid.uuid4().hex[:12], "output_index": index, "text": "", "fields": {}}
            return [
                self._event("response.output_item.added", output_index=index, item={"id": reasoning["id"], "type": "reasoning", "status": "in_progress", "summary": [], "content": []}),
                self._event("response.reasoning_summary_part.added", item_id=reasoning["id"], output_index=index, summary_index=0, part={"type": "summary_text", "text": ""}),
            ]

        def close_reasoning():
            nonlocal reasoning
            if reasoning is None:
                return []
            state, reasoning = reasoning, None
            text = state["text"]
            item = {"id": state["id"], "type": "reasoning", "status": "completed", "summary": ([{"type": "summary_text", "text": text}] if text else []), "content": []}
            carrier = _pack_reasoning(self.model, state["fields"])
            if carrier:
                item["encrypted_content"] = carrier
            items[state["output_index"]] = item
            return [
                self._event("response.reasoning_summary_text.done", item_id=state["id"], output_index=state["output_index"], summary_index=0, text=text),
                self._event("response.reasoning_summary_part.done", item_id=state["id"], output_index=state["output_index"], summary_index=0, part={"type": "summary_text", "text": text}),
                self._event("response.output_item.done", output_index=state["output_index"], item=item),
            ]

        def open_message():
            nonlocal message
            if message is not None:
                return []
            index = self.next_output_index
            self.next_output_index += 1
            message = {"id": "msg_" + uuid.uuid4().hex[:12], "output_index": index, "text": ""}
            return [
                self._event("response.output_item.added", output_index=index, item={"id": message["id"], "type": "message", "status": "in_progress", "role": "assistant", "content": []}),
                self._event("response.content_part.added", item_id=message["id"], output_index=index, content_index=0, part={"type": "output_text", "text": "", "annotations": []}),
            ]

        def close_message():
            nonlocal message
            if message is None:
                return []
            state, message = message, None
            part = {"type": "output_text", "text": state["text"], "annotations": []}
            item = {"id": state["id"], "type": "message", "status": "completed", "role": "assistant", "content": [part]}
            items[state["output_index"]] = item
            return [
                self._event("response.output_text.done", item_id=state["id"], output_index=state["output_index"], content_index=0, text=state["text"]),
                self._event("response.content_part.done", item_id=state["id"], output_index=state["output_index"], content_index=0, part=part),
                self._event("response.output_item.done", output_index=state["output_index"], item=item),
            ]

        def process_frame(frame):
            nonlocal upstream_usage
            data = frame["data"]
            if not data or data == "[DONE]":
                self.metrics["done"] = self.metrics["done"] or data == "[DONE]"
                return
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                log.warning("local bridge ignored invalid JSON event bytes=%d", len(data.encode("utf-8")))
                return
            self.metrics["events"] += 1
            if isinstance(obj.get("usage"), dict):
                upstream_usage = obj["usage"]
            choices = obj.get("choices") or []
            if not choices:
                source = obj.get("error") if isinstance(obj.get("error"), dict) else obj
                if isinstance(source, dict) and any(source.get(key) is not None for key in ("code", "type", "message")):
                    rejection = {
                        key: str(source[key])[:200]
                        for key in ("code", "type") if source.get(key) is not None
                    }
                    self.fail("server_error", "Local 上游拒绝了请求。")
                    log.warning("local bridge upstream rejection model=%s fields=%s", self.model, rejection or {"present": True})
                return
            choice = choices[0] or {}
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
                if self.finish_reason == "content_filter":
                    self.fail("content_filter", "Local 上游拒绝了请求。")
            delta = choice.get("delta") or {}
            for field in ("reasoning", "reasoning_content"):
                value = delta.get(field)
                if isinstance(value, str) and value:
                    for event in close_message() + open_reasoning():
                        yield event
                    reasoning["text"] += value
                    reasoning["fields"][field] = str(reasoning["fields"].get(field) or "") + value
                    self.metrics["reasoning_chars"] += len(value)
                    yield self._event("response.reasoning_summary_text.delta", item_id=reasoning["id"], output_index=reasoning["output_index"], summary_index=0, delta=value)
            if delta.get("reasoning_details") not in (None, "", [], {}):
                for event in close_message() + open_reasoning():
                    yield event
                reasoning["fields"]["reasoning_details"] = _merge_reasoning_details(reasoning["fields"].get("reasoning_details"), delta["reasoning_details"])
            text = delta.get("content")
            if isinstance(text, str) and text:
                for event in close_reasoning() + open_message():
                    yield event
                message["text"] += text
                self.metrics["content_chars"] += len(text)
                yield self._event("response.output_text.delta", item_id=message["id"], output_index=message["output_index"], content_index=0, delta=text)
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                for event in close_reasoning() + close_message():
                    yield event
                index = int(call.get("index") or 0)
                function = call.get("function") or call.get("custom") or {}
                state = tools.get(index)
                if state is None:
                    state = {
                        "call_id": str(call.get("id") or "call_" + uuid.uuid4().hex[:12]),
                        "name": "", "arguments": "", "output_index": self.next_output_index,
                    }
                    self.next_output_index += 1
                    tools[index] = state
                if call.get("id"):
                    state["call_id"] = str(call["id"])
                if function.get("name"):
                    name_part = str(function["name"])
                    if not state["name"]:
                        state["name"] = name_part
                    elif name_part.startswith(state["name"]):
                        state["name"] = name_part
                    elif name_part != state["name"]:
                        state["name"] += name_part
                arguments = function.get("arguments") or ""
                if call.get("custom") and not arguments:
                    arguments = function.get("input") or ""
                if arguments:
                    state["arguments"] += str(arguments)

        parser = SSEBuffer()
        for chunk in chunks:
            for frame in parser.feed(chunk):
                yield from process_frame(frame)
        for frame in parser.finish():
            yield from process_frame(frame)
        for event in close_reasoning() + close_message():
            yield event
        converted = []
        downgraded = []
        for index in sorted(tools):
            state = tools[index]
            try:
                spec, value = self.registry.decode_call(state["name"], state["arguments"])
            except ProtocolError as exc:
                # 纠偏：模型写错工具名时先按归一化名称唯一匹配；命中后重新校验参数。
                recovered = self.registry.recover_call(state["name"])
                value = None
                if recovered is not None:
                    try:
                        value = self.registry.decode_arguments(recovered, state["arguments"])
                    except ProtocolError as retry_exc:
                        exc, recovered = retry_exc, None
                if recovered is None:
                    # 纠偏失败不再中断整轮响应，只把这一条调用降级成说明文本。
                    downgraded.append((state, exc))
                    log.warning(
                        "local bridge downgraded tool call model=%s code=%s tool=%s arguments_chars=%d",
                        self.model, exc.code, state["name"] or "-", len(state["arguments"]),
                    )
                    continue
                spec = recovered
            converted.append((state, spec, value))
        for state, spec, value in converted:
            item_id = ("ctc_" if spec.kind == "custom" else "fc_") + uuid.uuid4().hex[:12]
            namespace = ".".join(spec.namespace) or None
            if spec.kind == "custom":
                pending = {"id": item_id, "type": "custom_tool_call", "status": "in_progress", "call_id": state["call_id"], "name": spec.name, "input": ""}
                if namespace:
                    pending["namespace"] = namespace
                item = {**pending, "status": "completed", "input": value}
                yield self._event("response.output_item.added", output_index=state["output_index"], item=pending)
                if value:
                    yield self._event("response.custom_tool_call_input.delta", item_id=item_id, output_index=state["output_index"], delta=value)
                yield self._event("response.custom_tool_call_input.done", item_id=item_id, output_index=state["output_index"], input=value)
            else:
                pending = {"id": item_id, "type": "function_call", "status": "in_progress", "call_id": state["call_id"], "name": spec.name, "arguments": ""}
                if namespace:
                    pending["namespace"] = namespace
                item = {**pending, "status": "completed", "arguments": value}
                yield self._event("response.output_item.added", output_index=state["output_index"], item=pending)
                if value:
                    yield self._event("response.function_call_arguments.delta", item_id=item_id, output_index=state["output_index"], delta=value)
                yield self._event("response.function_call_arguments.done", item_id=item_id, output_index=state["output_index"], arguments=value)
            items[state["output_index"]] = item
            self.metrics["tool_calls"] += 1
            yield self._event("response.output_item.done", output_index=state["output_index"], item=item)
        if downgraded:
            # 说明文本与模型本轮正文一样放在 message 项里，保证 Codex 不会拿到空回复。
            notes = "\n".join(
                f"本地模型 {self.model} 返回了无法识别的工具调用「{state['name'] or '未命名'}」，本轮已跳过；原因：{exc}"
                for state, exc in downgraded
            )
            for event in open_message():
                yield event
            message["text"] = notes
            self.metrics["content_chars"] += len(notes)
            self.metrics["tool_downgrades"] += len(downgraded)
            yield self._event("response.output_text.delta", item_id=message["id"], output_index=message["output_index"], content_index=0, delta=notes)
            for event in close_message():
                yield event
        self.output = [items[index] for index in sorted(items)]
        reasoning_text = "".join(
            part.get("text", "") for item in self.output if item.get("type") == "reasoning" for part in item.get("summary") or []
        )
        self.usage = _response_usage(upstream_usage, reasoning_text)
        log.info(
            "local bridge stream model=%s events=%d finish=%s content_chars=%d reasoning_chars=%d tools=%d downgrades=%d done=%s",
            self.model, self.metrics["events"], self.finish_reason or "-", self.metrics["content_chars"],
            self.metrics["reasoning_chars"], self.metrics["tool_calls"], self.metrics["tool_downgrades"], self.metrics["done"],
        )


def chat_stream_to_responses(chunks: Iterable[bytes], model: str, registry: Optional[ToolRegistry] = None) -> Iterable[bytes]:
    bridge = ChatStreamBridge(model, registry)
    for payload in bridge.events(chunks):
        yield _sse(payload["type"], payload)
    event_type, final = _finish_bridge(bridge, list(bridge.output), bridge.usage)
    payload = bridge._event(event_type, response=final)
    yield _sse(event_type, payload)


def _nonstream_bridge(
    payload: bytes, model: str, registry: Optional[ToolRegistry] = None,
    response_id: Optional[str] = None, output_index_start: int = 0, created: Optional[int] = None,
) -> ChatStreamBridge:
    obj = json.loads(payload or b"{}")
    choice = (obj.get("choices") or [{}])[0] if isinstance(obj, dict) else {}
    message = choice.get("message") or {}
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        # 完整调用按数组位置编号，避免被流式解析器当成同一条调用的分片拼接。
        message["tool_calls"] = [
            {**call, "index": index} if isinstance(call, dict) else call
            for index, call in enumerate(tool_calls)
        ]
        log.info("非流式工具调用已独立编号 model=%s count=%d", model, len(tool_calls))
    frame = {
        "choices": [{"delta": message, "finish_reason": choice.get("finish_reason")}],
        "usage": obj.get("usage") if isinstance(obj, dict) else {},
    }
    if isinstance(obj, dict) and obj.get("error"):
        frame = {"error": obj["error"], "choices": [], "usage": obj.get("usage")}
    bridge = ChatStreamBridge(
        model, registry, response_id=response_id, output_index_start=output_index_start,
        emit_lifecycle=False, created=created or (obj.get("created") if isinstance(obj, dict) else None),
    )
    encoded = ("data: " + json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n\ndata: [DONE]\n\n").encode()
    list(bridge.events([encoded]))
    if not (obj.get("choices") if isinstance(obj, dict) else None) and not bridge.failure:
        bridge.fail("server_error", "Local 上游拒绝了请求。")
    return bridge


def chat_response_to_responses(payload: bytes, model: str, registry: Optional[ToolRegistry] = None) -> bytes:
    bridge = _nonstream_bridge(payload, model, registry)
    _event_type, result = _finish_bridge(bridge, list(bridge.output), bridge.usage)
    return json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()


def _clean_headers(headers: dict[str, str], local: bool) -> dict[str, str]:
    if local:
        canonical = {"accept": "Accept", "content-type": "Content-Type", "user-agent": "User-Agent"}
        return {
            canonical[key.lower()]: value
            for key, value in headers.items()
            if key.lower() in canonical
        }
    result = {}
    for key, value in headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP or lower == "accept-encoding":
            continue
        result[key] = value
    return result


def _official_url(path: str) -> str:
    parsed = urlsplit(path)
    suffix = parsed.path[3:] if parsed.path == "/v1" or parsed.path.startswith("/v1/") else parsed.path
    return OFFICIAL_BASE_URL.rstrip("/") + "/" + suffix.lstrip("/") + (("?" + parsed.query) if parsed.query else "")


def _credential_kind(headers: dict[str, str]) -> str:
    """只给日志用：官方请求靠哪种登录态转发，绝不输出凭据内容。"""
    lowered = {key.lower() for key in headers}
    if "authorization" in lowered:
        return "authorization"
    if lowered & {"cookie", "chatgpt-account-id"}:
        return "cookie"
    return "none"


def _codex_header_summary(headers: dict[str, str]) -> str:
    """日志用：列出 x-codex-* 头名（不含值），有 thread_source 时附上短值。"""
    names = sorted({key.lower() for key in headers if key.lower().startswith("x-codex-")})
    summary = ",".join(names) or "-"
    source = _thread_source(headers)
    return summary if not source else f"{summary};thread_source={source}"


def _thread_source(headers: dict[str, str]) -> str:
    value = next((item for key, item in headers.items() if key.lower() == "x-codex-turn-metadata"), "")
    if not value or len(value) > 4096:
        return ""
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return ""
    source = data.get("thread_source") if isinstance(data, dict) else None
    if not isinstance(source, str):
        return ""
    return re.sub(r"[^A-Za-z0-9_.:-]", "", source)[:64]


def _title_metadata(body_obj: dict, headers: dict[str, str]) -> dict:
    raw = next((value for key, value in headers.items() if key.lower() == "x-codex-turn-metadata"), "")
    if isinstance(raw, str) and len(raw) <= 4096:
        try:
            value = json.loads(raw)
            if isinstance(value, dict) and value.get("thread_source") == "thread_title":
                return value
        except ValueError:
            pass
    client = body_obj.get("client_metadata")
    if isinstance(client, dict):
        value = client.get("x-codex-turn-metadata")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = None
        if isinstance(value, dict) and value.get("thread_source") == "thread_title":
            return {**client, **value}
        if client.get("thread_source") == "thread_title":
            return client
    return {}


def _is_title_request(request: PreparedRequest, body_obj: dict) -> bool:
    if request.method != "POST" or urlsplit(request.path).path != "/v1/responses":
        return False
    if _title_metadata(body_obj, request.headers):
        return True
    # Older Codex builds omit the source marker. The structured title schema is
    # specific enough to recognize without treating ordinary JSON tasks as titles.
    text_options = body_obj.get("text")
    fmt = text_options.get("format") if isinstance(text_options, dict) else None
    schema = fmt.get("schema") if isinstance(fmt, dict) else None
    required = schema.get("required") if isinstance(schema, dict) else None
    return bool(
        isinstance(fmt, dict) and fmt.get("type") == "json_schema"
        and isinstance(required, list) and {"title", "description"}.issubset(required)
        and "User prompt:" in str(body_obj.get("input") or "")
    )


def _title_session_id(body_obj: dict, headers: dict[str, str]) -> str:
    client = body_obj.get("client_metadata")
    candidates = [_title_metadata(body_obj, headers), client if isinstance(client, dict) else {}]
    for data in candidates:
        for field in ("session_id", "thread_id", "conversation_id"):
            value = data.get(field)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
                return value
    return ""


def _title_call_ids(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    return [
        str(item["call_id"])
        for item in items if isinstance(item, dict) and item.get("type") in {
            "function_call", "custom_tool_call", "tool_search_call",
            "function_call_output", "custom_tool_call_output", "tool_search_call_output",
        } and isinstance(item.get("call_id"), str) and item["call_id"]
    ]


def _valid_turn_id(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        return ""
    try:
        normalized = str(uuid.UUID(value))
        return normalized if normalized == value.lower() else ""
    except ValueError:
        return ""


def _codex_turn_metadata(body_obj: dict, headers: dict[str, str]) -> dict:
    client = body_obj.get("client_metadata")
    client = client if isinstance(client, dict) else {}

    def decoded(value: object) -> dict:
        if isinstance(value, dict):
            return value
        if not isinstance(value, str) or not value or len(value) > 8192:
            return {}
        try:
            result = json.loads(value)
        except ValueError:
            return {}
        return result if isinstance(result, dict) else {}

    embedded = decoded(client.get("x-codex-turn-metadata"))
    header = decoded(next((value for key, value in headers.items() if key.lower() == "x-codex-turn-metadata"), ""))
    return {**client, **embedded, **header}


def _request_thread_ids(body_obj: dict, headers: dict[str, str]) -> list[str]:
    metadata = _codex_turn_metadata(body_obj, headers)
    result = []
    for field in ("thread_id", "session_id"):
        value = _valid_turn_id(metadata.get(field))
        if value and value not in result:
            result.append(value)
    return result


def _request_thread_source(body_obj: dict, headers: dict[str, str]) -> str:
    source = _codex_turn_metadata(body_obj, headers).get("thread_source")
    if not isinstance(source, str):
        return ""
    return re.sub(r"[^A-Za-z0-9_.:-]", "", source)[:64]


def _parent_thread_id(body_obj: dict, headers: dict[str, str]) -> str:
    header_value = next((value for key, value in headers.items() if key.lower() == "x-codex-parent-thread-id"), "")
    return _valid_turn_id(header_value) or _valid_turn_id(_codex_turn_metadata(body_obj, headers).get("parent_thread_id"))


def _request_turn_id(body_obj: dict, headers: dict[str, str]) -> str:
    client_metadata = body_obj.get("client_metadata")
    body_id = _valid_turn_id(client_metadata.get("turn_id")) if isinstance(client_metadata, dict) else ""
    raw_metadata = next((value for key, value in headers.items() if key.lower() == "x-codex-turn-metadata"), "")
    header_id = ""
    if raw_metadata and len(raw_metadata) <= 8192:
        try:
            metadata = json.loads(raw_metadata)
            if isinstance(metadata, dict):
                header_id = _valid_turn_id(metadata.get("turn_id"))
        except (TypeError, ValueError):
            pass
    return "" if body_id and header_id and body_id != header_id else body_id or header_id


def _image_turn_id(headers: dict[str, str]) -> str:
    value = next((item for key, item in headers.items() if key.lower() == "x-codex-image-turn-id"), "")
    return _valid_turn_id(value)


def official_priority_models(path: Path) -> list:
    """读 keys.json，返回被标记为 official（走个人账号）的模型名，失败返回空列表。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    priority = data.get("model_priority") if isinstance(data, dict) else None
    if not isinstance(priority, dict):
        return []
    return sorted(name for name, target in priority.items() if target == "official" and isinstance(name, str))


def _json_error(status: int, message: str, code: str = "invalid_request_error") -> UpstreamResponse:
    error_type = "invalid_request_error" if status < 500 else "server_error"
    payload = json.dumps(
        {"error": {"message": message, "type": error_type, "code": code, "param": None}},
        ensure_ascii=False, separators=(",", ":"),
    ).encode()
    return UpstreamResponse(status, {"content-type": "application/json; charset=utf-8"}, payload)


def _reasoning_text(items: list[dict]) -> str:
    return "".join(
        str(part.get("text") or "")
        for item in items if item.get("type") == "reasoning"
        for part in item.get("summary") or [] if isinstance(part, dict)
    )


def _reasoning_fields(items: list[dict], model: str) -> dict:
    fields = {}
    for item in items:
        if item.get("type") != "reasoning":
            continue
        restored = _unpack_reasoning(item.get("encrypted_content"), model)
        for key, value in restored.items():
            if key == "reasoning_details":
                fields[key] = _merge_reasoning_details(fields.get(key), value)
            elif isinstance(value, str):
                fields[key] = str(fields.get(key) or "") + value
            else:
                fields[key] = value
    text = _reasoning_text(items)
    if text and not fields.get("reasoning_content") and not fields.get("reasoning"):
        fields["reasoning_content"] = text
    return fields


def _retry_chat(chat: dict, items: list[dict], model: str) -> dict:
    result = copy.deepcopy(chat)
    prior_reasoning = {"role": "assistant", "content": None}
    prior_reasoning.update(_reasoning_fields(items, model))
    result["messages"].append(prior_reasoning)
    result["messages"].append({"role": "user", "content": RECOVERY_MESSAGE})
    return result


def _finish_bridge(bridge: ChatStreamBridge, output: list[dict], usage: dict) -> tuple[str, dict]:
    if bridge.failure:
        response = bridge.final_response(output=output, usage=usage, failed=True)
        return "response.failed", response
    if bridge.finish_reason == "length":
        response = bridge.final_response(output=output, usage=usage)
        return "response.incomplete", response
    if not bridge.actionable:
        bridge.fail("server_error", "Local 模型未产生正文或工具调用。")
        response = bridge.final_response(output=output, usage=usage, failed=True)
        return "response.failed", response
    response = bridge.final_response(output=output, usage=usage)
    return "response.completed", response


def _local_stream_body(
    first_response: UpstreamResponse, chat: dict, model: str, registry: ToolRegistry,
    request_upstream, has_tool_outputs: bool,
) -> Iterable[bytes]:
    started = time.monotonic()
    first = ChatStreamBridge(model, registry)
    try:
        for payload in first.events(first_response.body):
            yield _sse(payload["type"], payload)
    except (OSError, TimeoutError, http.client.HTTPException, ValueError) as exc:
        first.fail("server_error", "Local 上游流式响应中断。")
        log.error("local bridge stream interrupted model=%s kind=%s", model, type(exc).__name__)
    output = list(first.output)
    usage = first.usage
    terminal = first
    retried = False
    if (
        not first.failure and not first.actionable and first.has_reasoning
        and first.finish_reason != "length" and not has_tool_outputs
    ):
        retried = True
        try:
            retry_response = request_upstream(_retry_chat(chat, first.output, model))
            if retry_response.status >= 400:
                first.fail("server_error", f"Local 上游重试返回 HTTP {retry_response.status}。")
            else:
                second = ChatStreamBridge(
                    model, registry, response_id=first.response_id, sequence_start=first.sequence,
                    output_index_start=first.next_output_index, emit_lifecycle=False, created=first.created,
                )
                try:
                    for payload in second.events(retry_response.body):
                        yield _sse(payload["type"], payload)
                except (OSError, TimeoutError, http.client.HTTPException, ValueError) as exc:
                    second.fail("server_error", "Local 上游重试响应中断。")
                    log.error("local bridge retry interrupted model=%s kind=%s", model, type(exc).__name__)
                output.extend(second.output)
                usage = _sum_usage(first.usage, second.usage)
                terminal = second
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            first.fail("server_error", "Local 上游重试请求失败。")
            log.error("local bridge retry request failed model=%s kind=%s", model, type(exc).__name__)
    event_type, final = _finish_bridge(terminal, output, usage)
    payload = terminal._event(event_type, response=final)
    yield _sse(event_type, payload)
    log.info(
        "local bridge terminal model=%s status=%s retried=%s tool_output_history=%s content_chars=%d reasoning_chars=%d tool_calls=%d ms=%d",
        model, final["status"], retried, has_tool_outputs,
        sum(item.metrics["content_chars"] for item in ({first, terminal} if retried else {first})),
        sum(item.metrics["reasoning_chars"] for item in ({first, terminal} if retried else {first})),
        sum(item.metrics["tool_calls"] for item in ({first, terminal} if retried else {first})),
        int((time.monotonic() - started) * 1000),
    )


def _local_nonstream_body(
    first_payload: bytes, chat: dict, model: str, registry: ToolRegistry,
    request_upstream, has_tool_outputs: bool,
) -> bytes:
    first = _nonstream_bridge(first_payload, model, registry)
    output = list(first.output)
    usage = first.usage
    terminal = first
    retried = False
    if (
        not first.failure and not first.actionable and first.has_reasoning
        and first.finish_reason != "length" and not has_tool_outputs
    ):
        retried = True
        retry_response = request_upstream(_retry_chat(chat, first.output, model))
        if retry_response.status >= 400:
            first.fail("server_error", f"Local 上游重试返回 HTTP {retry_response.status}。")
        else:
            second = _nonstream_bridge(
                retry_response.body, model, registry, response_id=first.response_id,
                output_index_start=first.next_output_index, created=first.created,
            )
            output.extend(second.output)
            usage = _sum_usage(first.usage, second.usage)
            terminal = second
    _event_type, final = _finish_bridge(terminal, output, usage)
    log.info(
        "local bridge terminal model=%s status=%s retried=%s tool_output_history=%s output_items=%d",
        model, final["status"], retried, has_tool_outputs, len(output),
    )
    return json.dumps(final, ensure_ascii=False, separators=(",", ":")).encode()


def _converted_headers(headers: dict[str, str], stream: bool) -> dict[str, str]:
    result = {
        key: value for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP | {"content-encoding", "content-type"}
    }
    result["content-type"] = "text/event-stream" if stream else "application/json; charset=utf-8"
    return result


def _custom_tool_names(body_obj: dict) -> set[str]:
    """收集请求里 custom 工具的可用名称，用于纠正上游的 function_call 语法。"""
    names: set[str] = set()

    def walk(tool: object, namespace: str) -> None:
        if not isinstance(tool, dict):
            return
        kind = str(tool.get("type") or "")
        if kind == "namespace":
            children = tool.get("tools")
            if isinstance(children, list):
                prefix = namespace + str(tool.get("name") or "").strip() + "."
                for child in children:
                    walk(child, prefix)
            return
        if kind != "custom":
            return
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            return
        names.add(name.strip())
        if namespace:
            names.add(namespace + name.strip())

    tools = body_obj.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            walk(tool, "")
    request_input = body_obj.get("input")
    if isinstance(request_input, list):
        for item in request_input:
            if not isinstance(item, dict) or item.get("type") != "additional_tools":
                continue
            children = item.get("tools")
            if isinstance(children, list):
                for tool in children:
                    walk(tool, "")
    return names


def _split_imagegen_tools(tools: list, namespace: tuple[str, ...] = (), deferred: bool = False) -> tuple[list, list]:
    """从工具树取出 image_gen.imagegen，保留其余工具及原有顺序。"""
    kept = []
    found = []
    for tool in tools:
        if not isinstance(tool, dict):
            kept.append(tool)
            continue
        if tool.get("type") == "namespace" and isinstance(tool.get("tools"), list):
            children, extracted = _split_imagegen_tools(
                tool["tools"], namespace + (str(tool.get("name") or ""),),
                deferred or bool(tool.get("defer_loading")),
            )
            if extracted:
                found.extend(extracted)
                if children:
                    tool["tools"] = children
                    kept.append(tool)
            else:
                kept.append(tool)
            continue
        source = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = source.get("name")
        named = namespace == ("image_gen",) and name == "imagegen"
        qualified = not namespace and name == "image_gen.imagegen"
        explicit = not namespace and name == "imagegen" and (
            source.get("namespace") == "image_gen" or tool.get("namespace") == "image_gen"
        )
        if tool.get("type") == "function" and (named or qualified or explicit):
            origin = ("imagegen", "image_gen") if named or explicit else ("image_gen.imagegen", None)
            found.append((tool, origin, deferred))
            continue
        kept.append(tool)
    return kept, found


def _alias_imagegen_reference(value: object) -> bool:
    """把指定工具或历史调用里的图片工具名称改为上游可接受的顶层别名。"""
    if not isinstance(value, dict):
        return False
    source = value.get("function") if isinstance(value.get("function"), dict) else value
    name = source.get("name")
    namespace = source.get("namespace") or value.get("namespace")
    if not ((name == "imagegen" and namespace == "image_gen") or name == "image_gen.imagegen"):
        return False
    source["name"] = IMAGEGEN_ALIAS
    source.pop("namespace", None)
    value.pop("namespace", None)
    return True


def _alias_imagegen_request(body_obj: dict) -> tuple[Optional[tuple[str, Optional[str]]], int]:
    """保留图片工具能力，同时避开上游保留的 image_gen 命名空间。"""
    found = []
    top_tools = body_obj.get("tools")
    if isinstance(top_tools, list):
        body_obj["tools"], extracted = _split_imagegen_tools(top_tools)
        found.extend(extracted)
    request_input = body_obj.get("input")
    if isinstance(request_input, list):
        kept_input = []
        for item in request_input:
            if isinstance(item, dict) and item.get("type") == "additional_tools" and isinstance(item.get("tools"), list):
                item["tools"], extracted = _split_imagegen_tools(item["tools"])
                found.extend(extracted)
                if not item["tools"]:
                    continue
            kept_input.append(item)
        body_obj["input"] = kept_input
    if not found:
        return None, 0

    # 固定别名让下一轮的历史调用也能映射回来；遇到同名用户工具时明确拒绝。
    remaining = list(body_obj.get("tools") or [])
    for item in body_obj.get("input") or []:
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            remaining.extend(item.get("tools") or [])
    if any(isinstance(tool, dict) and tool.get("type") == "function" and tool.get("name") == IMAGEGEN_ALIAS for tool in remaining):
        raise ProtocolError("tool_alias_collision", "图片工具别名与其他工具重名。")

    original, origin, deferred = found[0]
    source = original.get("function") if isinstance(original.get("function"), dict) else original
    alias = {key: copy.deepcopy(value) for key, value in source.items() if key not in {"type", "name", "namespace"}}
    alias.update({"type": "function", "name": IMAGEGEN_ALIAS})
    if deferred or original.get("defer_loading"):
        alias["defer_loading"] = True
    body_obj.setdefault("tools", []).append(alias)

    choice = body_obj.get("tool_choice")
    if isinstance(choice, dict):
        if choice.get("type") == "allowed_tools" and isinstance(choice.get("tools"), list):
            for tool in choice["tools"]:
                _alias_imagegen_reference(tool)
        else:
            _alias_imagegen_reference(choice)
    for item in body_obj.get("input") or []:
        if isinstance(item, dict) and item.get("type") in {"function_call", "function_call_output"}:
            _alias_imagegen_reference(item)
    return origin, len(found)


def _custom_tool_input(arguments: object) -> str:
    """把 function_call 的 JSON 参数还原成 custom_tool_call 的自由文本 input。

    优先解包 Codex 的 {"input": "..."} 包装；模型把 custom 工具当普通函数、
    用 {"cmd": "..."} / {"command": "..."} 传参时也取其字符串，避免整段 JSON
    字面量被 custom 工具当成命令执行。
    """
    if not isinstance(arguments, str):
        return "" if arguments in (None, "") else json.dumps(arguments, ensure_ascii=False)
    try:
        wrapper = json.loads(arguments)
    except (TypeError, json.JSONDecodeError):
        return arguments
    if isinstance(wrapper, dict):
        for key in ("input", "cmd", "command"):
            value = wrapper.get(key)
            rest = [name for name, item in wrapper.items() if name != key and item not in (None, "", [], {})]
            if isinstance(value, str) and not rest:
                return value
    return arguments


def _rewrite_custom_tool_item(item: object, custom_names: set[str]) -> Optional[dict]:
    """上游把 custom 工具当作 function 调用时，改回 Codex 认识的 custom_tool_call。"""
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return None
    name = item.get("name")
    if not isinstance(name, str) or name.strip() not in custom_names:
        return None
    rewritten = dict(item)
    rewritten["type"] = "custom_tool_call"
    rewritten["input"] = _custom_tool_input(item.get("arguments"))
    rewritten.pop("arguments", None)
    return rewritten


def _rewrite_imagegen_tool_item(item: object, origin: Optional[tuple[str, Optional[str]]]) -> Optional[dict]:
    if not origin or not isinstance(item, dict) or item.get("type") != "function_call" or item.get("name") != IMAGEGEN_ALIAS:
        return None
    rewritten = dict(item)
    rewritten["name"] = origin[0]
    if origin[1]:
        rewritten["namespace"] = origin[1]
    else:
        rewritten.pop("namespace", None)
    return rewritten


def _rewrite_custom_output(
    response_obj: object, custom_names: set[str], imagegen_origin: Optional[tuple[str, Optional[str]]] = None,
) -> int:
    """改写 response.output 里的 custom 调用和图片工具别名；返回改写条数。"""
    if not isinstance(response_obj, dict) or not isinstance(response_obj.get("output"), list):
        return 0
    output = response_obj["output"]
    count = 0
    for index, item in enumerate(output):
        rewritten = _rewrite_imagegen_tool_item(item, imagegen_origin) or _rewrite_custom_tool_item(item, custom_names)
        if rewritten is None:
            continue
        output[index] = rewritten
        count += 1
    return count


def _sse_frame(event_name: str, data: str) -> bytes:
    prefix = f"event: {event_name}\n" if event_name else ""
    return (prefix + f"data: {data}\n\n").encode("utf-8")


def _rewrite_responses_stream(
    chunks: Iterable[bytes], custom_names: set[str], model: str, source_id: str,
    imagegen_origin: Optional[tuple[str, Optional[str]]] = None,
) -> Iterable[bytes]:
    """流式直通时还原图片工具别名，并纠正 custom 工具的 function_call 事件。

    工具参数先缓冲到 arguments.done，再一次性补发 custom_tool_call_input 事件；
    正文与其余事件仍按原始节奏透传。
    """
    parser = SSEBuffer()
    pending: dict[str, dict] = {}
    counters = {"rewrites": 0}

    def dump(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def process(frame: dict) -> list[bytes]:
        raw_event = str(frame.get("event") or "")
        raw_data = str(frame.get("data") or "")
        if not raw_data.strip() or raw_data.strip() == "[DONE]":
            return [_sse_frame(raw_event, raw_data)]
        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError:
            return [_sse_frame(raw_event, raw_data)]
        if not isinstance(payload, dict):
            return [_sse_frame(raw_event, raw_data)]
        event_type = str(payload.get("type") or raw_event)
        if event_type == "response.output_item.added":
            imagegen_item = _rewrite_imagegen_tool_item(payload.get("item"), imagegen_origin)
            if imagegen_item is not None:
                payload["item"] = imagegen_item
                counters["rewrites"] += 1
                return [_sse_frame(raw_event, dump(payload))]
            item = _rewrite_custom_tool_item(payload.get("item"), custom_names)
            if item is None:
                return [_sse_frame(raw_event, raw_data)]
            item_id = str(item.get("id") or "")
            pending[item_id] = {"item": item, "arguments": "", "output_index": payload.get("output_index")}
            payload["item"] = item
            counters["rewrites"] += 1
            log.info(
                "local responses passthrough rewrote tool event model=%s source=%s event=%s tool=%s",
                model, source_id, event_type, item.get("name"),
            )
            return [_sse_frame(raw_event, dump(payload))]
        if event_type == "response.function_call_arguments.delta":
            state = pending.get(str(payload.get("item_id") or ""))
            if state is None:
                return [_sse_frame(raw_event, raw_data)]
            delta = payload.get("delta")
            if isinstance(delta, str):
                state["arguments"] += delta
            return []
        if event_type == "response.function_call_arguments.done":
            item_id = str(payload.get("item_id") or "")
            state = pending.get(item_id)
            if state is None:
                return [_sse_frame(raw_event, raw_data)]
            arguments = payload.get("arguments")
            if isinstance(arguments, str):
                state["arguments"] = arguments
            value = _custom_tool_input(state["arguments"])
            output_index = payload.get("output_index", state.get("output_index"))
            state["item"] = {**state["item"], "status": "completed", "input": value}
            frames = []
            if value:
                frames.append(_sse_frame("response.custom_tool_call_input.delta", dump({
                    "type": "response.custom_tool_call_input.delta",
                    "item_id": item_id, "output_index": output_index, "delta": value,
                })))
            frames.append(_sse_frame("response.custom_tool_call_input.done", dump({
                "type": "response.custom_tool_call_input.done",
                "item_id": item_id, "output_index": output_index, "input": value,
            })))
            counters["rewrites"] += 1
            return frames
        if event_type == "response.output_item.done":
            item = payload.get("item")
            item_id = str(item.get("id") or "") if isinstance(item, dict) else ""
            pending.pop(item_id, None)
            rewritten = _rewrite_imagegen_tool_item(item, imagegen_origin) or _rewrite_custom_tool_item(item, custom_names)
            if rewritten is None:
                return [_sse_frame(raw_event, raw_data)]
            payload["item"] = rewritten
            counters["rewrites"] += 1
            return [_sse_frame(raw_event, dump(payload))]
        if event_type in {"response.completed", "response.in_progress", "response.incomplete", "response.failed"}:
            response_obj = payload.get("response")
            rewritten = _rewrite_custom_output(response_obj, custom_names, imagegen_origin)
            if rewritten:
                counters["rewrites"] += rewritten
                return [_sse_frame(raw_event, dump(payload))]
            return [_sse_frame(raw_event, raw_data)]
        return [_sse_frame(raw_event, raw_data)]

    try:
        for chunk in chunks:
            for frame in parser.feed(chunk):
                for output in process(frame):
                    yield output
        for frame in parser.finish():
            for output in process(frame):
                yield output
    finally:
        log.info(
            "local responses passthrough tool rewrite model=%s source=%s stream=true rewrites=%d pending=%d",
            model, source_id, counters["rewrites"], len(pending),
        )


def _rewrite_responses_body(
    body: bytes, custom_names: set[str], model: str, source_id: str,
    imagegen_origin: Optional[tuple[str, Optional[str]]] = None,
) -> bytes:
    """非流式直通时还原图片工具别名，并纠正 custom 工具调用。"""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    count = _rewrite_custom_output(payload, custom_names, imagegen_origin)
    if not count:
        return body
    log.info(
        "local responses passthrough tool rewrite model=%s source=%s stream=false rewrites=%d",
        model, source_id, count,
    )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _responses_passthrough(
    request: PreparedRequest, body_obj: dict, model_record: dict, upstream_base: str,
    api_key: str, transport, model: str, source_id: str, image_turn_routes: ImageTurnRoutes,
) -> UpstreamResponse:
    """原生 /responses 直通；仅适配图片工具保留名称和 custom 调用语法。"""
    upstream_model = str(model_record.get("upstream_model") or model)
    payload_obj = copy.deepcopy(body_obj)
    payload_obj, agent_message_count = _normalize_agent_messages(payload_obj)
    request_input = payload_obj.get("input")
    encrypted_agent_count = sum(
        isinstance(item, dict) and item.get("type") == "agent_message" for item in request_input
    ) if isinstance(request_input, list) else 0
    if agent_message_count:
        log.info(
            "local responses agent messages normalized rid=%s model=%s source=%s rewrites=%d",
            request.request_id or "-", model, source_id, agent_message_count,
        )
    imagegen_origin, imagegen_count = _alias_imagegen_request(payload_obj)
    image_turn_id = _request_turn_id(body_obj, request.headers) if imagegen_origin else ""
    if imagegen_origin and not image_turn_id:
        log.warning("local image turn rejected rid=%s model=%s source=%s reason=turn_id_missing", request.request_id or "-", model, source_id)
        return _json_error(400, "图片工具请求缺少 Codex 回合 ID，无法保证使用同一公司 Key。", "image_turn_id_missing")
    payload_obj["model"] = upstream_model
    payload = json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream = bool(body_obj.get("stream"))
    headers = _clean_headers(request.headers, local=True)
    headers["Content-Type"] = "application/json"
    headers["Authorization"] = "Bearer " + api_key
    if encrypted_agent_count:
        beta = next((value for key, value in request.headers.items() if key.lower() == "openai-beta"), "")
        headers["OpenAI-Beta"] = beta if "responses_multi_agent=v1" in {part.strip() for part in beta.split(",")} else (
            (beta + "," if beta else "") + "responses_multi_agent=v1"
        )
        log.info(
            "local responses encrypted agent messages preserved rid=%s model=%s source=%s count=%d",
            request.request_id or "-", model, source_id, encrypted_agent_count,
        )
    if imagegen_origin:
        log.info(
            "local responses imagegen alias rid=%s model=%s source=%s original=%s alias=%s definitions=%d",
            request.request_id or "-", model, source_id,
            (imagegen_origin[1] + "." if imagegen_origin[1] else "") + imagegen_origin[0],
            IMAGEGEN_ALIAS, imagegen_count,
        )
    log.info(
        "local responses passthrough rid=%s model=%s source=%s upstream_model=%s stream=%s wire=responses route=local",
        request.request_id or "-", model, source_id, upstream_model, stream,
    )
    response = transport("POST", upstream_base.rstrip("/") + "/responses", headers, payload, stream)
    if response.status >= 400:
        log.warning("local responses upstream rejected rid=%s model=%s source=%s status=%d", request.request_id or "-", model, source_id, response.status)
        return response
    if imagegen_origin:
        image_turn_routes.remember(image_turn_id, model)
        log.info("local image turn bound rid=%s model=%s source=%s wire=responses", request.request_id or "-", model, source_id)
    custom_names = _custom_tool_names(body_obj)
    if custom_names or imagegen_origin:
        if stream:
            response = UpstreamResponse(
                response.status, response.headers,
                _rewrite_responses_stream(response.body, custom_names, model, source_id, imagegen_origin), response.reason,
            )
        else:
            rewritten = _rewrite_responses_body(response.body, custom_names, model, source_id, imagegen_origin)
            if rewritten is not response.body:
                response = UpstreamResponse(response.status, response.headers, rewritten, response.reason)
    log.info("local responses accepted model=%s source=%s status=%d stream=%s", model, source_id, response.status, stream)
    return response


def _route_local_image(
    request: PreparedRequest, body_obj: dict, manifest: dict, local_key: Optional[str],
    transport, image_turn_routes: ImageTurnRoutes,
    expected_source: Optional[str] = None,
) -> Optional[UpstreamResponse]:
    if request.method != "POST" or urlsplit(request.path).path != "/v1/images/generations":
        return None
    turn_id = _image_turn_id(request.headers)
    source_model = image_turn_routes.model_for(turn_id) if turn_id else None
    if not source_model:
        return None
    record = _model_record(manifest, source_model)
    if not record:
        return _json_error(503, "图片请求对应的本地模型已不可用。", "image_source_unavailable")
    if expected_source and expected_source != _source_fingerprint(manifest, record, request.headers, local_key):
        raise ProtocolError("subagent_image_source_changed", "子代理图片回合与父会话来源不一致，已阻止跨 Key 请求。")
    upstream_base, source_id, source_key = _model_source(manifest, record)
    api_key = source_key or local_key
    if not upstream_base or not api_key:
        log.error("local image route blocked rid=%s source_model=%s source=%s reason=source_unavailable", request.request_id or "-", source_model, source_id)
        return _json_error(503, "图片请求对应的公司 Key 已不可用。", "image_source_unavailable")
    headers = _clean_headers(request.headers, local=True)
    headers["Content-Type"] = "application/json"
    headers["Authorization"] = "Bearer " + api_key
    payload = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    log.info(
        "local image request rid=%s model=%s source_model=%s source=%s wire=images route=local",
        request.request_id or "-", body_obj.get("model") or "-", source_model, source_id,
    )
    response = transport("POST", upstream_base.rstrip("/") + "/images/generations", headers, payload, False)
    log.info("local image response rid=%s source_model=%s source=%s status=%d", request.request_id or "-", source_model, source_id, response.status)
    return response


def _title_candidates(manifest: dict, local_key: Optional[str]) -> list[tuple[dict, str, str, str]]:
    sources = manifest.get("sources") or {}
    source_order = {source_id: index for index, source_id in enumerate(sources)}
    candidates = []
    for index, record in enumerate(manifest.get("models") or []):
        base, source_id, source_key = _model_source(manifest, record)
        api_key = source_key if manifest.get("version") == 2 else local_key
        if not base or not api_key or (manifest.get("version") == 2 and sources.get(source_id, {}).get("kind") != "key"):
            continue
        candidates.append((record, base, source_id, api_key, index))
    candidates.sort(key=lambda item: (
        0 if item[0].get("wire_api") == "responses" else 1,
        source_order.get(item[2], 0), -int(item[0].get("priority") or 0), item[4],
    ))
    return [(record, base, source_id, api_key) for record, base, source_id, api_key, _ in candidates]


def _chat_title_body(body_obj: dict) -> tuple[dict, int]:
    """Chat 桥接只能暴露可映射的工具；Codex 标题请求还会带内置工具。"""
    result = copy.deepcopy(body_obj)
    omitted = 0

    def keep_tools(tools: list) -> list:
        nonlocal omitted
        kept = []
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") == "namespace" and isinstance(tool.get("tools"), list):
                tool["tools"] = keep_tools(tool["tools"])
                if tool["tools"]:
                    kept.append(tool)
                else:
                    omitted += 1
            elif isinstance(tool, dict) and tool.get("type") in {"function", "custom", "tool_search"}:
                kept.append(tool)
            else:
                omitted += 1
        return kept

    if isinstance(result.get("tools"), list):
        result["tools"] = keep_tools(result["tools"])
    if isinstance(result.get("input"), list):
        for item in result["input"]:
            if isinstance(item, dict) and item.get("type") == "additional_tools" and isinstance(item.get("tools"), list):
                item["tools"] = keep_tools(item["tools"])
    return result, omitted


def _title_output_valid(response: dict) -> tuple[bool, bool, str]:
    if response.get("status") != "completed":
        return False, False, "response_not_completed"
    output = response.get("output")
    if not isinstance(output, list):
        return False, False, "output_missing"
    if any(isinstance(item, dict) and item.get("type") in {"function_call", "custom_tool_call", "tool_search_call"} for item in output):
        return True, True, "tool_call"
    content = "".join(
        str(part.get("text") or "")
        for item in output if isinstance(item, dict) and item.get("type") == "message"
        for part in item.get("content") or [] if isinstance(part, dict) and part.get("type") == "output_text"
    )
    try:
        title = json.loads(content)
    except (TypeError, ValueError):
        return False, False, "invalid_json"
    if not isinstance(title, dict) or not all(isinstance(title.get(field), str) and title[field].strip() for field in ("title", "description")):
        return False, False, "invalid_title_fields"
    if len(title["title"]) > 36 or len(title["description"]) > 100:
        return False, False, "title_too_long"
    return True, False, "title_valid"


def _capture_title_response(response: UpstreamResponse, stream: bool, deadline: float) -> tuple[UpstreamResponse, dict]:
    body = response.body
    if isinstance(body, (bytes, bytearray)):
        payload = bytes(body)
    else:
        chunks = []
        size = 0
        try:
            for chunk in body:
                if time.monotonic() >= deadline:
                    raise TimeoutError("title deadline exceeded")
                size += len(chunk)
                if size > TITLE_MAX_RESPONSE_BYTES:
                    raise ValueError("title response too large")
                chunks.append(chunk)
        finally:
            close = getattr(body, "close", None)
            if close:
                close()
        payload = b"".join(chunks)
    if len(payload) > TITLE_MAX_RESPONSE_BYTES or time.monotonic() >= deadline:
        raise TimeoutError("title deadline exceeded")
    if stream:
        parser = SSEBuffer()
        events = parser.feed(payload) + parser.finish()
        terminals = []
        for event in events:
            if event["event"] in {"response.completed", "response.failed", "response.incomplete"}:
                terminals.append(json.loads(event["data"]))
        final = terminals[-1].get("response") if terminals else None
    else:
        final = json.loads(payload)
    if not isinstance(final, dict):
        raise ValueError("title response missing terminal response")
    return UpstreamResponse(response.status, response.headers, payload, response.reason), final


def _route_title_request(
    request: PreparedRequest, body_obj: dict, manifest: dict, local_key: Optional[str],
    transport, image_turn_routes: ImageTurnRoutes,
) -> UpstreamResponse:
    deadline = time.monotonic() + TITLE_TIMEOUT_SECONDS
    session_id = _title_session_id(body_obj, request.headers)
    incoming_call_ids = _title_call_ids(body_obj.get("input"))
    bound_model = TITLE_SESSION_ROUTES.model_for(session_id) if session_id else None
    if not bound_model:
        bound_model = next((model for call_id in incoming_call_ids if (model := TITLE_CALL_ROUTES.model_for(call_id))), None)
    candidates = _title_candidates(manifest, local_key)
    if bound_model:
        candidates = [candidate for candidate in candidates if candidate[0]["slug"] == bound_model]
    log.info(
        "title route start rid=%s requested_model=%s candidates=%d bound=%s deadline_s=%d",
        request.request_id or "-", str(body_obj.get("model") or "-"), len(candidates), bool(bound_model), TITLE_TIMEOUT_SECONDS,
    )
    last_reason = "no_enabled_gateway_model"
    for record, base, source_id, api_key in candidates:
        if time.monotonic() >= deadline:
            last_reason = "deadline_exceeded"
            break
        model = record["slug"]
        wire = "responses" if record.get("wire_api") == "responses" else "chat"

        def bounded_transport(method, url, headers, body, stream):
            if time.monotonic() >= deadline:
                raise TimeoutError("title deadline exceeded")
            if transport is default_transport:
                # Title responses are small. Reading each attempt to completion
                # keeps both the timeout and failover decision inside this call.
                result = default_transport(method, url, headers, body, False, deadline=deadline)
                if stream and result.status < 400:
                    result.body = iter([result.body])
                return result
            return transport(method, url, headers, body, stream)

        log.info("title route attempt rid=%s model=%s source=%s wire=%s", request.request_id or "-", model, source_id, wire)
        try:
            if wire == "responses":
                response = _responses_passthrough(
                    request, body_obj, record, base, api_key, bounded_transport, model, source_id, image_turn_routes,
                )
            else:
                chat_body, omitted = _chat_title_body(body_obj)
                if omitted:
                    log.info("title chat tools omitted rid=%s model=%s source=%s count=%d", request.request_id or "-", model, source_id, omitted)
                registry = ToolRegistry.from_body(chat_body)
                chat = build_chat_request(chat_body, record, registry)
                chat["messages"].insert(0, {"role": "system", "content": (
                    "Return a JSON object with two nonempty string fields named title and description. "
                    "The title must be at most 36 characters and the description at most 100 characters. "
                    "Do not wrap the JSON in Markdown."
                )})
                chat["response_format"] = {"type": "json_object"}
                headers = _clean_headers(request.headers, local=True)
                headers["Content-Type"] = "application/json"
                headers["Authorization"] = "Bearer " + api_key

                def request_upstream(request_chat):
                    payload = json.dumps(request_chat, ensure_ascii=False, separators=(",", ":")).encode()
                    return bounded_transport("POST", base.rstrip("/") + "/chat/completions", headers, payload, chat["stream"])

                response = request_upstream(chat)
                if response.status in {400, 422}:
                    chat.pop("response_format")
                    log.info("title route json mode retry rid=%s model=%s source=%s status=%d", request.request_id or "-", model, source_id, response.status)
                    response = request_upstream(chat)
                if response.status < 400:
                    has_tool_outputs = _has_tool_outputs(chat_body)
                    converted = (
                        _local_stream_body(response, chat, model, registry, request_upstream, has_tool_outputs)
                        if chat["stream"] else
                        _local_nonstream_body(response.body, chat, model, registry, request_upstream, has_tool_outputs)
                    )
                    response = UpstreamResponse(response.status, _converted_headers(response.headers, chat["stream"]), converted, response.reason)
            if response.status >= 400:
                last_reason = f"upstream_http_{response.status}"
            else:
                response, final = _capture_title_response(response, bool(body_obj.get("stream")), deadline)
                valid, tool_call, last_reason = _title_output_valid(final)
                if valid:
                    for call_id in incoming_call_ids:
                        TITLE_CALL_ROUTES.forget(call_id)
                    if tool_call:
                        for call_id in _title_call_ids(final.get("output")):
                            TITLE_CALL_ROUTES.remember(call_id, model)
                    if session_id:
                        if tool_call:
                            TITLE_SESSION_ROUTES.remember(session_id, model)
                        else:
                            TITLE_SESSION_ROUTES.forget(session_id)
                    log.info("title route accepted rid=%s model=%s source=%s wire=%s result=%s", request.request_id or "-", model, source_id, wire, last_reason)
                    return response
        except (OSError, TimeoutError, http.client.HTTPException, ValueError, ProtocolError) as exc:
            last_reason = (
                "deadline_exceeded" if time.monotonic() >= deadline else
                exc.code if isinstance(exc, ProtocolError) else type(exc).__name__
            )
        log.warning("title route failed rid=%s model=%s source=%s wire=%s reason=%s", request.request_id or "-", model, source_id, wire, last_reason)
        if bound_model:
            break
    # Codex retries upstream failures. This is a terminal title result: the
    # desktop can keep its provisional title without retrying another model.
    status = 400
    log.error("title route unavailable rid=%s requested_model=%s status=%d reason=%s", request.request_id or "-", str(body_obj.get("model") or "-"), status, last_reason)
    return _json_error(status, "已启用的网关模型无法生成标题，Codex 将保留临时标题。", "title_gateway_unavailable")


def _source_fingerprint(manifest: dict, record: Optional[dict], headers: dict[str, str], local_key: Optional[str]) -> str:
    if record:
        upstream, source, key = _model_source(manifest, record)
        identity = key or local_key or ""
    else:
        upstream, source = OFFICIAL_BASE_URL, "official"
        auth = next((value for key, value in headers.items() if key.lower() == "authorization"), "")
        account = next((value for key, value in headers.items() if key.lower() == "chatgpt-account-id"), "")
        identity = auth or next((value for key, value in headers.items() if key.lower() == "cookie"), "")
        # Use stable account claims so an OAuth token refresh does not change the route.
        token = auth.removeprefix("Bearer ").split(".")
        if len(token) == 3:
            try:
                claims = json.loads(base64.urlsafe_b64decode(token[1] + "=" * (-len(token[1]) % 4)))
                details = claims.get("https://api.openai.com/auth", {})
                claimed_account = details.get("chatgpt_account_id", "")
                if isinstance(claimed_account, str):
                    account = account or claimed_account
                if account and isinstance(claims.get("sub"), str):
                    identity = claims["sub"]
            except (ValueError, TypeError, AttributeError):
                pass
        # ponytail: opaque tokens require a fresh parent request after token rotation.
        identity = account + ":" + identity
    return hashlib.sha256(json.dumps([source, upstream, identity]).encode()).hexdigest()


def _inherited_subagent_model(
    body_obj: dict, headers: dict[str, str], manifest: dict, thread_routes: ModelRoutes,
    local_key: Optional[str] = None, request_path: str = "/v1/responses",
) -> Optional[tuple[str, Optional[dict]]]:
    parent_id = _parent_thread_id(body_obj, headers)
    child_request = (
        _request_thread_source(body_obj, headers) == "subagent" or parent_id
        or any(key.lower() == "x-codex-parent-thread-id" for key in headers)
        or "parent_thread_id" in _codex_turn_metadata(body_obj, headers)
    )
    if not child_request:
        return None
    if not parent_id:
        raise ProtocolError("subagent_parent_missing", "子代理请求缺少父会话 ID，无法确认同一来源。")
    parent_model = thread_routes.model_for(parent_id)
    if not parent_model:
        raise ProtocolError("subagent_source_unbound", "父会话来源记录已丢失，请先在父会话继续一轮后再启动子代理。")
    parent_record = _model_record(manifest, parent_model)
    if parent_model.startswith("local/") and not parent_record:
        raise ProtocolError("subagent_source_unavailable", "父会话的模型来源已不可用，请重新确认父会话来源。")
    if thread_routes.fingerprint_for(parent_id) != _source_fingerprint(manifest, parent_record, headers, local_key):
        raise ProtocolError("subagent_source_changed", "父会话的 Key、上游或官方账号已变更，请先在父会话继续一轮确认来源。")
    if urlsplit(request_path).path == "/v1/images/generations":
        return parent_model, parent_record
    requested_model = str(body_obj.get("model") or "").strip()
    requested_record = _model_record(manifest, requested_model)
    upstream_model = str(requested_record.get("upstream_model") or requested_model) if requested_record else requested_model
    if not parent_record:
        if requested_model.startswith("local/"):
            official_models = manifest.get("official_models", [])
            cache_path = manifest.get("official_cache_path")
            if cache_path:
                try:
                    cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
                    current_models = cache["models"]
                    if not isinstance(current_models, list) or not current_models or any(
                        not isinstance(item, dict) or not isinstance(item.get("slug"), str)
                        or not item["slug"].strip() for item in current_models
                    ):
                        raise ValueError("invalid official cache")
                    official_models = [item["slug"] for item in current_models]
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    log.warning("subagent official catalog refresh failed kind=%s; using runtime snapshot", type(exc).__name__)
            canonical = upstream_model if upstream_model in official_models else next((
                slug for slug in official_models if _model_identity(slug) == _model_identity(upstream_model)
            ), None)
            if not requested_record or not canonical:
                raise ProtocolError("subagent_model_unavailable", "官方父会话没有该子代理模型，请选择官方来源中可用的子模型。")
            upstream_model = canonical
        return upstream_model, None
    source = str(parent_record.get("source") or "local")
    candidates = [
        item for item in manifest["models"] + manifest.get("agent_models", [])
        if str(item.get("source") or "local") == source
        and _model_identity(str(item.get("upstream_model") or item["slug"])) == _model_identity(upstream_model)
    ]
    candidate = next((item for item in candidates if str(item.get("upstream_model") or item["slug"]) == upstream_model),
                     candidates[0] if candidates else None)
    if not candidate:
        raise ProtocolError("subagent_model_unavailable", "父会话的 Key 没有该子代理模型，请勾选该 Key 下的模型并刷新；不会切换其他 Key。")
    return candidate["slug"], candidate


def route_request(
    request: PreparedRequest, manifest: dict, local_key: Optional[str], transport,
    image_turn_routes: Optional[ImageTurnRoutes] = None,
    thread_routes: Optional[ModelRoutes] = None,
) -> UpstreamResponse:
    image_turn_routes = image_turn_routes if image_turn_routes is not None else IMAGE_TURN_ROUTES
    thread_routes = thread_routes if thread_routes is not None else THREAD_MODEL_ROUTES
    body_obj = {}
    model = ""
    if request.body:
        try:
            body_obj = _decoded_json(request.body, request.headers)
            model = str(body_obj.get("model") or "").strip()
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            return _json_error(400, "无法解析请求正文，请检查 JSON 或压缩格式。")
    model_record = _model_record(manifest, model)
    routed_body_changed = False
    try:
        inherited = _inherited_subagent_model(body_obj, request.headers, manifest, thread_routes, local_key, request.path)
        image_request = urlsplit(request.path).path == "/v1/images/generations"
        if inherited:
            requested_model = model
            model, model_record = inherited
            if model != requested_model and not image_request:
                body_obj = copy.deepcopy(body_obj)
                body_obj["model"] = model
                routed_body_changed = True
            log.info(
                "subagent source bound rid=%s parent=%s child=%s requested_model=%s model=%s source=%s wire=%s",
                request.request_id or "-", _parent_thread_id(body_obj, request.headers),
                ",".join(_request_thread_ids(body_obj, request.headers)) or "-", requested_model or "-", model,
                str(model_record.get("source") or "local") if model_record else "official",
                (model_record.get("wire_api") or "chat") if model_record else "responses",
            )
            if image_request:
                local_image = _route_local_image(
                    request, body_obj, manifest, local_key, transport, image_turn_routes,
                    _source_fingerprint(manifest, model_record, request.headers, local_key),
                )
                if local_image is not None:
                    return local_image
                if model_record:
                    raise ProtocolError("subagent_image_source_unbound", "子代理图片回合缺少来源记录，已阻止切换到官方账号。")
                model, model_record = requested_model, None
        if not inherited and _is_title_request(request, body_obj):
            return _route_title_request(request, body_obj, manifest, local_key, transport, image_turn_routes)
        if model.startswith("local/") and not model_record:
            return _json_error(400, f"Local 模型 {model} 未在 local-models.json 登记，请登记后刷新模型列表。")
        if not inherited:
            local_image = _route_local_image(request, body_obj, manifest, local_key, transport, image_turn_routes)
            if local_image is not None:
                return local_image
        if model_record:
            upstream_base, source_id, source_key = _model_source(manifest, model_record)
            api_key = source_key or local_key
            if not upstream_base:
                log.error("local route blocked model=%s reason=source_unavailable", model)
                return _json_error(503, "该模型的路由来源不可用，请在管理界面重新保存 Key。", "source_unavailable")
            if not api_key:
                log.error("local route blocked model=%s source=%s reason=key_missing", model, source_id)
                return _json_error(503, "该模型所属的 Key 未设置密钥，请先在管理界面配置。", "key_missing")
            for thread_id in _request_thread_ids(body_obj, request.headers):
                thread_routes.remember(thread_id, model, _source_fingerprint(manifest, model_record, request.headers, local_key))
            if body_obj.get("generate") is False:
                return _json_error(400, "HTTP-only Local 桥接不支持 generate:false 预热请求。", "unsupported_prewarm")
            if body_obj.get("previous_response_id") and not _has_full_context(body_obj):
                return _json_error(400, "Local HTTP 请求必须重放完整上下文，不能只使用 previous_response_id。", "previous_response_not_found")
            if model_record.get("wire_api") == "responses":
                return _responses_passthrough(
                    request, body_obj, model_record, upstream_base, api_key, transport, model, source_id, image_turn_routes,
                )
            registry = ToolRegistry.from_body(body_obj)
            chat = build_chat_request(body_obj, model_record, registry)
            headers = _clean_headers(request.headers, local=True)
            headers["Content-Type"] = "application/json"
            headers["Authorization"] = "Bearer " + api_key
            input_types = _input_type_counts(body_obj.get("input"))
            metrics = registry.metrics()
            diagnostics = _chat_diagnostics(chat)
            has_tool_outputs = _has_tool_outputs(body_obj)
            content_type_headers = sum(name.lower() == "content-type" for name in headers)
            log.info(
                "local bridge request rid=%s model=%s source=%s input_types=%s additional_groups=%d namespaces=%d functions=%d custom=%d history_only=%d tool_search=%d web_search_skipped=%d web_search_history_skipped=%d web_search_choices_skipped=%d messages=%d upstream_tools=%d images=%d tool_images=%d tool_output_history=%s content_type_headers=%d reasoning_language=%s",
                request.request_id or "-", model, source_id, input_types, metrics["additional_groups"], metrics["namespaces"], metrics["functions"], metrics["custom"],
                metrics["history_only"], registry.tool_search_count, registry.web_search_count, registry.web_search_history_count, registry.web_search_choice_count,
                diagnostics["messages"], diagnostics["tools"], diagnostics["images"], diagnostics["tool_images"], has_tool_outputs,
                content_type_headers, LOCAL_REASONING_LANGUAGE,
            )

            def request_upstream(request_chat: dict) -> UpstreamResponse:
                payload = json.dumps(request_chat, ensure_ascii=False, separators=(",", ":")).encode()
                return transport("POST", upstream_base.rstrip("/") + "/chat/completions", headers, payload, request_chat["stream"])

            response = request_upstream(chat)
            if response.status >= 400:
                log.warning(
                    "local upstream rejected rid=%s model=%s source=%s wire=chat status=%d messages=%d payload_bytes=%d",
                    request.request_id or "-", model, source_id, response.status, len(chat["messages"]),
                    len(json.dumps(chat, ensure_ascii=False, separators=(",", ":")).encode()),
                )
                return _json_error(response.status, f"Local 上游返回 HTTP {response.status}。", "upstream_error")
            response_headers = _converted_headers(response.headers, chat["stream"])
            if chat["stream"]:
                converted = _local_stream_body(response, chat, model, registry, request_upstream, has_tool_outputs)
                return UpstreamResponse(response.status, response_headers, converted, response.reason)
            converted = _local_nonstream_body(response.body, chat, model, registry, request_upstream, has_tool_outputs)
            return UpstreamResponse(response.status, response_headers, converted, response.reason)
        if urlsplit(request.path).path == "/v1/responses":
            turn_id = _request_turn_id(body_obj, request.headers)
            if turn_id:
                image_turn_routes.forget(turn_id)
            for thread_id in _request_thread_ids(body_obj, request.headers):
                thread_routes.remember(thread_id, model, _source_fingerprint(manifest, None, request.headers, local_key))
        headers = _clean_headers(request.headers, local=False)
        official_url = _official_url(request.path)
        log.info(
            "official account usage rid=%s model=%s path=%s target=%s credential=%s codex_headers=%s body_bytes=%d",
            request.request_id or "-", model or "-", urlsplit(request.path).path, urlsplit(official_url).netloc,
            _credential_kind(request.headers), _codex_header_summary(request.headers), len(request.body or b""),
        )
        payload = request.body
        if routed_body_changed:
            payload = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers = {key: value for key, value in headers.items() if key.lower() not in {"content-encoding", "content-type"}}
            headers["Content-Type"] = "application/json"
        return transport(request.method, official_url, headers, payload, True)
    except ProtocolError as exc:
        log.warning(
            "local bridge protocol rejected rid=%s parent=%s child=%s model=%s code=%s",
            request.request_id or "-", _parent_thread_id(body_obj, request.headers) or "-",
            ",".join(_request_thread_ids(body_obj, request.headers)) or "-", model or "-", exc.code,
        )
        return _json_error(400, str(exc), exc.code)
    except (ValueError, json.JSONDecodeError) as exc:
        log.error("local bridge conversion failed model=%s kind=%s", model or "-", type(exc).__name__)
        return _json_error(502, "Local 桥接处理响应失败，请查看路由日志。", "bridge_error")
    except TimeoutError:
        return _json_error(504, "上游请求超时，请稍后重试并查看路由日志。")
    except (OSError, http.client.HTTPException) as exc:
        log.error("upstream connection failed kind=%s", type(exc).__name__)
        return _json_error(502, "无法连接上游，请检查网络并查看路由日志。")


def default_transport(
    method: str, url: str, headers: dict[str, str], body: bytes, stream: bool,
    deadline: Optional[float] = None,
) -> UpstreamResponse:
    parsed = urlsplit(url)
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    timeout = min(TIMEOUT_SECONDS, max(0.001, deadline - time.monotonic())) if deadline else TIMEOUT_SECONDS
    connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
    path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    try:
        connection.request(method, path, body=body or None, headers=headers)
        if deadline and connection.sock:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("title deadline exceeded")
            connection.sock.settimeout(remaining)
        response = connection.getresponse()
    except Exception:
        connection.close()
        raise
    response_headers = {key: value for key, value in response.getheaders() if key.lower() not in HOP_BY_HOP}
    def read_chunk(size: Optional[int] = None) -> bytes:
        if deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("title deadline exceeded")
            sock = connection.sock or getattr(getattr(response.fp, "raw", None), "_sock", None)
            if sock:
                sock.settimeout(remaining)
            return response.read1(size or 64 * 1024)
        # Forward received SSE bytes before a truncated HTTP ending can discard them.
        if stream and size:
            return response.read1(size)
        return response.read(size) if size else response.read()
    if stream:
        def chunks():
            try:
                while True:
                    chunk = read_chunk(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                connection.close()
        payload = chunks()
    else:
        try:
            if deadline:
                parts = []
                size = 0
                while True:
                    chunk = read_chunk(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > TITLE_MAX_RESPONSE_BYTES:
                        raise ValueError("title response too large")
                    parts.append(chunk)
                payload = b"".join(parts)
            else:
                payload = read_chunk()
        finally:
            connection.close()
    return UpstreamResponse(response.status, response_headers, payload, response.reason)


class RouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    manifest: dict = {}
    runtime_dir = Path(".")

    def log_message(self, _format, *_args):
        return

    def _send(self, response: UpstreamResponse, request_id: str, started: float, model: str, route: str) -> None:
        self.send_response(response.status, response.reason)
        streaming = not isinstance(response.body, (bytes, bytearray))
        for key, value in response.headers.items():
            if key.lower() not in HOP_BY_HOP and key.lower() != "content-length":
                self.send_header(key, value)
        if not streaming:
            self.send_header("Content-Length", str(len(response.body)))
        else:
            # The proxy streams bytes as they arrive and closes the connection
            # to delimit the body; it never buffers prompts or model output.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.send_header("X-Router-Request-Id", request_id)
        self.end_headers()
        total = 0
        try:
            chunks = [response.body] if not streaming else response.body
            for chunk in chunks:
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    total += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            log.info("client disconnected rid=%s", request_id)
        except http.client.IncompleteRead as exc:
            log.warning("upstream stream truncated rid=%s forwarded_bytes=%d partial_bytes=%d", request_id, total, len(exc.partial))
            raise
        finally:
            log.info("request rid=%s model=%s route=%s status=%d ms=%d bytes=%d", request_id, model or "-", route, response.status, int((time.monotonic() - started) * 1000), total)

    def _handle(self) -> None:
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        if self.command == "GET" and self.headers.get("Upgrade", "").lower() == "websocket":
            log.warning("websocket rejected rid=%s path=%s reason=http_only", request_id, urlsplit(self.path).path)
            response = _json_error(426, "当前 Provider 必须使用 HTTP Responses，请确认 supports_websockets=false。", "unsupported_transport")
            self._send(response, request_id, started, "-", "websocket-reject")
            return
        if self.command == "GET" and urlsplit(self.path).path == "/healthz":
            payload = json.dumps(health_payload(self.manifest, read_local_key(self.runtime_dir / LOCAL_KEY_FILE)), separators=(",", ":")).encode()
            self._send(UpstreamResponse(200, {"Content-Type": "application/json"}, payload), request_id, started, "-", "health")
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        headers = {key: value for key, value in self.headers.items()}
        obj = {}
        try:
            obj = _decoded_json(body, headers) if body else {}
            model = str(obj.get("model") or "")
        except Exception:
            model = ""
        image_turn = _image_turn_id(headers) if urlsplit(self.path).path == "/v1/images/generations" else ""
        route = (
            "subagent" if _request_thread_source(obj, headers) == "subagent" or _parent_thread_id(obj, headers)
            else "title-gateway" if _is_title_request(PreparedRequest(self.command, self.path, headers, body), obj)
            else "local-image" if image_turn and IMAGE_TURN_ROUTES.model_for(image_turn)
            else "local" if _model_record(self.manifest, model)
            else "reject" if model.startswith("local/") else "official"
        )
        encoding = self.headers.get("Content-Encoding", "identity")
        log.info("request start rid=%s method=%s path=%s encoding=%s", request_id, self.command, urlsplit(self.path).path, encoding)
        response = route_request(
            PreparedRequest(self.command, self.path, headers, body, request_id),
            self.manifest, read_local_key(self.runtime_dir / LOCAL_KEY_FILE), default_transport,
        )
        if route == "official" and response.status in {404, 410}:
            log.error("official path incompatible rid=%s status=%d path=%s", request_id, response.status, urlsplit(self.path).path)
        self._send(response, request_id, started, model, route)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Codex official and Local model router")
    parser.add_argument("--runtime-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--host", default=LISTEN_HOST)
    parser.add_argument("--port", type=int, default=LISTEN_PORT)
    args = parser.parse_args(argv)
    configure_logging(args.runtime_dir / "logs")
    if not zstd_decoder_available():
        log.error("startup blocked reason=zstd_decoder_unavailable")
        raise SystemExit("缺少 zstd 解码器，路由未启动。")
    manifest = load_manifest(args.runtime_dir / MANIFEST_FILE)
    RouterHandler.manifest = manifest
    RouterHandler.runtime_dir = args.runtime_dir
    server = ThreadingHTTPServer((args.host, args.port), RouterHandler)
    log.info(
        "startup version=%s transport=http-only bridge=%s listen=%s:%d manifest_version=%s local_models=%d sources=%d default_upstream=%s",
        VERSION, BRIDGE_NAME, args.host, args.port, manifest["version"], len(manifest["models"]),
        len(manifest.get("sources") or {}), DEFAULT_UPSTREAM,
    )
    priority_path = args.runtime_dir / "keys.json"
    official_models = official_priority_models(priority_path)
    log.info(
        "official priority models=%s count=%d keys=%s",
        ",".join(official_models) or "-", len(official_models), priority_path,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        log.info("shutdown complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
