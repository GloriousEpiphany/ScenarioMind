"""Validated launcher for the StockM1 Streamlit application."""

from __future__ import annotations

from importlib import import_module
import os
from pathlib import Path
import subprocess
import sys


REQUIRED_IMPORTS = (
    "numpy",
    "pandas",
    "scipy",
    "sklearn",
    "streamlit",
    "plotly",
)


def validate_runtime() -> None:
    if not ((3, 11) <= sys.version_info[:2] <= (3, 13)):
        raise RuntimeError(
            f"StockM1 requires Python 3.11-3.13; current interpreter is "
            f"{sys.executable} ({sys.version.split()[0]})."
        )
    missing = []
    import_errors = []
    for module_name in REQUIRED_IMPORTS:
        try:
            import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name or str(exc.name).startswith(f"{module_name}."):
                missing.append(module_name)
            else:
                import_errors.append(f"{module_name}: missing transitive dependency {exc.name}")
        except Exception as exc:
            import_errors.append(f"{module_name}: {type(exc).__name__}: {exc}")
    if missing or import_errors:
        details = []
        if missing:
            details.append(f"missing modules={sorted(missing)}")
        details.extend(import_errors)
        raise RuntimeError(
            "StockM1 runtime validation failed for "
            f"{sys.executable}: {'; '.join(details)}. "
            "Select the PyCharm SDK named 'stock' or install the exact requirements.txt lock "
            "into the chosen interpreter."
        )


def main() -> None:
    validate_runtime()
    project_root = Path(__file__).resolve().parent
    os.chdir(project_root)
    app_path = project_root / "visualization" / "app.py"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(app_path),
            "--server.port=8501",
            "--server.address=localhost",
            "--browser.gatherUsageStats=false",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
