"""Load the actual advisory APIs before acquisition or model spending begins."""

from importlib import import_module
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
