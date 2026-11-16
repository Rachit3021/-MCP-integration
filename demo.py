"""
demo.py
=======

A guided tour of the Milestone 2 MCP architecture. Every command starts the
real servers over stdio and talks JSON-RPC 2.0 through ``MCPClientManager``.

Commands
--------
    python demo.py discover                  # servers, tools, resources, templates
    python demo.py resources                 # read real MCP resources from both servers
    python demo.py batch --job JOB-001       # rank resumes, optionally persist scores
    python demo.py watch                     # baseline -> add file -> diff -> remove
    python demo.py agent --job JOB-001       # full LangGraph agent run (offline by default)
    python demo.py all                       # everything, in demo order

Use ``--llm`` with ``agent``/``all`` to use Groq when ``GROQ_API_KEY`` is set.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from matching_agent import MatchingAgent, _configure_logging
from mcp_client_manager import MCPClientManager
from mcp_config import load_config

PROJECT_ROOT = Path(__file__).resolve().parent


def banner(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}", flush=True)


def _say(message: str) -> None:
    print(message, flush=True)


async def cmd_discover(config: dict[str, Any], args: argparse.Namespace) -> int:
    banner("1. MCP discovery: one client, two servers")
    async with MCPClientManager(config) as manager:
        _say(manager.summary_line())
        for server in manager.catalog()["servers"]:
            info = server.get("server_info") or {}
            _say(
                f"\n[{server['name']}] {info.get('name')} v{info.get('version')} "
                f"(protocol {server.get('protocol_version')})"
            )
            _say("  tools:     " + ", ".join(server["tools"]))
            _say("  resources: " + (", ".join(server["resources"]) or "-"))
            _say("  templates: " + (", ".join(server["resource_templates"]) or "-"))
        if args.json:
            print(json.dumps(manager.catalog(), indent=2, default=str))
    return 0


async def cmd_resources(config: dict[str, Any], args: argparse.Namespace) -> int:
    banner("2. Resources: application-controlled context")
    uris = [
        ("resumes://catalog", "resume files"),
        ("resumes://summary", "aggregate skills"),
        ("jobs://catalog", "open roles"),
        ("jobs://stats", "database stats"),
        ("workspace://stats", "server stats"),
        ("jobs://matches", "saved matches"),
    ]
    snapshot: dict[str, Any] = {}
    async with MCPClientManager(config) as manager:
        for uri, label in uris:
            result = await manager.read_resource(uri)
            status = "ok" if result.ok else f"ERROR: {result.error}"
            _say(f"  {uri:<28} {label:<20} {status} ({result.duration_ms:.0f} ms)")
            if result.ok:
                snapshot[uri] = result.data

        sample = next(
            (
                entry["name"]
                for entry in (snapshot.get("resumes://catalog") or {}).get("files", [])
                if entry.get("name", "").endswith((".txt", ".md"))
            ),
            None,
        )
        if sample:
            uri = f"resumes://file/{sample}"
            result = await manager.read_resource(uri)
            _say(f"  {uri:<28} {'template read':<20} {'ok' if result.ok else result.error}")

        payload = json.dumps(snapshot, indent=2, ensure_ascii=False, default=str)
        written = await manager.call_tool(
            "filesystem__write_file",
            {"filepath": "reports/mcp_resource_snapshot.json", "content": payload},
        )
        _say(
            f"\n  snapshot -> reports/mcp_resource_snapshot.json "
            f"({'written' if written.ok else written.error})"
        )
    return 0


async def _job(config: dict[str, Any], manager: MCPClientManager, job_id: str) -> dict[str, Any]:
    result = await manager.read_resource(f"jobs://job/{job_id}")
    if not result.ok or not isinstance(result.data, dict):
        raise SystemExit(f"Could not load {job_id}: {result.error}")
    return result.data.get("job", result.data)


async def cmd_batch(config: dict[str, Any], args: argparse.Namespace) -> int:
    banner(f"3. Batch processing resumes against {args.job}")
    run_id = "demo-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    async with MCPClientManager(config) as manager:
        job = await _job(config, manager, args.job)
        keywords = list(job.get("required_skills") or [])
        _say(f"  role: {job.get('title')} at {job.get('company')}")
        _say(f"  keywords: {', '.join(keywords)}")
        result = await manager.call_tool(
            "filesystem__batch_process",
            {
                "directory": str(config["resumes"]["directory"]),
                "operations": ["metadata", "skills", "match"],
                "keywords": keywords,
            },
        )
        if not result.ok:
            _say(f"  batch failed: {result.error}")
            return 1
        data = result.data or {}
        _say(
            f"  processed {data.get('files_processed')} files in {data.get('duration_ms')} ms "
            f"(concurrency {data.get('concurrency')})"
        )
        _say(f"\n  {'#':>2}  {'candidate':<28} {'score':>6}  matched")
        for item in data.get("results", []):
            match = item.get("match") or {}
            _say(
                f"  {item.get('rank', 0):>2}  {item.get('file', '?'):<28} "
                f"{match.get('score', 0):>5.1f}%  "
                f"{', '.join(match.get('matched', [])) or '-'}"
            )

        if args.save:
            saved = 0
            for item in data.get("results", []):
                match = item.get("match") or {}
                response = await manager.call_tool(
                    "jobs__save_match_result",
                    {
                        "job_id": args.job,
                        "resume_name": item.get("file"),
                        "score": match.get("score", 0),
                        "matched_skills": match.get("matched", []),
                        "run_id": run_id,
                    },
                )
                saved += 1 if response.ok else 0
            _say(f"\n  persisted {saved} results on the job-board server (run {run_id})")

    return 0


async def cmd_watch(config: dict[str, Any], args: argparse.Namespace) -> int:
    banner("4. watch_directory: detect new and removed resumes")
    inbox = Path(str(config["workspace"]["root"])) / "inbox" / "demo_watch"
    inbox.mkdir(parents=True, exist_ok=True)
    for existing in inbox.glob("*.txt"):
        existing.unlink()

    source = next(
        (
            path
            for path in (Path(str(config["workspace"]["root"])) / "resumes").glob("*.txt")
        ),
        None,
    )
    if source is None:
        _say("  no .txt resume found to copy")
        return 1
    copied = inbox / source.name

    async with MCPClientManager(config) as manager:
        baseline = await manager.call_tool(
            "filesystem__watch_directory", {"directory": "inbox/demo_watch", "extensions": [".txt"]}
        )
        _say(f"  baseline: {baseline.data.get('counts') if baseline.ok else baseline.error}")

        shutil.copy2(source, copied)
        changed = await manager.call_tool(
            "filesystem__watch_directory", {"directory": "inbox/demo_watch", "extensions": [".txt"]}
        )
        if changed.ok:
            _say(
                f"  after add: new={changed.data.get('counts', {}).get('new')} "
                f"-> {[entry['name'] for entry in changed.data.get('new', [])]}"
            )

        copied.unlink()
        removed = await manager.call_tool(
            "filesystem__watch_directory", {"directory": "inbox/demo_watch", "extensions": [".txt"]}
        )
        if removed.ok:
            _say(
                f"  after remove: removed={removed.data.get('counts', {}).get('removed')} "
                f"-> {[entry['name'] for entry in removed.data.get('removed', [])]}"
            )

        if args.json:
            print(
                json.dumps(
                    {"baseline": baseline.data, "added": changed.data, "removed": removed.data},
                    indent=2,
                    default=str,
                )
            )
    return 0


async def cmd_agent(config: dict[str, Any], args: argparse.Namespace) -> int:
    banner("5. LangGraph agent over MCP")
    agent = MatchingAgent(config, offline=not args.llm)
    prompt = (
        f"Find the best candidates for {args.job}. Compare every resume, rank them by "
        "required-skill coverage, save the results and write a shortlist report."
    )
    result = await agent.run(prompt, job_id=args.job)
    _say("\n" + result.answer)
    _say(f"\n  planner: {result.planner}")
    _say(f"  report:  {result.report_path}")
    _say(f"  trace:   {result.trace_path}")
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, default=str))
    return 0


async def run_all(config: dict[str, Any], args: argparse.Namespace) -> int:
    started = time.perf_counter()
    await cmd_discover(config, args)
    await cmd_resources(config, args)
    await cmd_batch(config, args)
    await cmd_watch(config, args)
    await cmd_agent(config, args)
    _say(f"\nAll demo steps completed in {time.perf_counter() - started:.1f}s")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Milestone 2 MCP demo runner")
    parser.add_argument("--config", help="Path to mcp_config.json")
    parser.add_argument("--job", default="JOB-001", help="Job ID (default: JOB-001)")
    parser.add_argument("--llm", action="store_true", help="Use Groq instead of the offline planner")
    parser.add_argument("--save", action="store_true", help="Persist batch scores in the jobs DB")
    parser.add_argument("--json", action="store_true", help="Print raw JSON where supported")
    parser.add_argument("--log-level", default="WARNING", help="Server log level (default: WARNING)")
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["discover", "resources", "batch", "watch", "agent", "all"],
    )
    args = parser.parse_args(argv)

    config = load_config(args.config, overrides={"logging": {"level": args.log_level}})
    _configure_logging(str(config["logging"]["level"]))

    if args.command == "batch":
        args.save = True  # the batch demo always shows persistence

    command = {
        "discover": cmd_discover,
        "resources": cmd_resources,
        "batch": cmd_batch,
        "watch": cmd_watch,
        "agent": cmd_agent,
        "all": run_all,
    }[args.command]

    try:
        code = asyncio.run(command(config, args))
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    main()
