"""Bonus server tests: SQLite-backed job board over MCP."""

from __future__ import annotations

import json
from pathlib import Path

from mcp import Client

from job_board_mcp_server import JobBoardServer
from mcp_test_utils import payload, run, make_config


def test_job_board_tools_and_resources(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    impl = JobBoardServer(config)

    async def scenario() -> dict:
        async with Client(impl.server, mode="legacy") as client:
            tools = await client.list_tools()
            resources = await client.list_resources()
            templates = await client.list_resource_templates()

            python_jobs = await client.call_tool(
                "search_jobs", {"query": "Python", "limit": 10}
            )
            remote_jobs = await client.call_tool(
                "search_jobs", {"location": "Remote", "limit": 10}
            )
            junior_jobs = await client.call_tool(
                "search_jobs", {"experience": 2, "limit": 10}
            )
            job = await client.call_tool("get_job", {"job_id": "job-001"})
            missing = await client.call_tool("get_job", {"job_id": "JOB-999"})

            saved_one = await client.call_tool(
                "save_match_result",
                {
                    "job_id": "JOB-001",
                    "resume_name": "resume_john_doe.pdf",
                    "score": 100,
                    "matched_skills": ["Python", "AWS"],
                    "run_id": "pytest",
                },
            )
            saved_two = await client.call_tool(
                "save_match_result",
                {
                    "job_id": "JOB-001",
                    "resume_name": "resume_rahul_verma.txt",
                    "score": 50,
                    "matched_skills": ["Python"],
                    "run_id": "pytest",
                },
            )
            updated = await client.call_tool(
                "save_match_result",
                {
                    "job_id": "JOB-001",
                    "resume_name": "resume_john_doe.pdf",
                    "score": 99.5,
                    "matched_skills": ["Python", "AWS", "Docker"],
                    "run_id": "pytest",
                },
            )
            bad_score = await client.call_tool(
                "save_match_result",
                {"job_id": "JOB-001", "resume_name": "x.txt", "score": 101},
            )
            results = await client.call_tool(
                "list_match_results", {"job_id": "JOB-001", "run_id": "pytest"}
            )
            matches_resource = await client.read_resource("jobs://matches/JOB-001")
            catalog = await client.read_resource("jobs://catalog")
            stats = await client.call_tool("get_stats", {})
            cleared = await client.call_tool(
                "clear_match_results", {"job_id": "JOB-001"}
            )
            after_clear = await client.call_tool(
                "list_match_results", {"job_id": "JOB-001", "run_id": "pytest"}
            )
            unknown = None
            try:
                await client.read_resource("jobs://job/JOB-999")
            except Exception as exc:
                unknown = str(exc)

            return {
                "tools": [tool.name for tool in tools.tools],
                "resources": [resource.uri for resource in resources.resources],
                "templates": [template.uri_template for template in templates.resource_templates],
                "python": payload(python_jobs),
                "remote": payload(remote_jobs),
                "junior": payload(junior_jobs),
                "job": payload(job),
                "missing": missing.is_error,
                "saved_one": payload(saved_one),
                "saved_two": payload(saved_two),
                "updated": payload(updated),
                "bad_score": bad_score.is_error,
                "results": payload(results),
                "matches_resource": json.loads(matches_resource.contents[0].text),
                "catalog": json.loads(catalog.contents[0].text),
                "stats": payload(stats),
                "cleared": payload(cleared),
                "after_clear": payload(after_clear),
                "unknown": unknown,
            }

    data = run(scenario())

    assert data["tools"] == [
        "search_jobs", "get_job", "save_match_result", "list_match_results",
        "clear_match_results", "get_config", "get_stats",
    ]
    assert data["resources"] == [
        "jobs://catalog", "jobs://matches", "jobs://stats", "jobs://config",
    ]
    assert data["templates"] == [
        "jobs://job/{job_id}", "jobs://matches/{job_id}",
    ]

    assert data["python"]["count"] >= 3
    assert {job["job_id"] for job in data["python"]["jobs"]} >= {"JOB-001", "JOB-004", "JOB-007"}
    assert [job["job_id"] for job in data["remote"]["jobs"]] == ["JOB-003"]
    assert all(job["min_experience"] <= 2 for job in data["junior"]["jobs"])

    assert data["job"]["success"] is True
    assert data["job"]["job"]["title"] == "Senior Python Engineer"
    assert data["missing"] is True

    assert data["saved_one"]["saved"] is True
    assert data["saved_two"]["saved"] is True
    assert data["updated"]["score"] == 99.5
    assert data["bad_score"] is True
    assert data["results"]["count"] == 2
    assert data["results"]["results"][0]["resume_name"] == "resume_john_doe.pdf"
    assert data["results"]["results"][0]["score"] == 99.5
    assert data["results"]["results"][0]["matched_skills"] == ["Python", "AWS", "Docker"]

    assert data["matches_resource"]["count"] == 2
    assert data["catalog"]["count"] == 7
    assert data["stats"]["success"] is True
    assert data["stats"]["job_count"] == 7
    assert data["stats"]["match_result_count"] == 2

    assert data["cleared"]["deleted"] == 2
    assert data["after_clear"]["count"] == 0
    assert data["unknown"] and "JOB-999" in data["unknown"]


def test_job_board_search_by_skill_and_config(tmp_path: Path) -> None:
    impl = JobBoardServer(make_config(tmp_path))

    async def scenario() -> tuple[dict, dict, dict]:
        async with Client(impl.server, mode="legacy") as client:
            skill = await client.call_tool("search_jobs", {"skill": "Terraform"})
            config_result = await client.call_tool("get_config", {})
            config_resource = await client.read_resource("jobs://config")
            return payload(skill), payload(config_result), json.loads(config_resource.contents[0].text)

    skill, config_result, config_resource = run(scenario())
    assert skill["count"] == 1
    assert skill["jobs"][0]["job_id"] == "JOB-003"
    assert config_result["server"]["name"] == "job-board-mcp"
    assert config_result["job_count"] == 7
    assert config_resource["database"].endswith("jobs.db")


def test_empty_match_resource_is_not_an_error(tmp_path: Path) -> None:
    impl = JobBoardServer(make_config(tmp_path))

    async def scenario() -> dict:
        async with Client(impl.server, mode="legacy") as client:
            result = await client.read_resource("jobs://matches/JOB-002")
            return json.loads(result.contents[0].text)

    data = run(scenario())
    assert data["success"] is True
    assert data["count"] == 0
