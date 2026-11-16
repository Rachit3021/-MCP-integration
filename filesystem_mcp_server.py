"""
filesystem_mcp_server.py
========================

Milestone 2, Part A: a production-style Model Context Protocol (MCP) server
that exposes the Milestone 1 file-system tools (``fs_tools.py``) over the
standard MCP interface.

Why this file exists
--------------------
In Milestone 1 the agent imported ``fs_tools`` directly. That works for one
application, but every new agent had to duplicate tool schemas, path safety and
error handling. MCP turns those functions into a reusable service:

- **Tools** (model-controlled actions):
  ``read_file``, ``list_files``, ``write_file``, ``search_in_file`` and the new
  ``watch_directory`` and ``batch_process`` capabilities, plus ``get_config``
  and ``get_stats`` for introspection.
- **Resources** (application-controlled read-only context):
  ``resumes://catalog``, ``resumes://summary``, ``resumes://file/{name}``,
  ``workspace://config``, ``workspace://stats`` and ``workspace://file/{+path}``.
- **JSON-RPC 2.0**: the official MCP SDK owns the wire protocol, including the
  ``initialize`` handshake, capability negotiation, standard error codes
  (``-32601`` method not found, ``-32602`` invalid params, ``-32603`` internal
  error) and MCP's in-band tool errors (``isError: true``).

Run it
------
    python filesystem_mcp_server.py                 # stdio (used by the agent)
    python filesystem_mcp_server.py --selftest      # one-process smoke test
    python filesystem_mcp_server.py --transport streamable-http --port 8765

Important stdio rule: **never write logs to stdout**. Stdout is the JSON-RPC
channel. All diagnostics go to stderr via ``logging``.
"""

from __future__ import annotations

import argparse
import functools
import inspect
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp.types import ToolAnnotations

import fs_tools
from mcp_config import load_config, public_config

logger = logging.getLogger("filesystem_mcp_server")

SERVER_NAME = "filesystem-mcp"
SERVER_VERSION = "2.0.0"

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

BATCH_OPERATIONS = {"metadata", "read", "search", "skills", "match"}
IGNORED_DIRECTORIES = {".git", ".hg", ".svn", "__pycache__", "venv", ".venv", "node_modules"}

TOOL_NAMES = [
    "read_file",
    "list_files",
    "write_file",
    "search_in_file",
    "watch_directory",
    "batch_process",
    "get_config",
    "get_stats",
]
STATIC_RESOURCES = [
    "resumes://catalog",
    "resumes://summary",
    "workspace://config",
    "workspace://stats",
]
RESOURCE_TEMPLATES = [
    "resumes://file/{name}",
    "workspace://file/{+path}",
]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _error(message: str, code: str = "ERROR", **extra: Any) -> dict[str, Any]:
    """Consistent, model-readable error payload (never raise for expected errors)."""
    return {"success": False, "error": message, "error_code": code, **extra}


def _is_error_payload(value: Any) -> bool:
    if isinstance(value, dict):
        return value.get("success") is False
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        return value[0].get("success") is False
    return False


def _normalise_extension(value: str | None) -> str | None:
    if not value or not str(value).strip():
        return None
    ext = str(value).strip().lower()
    return ext if ext.startswith(".") else f".{ext}"


def _normalise_extensions(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        ext = _normalise_extension(str(value))
        if ext and ext not in out:
            out.append(ext)
    return out


def _dedupe_keywords(values: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        keyword = str(value).strip()
        if not keyword:
            continue
        key = keyword.casefold()
        if key not in seen:
            seen.add(key)
            out.append(keyword)
    return out


def _keyword_present(text: str, keyword: str) -> bool:
    """Whole-token, case-insensitive match that tolerates whitespace line breaks."""
    parts = [re.escape(part) for part in str(keyword).split()]
    if not parts:
        return False
    pattern = r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class ServerStats:
    """Thread-safe counters surfaced through ``workspace://stats``."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self._lock = threading.Lock()
        self.tool_calls = 0
        self.tool_errors = 0
        self.resource_reads = 0
        self.resource_errors = 0
        self.total_tool_ms = 0.0
        self.by_tool: dict[str, dict[str, Any]] = {}

    def record_tool(self, name: str, duration_ms: float, ok: bool) -> None:
        with self._lock:
            self.tool_calls += 1
            self.total_tool_ms += duration_ms
            if not ok:
                self.tool_errors += 1
            entry = self.by_tool.setdefault(name, {"calls": 0, "errors": 0, "total_ms": 0.0})
            entry["calls"] += 1
            entry["errors"] += 0 if ok else 1
            entry["total_ms"] = round(entry["total_ms"] + duration_ms, 2)

    def record_resource(self, ok: bool) -> None:
        with self._lock:
            self.resource_reads += 1
            if not ok:
                self.resource_errors += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            by_tool = {
                name: {
                    "calls": data["calls"],
                    "errors": data["errors"],
                    "total_ms": round(data["total_ms"], 2),
                    "avg_ms": round(data["total_ms"] / data["calls"], 2) if data["calls"] else 0.0,
                }
                for name, data in self.by_tool.items()
            }
            return {
                "tool_calls": self.tool_calls,
                "tool_errors": self.tool_errors,
                "resource_reads": self.resource_reads,
                "resource_errors": self.resource_errors,
                "total_tool_ms": round(self.total_tool_ms, 2),
                "by_tool": by_tool,
            }


class FilesystemMCPServer:
    """Builds an ``MCPServer`` bound to one workspace and one configuration."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or load_config()
        self.workspace = Path(str(self.config["workspace"]["root"])).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.stats = ServerStats()
        self._watch_state: dict[str, dict[str, dict[str, Any]]] = {}
        self._watch_lock = threading.Lock()
        self._allowed_read = set(self.config["workspace"]["allowed_read_extensions"])
        self._blocked_write = set(self.config["workspace"]["blocked_write_extensions"])
        self._lexicon = list(dict.fromkeys(self.config["skills"]["lexicon"]))

        self.server = MCPServer(
            name=str(self.config["server"]["name"]),
            title=str(self.config["server"].get("title") or ""),
            version=str(self.config["server"]["version"]),
            instructions=str(self.config["server"].get("instructions") or ""),
            log_level=str(self.config["logging"]["level"]).upper(),
        )
        self._register_tools()
        self._register_resources()
        logger.info(
            "filesystem MCP server ready: workspace=%s tools=%d resources=%d",
            self.workspace,
            len(TOOL_NAMES),
            len(STATIC_RESOURCES) + len(RESOURCE_TEMPLATES),
        )

    # ------------------------------------------------------------------ #
    # Path safety
    # ------------------------------------------------------------------ #
    def _resolve(self, user_path: str, *, label: str = "path") -> Path:
        if not user_path or not str(user_path).strip():
            raise ValueError(f"{label} must be a non-empty string")
        candidate = Path(str(user_path)).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.workspace):
            raise PermissionError(f"Access outside the workspace is not allowed: {user_path}")
        return resolved

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.workspace).as_posix()
        except ValueError:
            return str(path)

    def _check_readable(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return _error(f"File not found: {self._relative(path)}", "NOT_FOUND")
        if not path.is_file():
            return _error(f"Path is not a file: {self._relative(path)}", "INVALID_PATH")
        extension = path.suffix.lower()
        if extension not in self._allowed_read:
            return _error(
                f"Unsupported file type '{extension or '(none)'}'. "
                f"Supported: {', '.join(sorted(self._allowed_read))}",
                "UNSUPPORTED_TYPE",
            )
        size = path.stat().st_size
        if size > int(self.config["workspace"]["max_file_bytes"]):
            return _error(
                f"File is too large ({_human_size(size)}); limit is "
                f"{_human_size(int(self.config['workspace']['max_file_bytes']))}",
                "FILE_TOO_LARGE",
            )
        return None

    # ------------------------------------------------------------------ #
    # Tool registration + stats wrapper
    # ------------------------------------------------------------------ #
    def _register_tools(self) -> None:
        self._add_tool("read_file", self.read_file, READ_ONLY)
        self._add_tool("list_files", self.list_files, READ_ONLY)
        self._add_tool("write_file", self.write_file, WRITE)
        self._add_tool("search_in_file", self.search_in_file, READ_ONLY)
        self._add_tool("watch_directory", self.watch_directory, READ_ONLY)
        self._add_tool("batch_process", self.batch_process, READ_ONLY)
        self._add_tool("get_config", self.get_config, READ_ONLY)
        self._add_tool("get_stats", self.get_stats, READ_ONLY)

    def _add_tool(self, name: str, fn: Any, annotations: ToolAnnotations) -> None:
        """Register a tool and record duration/error counters around every call."""
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                try:
                    result = await fn(*args, **kwargs)
                except Exception:
                    self.stats.record_tool(name, (time.perf_counter() - start) * 1000, False)
                    raise
                self.stats.record_tool(name, (time.perf_counter() - start) * 1000, not _is_error_payload(result))
                self._raise_if_error(result)
                return result

        else:

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                try:
                    result = fn(*args, **kwargs)
                except Exception:
                    self.stats.record_tool(name, (time.perf_counter() - start) * 1000, False)
                    raise
                self.stats.record_tool(name, (time.perf_counter() - start) * 1000, not _is_error_payload(result))
                self._raise_if_error(result)
                return result

        self.server.add_tool(wrapper, name=name, annotations=annotations)

    @staticmethod
    def _raise_if_error(result: Any) -> None:
        """Turn structured error payloads into MCP ``isError: true`` results.

        The JSON payload is preserved as the error text, so an agent still gets
        the machine-readable details (``error_code``, path, ...) while MCP
        clients see the standard tool-failure flag.
        """
        if _is_error_payload(result):
            raise ToolError(json.dumps(result, ensure_ascii=False, default=str))

    # ------------------------------------------------------------------ #
    # Tool 1: read_file
    # ------------------------------------------------------------------ #
    def read_file(self, filepath: str) -> dict[str, Any]:
        """Read a resume or document and return its extracted text plus metadata.

        Supports .pdf, .docx, .txt and .md. Paths are relative to the server
        workspace. Failures return ``{"success": false, ...}`` instead of raising,
        so an agent can recover.
        """
        try:
            path = self._resolve(filepath, label="filepath")
        except (ValueError, PermissionError) as exc:
            return _error(str(exc), "INVALID_PATH", filepath=filepath)

        problem = self._check_readable(path)
        if problem:
            return problem

        result = fs_tools.read_file(str(path))
        if not result.get("success"):
            return _error(result.get("error", "Could not read file"), "READ_FAILED", filepath=filepath)

        result["filepath"] = self._relative(path)
        if isinstance(result.get("metadata"), dict):
            result["metadata"]["path"] = self._relative(path)
        return result

    # ------------------------------------------------------------------ #
    # Tool 2: list_files
    # ------------------------------------------------------------------ #
    def list_files(self, directory: str = ".", extension: str | None = None) -> dict[str, Any]:
        """List files in a directory (non-recursive) with size and modified date.

        ``extension`` is optional, e.g. ".pdf" or "docx". Hidden files are skipped.
        """
        try:
            path = self._resolve(directory, label="directory")
        except (ValueError, PermissionError) as exc:
            return _error(str(exc), "INVALID_PATH", directory=directory)

        if not path.exists():
            return _error(f"Directory not found: {directory}", "NOT_FOUND", directory=directory)
        if not path.is_dir():
            return _error(f"Path is not a directory: {directory}", "INVALID_PATH", directory=directory)

        ext_filter = _normalise_extension(extension)
        files = fs_tools.list_files(str(path), ext_filter)
        if files and isinstance(files[0], dict) and files[0].get("success") is False:
            return _error(files[0].get("error", "Could not list directory"), "LIST_FAILED", directory=directory)

        for entry in files:
            entry["path"] = self._relative(Path(entry["path"]))
        return {
            "success": True,
            "directory": self._relative(path),
            "extension": ext_filter,
            "count": len(files),
            "files": files,
        }

    # ------------------------------------------------------------------ #
    # Tool 3: write_file
    # ------------------------------------------------------------------ #
    def write_file(self, filepath: str, content: str, append: bool = False) -> dict[str, Any]:
        """Write plain text (Markdown, JSON, ...) to a file.

        Parent directories are created automatically. Binary formats such as
        .pdf and .docx are refused because writing text into them corrupts them.
        Set ``append=true`` to append instead of overwrite.
        """
        try:
            path = self._resolve(filepath, label="filepath")
        except (ValueError, PermissionError) as exc:
            return _error(str(exc), "INVALID_PATH", filepath=filepath)

        if content is None:
            return _error("content must not be None", "INVALID_ARGUMENT", filepath=filepath)
        if path.suffix.lower() in self._blocked_write:
            return _error(
                f"write_file writes plain text and cannot produce '{path.suffix}' files",
                "UNSUPPORTED_TYPE",
                filepath=filepath,
            )

        result = fs_tools.write_file(str(path), str(content), append=bool(append))
        if not result.get("success"):
            return _error(result.get("error", "Could not write file"), "WRITE_FAILED", filepath=filepath)
        result["filepath"] = self._relative(path)
        return result

    # ------------------------------------------------------------------ #
    # Tool 4: search_in_file
    # ------------------------------------------------------------------ #
    def search_in_file(
        self,
        filepath: str,
        keyword: str,
        context_chars: int = 80,
        max_matches: int = 25,
    ) -> dict[str, Any]:
        """Case-insensitive keyword/phrase search inside one document.

        Returns the total match count and each match with surrounding context.
        Multi-word phrases match across line breaks in extracted PDF text.
        """
        try:
            path = self._resolve(filepath, label="filepath")
        except (ValueError, PermissionError) as exc:
            return _error(str(exc), "INVALID_PATH", filepath=filepath)

        problem = self._check_readable(path)
        if problem:
            return problem

        result = fs_tools.search_in_file(
            str(path),
            keyword,
            context_chars=max(0, min(int(context_chars), 1000)),
            max_matches=max(1, min(int(max_matches), 200)),
        )
        if not result.get("success"):
            return _error(result.get("error", "Search failed"), "SEARCH_FAILED", filepath=filepath)
        result["filepath"] = self._relative(path)
        return result

    # ------------------------------------------------------------------ #
    # Tool 5: watch_directory (new MCP capability)
    # ------------------------------------------------------------------ #
    async def watch_directory(
        self,
        directory: str = "resumes",
        extensions: list[str] | None = None,
        recursive: bool = False,
        reset: bool = False,
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        """Monitor a directory for new, modified or removed resume files.

        The first call (or ``reset=true``) establishes a baseline. Later calls
        diff against it and return ``new``/``modified``/``removed`` entries.
        ``wait_seconds`` long-polls until a change appears or the configured
        maximum wait elapses, which is useful for an "inbox" workflow.
        """
        try:
            path = self._resolve(directory, label="directory")
        except (ValueError, PermissionError) as exc:
            return _error(str(exc), "INVALID_PATH", directory=directory)

        if not path.exists() or not path.is_dir():
            return _error(f"Directory not found: {directory}", "NOT_FOUND", directory=directory)

        exts = _normalise_extensions(extensions) if extensions else list(
            self.config["watch"]["default_extensions"]
        )
        recursive = bool(recursive)
        key = f"{path}|recursive={recursive}|ext={','.join(exts) or '*'}"
        poll = float(self.config["watch"]["poll_interval_seconds"])
        max_wait = float(self.config["watch"]["max_wait_seconds"])
        wait = max(0.0, min(float(wait_seconds or 0.0), max_wait))
        started = time.perf_counter()

        def snapshot() -> dict[str, dict[str, Any]]:
            return self._snapshot(path, exts, recursive)

        current = snapshot()
        with self._watch_lock:
            previous = self._watch_state.get(key)
            if previous is None or reset:
                self._watch_state[key] = current
                baseline = True
            else:
                baseline = False

        if baseline:
            return {
                "success": True,
                "directory": self._relative(path),
                "baseline": True,
                "recursive": recursive,
                "extensions": exts,
                "snapshot_time": _iso(time.time()),
                "counts": {"current": len(current), "new": 0, "modified": 0, "removed": 0},
                "files": [self._public_entry(value) for value in current.values()],
                "changed": False,
                "waited_ms": round((time.perf_counter() - started) * 1000, 1),
            }

        changes = self._diff(previous, current)
        deadline = time.monotonic() + wait
        while wait > 0 and not changes["changed"] and time.monotonic() < deadline:
            await anyio.sleep(poll)
            current = snapshot()
            changes = self._diff(previous, current)

        with self._watch_lock:
            self._watch_state[key] = current

        return {
            "success": True,
            "directory": self._relative(path),
            "baseline": False,
            "recursive": recursive,
            "extensions": exts,
            "snapshot_time": _iso(time.time()),
            "counts": {
                "current": len(current),
                "new": len(changes["new"]),
                "modified": len(changes["modified"]),
                "removed": len(changes["removed"]),
                "unchanged": len(current) - len(changes["new"]) - len(changes["modified"]),
            },
            "new": changes["new"],
            "modified": changes["modified"],
            "removed": changes["removed"],
            "changed": changes["changed"],
            "waited_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def _iter_files(self, directory: Path, extensions: list[str], recursive: bool):
        iterator = directory.rglob("*") if recursive else directory.iterdir()
        for entry in iterator:
            try:
                # Symlinks can point outside the workspace; the sandbox must not follow them
                # when walking a directory tree.
                if entry.is_symlink() or not entry.is_file() or entry.name.startswith("."):
                    continue
                if recursive and any(part in IGNORED_DIRECTORIES for part in entry.relative_to(directory).parts):
                    continue
                if extensions and entry.suffix.lower() not in extensions:
                    continue
                yield entry
            except OSError:
                continue

    def _snapshot(self, directory: Path, extensions: list[str], recursive: bool) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for entry in self._iter_files(directory, extensions, recursive):
            try:
                stat = entry.stat()
            except OSError:
                continue
            relative = self._relative(entry)
            out[relative] = {
                "name": entry.name,
                "path": relative,
                "size_bytes": stat.st_size,
                "size_human": _human_size(stat.st_size),
                "modified": _iso(stat.st_mtime),
                "mtime_ns": stat.st_mtime_ns,
            }
        return out

    @staticmethod
    def _public_entry(entry: dict[str, Any]) -> dict[str, Any]:
        """Copy a snapshot entry without the internal mtime used for diffing."""
        return {key: value for key, value in entry.items() if key != "mtime_ns"}

    @classmethod
    def _diff(cls, previous: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]]) -> dict[str, Any]:
        new = [cls._public_entry(current[key]) for key in sorted(current.keys() - previous.keys())]
        removed = [cls._public_entry(previous[key]) for key in sorted(previous.keys() - current.keys())]
        modified = [
            cls._public_entry(current[key])
            for key in sorted(previous.keys() & current.keys())
            if previous[key].get("mtime_ns") != current[key].get("mtime_ns")
            or previous[key].get("size_bytes") != current[key].get("size_bytes")
        ]
        changed = bool(new or removed or modified)
        return {"new": new, "modified": modified, "removed": removed, "changed": changed}

    # ------------------------------------------------------------------ #
    # Tool 6: batch_process (new MCP capability)
    # ------------------------------------------------------------------ #
    async def batch_process(
        self,
        directory: str = "resumes",
        operations: list[str] | None = None,
        extension: str | None = None,
        recursive: bool = False,
        keywords: list[str] | None = None,
        max_files: int | None = None,
    ) -> dict[str, Any]:
        """Process many documents in one MCP call.

        ``operations`` is any combination of: ``metadata``, ``read``, ``search``,
        ``skills`` and ``match``. ``search`` and ``match`` require ``keywords``.
        File parsing runs in a bounded thread pool, and each file is read only
        once even when several operations are requested.
        """
        started = time.perf_counter()
        try:
            path = self._resolve(directory, label="directory")
        except (ValueError, PermissionError) as exc:
            return _error(str(exc), "INVALID_PATH", directory=directory)

        if not path.exists() or not path.is_dir():
            return _error(f"Directory not found: {directory}", "NOT_FOUND", directory=directory)

        ops = [str(op).strip().lower() for op in (operations or ["metadata"]) if str(op).strip()]
        if not ops:
            ops = ["metadata"]
        unknown = sorted(set(ops) - BATCH_OPERATIONS)
        if unknown:
            return _error(
                f"Unknown operation(s): {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(BATCH_OPERATIONS))}",
                "INVALID_ARGUMENT",
                operations=ops,
            )

        keyword_list = _dedupe_keywords(keywords)
        if ("search" in ops or "match" in ops) and not keyword_list:
            return _error(
                "keywords must be a non-empty list when using 'search' or 'match'",
                "INVALID_ARGUMENT",
                operations=ops,
            )

        extension_filter = _normalise_extension(extension)
        exts = [extension_filter] if extension_filter else []
        recursive = bool(recursive)
        configured_max = int(self.config["batch"]["max_files"])
        requested_max = configured_max if max_files is None else max(1, int(max_files))
        limit = min(configured_max, requested_max)

        all_files = list(self._iter_files(path, exts, recursive))
        all_files.sort(key=lambda item: self._relative(item).lower())
        truncated = len(all_files) > limit
        files = all_files[:limit]

        if not files:
            return {
                "success": True,
                "directory": self._relative(path),
                "operations": ops,
                "keywords": keyword_list,
                "files_processed": 0,
                "files_failed": 0,
                "results": [],
                "truncated": False,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }

        concurrency = int(self.config["batch"]["concurrency"])
        preview_chars = int(self.config["batch"]["text_preview_chars"])
        match_limit = int(self.config["batch"]["max_matches_per_keyword"])

        def process_one(file_path: Path) -> dict[str, Any]:
            relative = self._relative(file_path)
            doc = fs_tools.read_file(str(file_path))
            if not doc.get("success"):
                return {
                    "file": file_path.name,
                    "path": relative,
                    "success": False,
                    "error": doc.get("error", "Could not read file"),
                    "error_code": "READ_FAILED",
                }

            text = doc.get("content", "")
            metadata = dict(doc.get("metadata") or {})
            metadata["path"] = relative
            result: dict[str, Any] = {
                "file": file_path.name,
                "path": relative,
                "success": True,
                "metadata": metadata,
            }
            if "read" in ops:
                result["text"] = text[:preview_chars]
                result["text_truncated"] = len(text) > preview_chars
            if "search" in ops:
                result["search"] = [
                    fs_tools.search_in_text(
                        text,
                        keyword,
                        context_chars=80,
                        max_matches=match_limit,
                        filepath=relative,
                    )
                    for keyword in keyword_list
                ]
            if "skills" in ops:
                result["skills"] = self._extract_skills(text)
            if "match" in ops:
                result["match"] = self._score_match(text, keyword_list)
            return result

        def run_batch() -> list[dict[str, Any]]:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                return list(pool.map(process_one, files))

        # Keep the event loop responsive while PDF/DOCX parsing happens.
        results = await anyio.to_thread.run_sync(run_batch)

        if "match" in ops:
            results.sort(key=lambda item: item.get("match", {}).get("score", -1), reverse=True)
            for rank, item in enumerate(results, start=1):
                item["rank"] = rank

        failed = sum(1 for item in results if not item.get("success"))
        return {
            "success": True,
            "directory": self._relative(path),
            "operations": ops,
            "keywords": keyword_list,
            "recursive": recursive,
            "extension": extension_filter,
            "concurrency": concurrency,
            "files_processed": len(results),
            "files_failed": failed,
            "truncated": truncated,
            "max_files": limit,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "results": results,
        }

    def _extract_skills(self, text: str) -> list[str]:
        return [skill for skill in self._lexicon if _keyword_present(text, skill)]

    @staticmethod
    def _score_match(text: str, keywords: list[str]) -> dict[str, Any]:
        matched = [keyword for keyword in keywords if _keyword_present(text, keyword)]
        missing = [keyword for keyword in keywords if keyword not in matched]
        total = len(keywords)
        coverage = len(matched) / total if total else 0.0
        return {
            "score": round(coverage * 100, 1),
            "coverage": round(coverage, 4),
            "matched": matched,
            "missing": missing,
            "matched_count": len(matched),
            "total_keywords": total,
        }

    # ------------------------------------------------------------------ #
    # Introspection tools
    # ------------------------------------------------------------------ #
    def get_config(self) -> dict[str, Any]:
        """Return the effective server configuration and exposed capabilities."""
        return {
            "success": True,
            "server": {
                "name": self.config["server"]["name"],
                "title": self.config["server"].get("title"),
                "version": self.config["server"]["version"],
                "workspace": str(self.workspace),
                "protocol": "MCP / JSON-RPC 2.0",
            },
            "config": public_config(self.config),
            "tools": TOOL_NAMES,
            "resources": STATIC_RESOURCES,
            "resource_templates": RESOURCE_TEMPLATES,
        }

    def get_stats(self) -> dict[str, Any]:
        """Return runtime counters, uptime and watched-directory state."""
        snapshot = self.stats.snapshot()
        snapshot.update(
            {
                "success": True,
                "server": self.config["server"]["name"],
                "version": self.config["server"]["version"],
                "uptime_seconds": round(time.time() - self.stats.started_at, 2),
                "workspace": str(self.workspace),
                "watched_directories": len(self._watch_state),
            }
        )
        return snapshot

    # ------------------------------------------------------------------ #
    # Resources
    # ------------------------------------------------------------------ #
    def _register_resources(self) -> None:
        self.server.resource(
            "resumes://catalog",
            name="Resume catalog",
            description="JSON catalog of resume files with size and modified date.",
            mime_type="application/json",
        )(self.resume_catalog)
        self.server.resource(
            "resumes://summary",
            name="Resume collection summary",
            description="Aggregate counts, sizes and skill frequency across resumes.",
            mime_type="application/json",
        )(self.resume_summary)
        self.server.resource(
            "resumes://file/{name}",
            name="Resume text",
            description="Extracted text of one resume in the resumes directory.",
            mime_type="text/plain",
        )(self.read_resume_resource)
        self.server.resource(
            "workspace://config",
            name="Server configuration",
            description="Effective MCP server configuration and capabilities.",
            mime_type="application/json",
        )(self.workspace_config_resource)
        self.server.resource(
            "workspace://stats",
            name="Server statistics",
            description="Runtime counters and watch state for this MCP server.",
            mime_type="application/json",
        )(self.workspace_stats_resource)
        self.server.resource(
            "workspace://file/{+path}",
            name="Workspace file text",
            description="Extracted text of any supported file inside the workspace.",
            mime_type="text/plain",
        )(self.read_workspace_file_resource)

    def _resume_dir(self) -> Path:
        return self._resolve(self.config["resumes"]["directory"], label="resumes.directory")

    def resume_catalog(self) -> str:
        """Return a JSON catalog of the resumes directory."""
        directory = self._resume_dir()
        if not directory.exists():
            return json.dumps(
                {"success": False, "error": f"Resumes directory not found: {self._relative(directory)}"},
                indent=2,
            )
        files = fs_tools.list_files(str(directory))
        if files and isinstance(files[0], dict) and files[0].get("success") is False:
            return json.dumps({"success": False, "error": files[0].get("error")}, indent=2)
        for entry in files:
            entry["path"] = self._relative(Path(entry["path"]))
        return json.dumps(
            {
                "success": True,
                "directory": self._relative(directory),
                "count": len(files),
                "files": files,
            },
            indent=2,
        )

    def resume_summary(self) -> str:
        """Return aggregate statistics and a skill histogram for all resumes."""
        directory = self._resume_dir()
        if not directory.exists():
            return json.dumps(
                {"success": False, "error": f"Resumes directory not found: {self._relative(directory)}"},
                indent=2,
            )

        skill_counts: dict[str, int] = {}
        candidates: list[dict[str, Any]] = []
        by_extension: dict[str, int] = {}
        total_bytes = 0
        total_words = 0
        failures: list[dict[str, str]] = []

        for entry in sorted(directory.glob("*"), key=lambda p: p.name.lower()):
            if not entry.is_file() or entry.name.startswith("."):
                continue
            extension = entry.suffix.lower()
            if extension not in self._allowed_read:
                continue
            by_extension[extension] = by_extension.get(extension, 0) + 1
            total_bytes += entry.stat().st_size
            doc = fs_tools.read_file(str(entry))
            if not doc.get("success"):
                failures.append({"file": entry.name, "error": doc.get("error", "read failed")})
                continue
            text = doc.get("content", "")
            total_words += len(text.split())
            skills = self._extract_skills(text)
            for skill in skills:
                skill_counts[skill] = skill_counts.get(skill, 0) + 1
            candidates.append(
                {
                    "file": entry.name,
                    "path": self._relative(entry),
                    "skills": skills,
                    "word_count": len(text.split()),
                }
            )

        top_skills = sorted(skill_counts.items(), key=lambda item: (-item[1], item[0]))
        return json.dumps(
            {
                "success": True,
                "directory": self._relative(directory),
                "resume_count": sum(by_extension.values()),
                "by_extension": by_extension,
                "total_size_bytes": total_bytes,
                "total_size_human": _human_size(total_bytes),
                "total_words": total_words,
                "skill_frequency": [{"skill": name, "resumes": count} for name, count in top_skills],
                "candidates": candidates,
                "read_failures": failures,
            },
            indent=2,
        )

    def _read_resource_file(self, path: Path, uri: str) -> str:
        problem = self._check_readable(path)
        if problem:
            raise ResourceNotFoundError(problem.get("error", f"Unknown resource: {uri}"))
        result = fs_tools.read_file(str(path))
        if not result.get("success"):
            raise ResourceError(result.get("error", f"Could not read resource: {uri}"))
        return result.get("content", "")

    def read_resume_resource(self, name: str) -> str:
        """Read one resume by filename through a resource template."""
        cleaned = unquote(str(name))
        if not cleaned or Path(cleaned).name != cleaned:
            raise ResourceNotFoundError(f"Invalid resume name: {name}")
        path = (self._resume_dir() / cleaned).resolve()
        if not path.is_relative_to(self.workspace):
            raise ResourceNotFoundError(f"Invalid resume name: {name}")
        return self._read_resource_file(path, f"resumes://file/{cleaned}")

    def read_workspace_file_resource(self, path: str) -> str:
        """Read any supported workspace file through a resource template."""
        cleaned = unquote(str(path))
        try:
            resolved = self._resolve(cleaned, label="path")
        except (ValueError, PermissionError) as exc:
            raise ResourceNotFoundError(str(exc)) from exc
        return self._read_resource_file(resolved, f"workspace://file/{cleaned}")

    def workspace_config_resource(self) -> str:
        """Return the effective server configuration as JSON."""
        return json.dumps(self.get_config(), indent=2)

    def workspace_stats_resource(self) -> str:
        """Return runtime statistics as JSON, including watch baselines."""
        snapshot = self.get_stats()
        with self._watch_lock:
            snapshot["watch_state"] = [
                {"key": key, "files": len(value)} for key, value in self._watch_state.items()
            ]
        return json.dumps(snapshot, indent=2)


# --------------------------------------------------------------------------- #
# Factory / CLI
# --------------------------------------------------------------------------- #
def build_server(config: dict[str, Any] | None = None) -> MCPServer:
    """Create a ready-to-run MCP server (also used by tests and demo scripts)."""
    return FilesystemMCPServer(config).server


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _selftest(config: dict[str, Any]) -> dict[str, Any]:
    """Exercise the server through the official in-process MCP client."""
    from mcp import Client

    impl = FilesystemMCPServer(config)
    async with Client(impl.server, mode="legacy") as client:
        tools = await client.list_tools()
        resources = await client.list_resources()
        templates = await client.list_resource_templates()
        listing = await client.call_tool(
            "list_files",
            {"directory": str(config["resumes"]["directory"])},
        )
        config_result = await client.call_tool("get_config", {})
        stats_result = await client.call_tool("get_stats", {})
        catalog = await client.read_resource("resumes://catalog")
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
        "list_files": _payload(listing),
        "config": _payload(config_result),
        "stats": _payload(stats_result),
        "catalog": json.loads(catalog.contents[0].text),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Filesystem MCP server (JSON-RPC 2.0 over stdio)")
    parser.add_argument("--config", help="Path to mcp_config.json")
    parser.add_argument("--workspace", help="Override workspace root")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING or ERROR")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="stdio for agents; streamable-http for networked deployment",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--selftest", action="store_true", help="Run an in-process smoke test and exit")
    args = parser.parse_args(argv)

    overrides: dict[str, Any] = {}
    if args.workspace:
        overrides.setdefault("workspace", {})["root"] = args.workspace
    if args.log_level:
        overrides.setdefault("logging", {})["level"] = args.log_level

    config = load_config(args.config, overrides=overrides)
    _configure_logging(str(config["logging"]["level"]))

    if args.selftest:
        result = anyio.run(_selftest, config)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    impl = FilesystemMCPServer(config)
    if args.transport == "streamable-http":
        logger.info("starting streamable-http transport on %s:%s", args.host, args.port)
        impl.server.run("streamable-http", host=args.host, port=args.port, json_response=True)
    else:
        logger.info("starting stdio transport")
        impl.server.run("stdio")


if __name__ == "__main__":
    main()
