"""Shared helpers for the MCP test suite (not collected by pytest)."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any, Awaitable

from mcp_config import load_config

PROJECT_ROOT = Path(__file__).resolve().parent


def copy_resumes(destination: Path) -> Path:
    target = destination / "resumes"
    shutil.copytree(PROJECT_ROOT / "resumes", target)
    return target


def make_config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """Config rooted in a temporary workspace with a temporary jobs database."""
    base = {
        "workspace": {"root": str(tmp_path)},
        "jobs": {"db_path": str(tmp_path / "data" / "jobs.db")},
        "logging": {"level": "ERROR"},
    }
    for key, value in overrides.items():
        base[key] = {**base.get(key, {}), **value} if isinstance(value, dict) else value
    return load_config(overrides=base)


def run(coroutine: Awaitable[Any]) -> Any:
    return asyncio.run(coroutine)


def payload(result: Any) -> Any:
    """Decode the first text block of an MCP tool result."""
    for block in result.content:
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return block.text
    return None


__all__ = ["PROJECT_ROOT", "copy_resumes", "make_config", "run", "payload"]
