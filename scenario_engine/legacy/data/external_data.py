"""External data source registry for V2.

No external data is fetched here. The registry records connector availability
and prevents accidental use of unverified data sources.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from scenario_engine.legacy.scenarios.schemas import ExternalDataSourceStatus


class ExternalDataRegistry:
    SUPPORTED_TYPES = {"mcp", "file", "manual", "gdelt_doc"}

    def __init__(self):
        self.sources: dict[str, ExternalDataSourceStatus] = {}
        self.event_connectors: dict[str, Callable[[], list[str]]] = {}

    def register(self, source_name: str, connector_type: str, available: bool, reason: str, metadata: dict | None = None) -> ExternalDataSourceStatus:
        if connector_type not in self.SUPPORTED_TYPES:
            raise ValueError("unsupported connector_type")
        status = ExternalDataSourceStatus(
            source_name=source_name,
            connector_type=connector_type,
            status="available" if available else "unavailable",
            reason=reason,
            last_checked_at=datetime.now().isoformat(timespec="seconds"),
            metadata=metadata or {},
        )
        self.sources[source_name] = status
        return status

    def register_event_connector(self, source_name: str, connector: Callable[[], list[str]]) -> None:
        self.event_connectors[source_name] = connector

    def fetch_events(self, source_name: str) -> list[str]:
        connector = self.event_connectors.get(source_name)
        if connector is None:
            raise ValueError("mcp event source requires an actual connector; no MCP fetch is performed")
        events = connector()
        if not isinstance(events, list):
            raise ValueError("mcp event connector must return list[str]")
        return [str(item).strip() for item in events if str(item).strip()]

    def get_status(self, source_name: str) -> ExternalDataSourceStatus:
        if source_name in self.sources:
            return self.sources[source_name]
        return ExternalDataSourceStatus(
            source_name=source_name,
            connector_type="mcp",
            status="unavailable",
            reason="未注册外部数据连接器；系统不会伪造外部数据。",
            last_checked_at=datetime.now().isoformat(timespec="seconds"),
        )


class McpJsonRpcEventConnector:
    """Minimal MCP tools/call connector over a JSON-RPC transport callable."""

    def __init__(self, transport, tool_name: str, arguments: dict | None = None):
        if not tool_name.strip():
            raise ValueError("tool_name is required")
        self.transport = transport
        self.tool_name = tool_name
        self.arguments = arguments or {}
        self._next_id = 1

    def __call__(self) -> list[str]:
        response = self.transport(
            {
                "jsonrpc": "2.0",
                "id": self._request_id(),
                "method": "tools/call",
                "params": {"name": self.tool_name, "arguments": self.arguments},
            }
        )
        if not isinstance(response, dict):
            raise ValueError("MCP transport must return a JSON-RPC response dict")
        if response.get("error"):
            raise ValueError(f"MCP tools/call failed: {response['error']}")
        result = response.get("result") or {}
        content = result.get("content") or []
        events = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text") or "").strip()
                if text:
                    events.append(text)
        return events

    def _request_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id
