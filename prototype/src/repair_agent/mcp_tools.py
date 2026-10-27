"""MCP client side: connect to the stdio servers, list tools, call tools (mcp 2.x ``Client``)."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any

from mcp import Client, StdioServerParameters


def result_text(res: Any) -> tuple[str, bool]:
    parts: list[str] = []
    for block in getattr(res, "content", None) or []:
        t = getattr(block, "text", None)
        parts.append(t if t is not None else str(block))
    if not parts and getattr(res, "structured_content", None) is not None:
        parts.append(json.dumps(res.structured_content, default=str))
    return "\n".join(parts), bool(getattr(res, "is_error", False))


class McpToolbox:
    """Async context manager. ``tools`` are OpenAI-style function specs with bare (unique) tool names."""

    def __init__(self, specs: dict[str, dict]):
        self.specs = specs
        self.tools: list[dict] = []
        self._clients: dict[str, Any] = {}  # bare tool name -> Client
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> McpToolbox:  # noqa: PYI034
        try:
            for spec in self.specs.values():
                params = StdioServerParameters(command=spec["command"], args=spec.get("args", []), env=spec.get("env"))
                client = await self._stack.enter_async_context(Client(params))
                listing = await client.list_tools()
                for t in listing.tools:
                    if t.name in self._clients:
                        continue
                    self._clients[t.name] = client
                    self.tools.append({
                        "type": "function",
                        "function": {"name": t.name, "description": t.description or "", "parameters": t.input_schema},
                    })
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    async def call(self, name: str, args: dict) -> tuple[str, bool]:
        """(text, is_error). Never raises."""
        client = self._clients.get(name)
        if client is None:
            return json.dumps({"error": f"unknown tool {name}"}), True
        try:
            return result_text(await client.call_tool(name, args or {}))
        except Exception as e:  # transport/validation errors go back to the model as a result  # noqa: BLE001
            return json.dumps({"error": f"{type(e).__name__}: {e}"[:500]}), True
