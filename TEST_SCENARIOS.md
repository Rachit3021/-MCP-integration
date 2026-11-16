# Test Scenarios

Two layers of verification:

1. **Automated** — `pytest -v` (20 tests) covers tools, resources, watch/batch,
   SQLite, JSON-RPC, the multi-server manager and the full offline agent run.
2. **Manual / demo** — commands a reviewer can run to see the same behaviour and
   inspect artifacts.

All automated tests use temporary workspaces and temporary SQLite databases; they
do not modify `resumes/`, `reports/`, `runs/` or `data/jobs.db`.

## 1. Automated suite

```bash
venv\Scripts\python.exe -m pytest -v
```

Expected: `20 passed` (about 12–15 seconds on Windows).

| # | Test | Requirement covered | Expected result |
| ---: | --- | --- | --- |
| 1 | `test_filesystem_tools_resources_and_errors` | Part A tools, schemas, resources, sandbox | 8 tools in order; 4 resources; 2 templates; read/search/write/batch succeed; `../secret.txt` is an `isError`; unknown resource raises |
| 2 | `test_watch_directory_baseline_new_modified_removed` | `watch_directory` | First call is a baseline; adding a file reports `new=1`; a second call reports no change; deleting reports `removed=1` |
| 3 | `test_batch_process_validates_operations_and_keywords` | `batch_process` validation | Unknown operation and missing keywords produce structured `isError` payloads |
| 4 | `test_config_validation_rejects_bad_limits` | Config management | `concurrency=0` and `mcp_timeout_seconds=0` raise `ValueError` |
| 5–10 | `test_fs_tools.py` (Milestone 1) | Regression safety | All six original tool tests still pass after `search_in_text` refactor |
| 11 | `test_job_board_tools_and_resources` | Bonus server tools/resources | Search by text/location/experience; get, save (upsert), list, clear; 7 jobs; 2 match rows; unknown job is `isError`; missing resource raises |
| 12 | `test_job_board_search_by_skill_and_config` | Bonus server filtering/config | `Terraform` returns only JOB-003; config reports 7 jobs and the SQLite path |
| 13 | `test_empty_match_resource_is_not_an_error` | Resource semantics | `jobs://matches/JOB-002` returns `count: 0`, not a 404 |
| 14 | `test_initialize_and_capabilities` | JSON-RPC handshake | `jsonrpc: "2.0"`, id echoed, serverInfo, negotiated protocol, tool/resource capabilities |
| 15 | `test_discovery_call_and_standard_errors` | JSON-RPC discovery + errors | `tools/list`, `resources/list`, `resources/templates/list`, `tools/call`; unknown method `-32601`; invalid params `-32602`; unknown resource `-32602`; unknown tool `isError` |
| 16 | `test_malformed_json_does_not_kill_the_server` | Robustness | A malformed frame is dropped without poisoning the stream; the next request succeeds |
| 17 | `test_manager_discovers_and_calls_both_servers` | Multi-MCP manager | 2 servers, 15 tools, 8 resources, 4 templates; calls and resources from both servers succeed; tool errors normalised |
| 18 | `test_manager_openai_names_and_server_specs` | Naming/launch config | `filesystem__read_file`; absolute launch specs; `JOBS_DB` injected |
| 19 | `test_offline_agent_end_to_end` | Part B LangGraph flow | 6 rankings, John Doe 100%, Rahul 50%, 6 persisted rows, report + trace + verification event |
| 20 | `test_agent_does_not_import_filesystem_tools` | Decoupling | `matching_agent.py` contains no `fs_tools` or server imports |

### What test 19 proves in detail

- `discover` read `jobs://job/JOB-001`, `resumes://catalog` and `resumes://summary`
  through MCP resources.
- `plan` selected `filesystem__batch_process`; `act` executed it through JSON-RPC.
- `finalize` called `jobs__save_match_result` six times and
  `filesystem__write_file` for the report.
- The report was read back through `workspace://file/reports/...`.
- The JSON trace contains `plan`, `resource`, `tool`, `persist`, `report` and
  `verify` events.
- SQLite contains exactly six rows for JOB-001, ordered with John Doe first.

## 2. Manual server smoke tests

### 2.1 Filesystem server

```bash
venv\Scripts\python.exe filesystem_mcp_server.py --selftest
```

Expected: serverInfo `filesystem-mcp`, protocol `2025-11-25`, 8 tools, 4
resources, 2 templates, and a `list_files` result with 6 resumes.

### 2.2 Job board server

```bash
venv\Scripts\python.exe job_board_mcp_server.py --selftest
```

Expected: 7 tools, 4 resources, 2 templates, search returns 4 Python roles, job
JOB-001 loads, catalog has 7 roles, stats report `job_count: 7`.

### 2.3 Discovery from the agent

```bash
venv\Scripts\python.exe matching_agent.py --discover
```

Expected:

```
2 MCP servers connected: filesystem (8 tools, 6 resources), jobs (7 tools, 6 resources)
[filesystem] filesystem-mcp v2.0.0 protocol=2025-11-25
[ jobs ] job-board-mcp v1.0.0 protocol=2025-11-25
```

## 3. Manual JSON-RPC checks

Start the server and speak JSON-RPC directly (newline-delimited):

```python
import json, subprocess, sys
p = subprocess.Popen(
    [sys.executable, "filesystem_mcp_server.py", "--log-level", "ERROR"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
)
def send(obj):
    p.stdin.write(json.dumps(obj) + "\n"); p.stdin.flush()
def recv():
    return json.loads(p.stdout.readline())

send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{
    "protocolVersion":"2025-06-18","capabilities":{},
    "clientInfo":{"name":"manual","version":"1.0"}}})
print(recv())
send({"jsonrpc":"2.0","method":"notifications/initialized"})
send({"jsonrpc":"2.0","id":2,"method":"tools/list"})
print(recv())
send({"jsonrpc":"2.0","id":3,"method":"nope"})
print(recv())          # error.code == -32601
p.stdin.close(); p.wait()
```

| Scenario | Expected |
| --- | --- |
| `initialize` | `jsonrpc: "2.0"`, echoed id, `result.serverInfo.name == "filesystem-mcp"` |
| `tools/list` | 8 tool definitions with `inputSchema` |
| `resources/list` | 4 static URIs |
| `resources/templates/list` | 2 URI templates |
| unknown method | `error.code == -32601` |
| `tools/call` with `{}` | `error.code == -32602` |
| `resources/read` unknown URI | `error.code == -32602` |
| missing file / bad tool args | `result.isError == true` with JSON text |
| malformed JSON frame | dropped by the SDK transport; the next valid request still works |

## 4. Manual demo scenarios

### 4.1 Full demo (recommended for the video)

```bash
venv\Scripts\python.exe demo.py all
```

| Step | Observed |
| --- | --- |
| discover | 2 servers, 15 tools, 8 resources, 4 templates |
| resources | six resource reads plus one template read, all `ok` |
| batch | 6 files; John Doe 100%, Rahul Verma 50%, Alex Chen 12.5%, three at 0% |
| watch | baseline 0; after add `new=1`; after remove `removed=1` |
| agent | persisted 6 results; report and trace paths printed |

### 4.2 Individual commands

```bash
venv\Scripts\python.exe demo.py discover
venv\Scripts\python.exe demo.py resources
venv\Scripts\python.exe demo.py batch --job JOB-001
venv\Scripts\python.exe demo.py watch
venv\Scripts\python.exe demo.py agent --job JOB-001
```

`batch` always persists results (run id `demo-YYYYMMDD-HHMMSS`). `agent` uses the
offline planner unless `--llm` is supplied.

### 4.3 Offline agent

```bash
venv\Scripts\python.exe matching_agent.py --demo --offline
```

Expected ranking for JOB-001 (Senior Python Engineer):

| Rank | Candidate | Score | Matched |
| ---: | --- | ---: | --- |
| 1 | `resume_john_doe.pdf` | 100.0% | 8/8 |
| 2 | `resume_rahul_verma.txt` | 50.0% | 4/8 |
| 3 | `resume_alex_chen.docx` | 12.5% | 1/8 |
| 4–6 | Emily, Maria, Priya | 0.0% | 0/8 |

Artifacts:

- `reports/shortlist_JOB-001_<timestamp>.md`
- `reports/latest.md`
- `runs/run-<timestamp>.json` and `runs/latest.json`
- `data/jobs.db` rows for JOB-001

### 4.4 LLM planner

```bash
venv\Scripts\python.exe matching_agent.py --demo
```

Requires a non-empty `GROQ_API_KEY`. The LLM receives the 15 qualified MCP tool
schemas and plans calls itself. If the provider errors, the run logs a warning and
falls back to the offline planner (`planner: scripted (fallback)`). Use
`--require-llm` to make a missing key or provider failure fatal.

### 4.5 Path safety

```bash
venv\Scripts\python.exe -c "import asyncio; from mcp_client_manager import MCPClientManager; \
async def m():\
 async with MCPClientManager() as c:\
  r=await c.call_tool('filesystem__read_file', {'filepath':'../secret.txt'}); print(r.ok, r.error)\
asyncio.run(m())"
```

Expected: `False Access outside the workspace is not allowed: ../secret.txt`.

### 4.6 Watch with a live process

The `demo.py watch` scenario creates `inbox/demo_watch/`, establishes a baseline,
copies `resume_emily_watson.txt` into it, observes `new=1`, deletes it, and
observes `removed=1`. Watch state is per server process, so restarting resets the
baseline.

### 4.7 Job-board persistence

```bash
venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/jobs.db'); \
print(c.execute('select job_id,resume_name,score,run_id from match_results order by score desc').fetchall())"
```

Expected after an agent run: six rows for JOB-001, John Doe at `100.0`, then
Rahul at `50.0`, then the remaining candidates at `0.0`. Re-running with a new
`run_id` adds a new run; re-running with the same `run_id` updates in place
(unique constraint on `(job_id, resume_name, run_id)`).

## 5. Cross-cutting checks

| Check | How |
| --- | --- |
| No stdout pollution on stdio | Server logs go to stderr; `--selftest` output is the only stdout write outside JSON-RPC |
| Config precedence | Set `MCP_CONCURRENCY=2` and call `get_config`; `batch.concurrency` is 2 |
| Invalid config | `MCP_CONCURRENCY=0` fails fast with `batch.concurrency must be between 1 and 32` |
| Multi-server failure isolation | Stop the jobs server; filesystem tools still work, job calls return `ok=False` with an error |
| Report verification | Trace contains a `verify` event for `workspace://file/reports/...` |
| No secret leakage | `workspace://config` omits `_meta` and server env blocks |

## 6. Performance expectations (local machine)

| Operation | Typical |
| --- | --- |
| Server startup (each) | ~1.0–1.3 s |
| Batch match of 6 resumes | 40–230 ms |
| `resumes://summary` | ~300 ms |
| Full offline agent run | 3–4 s |
| Full demo (`demo.py all`) | 14–16 s |
| `pytest -v` | 12–15 s |

PDF/DOCX parsing dominates batch time; concurrency is 4 by default.
