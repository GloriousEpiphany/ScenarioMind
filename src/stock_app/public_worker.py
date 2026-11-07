"""Process boundary for public SDK calls which do not expose request timeouts."""

from dataclasses import asdict
from datetime import datetime
import argparse
import json
from pathlib import Path
import subprocess
import sys

from scenario_engine.errors import DataUnavailable
from src.stock_app.scenario_public_data import (
    PreparationItemResult, PublicPreparationResult, prepare_public_scenario_data,
)
from src.stock_app.public_transport import AuditedPublicDataModule, industry_source_profile


COMPONENTS = ("financials", "industry", "news", "money_flow")


def prepare_bounded_public_component(db_path, symbols, *, timeout=180, industry_profile=None, **options):
    profile = industry_source_profile(industry_profile)
    selected = [name for name in COMPONENTS if options.get("include_" + name)]
    if len(symbols) != 1 or len(selected) != 1:
        raise ValueError("bounded worker requires exactly one security and component")
    try:
        result = subprocess.run([sys.executable, "-m", "src.stock_app.public_worker", str(Path(db_path).resolve()),
            symbols[0], selected[0], "--industry-profile", profile], cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except subprocess.TimeoutExpired as exc:
        raise DataUnavailable("public acquisition exceeded its declared deadline; no partial result accepted",
                              context={"component": selected[0], "timeout_seconds": timeout}) from exc
    if result.returncode:
        raise DataUnavailable("public acquisition worker failed", context={"component": selected[0], "exit_code": result.returncode})
    try:
        payload = json.loads(result.stdout)
        return PublicPreparationResult(datetime.fromisoformat(payload["started_at"]), datetime.fromisoformat(payload["finished_at"]),
            tuple(PreparationItemResult(**item) for item in payload["items"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise DataUnavailable("public worker did not return a complete structured result") from exc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("db_path")
    parser.add_argument("symbol")
    parser.add_argument("component", choices=COMPONENTS)
    parser.add_argument("--industry-profile", default=None)
    arguments = parser.parse_args()
    db_path, symbol, component = arguments.db_path, arguments.symbol, arguments.component
    profile = industry_source_profile(arguments.industry_profile)
    if component not in COMPONENTS:
        raise ValueError("unknown public component")
    # Only JSON goes to stdout; SDK diagnostics cannot contaminate the envelope.
    from contextlib import redirect_stdout
    with redirect_stdout(sys.stderr):
        result = prepare_public_scenario_data(db_path, (symbol,),
            ak_module=AuditedPublicDataModule(industry_profile=profile),
            **{"include_" + name: name == component for name in COMPONENTS})
    print(json.dumps(asdict(result), default=lambda value: value.isoformat(), ensure_ascii=False))


if __name__ == "__main__":
    main()
