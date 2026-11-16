"""Wire-level JSON-RPC 2.0 compliance tests.

These deliberately bypass the MCP Python client: they spawn the server and speak
newline-delimited JSON-RPC directly, so the test proves the protocol itself is
compliant rather than merely proving our client wrapper works.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent


class RawClient:
    def __init__(self, workspace: Path) -> None:
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(PROJECT_ROOT / "filesystem_mcp_server.py"),
                "--workspace",
                str(workspace),
                "--log-level",
                "ERROR",
            ],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line)

    def send(self, payload: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def send_raw(self, payload: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(payload + "\n")
        self.process.stdin.flush()

    def receive(self, timeout: float = 15.0) -> dict[str, Any]:
        return json.loads(self.lines.get(timeout=timeout))

    def request(self, request_id: int, method: str, params: dict | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.send(payload)
        return self.receive()

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.process.kill()
            self.process.wait(timeout=10)


@pytest.fixture()
def raw_client(tmp_path: Path):
    (tmp_path / "sample.txt").write_text("Python and Docker", encoding="utf-8")
    client = RawClient(tmp_path)
    try:
        yield client
    finally:
        client.close()


def test_initialize_and_capabilities(raw_client: RawClient) -> None:
    response = raw_client.request(
        1,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest-raw", "version": "1.0"},
        },
    )
    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 1
    result = response["result"]
    assert result["serverInfo"]["name"] == "filesystem-mcp"
    assert result["protocolVersion"]
    assert {"tools", "resources"} <= set(result["capabilities"])

    raw_client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})


def test_discovery_call_and_standard_errors(raw_client: RawClient) -> None:
    raw_client.request(
        1,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest-raw", "version": "1.0"},
        },
    )
    raw_client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    tools = raw_client.request(2, "tools/list")
    assert tools["id"] == 2
    assert [tool["name"] for tool in tools["result"]["tools"]][:1] == ["read_file"]
    assert all("inputSchema" in tool for tool in tools["result"]["tools"])

    resources = raw_client.request(3, "resources/list")
    assert "resumes://catalog" in [item["uri"] for item in resources["result"]["resources"]]

    templates = raw_client.request(4, "resources/templates/list")
    assert "workspace://file/{+path}" in [
        item["uriTemplate"] for item in templates["result"]["resourceTemplates"]
    ]

    call = raw_client.request(
        5, "tools/call", {"name": "read_file", "arguments": {"filepath": "sample.txt"}}
    )
    assert call["id"] == 5
    text = call["result"]["content"][0]["text"]
    assert json.loads(text)["success"] is True
    assert "Python and Docker" in json.loads(text)["content"]

    unknown_method = raw_client.request(6, "does/not/exist")
    assert unknown_method["error"]["code"] == -32601
    assert unknown_method["id"] == 6

    invalid_params = raw_client.request(7, "tools/call", {})
    assert invalid_params["error"]["code"] == -32602

    unknown_resource = raw_client.request(
        8, "resources/read", {"uri": "resumes://missing.pdf"}
    )
    assert unknown_resource["error"]["code"] == -32602
    assert "missing.pdf" in unknown_resource["error"]["message"]

    unknown_tool = raw_client.request(
        9, "tools/call", {"name": "no_such_tool", "arguments": {}}
    )
    assert unknown_tool["result"]["isError"] is True
    assert "Unknown tool" in unknown_tool["result"]["content"][0]["text"]


def test_malformed_json_does_not_kill_the_server(raw_client: RawClient) -> None:
    raw_client.request(
        1,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest-raw", "version": "1.0"},
        },
    )
    raw_client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # The MCP SDK's stdio transport drops frames it cannot parse. The important
    # guarantee is that a bad frame does not crash or poison the stream.
    raw_client.send_raw("{not valid json")
    alive = raw_client.request(2, "tools/list")
    assert alive["id"] == 2
    assert alive["result"]["tools"]
