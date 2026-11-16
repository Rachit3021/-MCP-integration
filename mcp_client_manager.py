"""
mcp_client_manager.py
=====================

One async client, many MCP servers.

The agent in ``matching_agent.py`` never imports ``fs_tools`` or touches the
filesystem, SQLite or any server implementation. It only talks to this manager,
which speaks MCP/JSON-RPC 2.0 to every configured server over stdio.

What it provides
----------------
- Lifecycle: start/stop every enabled server from ``mcp_config.json`` using an
  ``AsyncExitStack``, so each child process is always terminated cleanly.
- Discovery: ``tools/list``, ``resources/list`` and ``resources/templates/list``
  for every server, merged into one catalog.
- OpenAI-compatible schemas: ``openai_tools()`` converts MCP tool definitions to
  the function-calling format used by Groq/OpenAI, with server prefixes to avoid
  name collisions (``filesystem__read_file``, ``jobs__search_jobs``).
- Calls: ``call_tool()`` and ``read_resource()`` return small, uniform result
  objects with timing and error details, so the agent can log and recover.

Tool-name convention
--------------------
``<server>__<tool>`` for the model, because OpenAI function names may only
contain letters, digits, ``_`` and ``-``. ``resolve_tool`` also accepts the
human-readable ``server.tool`` form and bare names when they are unambiguous.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Iterable

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import Resource, ResourceTemplate, Tool

from mcp_config import load_config, server_specs

logger = logging.getLogger("mcp_client_manager")

SCHEME_OWNERS = {
    "resumes": "filesystem",
    "workspace": "filesystem",
    "jobs": "jobs",
}


def qualified_name(server: str, tool: str) -> str:
    """Return the OpenAI-safe tool name for a server tool."""
    return f"{server}__{tool}"


def _split_qualified(name: str) -> tuple[str | None, str]:
    for separator in ("__", "."):
        if separator in name:
            server, _, tool = name.partition(separator)
            return server, tool
    return None, name


def _json_or_text(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _content_text(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            parts.append(getattr(block, "text", ""))
        elif block_type == "resource":
            resource = getattr(block, "resource", None)
            parts.append(getattr(resource, "text", "") or "")
        elif block_type == "image":
            parts.append(f"[image {getattr(block, 'mime_type', 'unknown')}]")
        else:
            parts.append(f"[{block_type or 'unknown'} content]")
    return "\n".join(part for part in parts if part)


@dataclass
class ToolCallResult:
    """Uniform result of one MCP ``tools/call``."""

    server: str
    tool: str
    ok: bool
    data: Any = None
    text: str = ""
    error: str | None = None
    duration_ms: float = 0.0
    raw_is_error: bool = False

    @property
    def name(self) -> str:
        return qualified_name(self.server, self.tool)

    def as_dict(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "tool": self.tool,
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class ResourceResult:
    """Uniform result of one MCP ``resources/read``."""

    server: str
    uri: str
    ok: bool
    text: str = ""
    mime_type: str | None = None
    data: Any = None
    error: str | None = None
    duration_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "uri": self.uri,
            "ok": self.ok,
            "mime_type": self.mime_type,
            "data": self.data,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class ServerConnection:
    """A live MCP client plus everything discovered from one server."""

    name: str
    client: Client
    spec: dict[str, Any]
    server_info: Any = None
    protocol_version: str | None = None
    instructions: str | None = None
    tools: list[Tool] = field(default_factory=list)
    resources: list[Resource] = field(default_factory=list)
    resource_templates: list[ResourceTemplate] = field(default_factory=list)

    def tool(self, name: str) -> Tool | None:
        return next((tool for tool in self.tools if tool.name == name), None)

    def summary(self) -> dict[str, Any]:
        info = None
        if self.server_info is not None:
            info = (
                self.server_info.model_dump()
                if hasattr(self.server_info, "model_dump")
                else dict(self.server_info)
            )
        return {
            "name": self.name,
            "server_info": info,
            "protocol_version": self.protocol_version,
            "instructions": self.instructions,
            "tools": [tool.name for tool in self.tools],
            "resources": [resource.uri for resource in self.resources],
            "resource_templates": [template.uri_template for template in self.resource_templates],
            "command": [self.spec.get("command"), *self.spec.get("args", [])],
        }


class MCPClientManager:
    """Own the lifecycle of every configured MCP server and expose one facade."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        server_names: Iterable[str] | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.config = config or load_config()
        self.specs = server_specs(self.config)
        if server_names is not None:
            wanted = list(server_names)
            missing = sorted(set(wanted) - set(self.specs))
            if missing:
                raise ValueError(f"Unknown or disabled MCP server(s): {', '.join(missing)}")
            self.specs = {name: self.specs[name] for name in wanted}
        timeout = timeout_seconds or self.config["agent"].get("mcp_timeout_seconds", 60)
        self.timeout_seconds = float(timeout)
        self.servers: dict[str, ServerConnection] = {}
        self._stack: AsyncExitStack | None = None
        self._started = False
        self.started_at = 0.0

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> "MCPClientManager":
        """Spawn every enabled server and perform MCP discovery."""
        if self._started:
            return self
        stack = AsyncExitStack()
        await stack.__aenter__()
        self._stack = stack
        try:
            for name, spec in self.specs.items():
                logger.info("starting MCP server '%s': %s %s", name, spec["command"], " ".join(spec["args"]))
                params = StdioServerParameters(
                    command=spec["command"],
                    args=spec["args"],
                    env=spec["env"],
                    cwd=spec["cwd"],
                )
                client = Client(
                    params,
                    mode="legacy",
                    read_timeout_seconds=self.timeout_seconds,
                )
                await stack.enter_async_context(client)
                connection = ServerConnection(name=name, client=client, spec=spec)
                connection.server_info = client.server_info
                connection.protocol_version = client.protocol_version
                connection.instructions = client.instructions
                self.servers[name] = connection
        except Exception:
            self.servers.clear()
            await stack.aclose()
            self._stack = None
            raise
        self.started_at = time.time()
        self._started = True
        try:
            await self.discover()
        except Exception:
            await self.close()
            raise
        logger.info(
            "MCP ready: %d servers, %d tools, %d resources, %d templates",
            len(self.servers),
            len(self.tools),
            len(self.resources),
            len(self.resource_templates),
        )
        return self

    async def close(self) -> None:
        """Terminate every server process and clear discovered state."""
        stack, self._stack = self._stack, None
        self._started = False
        self.servers.clear()
        if stack is not None:
            await stack.aclose()

    async def __aenter__(self) -> "MCPClientManager":
        return await self.start()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    @property
    def started(self) -> bool:
        return self._started

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    async def discover(self) -> dict[str, Any]:
        """Refresh tools, resources and templates from every server."""
        if not self._started:
            raise RuntimeError("MCPClientManager is not started")

        async def load(connection: ServerConnection) -> None:
            connection.tools = (await connection.client.list_tools()).tools
            connection.resources = (await connection.client.list_resources()).resources
            templates = await connection.client.list_resource_templates()
            connection.resource_templates = templates.resource_templates

        results = await asyncio.gather(
            *(load(connection) for connection in self.servers.values()),
            return_exceptions=True,
        )
        for connection, result in zip(list(self.servers.values()), results):
            if isinstance(result, BaseException):
                logger.error("discovery failed for '%s': %s", connection.name, result)
                raise result
        return self.catalog()

    @property
    def tools(self) -> list[tuple[str, Tool]]:
        return [
            (connection.name, tool)
            for connection in self.servers.values()
            for tool in connection.tools
        ]

    @property
    def resources(self) -> list[tuple[str, Resource]]:
        return [
            (connection.name, resource)
            for connection in self.servers.values()
            for resource in connection.resources
        ]

    @property
    def resource_templates(self) -> list[tuple[str, ResourceTemplate]]:
        return [
            (connection.name, template)
            for connection in self.servers.values()
            for template in connection.resource_templates
        ]

    def catalog(self) -> dict[str, Any]:
        """JSON-safe description of every server, tool and resource."""
        return {
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "server_count": len(self.servers),
            "tool_count": len(self.tools),
            "resource_count": len(self.resources),
            "resource_template_count": len(self.resource_templates),
            "servers": [connection.summary() for connection in self.servers.values()],
        }

    def tool_schemas(self) -> list[dict[str, Any]]:
        """Return qualified MCP tool definitions (name, description, schema)."""
        schemas = []
        for server, tool in self.tools:
            schemas.append(
                {
                    "name": qualified_name(server, tool.name),
                    "server": server,
                    "tool": tool.name,
                    "description": tool.description or f"{tool.name} on {server}",
                    "input_schema": tool.input_schema or {"type": "object", "properties": {}},
                    "annotations": (
                        tool.annotations.model_dump(exclude_none=True)
                        if getattr(tool, "annotations", None) is not None
                        else None
                    ),
                }
            )
        return schemas

    def openai_tools(self, *, include_writes: bool = True) -> list[dict[str, Any]]:
        """Return tool definitions in OpenAI/Groq function-calling format."""
        tools = []
        for schema in self.tool_schemas():
            annotations = schema.get("annotations") or {}
            if not include_writes and annotations.get("read_only_hint") is False:
                continue
            parameters = schema["input_schema"] or {"type": "object", "properties": {}}
            if "type" not in parameters:
                parameters = {"type": "object", **parameters}
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": schema["name"],
                        "description": schema["description"],
                        "parameters": parameters,
                    },
                }
            )
        return tools

    # ------------------------------------------------------------------ #
    # Resolution
    # ------------------------------------------------------------------ #
    def resolve_tool(self, name: str) -> tuple[ServerConnection, Tool]:
        """Resolve ``server__tool``, ``server.tool`` or an unambiguous bare name."""
        if not name or not str(name).strip():
            raise KeyError("Tool name must be a non-empty string")
        cleaned = str(name).strip()
        server_name, tool_name = _split_qualified(cleaned)

        if server_name:
            connection = self.servers.get(server_name)
            if connection is None:
                raise KeyError(f"Unknown MCP server: {server_name}")
            tool = connection.tool(tool_name)
            if tool is None:
                raise KeyError(f"Server '{server_name}' has no tool '{tool_name}'")
            return connection, tool

        matches = [
            (connection, connection.tool(cleaned))
            for connection in self.servers.values()
            if connection.tool(cleaned) is not None
        ]
        if not matches:
            raise KeyError(f"Unknown MCP tool: {cleaned}")
        if len(matches) > 1:
            owners = ", ".join(connection.name for connection, _ in matches)
            raise KeyError(f"Tool '{cleaned}' is ambiguous; qualify it with one of: {owners}")
        connection, tool = matches[0]
        assert tool is not None
        return connection, tool

    def server_for_uri(self, uri: str, server: str | None = None) -> str:
        """Pick the owning server for a resource URI."""
        if server:
            if server not in self.servers:
                raise KeyError(f"Unknown MCP server: {server}")
            return server
        scheme = str(uri).split("://", 1)[0].lower()
        owner = SCHEME_OWNERS.get(scheme)
        if owner and owner in self.servers:
            return owner
        candidates = [
            name
            for name, resources in (
                (connection.name, connection.resources) for connection in self.servers.values()
            )
            if any(resource.uri == uri for resource in resources)
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(self.servers) == 1:
            return next(iter(self.servers))
        raise KeyError(f"Cannot determine which MCP server owns resource: {uri}")

    # ------------------------------------------------------------------ #
    # Calls
    # ------------------------------------------------------------------ #
    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ToolCallResult:
        """Call one MCP tool and normalise success/error/text/JSON."""
        started = time.perf_counter()
        try:
            connection, tool = self.resolve_tool(name)
        except KeyError as exc:
            return ToolCallResult(
                server="",
                tool=str(name),
                ok=False,
                error=str(exc),
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        try:
            result = await connection.client.call_tool(tool.name, arguments or {})
        except Exception as exc:  # transport/protocol failure, not a tool error
            logger.error("MCP call failed: %s.%s: %s", connection.name, tool.name, exc)
            return ToolCallResult(
                server=connection.name,
                tool=tool.name,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        duration_ms = (time.perf_counter() - started) * 1000
        text = _content_text(result)
        raw_error = bool(getattr(result, "is_error", False))
        structured = getattr(result, "structured_content", None)
        data = structured if structured is not None else _json_or_text(text)

        error = None
        if raw_error:
            error = text or "MCP tool returned isError=true"
            # Servers wrap the JSON payload as "Error executing tool X: {...}".
            payload = _json_or_text(text)
            if not isinstance(payload, dict):
                brace = text.find("{")
                if brace >= 0:
                    payload = _json_or_text(text[brace:])
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                error = payload["error"]
        elif isinstance(data, dict) and data.get("success") is False:
            # Defensive: a server that returns a soft error without isError.
            raw_error = True
            error = str(data.get("error") or "Tool reported success=false")

        return ToolCallResult(
            server=connection.name,
            tool=tool.name,
            ok=not raw_error,
            data=data,
            text=text,
            error=error,
            duration_ms=duration_ms,
            raw_is_error=raw_error,
        )

    async def read_resource(self, uri: str, *, server: str | None = None) -> ResourceResult:
        """Read an MCP resource, choosing its owning server when omitted."""
        started = time.perf_counter()
        try:
            server_name = self.server_for_uri(uri, server)
            connection = self.servers[server_name]
        except KeyError as exc:
            return ResourceResult(
                server=server or "",
                uri=uri,
                ok=False,
                error=str(exc),
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        try:
            result = await connection.client.read_resource(uri)
        except Exception as exc:
            logger.warning("MCP resource failed: %s: %s", uri, exc)
            return ResourceResult(
                server=server_name,
                uri=uri,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        parts: list[str] = []
        mime_type = None
        for content in getattr(result, "contents", []) or []:
            mime_type = getattr(content, "mime_type", None) or mime_type
            text = getattr(content, "text", None)
            if text is None:
                blob = getattr(content, "blob", None)
                text = f"[binary resource {len(blob or '')} bytes]"
            parts.append(text)
        body = "\n".join(parts)
        return ResourceResult(
            server=server_name,
            uri=uri,
            ok=True,
            text=body,
            mime_type=mime_type,
            data=_json_or_text(body),
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    # ------------------------------------------------------------------ #
    # Presentation helpers
    # ------------------------------------------------------------------ #
    def describe_tools(self) -> list[dict[str, Any]]:
        """Compact tool list for the CLI ``--discover`` view."""
        return [
            {
                "name": schema["name"],
                "server": schema["server"],
                "tool": schema["tool"],
                "description": (schema["description"] or "").splitlines()[0][:110],
                "parameters": sorted((schema["input_schema"].get("properties") or {}).keys()),
                "read_only": (schema["annotations"] or {}).get("read_only_hint"),
            }
            for schema in self.tool_schemas()
        ]

    def describe_resources(self) -> list[dict[str, Any]]:
        rows = [
            {
                "server": server,
                "uri": resource.uri,
                "name": resource.name,
                "description": (resource.description or "")[:110],
                "mime_type": resource.mime_type,
                "template": False,
            }
            for server, resource in self.resources
        ]
        rows.extend(
            {
                "server": server,
                "uri": template.uri_template,
                "name": template.name,
                "description": (template.description or "")[:110],
                "mime_type": template.mime_type,
                "template": True,
            }
            for server, template in self.resource_templates
        )
        return rows

    def summary_line(self) -> str:
        servers = ", ".join(
            f"{connection.name} ({len(connection.tools)} tools, "
            f"{len(connection.resources) + len(connection.resource_templates)} resources)"
            for connection in self.servers.values()
        )
        return f"{len(self.servers)} MCP servers connected: {servers}"
