"""Truthful runtime configuration and capability audit for scenario preparation."""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from importlib.util import find_spec
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class CredentialSetupGuide:
    provider_id: str
    display_name: str
    required_variable_groups: tuple[tuple[str, ...], ...]
    optional_variables: tuple[str, ...]
    acquisition_url: str
    documentation_url: str
    guidance: str


@dataclass(frozen=True)
class RuntimeCredentialCodec:
    protection: str
    protect: Callable[[bytes], bytes]
    unprotect: Callable[[bytes], bytes]


_PROVIDER_VARIABLES = {
    "deepseek": (
        "SCENARIO_LLM_API_BASE",
        "SCENARIO_LLM_API_KEY",
        "SCENARIO_LLM_MODEL",
    ),
    "tushare": ("TUSHARE_TOKEN",),
}
_PROVIDER_ENABLE_VARIABLES = {
    "deepseek": "STOCKM1_SCENARIO_LLM_ENABLED",
    "tushare": "STOCKM1_TUSHARE_ENABLED",
}


def credential_setup_catalog() -> tuple[CredentialSetupGuide, ...]:
    """Describe the supported one-time setup and the official acquisition paths."""
    return (
        CredentialSetupGuide(
            provider_id="deepseek",
            display_name="DeepSeek API",
            required_variable_groups=((
                "SCENARIO_LLM_API_BASE",
                "SCENARIO_LLM_API_KEY",
                "SCENARIO_LLM_MODEL",
            ),),
            optional_variables=(),
            acquisition_url="https://platform.deepseek.com/api_keys",
            documentation_url="https://api-docs.deepseek.com/zh-cn/",
            guidance=(
                "在 DeepSeek 开放平台创建 API Key；应用会自动使用官方 base URL，"
                "无需编辑 .env。"
            ),
        ),
        CredentialSetupGuide(
            provider_id="tushare",
            display_name="TuShare Pro",
            required_variable_groups=(("TUSHARE_TOKEN",),),
            optional_variables=(),
            acquisition_url="https://tushare.pro/document/1?doc_id=39",
            documentation_url="https://tushare.pro/document/2?doc_id=27",
            guidance=(
                "登录 TuShare 后进入个人中心 → 账号与 TOKEN，复制唯一 token。"
                "应用会用 A 股 daily 接口做真实权限验证，并将手/千元自动换算为股/元。"
            ),
        ),
    )


def runtime_credential_store_path() -> Path:
    """Return a user-private credential path outside the source tree."""
    if os.name == "nt" and str(os.getenv("LOCALAPPDATA") or "").strip():
        root = Path(str(os.environ["LOCALAPPDATA"]))
    elif str(os.getenv("XDG_CONFIG_HOME") or "").strip():
        root = Path(str(os.environ["XDG_CONFIG_HOME"]))
    else:
        root = Path.home() / ".config"
    return root / "StockM1" / "runtime-credentials.json"


def _default_credential_codec() -> RuntimeCredentialCodec:
    if os.name == "nt":
        return RuntimeCredentialCodec(
            protection="windows-dpapi-current-user-v1",
            protect=_windows_dpapi_protect,
            unprotect=_windows_dpapi_unprotect,
        )
    return RuntimeCredentialCodec(
        protection="posix-user-file-0600-v1",
        protect=lambda payload: payload,
        unprotect=lambda payload: payload,
    )


def _windows_dpapi_protect(payload: bytes) -> bytes:
    return _windows_dpapi_transform(payload, decrypt=False)


def _windows_dpapi_unprotect(payload: bytes) -> bytes:
    return _windows_dpapi_transform(payload, decrypt=True)


def _windows_dpapi_transform(payload: bytes, *, decrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte)))

    source_buffer = (ctypes.c_byte * len(payload)).from_buffer_copy(payload)
    source_blob = DataBlob(len(payload), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    result_blob = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    if decrypt:
        succeeded = function(
            ctypes.byref(source_blob), None, None, None, None, 0x1, ctypes.byref(result_blob)
        )
    else:
        succeeded = function(
            ctypes.byref(source_blob), "StockM1 runtime credentials", None, None, None, 0x1,
            ctypes.byref(result_blob),
        )
    if not succeeded:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(result_blob.pbData, result_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result_blob.pbData, ctypes.c_void_p))


def _normalize_runtime_credentials(provider_id: str, values: Mapping[str, Any]) -> dict[str, str]:
    if provider_id not in _PROVIDER_VARIABLES:
        raise ValueError(f"不支持的凭证提供方：{provider_id}")
    allowed = set(_PROVIDER_VARIABLES[provider_id])
    unknown = set(values).difference(allowed)
    if unknown:
        raise ValueError(f"{provider_id} 包含不支持的字段：{sorted(unknown)}")
    normalized = {
        name: str(value).strip()
        for name, value in values.items()
        if str(value).strip()
    }
    if provider_id == "deepseek":
        required = set(_PROVIDER_VARIABLES[provider_id])
        if required.difference(normalized):
            raise ValueError("DeepSeek 需要 API base、API key 和 model")
    elif not normalized.get("TUSHARE_TOKEN"):
        raise ValueError("TuShare 需要 token")
    return normalized


def activate_runtime_credentials(
    provider_id: str,
    values: Mapping[str, Any],
    *,
    replace: bool = True,
) -> dict[str, str]:
    """Activate one provider for the current process without exposing secrets."""
    normalized = _normalize_runtime_credentials(provider_id, values)
    setter = os.environ.__setitem__ if replace else os.environ.setdefault
    if replace:
        for name in _PROVIDER_VARIABLES[provider_id]:
            if name not in normalized:
                os.environ.pop(name, None)
    for name, value in normalized.items():
        setter(name, value)
    setter(_PROVIDER_ENABLE_VARIABLES[provider_id], "1")
    return normalized


def _read_runtime_credential_payload(
    path: Path,
    codec: RuntimeCredentialCodec,
) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if envelope.get("version") != 1 or envelope.get("protection") != codec.protection:
        raise ValueError("本地凭证格式或保护方式不受当前系统支持")
    protected = base64.b64decode(str(envelope["payload"]), validate=True)
    decoded = json.loads(codec.unprotect(protected).decode("utf-8"))
    providers = decoded.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("本地凭证缺少 providers 对象")
    return {
        str(provider_id): {str(name): str(value) for name, value in values.items()}
        for provider_id, values in providers.items()
        if str(provider_id) in _PROVIDER_VARIABLES and isinstance(values, dict)
    }


def save_runtime_credentials(
    provider_id: str,
    values: Mapping[str, Any],
    *,
    store_path: str | Path | None = None,
    codec: RuntimeCredentialCodec | None = None,
) -> Path:
    """Persist a validated provider config in a per-user protected store."""
    path = Path(store_path) if store_path is not None else runtime_credential_store_path()
    selected_codec = codec or _default_credential_codec()
    normalized = activate_runtime_credentials(provider_id, values)
    providers = _read_runtime_credential_payload(path, selected_codec)
    providers[provider_id] = normalized
    raw = json.dumps(
        {"version": 1, "providers": providers}, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    envelope = {
        "version": 1,
        "protection": selected_codec.protection,
        "payload": base64.b64encode(selected_codec.protect(raw)).decode("ascii"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def load_runtime_credentials(
    *,
    store_path: str | Path | None = None,
    codec: RuntimeCredentialCodec | None = None,
) -> tuple[str, ...]:
    """Load saved credentials without overriding deployment-level environment values."""
    path = Path(store_path) if store_path is not None else runtime_credential_store_path()
    selected_codec = codec or _default_credential_codec()
    providers = _read_runtime_credential_payload(path, selected_codec)
    loaded: list[str] = []
    for provider_id, values in providers.items():
        activate_runtime_credentials(provider_id, values, replace=False)
        loaded.append(provider_id)
    return tuple(loaded)


def load_scenario_environment() -> tuple[str, ...]:
    """Load process > app credential store > legacy env, in that precedence order."""
    loaded: list[str] = []
    try:
        if load_runtime_credentials():
            loaded.append(str(runtime_credential_store_path()))
    except (OSError, ValueError, json.JSONDecodeError):
        # A corrupt or foreign-user store must never prevent the app from
        # starting; the setup UI can overwrite it after explicit validation.
        pass
    try:
        from dotenv import load_dotenv
    except ImportError:
        return tuple(loaded)
    for path in (PROJECT_ROOT / ".env", PROJECT_ROOT / "Agents_Experience" / ".env"):
        if path.exists():
            load_dotenv(path, override=False)
            loaded.append(str(path))
    return tuple(loaded)


def _enabled(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _present(*names: str) -> bool:
    return all(bool(str(os.getenv(name) or "").strip()) for name in names)


@dataclass(frozen=True)
class RuntimeCapability:
    capability_id: str
    configured: bool
    dependency_available: bool
    live_verified: bool
    status: str
    detail: str
    source_host: str | None = None
    model: str | None = None

    @property
    def usable(self) -> bool:
        return self.configured and self.dependency_available and self.live_verified

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"usable": self.usable}


def configured_runtime_capabilities() -> tuple[RuntimeCapability, ...]:
    """Return local configuration facts only; never claim a remote API was tested."""
    load_scenario_environment()
    llm_base = (
        os.getenv("SCENARIO_LLM_API_BASE")
        or os.getenv("QWEN_API_BASE")
        or os.getenv("OPENAI_BASE_URL")
        or ""
    ).strip()
    llm_model = (
        os.getenv("SCENARIO_LLM_MODEL")
        or os.getenv("QWEN_MODEL")
        or os.getenv("OPENAI_MODEL")
        or ""
    ).strip()
    llm_key = bool(
        str(
            os.getenv("SCENARIO_LLM_API_KEY")
            or os.getenv("QWEN_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or ""
        ).strip()
    )
    llm_configured = bool(llm_base and llm_model and llm_key)
    openai_available = find_spec("openai") is not None
    tushare_configured = _enabled(
        "STOCKM1_TUSHARE_ENABLED", default=_present("TUSHARE_TOKEN")
    ) and _present("TUSHARE_TOKEN")
    capabilities = [
        RuntimeCapability(
            capability_id="baostock", configured=True,
            dependency_available=find_spec("baostock") is not None,
            live_verified=False,
            status="dependency_missing" if find_spec("baostock") is None else "configured_not_live_verified",
            detail="免费匿名日线；需实际验证登录、交易日历及完整区间，配置不代表网络可用",
            source_host="public-api.baostock.com",
        ),
        RuntimeCapability(
            capability_id="eastmoney_http", configured=True,
            dependency_available=(find_spec("requests") is not None), live_verified=False,
            status="configured_not_live_verified" if find_spec("requests") is not None else "dependency_missing",
            detail="显式日线来源；需逐区间核对沪深基准交易日，缺日失败；不自动替换其他来源",
            source_host="push2his.eastmoney.com",
        ),
        RuntimeCapability(
            capability_id="scenario_llm",
            configured=llm_configured,
            dependency_available=openai_available,
            live_verified=False,
            status=(
                "not_configured" if not llm_configured
                else "dependency_missing" if not openai_available
                else "configured_not_live_verified"
            ),
            detail=(
                "API base、key 或 model 未完整配置" if not llm_configured
                else "openai SDK 未安装" if not openai_available
                else "配置可读取；必须执行真实 canary 后才能标记可用"
            ),
            source_host=urlparse(llm_base).netloc or None,
            model=llm_model or None,
        ),
        RuntimeCapability(
            capability_id="tencent_http",
            configured=_enabled("STOCKM1_TENCENT_ENABLED", default=True),
            dependency_available=find_spec("requests") is not None,
            live_verified=False,
            status="configured_not_live_verified",
            detail="无需 API key；每次运行仍需按标的和区间做真实响应校验",
            source_host="web.ifzq.gtimg.cn",
        ),
        RuntimeCapability(
            capability_id="akshare_public",
            configured=_enabled("STOCKM1_AKSHARE_ENABLED", default=True),
            dependency_available=find_spec("akshare") is not None,
            live_verified=False,
            status=("dependency_missing" if find_spec("akshare") is None else "configured_not_live_verified"),
            detail="公共研究适配器；各上游接口必须分别探测，不能以 SDK 可导入代表数据可用",
        ),
        RuntimeCapability(
            capability_id="tushare_pro",
            configured=tushare_configured,
            dependency_available=find_spec("tushare") is not None,
            live_verified=False,
            status=(
                "not_configured" if not tushare_configured
                else "dependency_missing" if find_spec("tushare") is None
                else "configured_not_live_verified"
            ),
            detail=(
                "需要 TuShare Pro token"
                if not tushare_configured
                else "token 可读取；必须执行真实 A 股 daily canary 后才能标记历史行情可用"
            ),
            source_host="api.tushare.pro",
        ),
    ]
    return tuple(capabilities)


def probe_llm_live(*, prompt: str = "只返回 JSON：{\"ok\": true}") -> RuntimeCapability:
    """Make one real structured request; errors are returned verbatim without secrets."""
    load_scenario_environment()
    local = next(item for item in configured_runtime_capabilities() if item.capability_id == "scenario_llm")
    if not local.configured or not local.dependency_available:
        return local
    try:
        from scenario_engine.llm import ScenarioLLMClient

        client = ScenarioLLMClient.from_environment()
        if client is None:
            raise RuntimeError("ScenarioLLMClient.from_environment() returned None")
        result = client.parse_scenario(prompt, "2026-08-17", horizon="1w")
        if not isinstance(result.parsed, dict) or not result.parsed:
            raise RuntimeError("provider returned an empty structured response")
        return RuntimeCapability(
            capability_id="scenario_llm",
            configured=True,
            dependency_available=True,
            live_verified=True,
            status="available",
            detail="真实 chat.completions canary 已返回非空 JSON",
            source_host=urlparse(client.api_base).netloc or None,
            model=client.model,
        )
    except Exception as exc:
        return RuntimeCapability(
            capability_id="scenario_llm",
            configured=True,
            dependency_available=True,
            live_verified=False,
            status="live_probe_failed",
            detail=f"{type(exc).__name__}: {exc}",
            source_host=local.source_host,
            model=local.model,
        )


def probe_tushare_live() -> RuntimeCapability:
    """Probe the exact TuShare ``daily`` dataset used for historical prices."""
    load_scenario_environment()
    local = next(item for item in configured_runtime_capabilities() if item.capability_id == "tushare_pro")
    if not local.configured or not local.dependency_available:
        return local
    try:
        from scenario_engine.data import AvailabilityStatus, TuShareAdapter

        adapter = TuShareAdapter.from_environment()
        manifest = next(
            item for item in adapter.capabilities()
            if (item.domain, item.dataset) == ("price", "equity_price")
        )
        action = {
            AvailabilityStatus.AUTH_FAILED: "TuShare token 被服务端拒绝，请从个人中心重新复制。",
            AvailabilityStatus.PERMISSION_DENIED: "当前 token 的积分或套餐未开放 A 股 daily 接口。",
            AvailabilityStatus.QUOTA_LIMITED: "当前调用频率已达限制，请稍后自动重试。",
            AvailabilityStatus.NETWORK_UNAVAILABLE: "本机网络无法连接 api.tushare.pro。",
            AvailabilityStatus.INVALID_RESPONSE: "TuShare daily 返回字段不符合历史行情契约。",
        }.get(manifest.status)
        return RuntimeCapability(
            capability_id="tushare_pro",
            configured=True,
            dependency_available=True,
            live_verified=manifest.status == AvailabilityStatus.AVAILABLE,
            status=manifest.status.value,
            detail=f"{manifest.detail}；{action}" if action else manifest.detail,
            source_host=local.source_host,
        )
    except Exception as exc:
        return RuntimeCapability(
            capability_id="tushare_pro",
            configured=True,
            dependency_available=True,
            live_verified=False,
            status="live_probe_failed",
            detail=f"{type(exc).__name__}: TuShare daily capability probe failed",
            source_host=local.source_host,
        )


def probe_advisory_live() -> RuntimeCapability:
    """Exercise two actual tool-call turns, including provider continuation state."""
    from scenario_engine.agents import AgentModelPolicy
    from scenario_engine.agents.model import ToolCallingModel
    from scenario_engine.agents.runtime import control_tool
    from scenario_engine.agents.tools import canonical

    load_scenario_environment()
    names = ("SCENARIO_LLM_MODEL", "SCENARIO_LLM_API_BASE", "SCENARIO_LLM_API_KEY")
    values = [str(os.getenv(name) or "").strip() for name in names]
    if not all(values):
        return RuntimeCapability("advisory_tools", False, find_spec("openai") is not None, False,
                                 "configuration_missing", "自治工具调用要求三个明确的 SCENARIO_LLM_* 配置")
    model_name, base_url, api_key = values
    try:
        from openai import OpenAI
        policy = AgentModelPolicy("connectivity-probe-v1", model_name, "connectivity-only-not-model-evaluation")
        descriptor = control_tool("runtime_probe", "Verify the tool transport", {})
        messages = [{"role": "system", "content": "For this connectivity check call runtime_probe exactly once. "
                     "Always use this function to acknowledge each tool result. Do not answer in prose."},
                    {"role": "user", "content": "Check the tool transport."}]
        with OpenAI(base_url=base_url, api_key=api_key, timeout=90, max_retries=0) as client:
            model = ToolCallingModel.for_endpoint(client, policy, base_url)
            for _ in range(2):
                call = model.next_call(messages, [descriptor])
                if call.name != "runtime_probe" or call.arguments:
                    raise ValueError("unexpected probe tool call")
                messages.extend([{"role": "assistant", "content": None, **call.assistant_state,
                    "tool_calls": [{"id": call.call_id, "type": "function", "function": {
                        "name": call.name, "arguments": canonical(call.arguments)}}]},
                    {"role": "tool", "tool_call_id": call.call_id, "content": '{"transport":"ok"}'}])
                messages.append({"role": "user", "content": "Now call runtime_probe once again with empty arguments to check continuation."})
        return RuntimeCapability("advisory_tools", True, True, True, "available",
            "固定模型两轮真实工具调用及续传通过；仅证明连通性", model=model_name)
    except ImportError:
        return RuntimeCapability("advisory_tools", True, False, False, "dependency_missing",
                                 "请安装 requirements-advisory.txt", model=model_name)
    except Exception as exc:
        context = getattr(exc, "context", {})
        return RuntimeCapability("advisory_tools", True, True, False, "live_probe_failed",
            f"{type(exc).__name__}; provider_error={context.get('error_type', 'contract')}; "
            f"http_status={context.get('http_status')}; "
            f"returned_model={context.get('returned_model')}; "
            f"finish_reason={context.get('finish_reason')}; calls={context.get('tool_call_count')}; "
            f"contract={str(exc) if type(exc).__name__ == 'ContractViolation' else 'provider request failed'}", model=model_name)


__all__ = [
    "PROJECT_ROOT",
    "CredentialSetupGuide",
    "RuntimeCapability",
    "RuntimeCredentialCodec",
    "activate_runtime_credentials",
    "configured_runtime_capabilities",
    "credential_setup_catalog",
    "load_scenario_environment",
    "load_runtime_credentials",
    "probe_llm_live",
    "probe_advisory_live",
    "probe_tushare_live",
    "runtime_credential_store_path",
    "save_runtime_credentials",
]
