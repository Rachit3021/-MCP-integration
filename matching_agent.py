"""
matching_agent.py
=================

Milestone 2, Part B: the resume-matching agent, rebuilt on LangGraph and MCP.

Milestone 1 imported ``fs_tools`` and called Python functions directly. This
version imports **no filesystem, PDF, DOCX or SQLite code**. It starts the MCP
servers as child processes and talks to them through standard JSON-RPC 2.0
using ``mcp_client_manager.MCPClientManager``.

LangGraph workflow
------------------
::

    START -> discover -> plan -> act -> plan -> ... -> finalize -> END
                          |                              ^
                          +----------- done -------------+

- ``discover``  reads MCP resources (job description, resume catalog, summary).
- ``plan``      asks the LLM (Groq/OpenAI-compatible) which MCP tools to call.
                A deterministic ``ScriptedPlanner`` runs the same workflow with
                no API key, which keeps demos and tests reproducible.
- ``act``       executes the tool calls through MCP and records observations.
- ``finalize``  ranks candidates, persists scores through the job-board MCP
                server, writes a Markdown report through the filesystem MCP
                server, and verifies it by reading a resource back.

Run
---
    python matching_agent.py --demo --offline
    python matching_agent.py --job JOB-001 --offline
    python matching_agent.py --prompt "Rank resumes for JOB-003" --offline
    python matching_agent.py --discover
    python matching_agent.py --require-llm          # fail if GROQ_API_KEY is empty

Artifacts: ``reports/shortlist_<job>_<timestamp>.md`` and a full JSON trace in
``runs/<run_id>.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import operator
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph

from mcp_client_manager import MCPClientManager, ToolCallResult, qualified_name
from mcp_config import load_config, server_specs

logger = logging.getLogger("matching_agent")

READ_TOOLS = {"read_file", "list_files", "search_in_file", "batch_process", "watch_directory"}
CONTEXT_RESOURCES = ("resumes://catalog", "resumes://summary", "jobs://config")


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
class AgentState(TypedDict, total=False):
    """Shared LangGraph state. Lists that accumulate use ``operator.add``."""

    prompt: str
    job_id: str
    run_id: str
    context: dict[str, Any]
    messages: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    observations: Annotated[list[dict[str, Any]], operator.add]
    events: Annotated[list[dict[str, Any]], operator.add]
    steps: int
    max_steps: int
    done: bool
    answer: str
    rankings: list[dict[str, Any]]
    report_path: str
    report_text: str
    persisted: int
    error: str


@dataclass
class PlannerDecision:
    """One planning step: either call tools or finish."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    message: str = ""
    messages: list[dict[str, Any]] | None = None


@dataclass
class AgentResult:
    run_id: str
    answer: str
    rankings: list[dict[str, Any]]
    report_path: str
    observations: list[dict[str, Any]]
    events: list[dict[str, Any]]
    persisted: int
    trace_path: str
    duration_seconds: float
    planner: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "planner": self.planner,
            "answer": self.answer,
            "rankings": self.rankings,
            "report_path": self.report_path,
            "persisted": self.persisted,
            "trace_path": self.trace_path,
            "duration_seconds": round(self.duration_seconds, 2),
            "tool_calls": [
                {
                    "name": observation["name"],
                    "ok": observation["ok"],
                    "duration_ms": observation["duration_ms"],
                    "error": observation.get("error"),
                }
                for observation in self.observations
            ],
            "events": len(self.events),
        }


# --------------------------------------------------------------------------- #
# Planners
# --------------------------------------------------------------------------- #
class ScriptedPlanner:
    """Deterministic two-step planner used offline and in tests.

    It proves the architecture without an API key: the same graph, the same MCP
    client, the same tools and the same persistence path as the LLM planner.
    """

    name = "scripted"

    def __init__(self) -> None:
        self.stage = 0

    async def plan(self, state: AgentState) -> PlannerDecision:
        job = (state.get("context") or {}).get("job") or {}
        if self.stage == 0:
            self.stage = 1
            keywords = list(job.get("required_skills") or [])
            if not keywords:
                keywords = ["Python"]
            return PlannerDecision(
                calls=[
                    {
                        "id": "scripted-batch",
                        "name": "filesystem__batch_process",
                        "arguments": {
                            "directory": str(self._config_value(state, "resumes", "resumes")),
                            "operations": ["metadata", "skills", "match"],
                            "keywords": keywords,
                        },
                    }
                ],
                message="Score every resume against the role's required skills.",
            )
        return PlannerDecision(done=True, message="Ranking complete; hand off to finalize.")

    @staticmethod
    def _config_value(state: AgentState, key: str, default: str) -> str:
        return str((state.get("context") or {}).get(key) or default)


class LLMPlanner:
    """Groq/OpenAI-compatible function-calling planner.

    MCP tool schemas are passed straight through as OpenAI ``tools``, which is
    the concrete payoff of MCP: no per-tool glue code on the agent side.
    """

    name = "llm"

    def __init__(self, manager: MCPClientManager, config: dict[str, Any]) -> None:
        from openai import OpenAI

        api_key = os.getenv("GROQ_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is empty; run with --offline instead")
        self.manager = manager
        self.config = config
        self.model = str(config["agent"]["model"])
        self.client = OpenAI(api_key=api_key, base_url=str(config["agent"]["base_url"]))
        self.tools = manager.openai_tools(include_writes=True)
        self.system_prompt = (
            "You are a resume-matching agent. You have MCP tools from two servers: "
            "a filesystem server (resumes, batch processing, reports) and a job-board "
            "server (job descriptions and saved match results).\n\n"
            "For a candidate ranking request:\n"
            "1. Call filesystem__batch_process on the resumes directory with operations "
            "['metadata','skills','match'] and the role's required skills as keywords.\n"
            "2. Inspect the scores, then stop and give a short summary.\n"
            "The runtime persists scores and writes the Markdown report after you finish, "
            "so do not call write_file or save_match_result yourself.\n"
            "Be concise. Never invent candidates or scores."
        )

    async def plan(self, state: AgentState) -> PlannerDecision:
        messages = list(state.get("messages") or [])
        if not messages:
            messages = [{"role": "user", "content": self._user_message(state)}]
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": self.system_prompt})

        response = await asyncio.to_thread(
            self.client.chat.completions.create,
            model=self.model,
            messages=messages,
            tools=self.tools,
            tool_choice="auto",
            temperature=float(self.config["agent"]["temperature"]),
        )
        message = response.choices[0].message
        assistant = message.model_dump(exclude_none=True)
        messages.append(assistant)

        calls: list[dict[str, Any]] = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            calls.append(
                {
                    "id": call.id,
                    "name": call.function.name,
                    "arguments": arguments,
                }
            )

        if calls:
            return PlannerDecision(calls=calls, message=assistant.get("content") or "", messages=messages)
        return PlannerDecision(done=True, message=assistant.get("content") or "", messages=messages)

    @staticmethod
    def _user_message(state: AgentState) -> str:
        context = state.get("context") or {}
        job = context.get("job") or {}
        catalog = context.get("resumes") or {}
        return json.dumps(
            {
                "request": state.get("prompt"),
                "job": job,
                "resume_catalog": {
                    "count": catalog.get("count"),
                    "files": [entry.get("name") for entry in catalog.get("files", [])],
                },
            },
            indent=2,
            default=str,
        )


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class MatchingAgent:
    """LangGraph + MCP resume-matching agent."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        offline: bool = False,
        require_llm: bool = False,
    ) -> None:
        self.config = config or load_config()
        self.offline = offline
        self.require_llm = require_llm
        self.manager: MCPClientManager | None = None
        self.planner: Any = None
        self.planner_name = "scripted" if offline else "llm"
        self.graph = self._build_graph()
        self._progress = True

    # ------------------------------------------------------------------ #
    # LangGraph wiring
    # ------------------------------------------------------------------ #
    def _build_graph(self):
        builder = StateGraph(AgentState)
        builder.add_node("discover", self._discover)
        builder.add_node("plan", self._plan)
        builder.add_node("act", self._act)
        builder.add_node("finalize", self._finalize)
        builder.add_edge(START, "discover")
        builder.add_edge("discover", "plan")
        builder.add_conditional_edges(
            "plan",
            self._route_after_plan,
            {"act": "act", "finalize": "finalize"},
        )
        builder.add_edge("act", "plan")
        builder.add_edge("finalize", END)
        return builder.compile()

    @staticmethod
    def _route_after_plan(state: AgentState) -> str:
        if state.get("done") or state.get("error"):
            return "finalize"
        if not state.get("pending"):
            return "finalize"
        if int(state.get("steps") or 0) >= int(state.get("max_steps") or 10):
            return "finalize"
        return "act"

    # ------------------------------------------------------------------ #
    # Nodes
    # ------------------------------------------------------------------ #
    async def _discover(self, state: AgentState) -> dict[str, Any]:
        assert self.manager is not None
        job_id = str(state.get("job_id") or self.config["agent"]["default_job_id"]).upper()
        events: list[dict[str, Any]] = []
        context: dict[str, Any] = {
            "job_id": job_id,
            "resumes": str(self.config["resumes"]["directory"]),
        }

        job = await self._resource_or_tool(
            f"jobs://job/{job_id}", "jobs__get_job", {"job_id": job_id}, events, unwrap="job"
        )
        if job is None:
            raise RuntimeError(f"Could not load job {job_id} from the job-board MCP server")
        context["job"] = job

        catalog = await self._read_json("resumes://catalog", events)
        context["catalog"] = catalog
        summary = await self._read_json("resumes://summary", events)
        context["summary"] = summary
        context["resume_files"] = [
            entry.get("path") for entry in (catalog or {}).get("files", []) if entry.get("path")
        ]

        if self._progress:
            _say(
                f"discovered JOB {job_id} ({job.get('title')}) and "
                f"{(catalog or {}).get('count', 0)} resumes from 2 MCP servers"
            )

        message = {
            "role": "user",
            "content": json.dumps(
                {
                    "request": state.get("prompt"),
                    "job_id": job_id,
                    "title": job.get("title"),
                    "required_skills": job.get("required_skills"),
                    "nice_to_have": job.get("nice_to_have"),
                    "resume_count": (catalog or {}).get("count"),
                },
                indent=2,
            ),
        }
        return {
            "job_id": job_id,
            "context": context,
            "events": events,
            "messages": [message],
        }

    async def _plan(self, state: AgentState) -> dict[str, Any]:
        assert self.manager is not None and self.planner is not None
        step = int(state.get("steps") or 0) + 1
        started = time.perf_counter()
        try:
            decision = await self.planner.plan(state)
        except Exception as exc:
            if self.offline:
                raise
            logger.exception("LLM planning failed, switching to the offline planner")
            self.planner = ScriptedPlanner()
            # If the batch call already ran, do not run it a second time.
            if any(
                observation.get("tool") == "batch_process" and observation.get("ok")
                for observation in (state.get("observations") or [])
            ):
                self.planner.stage = 1
            self.planner_name = "scripted (fallback)"
            self.config["_fallback_reason"] = f"{type(exc).__name__}: {exc}"
            decision = await self.planner.plan(state)

        duration_ms = (time.perf_counter() - started) * 1000
        event = {
            "type": "plan",
            "step": step,
            "planner": self.planner_name,
            "calls": [call["name"] for call in decision.calls],
            "done": decision.done,
            "message": decision.message,
            "duration_ms": round(duration_ms, 2),
            "at": _now(),
        }
        if self._progress:
            if decision.calls:
                _say(f"step {step}: " + ", ".join(call["name"] for call in decision.calls))
            else:
                _say(f"step {step}: planning complete")

        update: dict[str, Any] = {
            "steps": step,
            "pending": decision.calls,
            "done": decision.done,
            "events": [event],
        }
        if decision.messages is not None:
            update["messages"] = decision.messages
        if decision.done and decision.message:
            update["answer"] = decision.message
        return update

    async def _act(self, state: AgentState) -> dict[str, Any]:
        assert self.manager is not None
        observations: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        messages = list(state.get("messages") or [])
        limit = int(self.config["agent"]["max_tool_result_chars"])

        for call in state.get("pending") or []:
            name = str(call.get("name") or "")
            arguments = call.get("arguments") or {}
            result: ToolCallResult = await self.manager.call_tool(name, arguments)
            observation = {
                "name": result.name if result.server else name,
                "server": result.server,
                "tool": result.tool,
                "arguments": arguments,
                "ok": result.ok,
                "data": result.data,
                "error": result.error,
                "duration_ms": round(result.duration_ms, 2),
                "at": _now(),
            }
            observations.append(observation)
            events.append({"type": "tool", **{k: observation[k] for k in ("name", "ok", "error", "duration_ms", "at")}})

            if self._progress:
                status = "ok" if result.ok else f"ERROR: {result.error}"
                _say(f"  {result.name or name} -> {status} ({result.duration_ms:.0f} ms)")

            if call.get("id"):
                content = result.text if result.ok else f"ERROR: {result.error}"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": content[:limit],
                    }
                )

        return {"observations": observations, "events": events, "messages": messages, "pending": []}

    async def _finalize(self, state: AgentState) -> dict[str, Any]:
        assert self.manager is not None
        started = time.perf_counter()
        job = (state.get("context") or {}).get("job") or {}
        job_id = state.get("job_id") or self.config["agent"]["default_job_id"]
        run_id = state.get("run_id") or _run_id()
        rankings = self._rankings(state.get("observations") or [])
        events: list[dict[str, Any]] = []

        persisted = 0
        tool_rows: list[dict[str, Any]] = []
        if rankings:
            for candidate in rankings:
                saved = await self.manager.call_tool(
                    "jobs__save_match_result",
                    {
                        "job_id": job_id,
                        "resume_name": candidate["resume"],
                        "score": candidate["score"],
                        "matched_skills": candidate["matched"],
                        "run_id": run_id,
                    },
                )
                tool_rows.append(
                    {
                        "server": "jobs",
                        "name": saved.name,
                        "ok": saved.ok,
                        "duration_ms": saved.duration_ms,
                    }
                )
                events.append(
                    {
                        "type": "persist",
                        "name": saved.name,
                        "ok": saved.ok,
                        "error": saved.error,
                        "duration_ms": round(saved.duration_ms, 2),
                        "at": _now(),
                    }
                )
                if saved.ok:
                    persisted += 1
        if self._progress and rankings:
            _say(f"persisted {persisted}/{len(rankings)} match results to jobs MCP server")

        report = self._build_report(state, rankings, run_id, job, tool_rows)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        report_path = f"reports/shortlist_{job_id}_{timestamp}.md"
        written = await self.manager.call_tool(
            "filesystem__write_file", {"filepath": report_path, "content": report}
        )
        tool_rows.append(
            {
                "server": "filesystem",
                "name": written.name,
                "ok": written.ok,
                "duration_ms": written.duration_ms,
            }
        )
        events.append(
            {
                "type": "report",
                "name": written.name,
                "ok": written.ok,
                "error": written.error,
                "duration_ms": round(written.duration_ms, 2),
                "at": _now(),
            }
        )
        if written.ok:
            await self.manager.call_tool(
                "filesystem__write_file", {"filepath": "reports/latest.md", "content": report}
            )
            verification = await self.manager.read_resource(
                f"workspace://file/{report_path}", server="filesystem"
            )
            events.append(
                {
                    "type": "verify",
                    "name": "workspace://file/" + report_path,
                    "ok": verification.ok,
                    "error": verification.error,
                    "duration_ms": round(verification.duration_ms, 2),
                    "at": _now(),
                }
            )
            if self._progress:
                _say(f"wrote and verified {report_path} via MCP resources")

        answer = self._answer(job, rankings, report_path, persisted)
        events.append(
            {
                "type": "finalize",
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "at": _now(),
            }
        )
        return {
            "rankings": rankings,
            "answer": answer,
            "report_path": report_path if written.ok else "",
            "report_text": report,
            "persisted": persisted,
            "events": events,
        }

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _read_json(self, uri: str, events: list[dict[str, Any]]) -> Any:
        assert self.manager is not None
        result = await self.manager.read_resource(uri)
        events.append(
            {
                "type": "resource",
                "name": uri,
                "ok": result.ok,
                "error": result.error,
                "duration_ms": round(result.duration_ms, 2),
                "at": _now(),
            }
        )
        return result.data if result.ok else None

    async def _resource_or_tool(
        self,
        uri: str,
        tool: str,
        arguments: dict[str, Any],
        events: list[dict[str, Any]],
        *,
        unwrap: str | None = None,
    ) -> Any:
        assert self.manager is not None
        resource = await self.manager.read_resource(uri)
        events.append(
            {
                "type": "resource",
                "name": uri,
                "ok": resource.ok,
                "error": resource.error,
                "duration_ms": round(resource.duration_ms, 2),
                "at": _now(),
            }
        )
        if resource.ok:
            data = resource.data
            if unwrap and isinstance(data, dict):
                return data.get(unwrap, data)
            return data

        fallback = await self.manager.call_tool(tool, arguments)
        events.append(
            {
                "type": "tool",
                "name": fallback.name,
                "ok": fallback.ok,
                "error": fallback.error,
                "duration_ms": round(fallback.duration_ms, 2),
                "at": _now(),
            }
        )
        if fallback.ok and isinstance(fallback.data, dict) and unwrap:
            return fallback.data.get(unwrap, fallback.data)
        return fallback.data if fallback.ok else None

    @staticmethod
    def _rankings(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Extract and normalise batch_process results from any observation."""
        batch = None
        for observation in reversed(observations):
            if observation.get("tool") == "batch_process" and observation.get("ok"):
                data = observation.get("data") or {}
                if isinstance(data, dict) and data.get("results"):
                    batch = data
                    break
        if not batch:
            return []

        rankings = []
        for item in batch["results"]:
            if not item.get("success"):
                continue
            match = item.get("match") or {}
            rankings.append(
                {
                    "rank": int(item.get("rank") or 0),
                    "resume": item.get("file") or Path(str(item.get("path", ""))).name,
                    "path": item.get("path"),
                    "score": float(match.get("score") or 0.0),
                    "matched": list(match.get("matched") or []),
                    "missing": list(match.get("missing") or []),
                    "matched_count": int(match.get("matched_count") or 0),
                    "total_keywords": int(match.get("total_keywords") or 0),
                    "skills": list(item.get("skills") or []),
                }
            )
        rankings.sort(key=lambda row: (-row["score"], row["resume"].lower()))
        for index, row in enumerate(rankings, start=1):
            row["rank"] = index
        return rankings

    def _build_report(
        self,
        state: AgentState,
        rankings: list[dict[str, Any]],
        run_id: str,
        job: dict[str, Any],
        tool_rows: list[dict[str, Any]] | None = None,
    ) -> str:
        job_id = state.get("job_id") or self.config["agent"]["default_job_id"]
        planner = self.planner_name
        model = self.config["agent"]["model"] if "llm" in planner else "offline scripted planner"
        lines = [
            f"# Candidate Shortlist - {job_id}",
            "",
            f"**Role:** {job.get('title', 'Unknown')}  ",
            f"**Company:** {job.get('company', 'Unknown')}  ",
            f"**Location:** {job.get('location', 'Unknown')}  ",
            f"**Generated:** {datetime.now().isoformat(timespec='seconds')}  ",
            f"**Run:** `{run_id}`  ",
            f"**Planner:** {planner} ({model})",
            "",
            "## Ranking",
            "",
        ]
        if rankings:
            lines.extend(
                [
                    "| Rank | Candidate | Score | Matched skills | Missing skills |",
                    "| ---: | --- | ---: | --- | --- |",
                ]
            )
            for row in rankings:
                matched = ", ".join(row["matched"]) or "-"
                missing = ", ".join(row["missing"]) or "-"
                lines.append(
                    f"| {row['rank']} | `{row['resume']}` | {row['score']:.1f}% | {matched} | {missing} |"
                )
        else:
            lines.append("_No batch ranking was produced by this run._")

        lines.extend(["", "## Required skills", ""])
        skills = job.get("required_skills") or []
        lines.append(", ".join(f"`{skill}`" for skill in skills) if skills else "_None listed._")
        lines.extend(["", "## Tool trace", "", "| Server | Tool | OK | ms |", "| --- | --- | --- | ---: |"])
        for observation in state.get("observations") or []:
            lines.append(
                f"| {observation.get('server') or '-'} | `{observation.get('name')}` | "
                f"{'yes' if observation.get('ok') else 'no'} | {observation.get('duration_ms', 0):.0f} |"
            )
        for row in tool_rows or []:
            lines.append(
                f"| {row['server']} | `{row['name']}` | "
                f"{'yes' if row['ok'] else 'no'} | {row['duration_ms']:.0f} |"
            )
        lines.extend(
            [
                "",
                "## How this was produced",
                "",
                "1. `matching_agent.py` started both MCP servers over stdio.",
                "2. LangGraph ran `discover -> plan -> act -> plan -> finalize`.",
                "3. Candidate scores came from `filesystem__batch_process` over MCP.",
                "4. Scores were persisted with `jobs__save_match_result` on the job-board server.",
                "5. This report was written with `filesystem__write_file` and read back through `workspace://file/...`.",
                "",
            ]
        )
        return "\n".join(lines)

    def _answer(
        self,
        job: dict[str, Any],
        rankings: list[dict[str, Any]],
        report_path: str,
        persisted: int,
    ) -> str:
        title = f"{job.get('title', 'role')} ({job.get('company', 'company')})"
        if not rankings:
            return f"No candidate ranking was produced for {title}. See the trace for details."
        top = rankings[:3]
        bullets = "\n".join(
            f"{row['rank']}. **{row['resume']}** — {row['score']:.1f}% "
            f"({row['matched_count']}/{row['total_keywords']} required skills)"
            for row in top
        )
        lines = [
            f"Ranked {len(rankings)} candidates for **{title}**.",
            "",
            bullets,
            "",
            f"Saved {persisted} match results to the job-board MCP server.",
        ]
        if report_path:
            lines.append(f"Report: `{report_path}`")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Entry points
    # ------------------------------------------------------------------ #
    async def run(self, prompt: str, *, job_id: str | None = None) -> AgentResult:
        """Execute one full agent run, owning the MCP server lifecycle."""
        started = time.perf_counter()
        run_id = _run_id()
        load_dotenv()
        server_names = list(server_specs(self.config).keys())
        self._progress = True

        async with MCPClientManager(self.config, server_names=server_names) as manager:
            self.manager = manager
            if self.offline or not os.getenv("GROQ_API_KEY", "").strip():
                if not self.offline:
                    _say("GROQ_API_KEY is empty; using the deterministic offline planner")
                self.planner = ScriptedPlanner()
                self.planner_name = "scripted"
            else:
                try:
                    self.planner = LLMPlanner(manager, self.config)
                    self.planner_name = "llm"
                except Exception as exc:
                    if self.require_llm:
                        raise
                    logger.warning("LLM planner unavailable (%s); using offline planner", exc)
                    self.planner = ScriptedPlanner()
                    self.planner_name = "scripted"

            initial: AgentState = {
                "prompt": prompt,
                "job_id": (job_id or self.config["agent"]["default_job_id"]).upper(),
                "run_id": run_id,
                "messages": [],
                "pending": [],
                "observations": [],
                "events": [
                    {
                        "type": "start",
                        "run_id": run_id,
                        "prompt": prompt,
                        "planner": self.planner_name,
                        "servers": {
                            connection.name: {
                                "server_info": (
                                    connection.server_info.model_dump()
                                    if connection.server_info is not None
                                    and hasattr(connection.server_info, "model_dump")
                                    else None
                                ),
                                "protocol_version": connection.protocol_version,
                                "tools": len(connection.tools),
                                "resources": len(connection.resources)
                                + len(connection.resource_templates),
                            }
                            for connection in manager.servers.values()
                        },
                        "at": _now(),
                    }
                ],
                "steps": 0,
                "done": False,
                "max_steps": int(self.config["agent"]["max_steps"]),
            }
            final = await self.graph.ainvoke(initial)
            self.manager = None

        trace = {
            "run_id": run_id,
            "prompt": prompt,
            "planner": self.planner_name,
            "job_id": final.get("job_id"),
            "answer": final.get("answer", ""),
            "rankings": final.get("rankings", []),
            "persisted": final.get("persisted", 0),
            "report_path": final.get("report_path", ""),
            "events": final.get("events", []),
            "observations": final.get("observations", []),
            "duration_seconds": round(time.perf_counter() - started, 2),
        }
        trace_path = self._write_trace(run_id, trace)

        return AgentResult(
            run_id=run_id,
            answer=trace["answer"],
            rankings=trace["rankings"],
            report_path=trace["report_path"],
            observations=trace["observations"],
            events=trace["events"],
            persisted=trace["persisted"],
            trace_path=trace_path,
            duration_seconds=trace["duration_seconds"],
            planner=self.planner_name,
        )

    def _write_trace(self, run_id: str, trace: dict[str, Any]) -> str:
        directory = Path(str(self.config["agent"]["trace_dir"]))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{run_id}.json"
        path.write_text(json.dumps(trace, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        latest = directory / "latest.json"
        latest.write_text(json.dumps(trace, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return str(path)


# --------------------------------------------------------------------------- #
# CLI helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _run_id() -> str:
    return datetime.now().strftime("run-%Y%m%d-%H%M%S")


def _say(message: str) -> None:
    print(f"[agent] {message}", flush=True)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Windows consoles default to a legacy code page; keep output readable.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass


async def discover(config: dict[str, Any], *, as_json: bool = False) -> dict[str, Any]:
    """Print the merged multi-server MCP catalog."""
    async with MCPClientManager(config) as manager:
        catalog = manager.catalog()
        if as_json:
            print(json.dumps(catalog, indent=2, ensure_ascii=False, default=str))
            return catalog
        print(manager.summary_line())
        for server in catalog["servers"]:
            info = server.get("server_info") or {}
            print(
                f"\n[{server['name']}] {info.get('name', '?')} v{info.get('version', '?')} "
                f"protocol={server.get('protocol_version')}"
            )
            print("  tools:")
            for name in server["tools"]:
                print(f"    - {qualified_name(server['name'], name)}")
            print("  resources:")
            for uri in server["resources"]:
                print(f"    - {uri}")
            print("  templates:")
            for uri in server["resource_templates"]:
                print(f"    - {uri}")
        return catalog


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="LangGraph resume-matching agent over MCP")
    parser.add_argument("--config", help="Path to mcp_config.json")
    parser.add_argument("--job", help="Job ID to match against (default: JOB-001)")
    parser.add_argument("--prompt", help="Natural-language request for the agent")
    parser.add_argument("--offline", action="store_true", help="Force the deterministic planner")
    parser.add_argument(
        "--require-llm",
        action="store_true",
        help="Fail when GROQ_API_KEY is missing instead of falling back offline",
    )
    parser.add_argument("--discover", action="store_true", help="List MCP servers, tools and resources")
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON")
    parser.add_argument("--demo", action="store_true", help="Use the demo prompt for JOB-001")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING or ERROR")
    args = parser.parse_args(argv)

    load_dotenv()
    overrides: dict[str, Any] = {}
    if args.log_level:
        overrides.setdefault("logging", {})["level"] = args.log_level
    config = load_config(args.config, overrides=overrides)
    _configure_logging(str(config["logging"]["level"]))

    job_id = (args.job or config["agent"]["default_job_id"]).upper()
    if args.demo:
        prompt = (
            f"Find the best candidates for {job_id}. Compare every resume in the resumes "
            "directory, rank them by required-skill coverage, save the match results, and "
            "write a shortlist report."
        )
    else:
        prompt = args.prompt or (
            f"Rank all resumes for {job_id} by how well they match the required skills, "
            "save the results and write a Markdown report."
        )

    if args.discover:
        anyio_run(discover(config, as_json=args.json))
        return

    agent = MatchingAgent(config, offline=args.offline, require_llm=args.require_llm)
    try:
        result = anyio_run(agent.run(prompt, job_id=job_id))
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        logger.error("agent run failed: %s", exc)
        raise SystemExit(1) from exc

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False, default=str))
        return

    print("\n" + "=" * 74)
    print(result.answer)
    print("=" * 74)
    print(f"planner: {result.planner}")
    print(f"tool calls: {len(result.observations)} | persisted: {result.persisted}")
    print(f"report: {result.report_path or '(not written)'}")
    print(f"trace:  {result.trace_path}")
    print(f"time:   {result.duration_seconds:.2f}s")


def anyio_run(coroutine: Any) -> Any:
    """Run a coroutine on a fresh event loop (kept as a small test seam)."""
    return asyncio.run(coroutine)


if __name__ == "__main__":
    main()
