"""Truthful runtime configuration and capability audit for scenario preparation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib.util import find_spec
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_scenario_environment() -> tuple[str, ...]:
    """Load root and legacy env files without overwriting process-level secrets."""
    loaded: list[str] = []
    try:
        from dotenv import load_dotenv
    except ImportError:
        return ()
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
    capabilities = [
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
            configured=_enabled("STOCKM1_TUSHARE_ENABLED") and _present("TUSHARE_TOKEN"),
            dependency_available=find_spec("tushare") is not None,
            live_verified=False,
            status=(
                "not_configured" if not (_enabled("STOCKM1_TUSHARE_ENABLED") and _present("TUSHARE_TOKEN"))
                else "dependency_missing" if find_spec("tushare") is None
                else "configured_not_live_verified"
            ),
            detail="需要 TuShare Pro token；具体数据集还必须通过权限和字段 canary",
        ),
        RuntimeCapability(
            capability_id="rqdata",
            configured=_enabled("STOCKM1_RQDATA_ENABLED") and (
                _present("RQDATA_USERNAME", "RQDATA_PASSWORD") or _present("RQDATAC2_CONF")
            ),
            dependency_available=find_spec("rqdatac") is not None,
            live_verified=False,
            status=(
                "not_configured"
                if not (
                    _enabled("STOCKM1_RQDATA_ENABLED")
                    and (_present("RQDATA_USERNAME", "RQDATA_PASSWORD") or _present("RQDATAC2_CONF"))
                )
                else "dependency_missing" if find_spec("rqdatac") is None
                else "configured_not_live_verified"
            ),
            detail="需要已购买的 RQData 权限；不同数据集必须分别探测",
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


__all__ = [
    "PROJECT_ROOT",
    "RuntimeCapability",
    "configured_runtime_capabilities",
    "load_scenario_environment",
    "probe_llm_live",
]
