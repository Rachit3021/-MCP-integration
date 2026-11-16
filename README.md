# Resume Matching over MCP

**Milestone 2:** the Milestone 1 file-assistant tools are now two independent
[Model Context Protocol](https://modelcontextprotocol.io) servers, and a
LangGraph agent consumes them over JSON-RPC 2.0 — without importing any
filesystem, PDF, DOCX or SQLite code.

**Demo video:** (link coming soon)

```
CLI / demo.py ──> matching_agent.py (LangGraph)
                        │
                        ▼
              mcp_client_manager.py
                 │            │
        stdio / JSON-RPC      stdio / JSON-RPC
                 ▼            ▼
   filesystem_mcp_server   job_board_mcp_server
        (resumes, reports)      (SQLite jobs + matches)
```

## What changed from Milestone 1

| Milestone 1 | Milestone 2 |
| --- | --- |
| Agent imported `fs_tools.py` directly | Agent imports only the MCP client manager |
| Four local functions | 15 MCP tools across 2 servers: file read/list/write/search, watch, batch, jobs search/get/save/list/clear, config, stats |
| No resource model | 8 static resources + 4 URI templates, discovered over MCP |
| Synchronous loop | Explicit LangGraph state machine: `discover → plan → act → plan → finalize` |
| Single provider | Multi-MCP bonus: filesystem + SQLite job board |
| No persistence | Reports, JSON traces and match scores saved per run |

## Features

- **Part A — MCP filesystem server.** All Milestone 1 tools plus
  `watch_directory` (baseline/diff and long-poll) and `batch_process` (metadata,
  read, search, skills, match). Path sandboxing, structured errors and stats are
  enforced server-side.
- **Part B — LangGraph + MCP agent.** LLM function calling through Groq/OpenAI,
  with a deterministic offline planner for reproducible demos and tests.
- **Bonus — second MCP server.** SQLite job board with seven seeded roles,
  search, match persistence and its own resources.
- **JSON-RPC 2.0.** `initialize`, `tools/list`, `tools/call`, `resources/list`,
  `resources/templates/list`, `resources/read`, standard `-32601`/`-32602` errors
  and in-band `isError` tool failures.
- **Configuration management.** `mcp_config.json`, env overrides, absolute launch
  specs, validation and sandboxed paths.
- **Artifacts.** Markdown shortlist report, `runs/*.json` trace, SQLite match rows.

## Requirements

- Python 3.11+ (verified on 3.14.6)
- Windows, macOS or Linux
- No API key required for the offline planner or the automated tests

## Setup

```bash
git clone <your-repo-url> && cd llm-file-assistant
python -m venv venv
venv\Scripts\activate            # Windows
# source venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
cp .env.example .env             # optional: add GROQ_API_KEY for the LLM planner
python generate_sample_resumes.py
```

The sample resumes are already committed; run the generator only if you want to
recreate them.

## Quick start (offline, deterministic)

```bash
venv\Scripts\python.exe matching_agent.py --demo --offline
```

Expected result for `JOB-001` (Senior Python Engineer):

```
1. resume_john_doe.pdf     — 100.0% (8/8 required skills)
2. resume_rahul_verma.txt  — 50.0% (4/8 required skills)
3. resume_alex_chen.docx   — 12.5% (1/8 required skills)

Saved 6 match results to the job-board MCP server.
Report: reports/shortlist_JOB-001_<timestamp>.md
```

## Guided demo

```bash
venv\Scripts\python.exe demo.py all
```

| Step | What it shows |
| --- | --- |
| `discover` | 2 servers, 15 tools, 8 resources, 4 templates |
| `resources` | Reads job/resume/stat resources and one URI template |
| `batch` | Ranks six resumes for a role and persists scores |
| `watch` | Baseline → add file → `new=1` → remove file → `removed=1` |
| `agent` | Full LangGraph run, report and trace |

Individual commands:

```bash
venv\Scripts\python.exe demo.py discover
venv\Scripts\python.exe demo.py resources
venv\Scripts\python.exe demo.py batch --job JOB-001
venv\Scripts\python.exe demo.py watch
venv\Scripts\python.exe demo.py agent --job JOB-001          # offline
venv\Scripts\python.exe demo.py agent --job JOB-001 --llm    # Groq
```

## Run in VS Code

1. **File → Open Folder…** and choose this project folder (not an individual file).
2. **Ctrl+Shift+P → Python: Select Interpreter** → pick `.\venv\Scripts\python.exe`.
3. **Ctrl+Shift+D** (Run and Debug) → choose a configuration → **F5**.

Committed launch configurations:

| Configuration | What it runs |
| --- | --- |
| `Demo: all steps` | `demo.py all` — discovery, resources, batch, watch, agent |
| `Agent: offline demo` | `matching_agent.py --demo --offline` |
| `Agent: LLM demo` | `matching_agent.py --demo` (loads `.env`) |
| `Tests: pytest` | full test suite with `-v` |
| `Filesystem MCP: selftest` | server health check, then exits |
| `Job board MCP: selftest` | server health check, then exits |
| `Filesystem MCP: HTTP :8765` | streamable-http transport |
| `Job board MCP: HTTP :8766` | streamable-http transport |

Prefer the integrated terminal? **Ctrl+`** and run the commands from
[Guided demo](#guided-demo) or [Agent commands](#agent-commands).

> **Why "Run Python File" looks stuck:** the servers default to **stdio**, so
> they wait silently for a JSON-RPC client on stdin. That is normal. Use the
> `--selftest` configuration to check a server, or let `demo.py` /
> `matching_agent.py` launch it as a subprocess. Use the HTTP configurations if
> you want a server that runs on its own.

## Agent commands

```bash
# Full agent, offline planner
venv\Scripts\python.exe matching_agent.py --demo --offline

# Any role, custom request
venv\Scripts\python.exe matching_agent.py --job JOB-003 --offline
venv\Scripts\python.exe matching_agent.py --prompt "Rank resumes for JOB-004" --offline

# Discovery only
venv\Scripts\python.exe matching_agent.py --discover

# LLM planner (requires GROQ_API_KEY)
venv\Scripts\python.exe matching_agent.py --demo
venv\Scripts\python.exe matching_agent.py --demo --require-llm

# Machine-readable result
venv\Scripts\python.exe matching_agent.py --demo --offline --json
```

If `GROQ_API_KEY` is empty, the agent prints a notice and uses the offline
planner. If the provider errors mid-run, it logs the failure and falls back to
the offline planner unless `--require-llm` is set.

## Server commands

```bash
# Filesystem MCP (stdio by default)
venv\Scripts\python.exe filesystem_mcp_server.py
venv\Scripts\python.exe filesystem_mcp_server.py --selftest
venv\Scripts\python.exe filesystem_mcp_server.py --transport streamable-http --port 8765

# Job-board MCP
venv\Scripts\python.exe job_board_mcp_server.py
venv\Scripts\python.exe job_board_mcp_server.py --selftest
venv\Scripts\python.exe job_board_mcp_server.py --reset --seed
venv\Scripts\python.exe job_board_mcp_server.py --transport streamable-http --port 8766
```

> Stdio rule: stdout is the JSON-RPC channel. All server logs go to stderr.

## MCP tools

### Filesystem server (`filesystem`)

| Tool | Description |
| --- | --- |
| `read_file(filepath)` | Extract text + metadata from PDF/DOCX/TXT/MD |
| `list_files(directory, extension?)` | List files with size and modified date |
| `write_file(filepath, content, append?)` | Write text; refuses binary formats |
| `search_in_file(filepath, keyword, context_chars?, max_matches?)` | Phrase search with context |
| `watch_directory(directory, extensions?, recursive?, reset?, wait_seconds?)` | Baseline/diff and long-poll |
| `batch_process(directory, operations?, extension?, recursive?, keywords?, max_files?)` | Metadata/read/search/skills/match |
| `get_config()` | Effective configuration and capabilities |
| `get_stats()` | Counters, latency, watch state |

### Job-board server (`jobs`)

| Tool | Description |
| --- | --- |
| `search_jobs(query?, location?, experience?, skill?, limit?)` | Filter seven seeded roles |
| `get_job(job_id)` | Full role description |
| `save_match_result(job_id, resume_name, score, matched_skills?, run_id?)` | Upsert a score |
| `list_match_results(job_id?, run_id?, limit?)` | Read scores, highest first |
| `clear_match_results(job_id?)` | Delete scores |
| `get_config()` / `get_stats()` | Database location and counts |

## MCP resources

| URI | Server | Content |
| --- | --- | --- |
| `resumes://catalog` | filesystem | Resume files with size/date |
| `resumes://summary` | filesystem | Counts and skill frequency |
| `resumes://file/{name}` | filesystem | One resume's extracted text |
| `workspace://config` | filesystem | Effective config (secrets stripped) |
| `workspace://stats` | filesystem | Runtime counters |
| `workspace://file/{+path}` | filesystem | Any supported workspace file |
| `jobs://catalog` | jobs | Seven roles and required skills |
| `jobs://matches` | jobs | Recent saved scores |
| `jobs://stats` | jobs | Database statistics |
| `jobs://config` | jobs | DB path and capabilities |
| `jobs://job/{job_id}` | jobs | Full role description |
| `jobs://matches/{job_id}` | jobs | Scores for one role |

## Tools and resources are different on purpose

- **Tools** are model-controlled actions: read, search, batch, persist.
- **Resources** are application-controlled context: job data, catalogs,
  configuration and stats. The `discover` node reads them before planning, which
  keeps the prompt small and makes context auditable.

## Configuration

`mcp_config.json` is the single source of truth. `mcp_config.py` merges defaults,
the file, environment overrides and explicit overrides, then validates and
resolves paths. See [ARCHITECTURE.md](ARCHITECTURE.md#6-configuration) for the
full precedence list.

Common environment variables:

| Variable | Effect |
| --- | --- |
| `MCP_CONFIG` | Alternate config file |
| `MCP_WORKSPACE` (or `WORKSPACE_DIR`) | Workspace root |
| `MCP_LOG_LEVEL` | Server log level |
| `MCP_CONCURRENCY` | Batch thread pool size |
| `MCP_MAX_BATCH_FILES` | Batch file cap |
| `MCP_MAX_FILE_MB` | Read size cap |
| `MCP_WATCH_POLL` | Watch poll interval |
| `MCP_CLIENT_TIMEOUT` | MCP request timeout |
| `JOBS_DB` | SQLite database path |
| `GROQ_MODEL` | Agent model id |

## Tests

```bash
venv\Scripts\python.exe -m pytest -v
```

20 tests cover:

- filesystem tools, schemas, resources, sandbox traversal and validation
- `watch_directory` baseline/new/modified/removed
- `batch_process` ranking and argument validation
- job-board search, upsert, list, clear, resources and stats
- raw JSON-RPC handshake, discovery, `-32601`, `-32602`, `isError`
- multi-server discovery and OpenAI schema conversion
- the full offline LangGraph run, report, trace and SQLite rows
- proof that the agent does not import `fs_tools` or server modules

See [TEST_SCENARIOS.md](TEST_SCENARIOS.md) for every case and expected output.

## Project structure

| File | Purpose |
| --- | --- |
| `fs_tools.py` | Milestone 1 parsing/writing internals, reused server-side |
| `filesystem_mcp_server.py` | **Part A** filesystem MCP server (tools, resources, watch, batch) |
| `job_board_mcp_server.py` | **Bonus** SQLite MCP server |
| `mcp_client_manager.py` | Multi-server stdio client, discovery, OpenAI schemas, call/resource façade |
| `matching_agent.py` | **Part B** LangGraph agent, LLM + offline planners, CLI |
| `demo.py` | Guided demo runner |
| `mcp_config.py` / `mcp_config.json` | Configuration loader and defaults |
| `.vscode/launch.json` | One-click demo, agent, test and server configurations |
| `llm_file_assistant.py` | Milestone 1 agent, kept for reference (not imported) |
| `generate_sample_resumes.py` | Creates the six sample resumes |
| `test_*.py` | Automated test suite |
| `ARCHITECTURE.md` | Diagrams and design decisions |
| `TEST_SCENARIOS.md` | Test matrix and manual scenarios |
| `DEMO_SCRIPT.md` | Timed 5–6 minute recording script |

Generated at runtime and git-ignored: `reports/` (except the sample),
`runs/`, `data/*.db`, `inbox/`.

## Example trace

Each run writes `runs/<run_id>.json`:

```json
{
  "run_id": "run-20261116-144333",
  "planner": "scripted",
  "rankings": [{"rank": 1, "resume": "resume_john_doe.pdf", "score": 100.0}],
  "persisted": 6,
  "report_path": "reports/shortlist_JOB-001_20261116-144336.md",
  "events": ["start", "resource", "plan", "tool", "plan", "persist", "report", "verify", "finalize"]
}
```

## Limitations

- Scanned/image-only PDFs have no OCR and return no text.
- Legacy `.doc` is unsupported; use `.docx`, `.pdf`, `.txt` or `.md`.
- Watch state is in-memory per server process; restarting resets baselines.
- The offline planner is intentionally narrow (skills coverage for one role).
- Scoring is keyword coverage, not semantic similarity.
- stdio MCP is local-only; use `streamable-http` for remote deployment.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — component, sequence and state diagrams
- [TEST_SCENARIOS.md](TEST_SCENARIOS.md) — automated and manual test matrix
- [DEMO_SCRIPT.md](DEMO_SCRIPT.md) — 5–6 minute walkthrough
