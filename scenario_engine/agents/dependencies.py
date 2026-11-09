"""Load the actual advisory APIs before acquisition or model spending begins."""

from importlib import import_module
import importlib.util
import sys

from scenario_engine.errors import RuntimeUnavailable


def require_advisory_runtime():
    """Check imports and symbols, including the separately packaged SQLite saver."""
    components = (
        ("langgraph.graph", ("StateGraph", "START", "END")),
        ("langgraph.checkpoint.sqlite", ("SqliteSaver",)),
        ("langsmith.run_helpers", ("tracing_context",)),
        ("openai", ("OpenAI",)),
    )
    loaded = {}
    for component, names in components:
        try:
            module = import_module(component)
            loaded.update((name, getattr(module, name)) for name in names)
        except (ImportError, AttributeError) as exc:
            raise RuntimeUnavailable(
                f"当前解释器 {sys.executable} 无法加载 {component}；请安装 requirements-advisory.txt，"
                "或用已配置的研究环境启动后恢复原任务。",
                context={"component": component, "requirements_file": "requirements-advisory.txt",
                         "python_executable": sys.executable, "error_type": type(exc).__name__},
            ) from exc
    return loaded


RESEARCH_PACKAGES = {
    "langgraph": "durable research execution",
    "langsmith": "local tracing control",
    "openai": "configured model client",
    "akshare": "public data acquisition",
    "curl_cffi": "configured public HTTPS transport",
    "gurobipy": "strict portfolio optimization",
}

def validate_runtime_environment() -> dict:
    packages = {}
    missing = []
    for package, purpose in RESEARCH_PACKAGES.items():
        installed = importlib.util.find_spec(package) is not None
        packages[package] = {"installed": installed, "purpose": purpose}
        if not installed:
            missing.append(package)
    advisory_error = None
    try:
        require_advisory_runtime()
    except RuntimeUnavailable as exc:
        advisory_error = exc.context
    return {
        "python_executable": sys.executable,
        "python_version": sys.version,
        "packages": packages,
        "full_external_runtime_available": not missing and advisory_error is None,
        "advisory_runtime_error": advisory_error,
        "missing_packages": missing,
        "install_hint": "Install requirements-full.txt in the active Python 3.11-3.13 environment. Imports do not validate model credentials, data access or solver licenses.",
    }
