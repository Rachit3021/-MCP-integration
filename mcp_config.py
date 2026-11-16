"""
mcp_config.py
=============

Single source of truth for configuration shared by:

- ``filesystem_mcp_server.py`` (server-side limits, workspace, watch settings)
- ``job_board_mcp_server.py`` (SQLite database location)
- ``matching_agent.py`` (MCP server commands, model and graph settings)

Precedence (lowest to highest):

1. Built-in ``DEFAULTS``
2. ``mcp_config.json`` (or the file named by ``MCP_CONFIG``)
3. Environment overrides (``MCP_WORKSPACE``, ``MCP_CONCURRENCY`` ...)
4. Explicit ``overrides`` argument

The loader also normalises paths and validates numeric ranges, so a bad config
fails fast with a clear message instead of producing strange runtime behaviour.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "mcp_config.json"

DEFAULT_SKILLS = [
    "Python", "Django", "FastAPI", "Flask", "PostgreSQL", "MySQL", "MongoDB",
    "Redis", "Docker", "Kubernetes", "AWS", "Azure", "GCP", "Terraform",
    "GitHub Actions", "Jenkins", "CI/CD", "Linux", "Bash", "Airflow", "ETL",
    "scikit-learn", "pandas", "matplotlib", "Tableau", "Excel", "SQL",
    "A/B testing", "Statistics", "Machine Learning", "Java", "Spring Boot",
    "Hibernate", "Kafka", "Microservices", "REST", "GraphQL", "JavaScript",
    "TypeScript", "React", "Next.js", "Tailwind CSS", "Jest", "Figma",
    "Prometheus", "Product strategy", "Roadmapping", "Agile", "Scrum", "JIRA",
    "User research", "Data analysis", "Node.js", "Go", "C#",
]

DEFAULTS: dict[str, Any] = {
    "server": {
        "name": "filesystem-mcp",
        "title": "Filesystem MCP Server",
        "version": "2.0.0",
        "instructions": (
            "Resume workspace server. All paths are sandboxed to the configured "
            "workspace. Prefer batch_process for multi-file work."
        ),
    },
    "workspace": {
        "root": ".",
        "allowed_read_extensions": [".txt", ".md", ".pdf", ".docx"],
        "blocked_write_extensions": [
            ".pdf", ".docx", ".doc", ".xlsx", ".png", ".jpg", ".jpeg", ".zip",
        ],
        "max_file_bytes": 20 * 1024 * 1024,
    },
    "resumes": {"directory": "resumes"},
    "batch": {
        "max_files": 200,
        "concurrency": 4,
        "text_preview_chars": 2000,
        "max_matches_per_keyword": 5,
    },
    "watch": {
        "poll_interval_seconds": 0.5,
        "max_wait_seconds": 30,
        "default_extensions": [".pdf", ".docx", ".txt", ".md"],
    },
    "skills": {"lexicon": DEFAULT_SKILLS},
    "logging": {"level": "INFO"},
    "servers": {
        "filesystem": {
            "enabled": True,
            "command": "auto",
            "args": ["filesystem_mcp_server.py"],
            "cwd": ".",
            "env": {},
        },
        "jobs": {
            "enabled": True,
            "command": "auto",
            "args": ["job_board_mcp_server.py"],
            "cwd": ".",
            "env": {},
        },
    },
    "jobs": {"db_path": "data/jobs.db", "auto_seed": True},
    "agent": {
        "model": "openai/gpt-oss-120b",
        "base_url": "https://api.groq.com/openai/v1",
        "max_steps": 10,
        "temperature": 0.2,
        "max_tool_result_chars": 16000,
        "mcp_timeout_seconds": 60,
        "report_dir": "reports",
        "trace_dir": "runs",
        "default_job_id": "JOB-001",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into ``base`` (returns a new dict)."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _env_int(name: str) -> int | None:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip() else None


def _env_float(name: str) -> float | None:
    raw = os.getenv(name)
    return float(raw) if raw and raw.strip() else None


def _normalise_extensions(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values or []:
        ext = str(value).strip().lower()
        if not ext:
            continue
        out.append(ext if ext.startswith(".") else f".{ext}")
    return sorted(set(out))


def load_config(
    path: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load, merge, normalise and validate the process configuration."""
    env_path = os.getenv("MCP_CONFIG")
    config_path = Path(path or env_path or DEFAULT_CONFIG_PATH).expanduser()
    if not config_path.is_absolute():
        config_path = (PROJECT_ROOT / config_path).resolve()

    file_data: dict[str, Any] = {}
    if config_path.exists():
        try:
            file_data = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {config_path}: {exc}") from exc
        if not isinstance(file_data, dict):
            raise ValueError(f"Config root must be an object: {config_path}")

    cfg = _deep_merge(DEFAULTS, file_data)

    # --- Environment overrides ------------------------------------------- #
    if os.getenv("MCP_WORKSPACE"):
        cfg["workspace"]["root"] = os.environ["MCP_WORKSPACE"]
    elif os.getenv("WORKSPACE_DIR"):
        # Milestone 1 compatibility.
        cfg["workspace"]["root"] = os.environ["WORKSPACE_DIR"]
    if os.getenv("MCP_LOG_LEVEL"):
        cfg["logging"]["level"] = os.environ["MCP_LOG_LEVEL"].upper()
    if (value := _env_int("MCP_CONCURRENCY")) is not None:
        cfg["batch"]["concurrency"] = value
    if (value := _env_int("MCP_MAX_BATCH_FILES")) is not None:
        cfg["batch"]["max_files"] = value
    if (value := _env_float("MCP_MAX_FILE_MB")) is not None:
        cfg["workspace"]["max_file_bytes"] = int(value * 1024 * 1024)
    if (value := _env_float("MCP_WATCH_POLL")) is not None:
        cfg["watch"]["poll_interval_seconds"] = value
    if (value := _env_float("MCP_CLIENT_TIMEOUT")) is not None:
        cfg["agent"]["mcp_timeout_seconds"] = value
    if os.getenv("JOBS_DB"):
        cfg["jobs"]["db_path"] = os.environ["JOBS_DB"]
    if os.getenv("GROQ_MODEL"):
        cfg["agent"]["model"] = os.environ["GROQ_MODEL"]

    if overrides:
        cfg = _deep_merge(cfg, overrides)

    # --- Path resolution -------------------------------------------------- #
    cfg["_meta"] = {
        "config_path": str(config_path),
        "project_root": str(PROJECT_ROOT),
        "python": sys.executable,
    }

    workspace = Path(str(cfg["workspace"]["root"])).expanduser()
    if not workspace.is_absolute():
        workspace = (config_path.parent / workspace).resolve()
    else:
        workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    cfg["workspace"]["root"] = str(workspace)
    cfg["workspace"]["allowed_read_extensions"] = _normalise_extensions(
        cfg["workspace"]["allowed_read_extensions"]
    )
    cfg["workspace"]["blocked_write_extensions"] = _normalise_extensions(
        cfg["workspace"]["blocked_write_extensions"]
    )
    cfg["watch"]["default_extensions"] = _normalise_extensions(
        cfg["watch"]["default_extensions"]
    )

    db_path = Path(str(cfg["jobs"]["db_path"])).expanduser()
    if not db_path.is_absolute():
        db_path = (workspace / db_path).resolve()
    cfg["jobs"]["db_path"] = str(db_path)

    for key in ("report_dir", "trace_dir"):
        target = Path(str(cfg["agent"][key])).expanduser()
        if not target.is_absolute():
            target = (workspace / target).resolve()
        cfg["agent"][key] = str(target)

    for name, spec in cfg["servers"].items():
        cwd = Path(str(spec.get("cwd", "."))).expanduser()
        if not cwd.is_absolute():
            cwd = (config_path.parent / cwd).resolve()
        spec["cwd"] = str(cwd)
        spec["name"] = name
        spec.setdefault("enabled", True)
        spec.setdefault("env", {})
        spec.setdefault("args", [])

    _validate(cfg)
    return cfg


def _validate(cfg: dict[str, Any]) -> None:
    if cfg["batch"]["concurrency"] < 1 or cfg["batch"]["concurrency"] > 32:
        raise ValueError("batch.concurrency must be between 1 and 32")
    if cfg["batch"]["max_files"] < 1 or cfg["batch"]["max_files"] > 5000:
        raise ValueError("batch.max_files must be between 1 and 5000")
    if cfg["workspace"]["max_file_bytes"] < 1:
        raise ValueError("workspace.max_file_bytes must be positive")
    if cfg["watch"]["poll_interval_seconds"] <= 0:
        raise ValueError("watch.poll_interval_seconds must be positive")
    if cfg["watch"]["max_wait_seconds"] < 0:
        raise ValueError("watch.max_wait_seconds must be >= 0")
    if cfg["agent"]["max_steps"] < 1:
        raise ValueError("agent.max_steps must be >= 1")
    if cfg["agent"]["mcp_timeout_seconds"] <= 0:
        raise ValueError("agent.mcp_timeout_seconds must be positive")
    for name, spec in cfg["servers"].items():
        if not spec.get("args"):
            raise ValueError(f"servers.{name}.args must not be empty")


def server_specs(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return enabled MCP stdio launch specs with absolute commands and paths."""
    specs: dict[str, dict[str, Any]] = {}
    for name, spec in cfg["servers"].items():
        if not spec.get("enabled", True):
            continue
        command = spec.get("command", "auto")
        if command in ("auto", "python", "python3"):
            command = sys.executable

        env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
        env.setdefault("MCP_WORKSPACE", str(cfg["workspace"]["root"]))
        env.setdefault("MCP_CONFIG", str(cfg["_meta"]["config_path"]))
        env.setdefault("MCP_LOG_LEVEL", str(cfg["logging"]["level"]))
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        if name == "jobs":
            env.setdefault("JOBS_DB", str(cfg["jobs"]["db_path"]))

        specs[name] = {
            "name": name,
            "command": command,
            "args": [str(arg) for arg in spec.get("args", [])],
            "cwd": str(spec.get("cwd") or cfg["_meta"]["project_root"]),
            "env": env,
        }
    return specs


def public_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """A JSON-safe config copy for MCP resources and logs (no process internals)."""
    out = copy.deepcopy(cfg)
    out.pop("_meta", None)
    for spec in out.get("servers", {}).values():
        spec.pop("env", None)
    return out
