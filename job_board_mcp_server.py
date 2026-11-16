"""
job_board_mcp_server.py
=======================

Bonus: a second, independent MCP server backed by SQLite.

The matching agent connects to both this server and ``filesystem_mcp_server.py``.
That demonstrates the real value of MCP: one agent, multiple standard tool and
resource providers, with no provider-specific glue code.

Tools
-----
- ``search_jobs(query, location, experience, skill, limit)``
- ``get_job(job_id)``
- ``save_match_result(job_id, resume_name, score, matched_skills, run_id)``
- ``list_match_results(job_id, run_id, limit)``
- ``clear_match_results(job_id)``
- ``get_config()`` / ``get_stats()``

Resources
---------
- ``jobs://catalog``                 compact list of all roles
- ``jobs://job/{job_id}``            full role description
- ``jobs://matches``                 recent saved match results
- ``jobs://matches/{job_id}``        saved results for one role
- ``jobs://stats``                   database statistics
- ``jobs://config``                  database location and capabilities

Run it
------
    python job_board_mcp_server.py --selftest
    python job_board_mcp_server.py                 # stdio (spawned by the agent)
    python job_board_mcp_server.py --reset --seed  # rebuild the demo database
"""

from __future__ import annotations

import argparse
import functools
import inspect
import json
import logging
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import ToolAnnotations

from mcp_config import load_config, public_config

logger = logging.getLogger("job_board_mcp_server")

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)

TOOL_NAMES = [
    "search_jobs",
    "get_job",
    "save_match_result",
    "list_match_results",
    "clear_match_results",
    "get_config",
    "get_stats",
]
STATIC_RESOURCES = ["jobs://catalog", "jobs://matches", "jobs://stats", "jobs://config"]
RESOURCE_TEMPLATES = ["jobs://job/{job_id}", "jobs://matches/{job_id}"]

SEED_JOBS: list[dict[str, Any]] = [
    {
        "id": "JOB-001",
        "title": "Senior Python Engineer",
        "company": "FinStack Technologies",
        "location": "Bengaluru, India",
        "employment_type": "Full-time",
        "min_experience": 5,
        "salary_range": "INR 35-50 LPA",
        "description": (
            "Build and scale Python microservices for a payments platform. You will own "
            "FastAPI services, PostgreSQL schemas, containerised deployments on AWS EKS, "
            "and machine-learning risk models used in production."
        ),
        "required_skills": [
            "Python", "FastAPI", "PostgreSQL", "Docker", "Kubernetes", "AWS",
            "scikit-learn", "Airflow",
        ],
        "nice_to_have": ["Django", "ETL", "Machine Learning", "Redis"],
    },
    {
        "id": "JOB-002",
        "title": "Java Backend Engineer",
        "company": "CoreBank Solutions",
        "location": "Pune, India",
        "employment_type": "Full-time",
        "min_experience": 3,
        "salary_range": "INR 22-32 LPA",
        "description": (
            "Develop Spring Boot services for enterprise banking. The role works with "
            "Hibernate, MySQL, Kafka event streaming and Jenkins delivery pipelines "
            "across a microservices estate."
        ),
        "required_skills": [
            "Java", "Spring Boot", "Hibernate", "MySQL", "Kafka", "Jenkins",
            "Microservices",
        ],
        "nice_to_have": ["Docker", "AWS", "REST"],
    },
    {
        "id": "JOB-003",
        "title": "DevOps / Platform Engineer",
        "company": "CloudNine Systems",
        "location": "Remote, India",
        "employment_type": "Full-time",
        "min_experience": 4,
        "salary_range": "INR 28-40 LPA",
        "description": (
            "Own cloud infrastructure and developer experience. You will manage "
            "Kubernetes clusters on AWS, codify infrastructure with Terraform, build "
            "GitHub Actions pipelines and improve observability with Prometheus."
        ),
        "required_skills": [
            "AWS", "Terraform", "Kubernetes", "Docker", "GitHub Actions", "Bash",
            "Python", "Prometheus",
        ],
        "nice_to_have": ["Go", "Linux", "CI/CD"],
    },
    {
        "id": "JOB-004",
        "title": "Data Analyst",
        "company": "ShopRight Retail",
        "location": "Singapore",
        "employment_type": "Full-time",
        "min_experience": 2,
        "salary_range": "SGD 70-95k",
        "description": (
            "Turn messy retail data into decisions. Build SQL models and Tableau "
            "dashboards, run A/B tests, and use Python (pandas, matplotlib) for "
            "analysis and automation."
        ),
        "required_skills": [
            "SQL", "Python", "pandas", "matplotlib", "Tableau", "Excel",
            "A/B testing", "Statistics",
        ],
        "nice_to_have": ["ETL", "Data analysis", "Machine Learning"],
    },
    {
        "id": "JOB-005",
        "title": "Frontend Engineer",
        "company": "Pixel Labs",
        "location": "Madrid, Spain",
        "employment_type": "Full-time",
        "min_experience": 3,
        "salary_range": "EUR 45-60k",
        "description": (
            "Build accessible, high-performance web applications in React and "
            "TypeScript. You will work with Next.js, Tailwind CSS, Jest and Figma in "
            "a product-focused design system team."
        ),
        "required_skills": [
            "JavaScript", "TypeScript", "React", "Next.js", "Tailwind CSS", "Jest",
            "Figma",
        ],
        "nice_to_have": ["Node.js", "GraphQL", "REST"],
    },
    {
        "id": "JOB-006",
        "title": "Product Manager, Analytics",
        "company": "Teamly",
        "location": "London, UK",
        "employment_type": "Full-time",
        "min_experience": 5,
        "salary_range": "GBP 70-90k",
        "description": (
            "Own the analytics product roadmap for a B2B SaaS platform. Run customer "
            "discovery, write clear specs, and work with engineering in an Agile/Scrum "
            "environment. SQL literacy is required."
        ),
        "required_skills": [
            "Product strategy", "Roadmapping", "Agile", "Scrum", "JIRA",
            "User research", "SQL",
        ],
        "nice_to_have": ["Data analysis", "A/B testing", "Statistics"],
    },
    {
        "id": "JOB-007",
        "title": "Machine Learning Engineer",
        "company": "FinStack Technologies",
        "location": "Bengaluru, India",
        "employment_type": "Full-time",
        "min_experience": 4,
        "salary_range": "INR 32-48 LPA",
        "description": (
            "Productionise machine-learning models. You will build ETL and Airflow "
            "pipelines, train models with scikit-learn, and deploy Python services on "
            "Docker and Kubernetes in AWS."
        ),
        "required_skills": [
            "Python", "scikit-learn", "Machine Learning", "ETL", "Airflow",
            "PostgreSQL", "Docker", "AWS",
        ],
        "nice_to_have": ["pandas", "Kubernetes", "FastAPI"],
    },
]


def _error(message: str, code: str = "ERROR", **extra: Any) -> dict[str, Any]:
    return {"success": False, "error": message, "error_code": code, **extra}


def _is_error_payload(value: Any) -> bool:
    return isinstance(value, dict) and value.get("success") is False


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [part.strip() for part in value.split(",") if part.strip()]
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    return []


class JobBoardServer:
    """SQLite-backed MCP server for job postings and saved match results."""

    def __init__(self, config: dict[str, Any] | None = None, *, auto_seed: bool = True) -> None:
        self.config = config or load_config()
        self.db_path = Path(str(self.config["jobs"]["db_path"])).resolve()
        self.started_at = time.time()
        self._lock = threading.Lock()
        self.tool_calls = 0
        self.tool_errors = 0
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if auto_seed and self.config["jobs"].get("auto_seed", True):
            seed_database(self.db_path)
        else:
            self._ensure_schema()

        self.server = MCPServer(
            name="job-board-mcp",
            title="Job Board MCP Server",
            version="1.0.0",
            instructions=(
                "Job database server. Use search_jobs/get_job to find roles, "
                "save_match_result to persist candidate scores, and the jobs:// "
                "resources for read-only catalog context."
            ),
            log_level=str(self.config["logging"]["level"]).upper(),
        )
        self._register_tools()
        self._register_resources()
        logger.info("job board MCP server ready: db=%s", self.db_path)

    # ------------------------------------------------------------------ #
    # Database helpers
    # ------------------------------------------------------------------ #
    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.db_path), timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        with self._db() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT NOT NULL,
                    employment_type TEXT NOT NULL,
                    min_experience REAL NOT NULL DEFAULT 0,
                    salary_range TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL,
                    required_skills TEXT NOT NULL,
                    nice_to_have TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS match_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    resume_name TEXT NOT NULL,
                    score REAL NOT NULL,
                    matched_skills TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, resume_name, run_id)
                );

                CREATE INDEX IF NOT EXISTS idx_match_job ON match_results(job_id);
                CREATE INDEX IF NOT EXISTS idx_match_run ON match_results(run_id);
                """
            )

    def _job_count(self) -> int:
        with self._db() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()
        return int(row["count"])

    @staticmethod
    def _row_to_job(row: sqlite3.Row, *, full: bool = True) -> dict[str, Any]:
        job = {
            "job_id": row["id"],
            "title": row["title"],
            "company": row["company"],
            "location": row["location"],
            "employment_type": row["employment_type"],
            "min_experience": row["min_experience"],
            "salary_range": row["salary_range"],
            "required_skills": _json_list(row["required_skills"]),
            "nice_to_have": _json_list(row["nice_to_have"]),
        }
        if full:
            job["description"] = row["description"]
        if "created_at" in row.keys():
            job["created_at"] = row["created_at"]
        return job

    # ------------------------------------------------------------------ #
    # Tool registration
    # ------------------------------------------------------------------ #
    def _register_tools(self) -> None:
        self._add_tool("search_jobs", self.search_jobs, READ_ONLY)
        self._add_tool("get_job", self.get_job, READ_ONLY)
        self._add_tool("save_match_result", self.save_match_result, WRITE)
        self._add_tool("list_match_results", self.list_match_results, READ_ONLY)
        self._add_tool("clear_match_results", self.clear_match_results, WRITE)
        self._add_tool("get_config", self.get_config, READ_ONLY)
        self._add_tool("get_stats", self.get_stats, READ_ONLY)

    def _add_tool(self, name: str, fn: Any, annotations: ToolAnnotations) -> None:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                with self._lock:
                    self.tool_calls += 1
                try:
                    result = await fn(*args, **kwargs)
                except Exception:
                    with self._lock:
                        self.tool_errors += 1
                    raise
                if _is_error_payload(result):
                    with self._lock:
                        self.tool_errors += 1
                    raise ToolError(json.dumps(result, ensure_ascii=False, default=str))
                return result

        else:

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                with self._lock:
                    self.tool_calls += 1
                try:
                    result = fn(*args, **kwargs)
                except Exception:
                    with self._lock:
                        self.tool_errors += 1
                    raise
                if _is_error_payload(result):
                    with self._lock:
                        self.tool_errors += 1
                    raise ToolError(json.dumps(result, ensure_ascii=False, default=str))
                return result

        self.server.add_tool(wrapper, name=name, annotations=annotations)

    # ------------------------------------------------------------------ #
    # Tools
    # ------------------------------------------------------------------ #
    def search_jobs(
        self,
        query: str = "",
        location: str = "",
        experience: float | None = None,
        skill: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search job postings by free text, location, experience and skill.

        ``experience`` filters to roles whose minimum requirement is at most the
        supplied number of years. ``limit`` is clamped to 1..100.
        """
        limit = max(1, min(int(limit), 100))
        conditions: list[str] = []
        params: list[Any] = []

        if query and query.strip():
            terms = [term for term in query.split() if term]
            clauses = []
            for term in terms:
                pattern = f"%{term}%"
                clauses.append(
                    "(title LIKE ? OR company LIKE ? OR description LIKE ? OR required_skills LIKE ?)"
                )
                params.extend([pattern, pattern, pattern, pattern])
            conditions.append("(" + " OR ".join(clauses) + ")")
        if location and location.strip():
            conditions.append("location LIKE ?")
            params.append(f"%{location.strip()}%")
        if experience is not None:
            conditions.append("min_experience <= ?")
            params.append(float(experience))
        if skill and skill.strip():
            conditions.append("required_skills LIKE ?")
            params.append(f"%{skill.strip()}%")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        with self._db() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs {where} ORDER BY id LIMIT ?", params
            ).fetchall()

        jobs = [self._row_to_job(row, full=False) for row in rows]
        return {"success": True, "count": len(jobs), "jobs": jobs}

    def get_job(self, job_id: str) -> dict[str, Any]:
        """Return the full description of one job by its ID (e.g. JOB-001)."""
        job_id = str(job_id).strip().upper()
        if not job_id:
            return _error("job_id must be a non-empty string", "INVALID_ARGUMENT")
        with self._db() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return _error(f"Job not found: {job_id}", "NOT_FOUND", job_id=job_id)
        return {"success": True, "job": self._row_to_job(row, full=True)}

    def save_match_result(
        self,
        job_id: str,
        resume_name: str,
        score: float,
        matched_skills: list[str] | None = None,
        run_id: str = "manual",
    ) -> dict[str, Any]:
        """Insert or update a candidate-to-job match score.

        Records are unique per ``(job_id, resume_name, run_id)``, so rerunning the
        same agent run updates rather than duplicates rows.
        """
        job_id = str(job_id).strip().upper()
        resume_name = str(resume_name).strip()
        run_id = str(run_id).strip() or "manual"
        if not job_id or not resume_name:
            return _error("job_id and resume_name are required", "INVALID_ARGUMENT")
        try:
            score = float(score)
        except (TypeError, ValueError):
            return _error("score must be a number between 0 and 100", "INVALID_ARGUMENT")
        if not 0.0 <= score <= 100.0:
            return _error("score must be between 0 and 100", "INVALID_ARGUMENT")

        skills = _json_list(matched_skills)
        created_at = datetime.now().isoformat(timespec="seconds")
        with self._db() as connection:
            exists = connection.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if exists is None:
                return _error(f"Job not found: {job_id}", "NOT_FOUND", job_id=job_id)
            connection.execute(
                """
                INSERT INTO match_results
                    (job_id, resume_name, score, matched_skills, run_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, resume_name, run_id)
                DO UPDATE SET
                    score = excluded.score,
                    matched_skills = excluded.matched_skills,
                    created_at = excluded.created_at
                """,
                (job_id, resume_name, score, json.dumps(skills), run_id, created_at),
            )

        return {
            "success": True,
            "saved": True,
            "job_id": job_id,
            "resume_name": resume_name,
            "score": score,
            "matched_skills": skills,
            "run_id": run_id,
            "created_at": created_at,
        }

    def list_match_results(
        self,
        job_id: str | None = None,
        run_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List saved match results, newest runs first, optionally filtered."""
        limit = max(1, min(int(limit), 200))
        conditions: list[str] = []
        params: list[Any] = []
        if job_id:
            conditions.append("job_id = ?")
            params.append(str(job_id).strip().upper())
        if run_id:
            conditions.append("run_id = ?")
            params.append(str(run_id).strip())
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        with self._db() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM match_results {where}
                ORDER BY score DESC, created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

        results = [
            {
                "id": row["id"],
                "job_id": row["job_id"],
                "resume_name": row["resume_name"],
                "score": row["score"],
                "matched_skills": _json_list(row["matched_skills"]),
                "run_id": row["run_id"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
        return {"success": True, "count": len(results), "results": results}

    def clear_match_results(self, job_id: str | None = None) -> dict[str, Any]:
        """Delete saved match results, all or for one job. Returns the row count."""
        with self._db() as connection:
            if job_id:
                cursor = connection.execute(
                    "DELETE FROM match_results WHERE job_id = ?", (str(job_id).strip().upper(),)
                )
            else:
                cursor = connection.execute("DELETE FROM match_results")
        return {"success": True, "deleted": cursor.rowcount}

    def get_config(self) -> dict[str, Any]:
        """Return database location and exposed capabilities."""
        return {
            "success": True,
            "server": {"name": "job-board-mcp", "version": "1.0.0", "transport": "stdio"},
            "database": str(self.db_path),
            "job_count": self._job_count(),
            "tools": TOOL_NAMES,
            "resources": STATIC_RESOURCES,
            "resource_templates": RESOURCE_TEMPLATES,
            "config": public_config(self.config),
        }

    def get_stats(self) -> dict[str, Any]:
        """Return database counts and runtime counters."""
        with self._db() as connection:
            job_count = connection.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
            match_count = connection.execute("SELECT COUNT(*) AS n FROM match_results").fetchone()["n"]
            by_location = connection.execute(
                "SELECT location, COUNT(*) AS n FROM jobs GROUP BY location ORDER BY n DESC"
            ).fetchall()
        return {
            "success": True,
            "uptime_seconds": round(time.time() - self.started_at, 2),
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "job_count": int(job_count),
            "match_result_count": int(match_count),
            "jobs_by_location": [{"location": row["location"], "jobs": row["n"]} for row in by_location],
        }

    # ------------------------------------------------------------------ #
    # Resources
    # ------------------------------------------------------------------ #
    def _register_resources(self) -> None:
        self.server.resource(
            "jobs://catalog",
            name="Job catalog",
            description="Compact catalog of all open roles and required skills.",
            mime_type="application/json",
        )(self.job_catalog)
        self.server.resource(
            "jobs://job/{job_id}",
            name="Job description",
            description="Full description of one role by job ID.",
            mime_type="application/json",
        )(self.job_resource)
        self.server.resource(
            "jobs://matches",
            name="Recent match results",
            description="Recent saved candidate scores across all roles.",
            mime_type="application/json",
        )(self.matches_resource)
        self.server.resource(
            "jobs://matches/{job_id}",
            name="Match results for a job",
            description="Saved candidate scores for one role.",
            mime_type="application/json",
        )(self.job_matches_resource)
        self.server.resource(
            "jobs://stats",
            name="Job database statistics",
            description="Job and match-result counts for this MCP server.",
            mime_type="application/json",
        )(self.stats_resource)
        self.server.resource(
            "jobs://config",
            name="Job server configuration",
            description="Database location and exposed tools/resources.",
            mime_type="application/json",
        )(self.config_resource)

    def job_catalog(self) -> str:
        """Return a compact JSON catalog of all jobs."""
        result = self.search_jobs(limit=100)
        return json.dumps(result, indent=2)

    def job_resource(self, job_id: str) -> str:
        """Return one job as JSON (resource template)."""
        result = self.get_job(job_id)
        if not result.get("success"):
            raise ResourceNotFoundError(result.get("error", f"Job not found: {job_id}"))
        return json.dumps(result, indent=2)

    def matches_resource(self) -> str:
        """Return recent saved match results as JSON."""
        return json.dumps(self.list_match_results(limit=50), indent=2)

    def job_matches_resource(self, job_id: str) -> str:
        """Return saved match results for one job as JSON."""
        result = self.list_match_results(job_id=job_id, limit=100)
        if not result.get("results"):
            # An empty list is a valid answer; do not turn it into a 404.
            return json.dumps(result, indent=2)
        return json.dumps(result, indent=2)

    def stats_resource(self) -> str:
        """Return database statistics as JSON."""
        return json.dumps(self.get_stats(), indent=2)

    def config_resource(self) -> str:
        """Return server configuration as JSON."""
        return json.dumps(self.get_config(), indent=2)


# --------------------------------------------------------------------------- #
# Seeding / factory / CLI
# --------------------------------------------------------------------------- #
def seed_database(db_path: Path, *, force: bool = False) -> int:
    """Create schema and insert demo jobs. Returns the number of jobs available."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT NOT NULL,
                employment_type TEXT NOT NULL,
                min_experience REAL NOT NULL DEFAULT 0,
                salary_range TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL,
                required_skills TEXT NOT NULL,
                nice_to_have TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS match_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                resume_name TEXT NOT NULL,
                score REAL NOT NULL,
                matched_skills TEXT NOT NULL,
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(job_id, resume_name, run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_match_job ON match_results(job_id);
            CREATE INDEX IF NOT EXISTS idx_match_run ON match_results(run_id);
            """
        )
        count = int(connection.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"])
        if force or count == 0:
            if force:
                connection.execute("DELETE FROM jobs")
                connection.execute("DELETE FROM match_results")
            created_at = datetime.now().isoformat(timespec="seconds")
            for job in SEED_JOBS:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO jobs
                        (id, title, company, location, employment_type, min_experience,
                         salary_range, description, required_skills, nice_to_have, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job["id"], job["title"], job["company"], job["location"],
                        job["employment_type"], job["min_experience"], job["salary_range"],
                        job["description"], json.dumps(job["required_skills"]),
                        json.dumps(job["nice_to_have"]), created_at,
                    ),
                )
        connection.commit()
        return int(connection.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"])
    finally:
        connection.close()


def build_server(config: dict[str, Any] | None = None) -> MCPServer:
    """Create a ready-to-run job-board MCP server (also used by tests)."""
    return JobBoardServer(config).server


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _selftest(config: dict[str, Any]) -> dict[str, Any]:
    from mcp import Client

    impl = JobBoardServer(config)
    async with Client(impl.server, mode="legacy") as client:
        tools = await client.list_tools()
        resources = await client.list_resources()
        templates = await client.list_resource_templates()
        search = await client.call_tool("search_jobs", {"query": "Python", "limit": 5})
        job = await client.call_tool("get_job", {"job_id": "JOB-001"})
        catalog = await client.read_resource("jobs://catalog")
        stats = await client.read_resource("jobs://stats")
        server_info = client.server_info.model_dump() if client.server_info else None
        protocol_version = client.protocol_version

    def _payload(result: Any) -> Any:
        for block in result.content:
            if getattr(block, "type", None) == "text":
                try:
                    return json.loads(block.text)
                except json.JSONDecodeError:
                    return block.text
        return None

    return {
        "server_info": server_info,
        "protocol_version": protocol_version,
        "tools": [tool.name for tool in tools.tools],
        "resources": [resource.uri for resource in resources.resources],
        "resource_templates": [template.uri_template for template in templates.resource_templates],
        "search": _payload(search),
        "job": _payload(job),
        "catalog": json.loads(catalog.contents[0].text),
        "stats": json.loads(stats.contents[0].text),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Job board MCP server (SQLite, JSON-RPC 2.0)")
    parser.add_argument("--config", help="Path to mcp_config.json")
    parser.add_argument("--db", help="Override SQLite database path")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING or ERROR")
    parser.add_argument("--reset", action="store_true", help="Delete and rebuild the database")
    parser.add_argument("--seed", action="store_true", help="Seed demo jobs if the DB is empty")
    parser.add_argument("--selftest", action="store_true", help="Run an in-process smoke test and exit")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)

    overrides: dict[str, Any] = {}
    if args.db:
        overrides.setdefault("jobs", {})["db_path"] = args.db
    if args.log_level:
        overrides.setdefault("logging", {})["level"] = args.log_level
    config = load_config(args.config, overrides=overrides)
    _configure_logging(str(config["logging"]["level"]))

    db_path = Path(str(config["jobs"]["db_path"]))
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)

    if args.selftest:
        result = anyio.run(_selftest, config)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    impl = JobBoardServer(config, auto_seed=not args.reset)
    if args.transport == "streamable-http":
        logger.info("starting streamable-http transport on %s:%s", args.host, args.port)
        impl.server.run("streamable-http", host=args.host, port=args.port, json_response=True)
    else:
        logger.info("starting stdio transport")
        impl.server.run("stdio")


if __name__ == "__main__":
    main()
