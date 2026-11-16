"""Part B tests: multi-server client manager and LangGraph agent."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from mcp_client_manager import MCPClientManager, qualified_name
from mcp_config import server_specs
from mcp_test_utils import copy_resumes, run, make_config
from matching_agent import MatchingAgent


def test_manager_discovers_and_calls_both_servers(tmp_path: Path) -> None:
    copy_resumes(tmp_path)
    config = make_config(tmp_path)

    async def scenario() -> dict:
        async with MCPClientManager(config) as manager:
            assert {connection.name for connection in manager.servers.values()} == {
                "filesystem",
                "jobs",
            }
            tools = manager.openai_tools()
            batch = await manager.call_tool(
                "filesystem__batch_process",
                {
                    "directory": "resumes",
                    "operations": ["metadata", "match"],
                    "keywords": ["Python", "FastAPI"],
                },
            )
            jobs = await manager.call_tool("jobs__search_jobs", {"query": "Python"})
            missing = await manager.call_tool("filesystem__read_file", {"filepath": "nope.txt"})
            unknown = await manager.call_tool("no_such_tool", {})
            resource = await manager.read_resource("jobs://job/JOB-001")
            catalog = await manager.read_resource("resumes://catalog")
            return {
                "summary": manager.summary_line(),
                "catalog": manager.catalog(),
                "tools": tools,
                "batch": batch,
                "jobs": jobs,
                "missing": missing,
                "unknown": unknown,
                "resource": resource,
                "resume_catalog": catalog,
            }

    data = run(scenario())

    assert "2 MCP servers connected" in data["summary"]
    assert data["catalog"]["tool_count"] == 15
    assert data["catalog"]["resource_count"] == 8
    assert data["catalog"]["resource_template_count"] == 4

    names = {tool["function"]["name"] for tool in data["tools"]}
    assert "filesystem__read_file" in names
    assert "filesystem__batch_process" in names
    assert "jobs__save_match_result" in names
    assert len(names) == 15
    assert all(tool["function"]["parameters"]["type"] == "object" for tool in data["tools"])

    assert data["batch"].ok is True
    assert data["batch"].data["results"][0]["file"] == "resume_john_doe.pdf"
    assert data["jobs"].ok is True
    assert data["jobs"].data["count"] >= 3

    assert data["missing"].ok is False
    assert "File not found" in data["missing"].error
    assert data["unknown"].ok is False
    assert "Unknown MCP tool" in data["unknown"].error

    assert data["resource"].ok is True
    assert data["resource"].data["job"]["job_id"] == "JOB-001"
    assert data["resume_catalog"].data["count"] == 6


def test_manager_openai_names_and_server_specs(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    specs = server_specs(config)
    assert set(specs) == {"filesystem", "jobs"}
    assert specs["filesystem"]["args"] == ["filesystem_mcp_server.py"]
    assert specs["jobs"]["env"]["JOBS_DB"] == str(tmp_path / "data" / "jobs.db")
    assert qualified_name("filesystem", "read_file") == "filesystem__read_file"


def test_offline_agent_end_to_end(tmp_path: Path) -> None:
    copy_resumes(tmp_path)
    config = make_config(tmp_path)
    agent = MatchingAgent(config, offline=True)

    prompt = (
        "Find the best candidates for JOB-001. Compare every resume, rank them by "
        "required-skill coverage, save the results and write a shortlist report."
    )
    result = run(agent.run(prompt, job_id="JOB-001"))

    assert result.planner == "scripted"
    assert len(result.rankings) == 6
    assert result.rankings[0]["resume"] == "resume_john_doe.pdf"
    assert result.rankings[0]["score"] == 100.0
    assert result.rankings[1]["resume"] == "resume_rahul_verma.txt"
    assert result.rankings[1]["score"] == 50.0
    assert all(
        result.rankings[index]["score"] >= result.rankings[index + 1]["score"]
        for index in range(len(result.rankings) - 1)
    )

    assert result.persisted == 6
    assert "Ranked 6 candidates" in result.answer
    assert "resume_john_doe.pdf" in result.answer

    report = Path(result.report_path)
    if not report.is_absolute():
        report = Path(str(config["workspace"]["root"])) / str(result.report_path)
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    assert "Candidate Shortlist" in text
    assert "| 1 | `resume_john_doe.pdf` | 100.0%" in text
    assert "jobs__save_match_result" in text
    assert (Path(str(config["workspace"]["root"])) / "reports" / "latest.md").exists()

    trace = Path(result.trace_path)
    assert trace.exists()
    trace_data = json.loads(trace.read_text(encoding="utf-8"))
    assert trace_data["run_id"] == result.run_id
    assert trace_data["persisted"] == 6
    assert {"plan", "resource", "tool", "persist", "report", "verify"} <= {
        event["type"] for event in trace_data["events"]
    }

    db_path = Path(str(config["jobs"]["db_path"]))
    with sqlite3.connect(str(db_path)) as connection:
        rows = connection.execute(
            "SELECT resume_name, score FROM match_results WHERE job_id = 'JOB-001' ORDER BY score DESC"
        ).fetchall()
    assert len(rows) == 6
    assert rows[0][0] == "resume_john_doe.pdf"
    assert rows[0][1] == 100.0


def test_agent_does_not_import_filesystem_tools() -> None:
    source = (Path(__file__).resolve().parent / "matching_agent.py").read_text(encoding="utf-8")
    assert "import fs_tools" not in source
    assert "from fs_tools" not in source
    assert "filesystem_mcp_server" not in source
    assert "job_board_mcp_server" not in source
