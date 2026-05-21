"""Environment validation for Scenario Lab runtimes."""

from __future__ import annotations

import importlib.util
import sys


FULL_VERSION_PACKAGES = {
    "pytest": "test execution inside the active conda environment",
    "langgraph": "durable multi-agent workflow execution",
    "deepeval": "LLM faithfulness and agent evaluation",
    "ragas": "RAG faithfulness evaluation",
    "opentelemetry": "enterprise tracing and observability",
}


def validate_runtime_environment() -> dict:
    packages = {}
    missing = []
    for package, purpose in FULL_VERSION_PACKAGES.items():
        installed = importlib.util.find_spec(package) is not None
        packages[package] = {"installed": installed, "purpose": purpose}
        if not installed:
            missing.append(package)
    return {
        "python_executable": sys.executable,
        "python_version": sys.version,
        "packages": packages,
        "full_external_runtime_available": not missing,
        "missing_packages": missing,
        "install_hint": "Install requirements-full.txt in the active stock conda environment; do not mark external capabilities implemented until this check passes.",
    }
