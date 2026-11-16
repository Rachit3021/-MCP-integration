"""Part A server tests: tools, resources, watch/batch and sandboxing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp import Client

from filesystem_mcp_server import FilesystemMCPServer
from mcp_test_utils import payload, run, make_config


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "resumes").mkdir()
    (tmp_path / "alice.txt").write_text(
        "Alice Smith\nPython, FastAPI, Docker and Kubernetes.", encoding="utf-8"
    )
    (tmp_path / "bob.txt").write_text("Bob Jones\nJava and Spring Boot.", encoding="utf-8")
    return tmp_path


def test_filesystem_tools_resources_and_errors(workspace: Path) -> None:
    config = make_config(workspace)
    impl = FilesystemMCPServer(config)

    async def scenario() -> dict:
        async with Client(impl.server, mode="legacy") as client:
            tools = await client.list_tools()
            resources = await client.list_resources()
            templates = await client.list_resource_templates()

            read = await client.call_tool("read_file", {"filepath": "alice.txt"})
            listing = await client.call_tool("list_files", {"directory": "."})
            search = await client.call_tool(
                "search_in_file", {"filepath": "alice.txt", "keyword": "FastAPI"}
            )
            written = await client.call_tool(
                "write_file", {"filepath": "notes/out.md", "content": "# Notes\nFastAPI"}
            )
            batch = await client.call_tool(
                "batch_process",
                {
                    "directory": ".",
                    "operations": ["metadata", "skills", "match"],
                    "keywords": ["Python", "FastAPI", "Docker"],
                },
            )
            traversal = await client.call_tool("read_file", {"filepath": "../secret.txt"})
            missing = await client.call_tool("read_file", {"filepath": "missing.txt"})
            config_result = await client.call_tool("get_config", {})
            stats = await client.call_tool("get_stats", {})

            catalog = await client.read_resource("resumes://catalog")
            notes = await client.read_resource("workspace://file/notes/out.md")
            config_resource = await client.read_resource("workspace://config")
            unknown = None
            try:
                await client.read_resource("resumes://nope")
            except Exception as exc:  # ResourceError / MCPError
                unknown = str(exc)

            return {
                "tools": [tool.name for tool in tools.tools],
                "resources": [resource.uri for resource in resources.resources],
                "templates": [template.uri_template for template in templates.resource_templates],
                "read": payload(read),
                "listing": payload(listing),
                "search": payload(search),
                "written": payload(written),
                "batch": payload(batch),
                "traversal_error": traversal.is_error,
                "traversal_text": traversal.content[0].text,
                "missing_error": missing.is_error,
                "config": payload(config_result),
                "stats": payload(stats),
                "catalog": json.loads(catalog.contents[0].text),
                "notes": notes.contents[0].text,
                "config_resource": json.loads(config_resource.contents[0].text),
                "unknown": unknown,
            }

    data = run(scenario())

    assert data["tools"] == [
        "read_file", "list_files", "write_file", "search_in_file",
        "watch_directory", "batch_process", "get_config", "get_stats",
    ]
    assert data["resources"] == [
        "resumes://catalog", "resumes://summary", "workspace://config", "workspace://stats",
    ]
    assert data["templates"] == ["resumes://file/{name}", "workspace://file/{+path}"]

    assert data["read"]["success"] is True
    assert "FastAPI" in data["read"]["content"]
    assert data["read"]["metadata"]["path"] == "alice.txt"

    names = {entry["name"] for entry in data["listing"]["files"]}
    assert {"alice.txt", "bob.txt"} <= names

    assert data["search"]["success"] is True
    assert data["search"]["match_count"] == 1
    assert data["search"]["matches"][0]["match"].lower() == "fastapi"

    assert data["written"]["success"] is True
    assert (workspace / "notes/out.md").read_text(encoding="utf-8") == "# Notes\nFastAPI"

    assert data["batch"]["success"] is True
    assert data["batch"]["files_processed"] == 2
    assert data["batch"]["results"][0]["file"] == "alice.txt"
    assert data["batch"]["results"][0]["match"]["score"] == 100.0

    assert data["traversal_error"] is True
    assert "outside the workspace" in data["traversal_text"]
    assert data["missing_error"] is True

    assert set(data["config"]["tools"]) == set(data["tools"])
    assert data["stats"]["success"] is True
    assert data["stats"]["tool_calls"] >= 8

    assert data["catalog"]["count"] == 0  # empty resumes directory
    assert data["notes"] == "# Notes\nFastAPI"
    assert data["config_resource"]["success"] is True
    assert data["unknown"] and "nope" in data["unknown"]


def test_watch_directory_baseline_new_modified_removed(workspace: Path) -> None:
    config = make_config(workspace)
    impl = FilesystemMCPServer(config)

    async def scenario() -> tuple[dict, dict, dict, dict]:
        async with Client(impl.server, mode="legacy") as client:
            baseline = await client.call_tool(
                "watch_directory", {"directory": "resumes", "extensions": [".txt"]}
            )
            (workspace / "resumes" / "new_candidate.txt").write_text(
                "Python developer", encoding="utf-8"
            )
            added = await client.call_tool(
                "watch_directory", {"directory": "resumes", "extensions": [".txt"]}
            )
            unchanged = await client.call_tool(
                "watch_directory", {"directory": "resumes", "extensions": [".txt"]}
            )
            (workspace / "resumes" / "new_candidate.txt").unlink()
            removed = await client.call_tool(
                "watch_directory", {"directory": "resumes", "extensions": [".txt"]}
            )
            return (
                payload(baseline),
                payload(added),
                payload(unchanged),
                payload(removed),
            )

    baseline, added, unchanged, removed = run(scenario())

    assert baseline["baseline"] is True
    assert baseline["counts"]["current"] == 0

    assert added["baseline"] is False
    assert added["changed"] is True
    assert added["counts"]["new"] == 1
    assert added["new"][0]["name"] == "new_candidate.txt"

    assert unchanged["changed"] is False
    assert unchanged["counts"]["new"] == 0

    assert removed["changed"] is True
    assert removed["counts"]["removed"] == 1
    assert removed["removed"][0]["name"] == "new_candidate.txt"


def test_batch_process_validates_operations_and_keywords(workspace: Path) -> None:
    impl = FilesystemMCPServer(make_config(workspace))

    async def scenario() -> tuple[Any, Any]:
        async with Client(impl.server, mode="legacy") as client:
            bad_op = await client.call_tool(
                "batch_process", {"directory": ".", "operations": ["explode"]}
            )
            no_keywords = await client.call_tool(
                "batch_process", {"directory": ".", "operations": ["match"]}
            )
            return bad_op, no_keywords

    bad_op, no_keywords = run(scenario())
    assert bad_op.is_error is True
    assert "Unknown operation" in bad_op.content[0].text
    assert no_keywords.is_error is True
    assert "keywords must be a non-empty list" in no_keywords.content[0].text


def test_config_validation_rejects_bad_limits(tmp_path: Path) -> None:
    from mcp_config import load_config

    with pytest.raises(ValueError, match="batch.concurrency"):
        load_config(overrides={"batch": {"concurrency": 0}})
    with pytest.raises(ValueError, match="mcp_timeout_seconds"):
        load_config(overrides={"agent": {"mcp_timeout_seconds": 0}})
