# Demo Script — 5 to 6 minutes

**Title:** From local tools to MCP: a multi-server resume-matching agent

**Goal:** show (1) the MCP servers, (2) resource discovery and calls, (3) new
watch/batch capabilities, (4) the LangGraph agent using two servers, and (5) the
persisted artifacts.

Run everything from the project root in Windows Terminal or PowerShell. The
offline planner is used for the recording so the demo is deterministic and does
not depend on network access. Say that the same code path runs the LLM planner
when `GROQ_API_KEY` is present.

---

## Pre-recording checklist (2 minutes, not recorded)

```powershell
.\venv\Scripts\python.exe -m pytest -q          # expect 20 passed
.\venv\Scripts\python.exe matching_agent.py --discover
Remove-Item reports\* -Force -ErrorAction SilentlyContinue
Remove-Item runs\* -Force -ErrorAction SilentlyContinue
```

- [ ] Terminal font ≥ 14 pt, window tall enough for 40 lines.
- [ ] `GROQ_API_KEY` either populated (for `--llm`) or deliberately offline.
- [ ] Close noisy apps; hide `.env`.
- [ ] Have `ARCHITECTURE.md` open in a second tab for the component diagram.
- [ ] Start screen: project root, repository visible.

Optional reset of the demo database:

```powershell
.\venv\Scripts\python.exe job_board_mcp_server.py --reset --seed
```

---

## 0:00 – 0:30 — Intro and problem

**On screen:** `ARCHITECTURE.md`, component diagram.

**Say:**

> Milestone 1 had an agent that imported Python file functions directly. That
> works for one app, but every new agent duplicates schemas, path safety and error
> handling. Milestone 2 turns those tools into MCP services. The agent now speaks
> JSON-RPC 2.0 over stdio and has no filesystem or SQLite code. I built two
> servers: a filesystem server and a bonus job-board server, and one LangGraph
> agent consumes both.

**Point at:** `matching_agent.py → mcp_client_manager.py → two servers`.

---

## 0:30 – 1:15 — Discovery: one client, two servers

```powershell
.\venv\Scripts\python.exe matching_agent.py --discover
```

**Say while it runs:**

> Both servers are spawned as child processes. The client performs `initialize`,
> then `tools/list`, `resources/list` and `resources/templates/list`.

**Point at:**

- `2 MCP servers connected: filesystem (8 tools, 6 resources), jobs (7 tools, 6 resources)`
- 15 qualified tools such as `filesystem__batch_process` and
  `jobs__save_match_result`.
- Four static resources and two URI templates per server.
- Protocol `2025-11-25`.

**Transition:** “Resources are the application-controlled half of MCP. Let’s read
real ones.”

---

## 1:15 – 2:05 — Resources: context without tools

```powershell
.\venv\Scripts\python.exe demo.py resources
```

**Say:**

> The agent reads the job description, the resume catalog, the resume summary and
> the job-board stats as resources. A template read pulls the text of one resume.

**Point at:** six `ok` lines and `resumes://file/resume_emily_watson.txt`.
Mention `reports/mcp_resource_snapshot.json` is written back through the
filesystem MCP server.

**Transition:** “Now the new batch capability: score every resume in one call.”

---

## 2:05 – 2:55 — Batch matching

```powershell
.\venv\Scripts\python.exe demo.py batch --job JOB-001
```

**Say:**

> `batch_process` parses each file once, extracts skills, and scores required-skill
> coverage. Parsing runs in a bounded thread pool, and the server is a separate
> process, so the agent stays responsive.

**Point at the ranking:**

| Rank | Candidate | Score |
| ---: | --- | ---: |
| 1 | `resume_john_doe.pdf` | 100.0% |
| 2 | `resume_rahul_verma.txt` | 50.0% |
| 3 | `resume_alex_chen.docx` | 12.5% |
| 4–6 | Emily, Maria, Priya | 0.0% |

**Say:** “The demo also persists these six scores to the SQLite-backed job-board
server with a run id, so reruns are auditable.”

---

## 2:55 – 3:40 — Watch directory

```powershell
.\venv\Scripts\python.exe demo.py watch
```

**Say:**

> `watch_directory` establishes a baseline, then diffs on later calls. The demo
> copies a resume into `inbox/demo_watch`, sees `new=1`, deletes it, and sees
> `removed=1`. This is the “new resume arrived” workflow without polling from the
> agent.

**Point at:**

```
baseline: {'current': 0, ...}
after add: new=1 -> ['resume_emily_watson.txt']
after remove: removed=1 -> ['resume_emily_watson.txt']
```

**Transition:** “Now the full Part B agent.”

---

## 3:40 – 5:05 — LangGraph agent over both servers

```powershell
.\venv\Scripts\python.exe demo.py agent --job JOB-001 --llm
```

> If the API key is unavailable or the provider is slow, use the offline command
> below. Do not edit code during the recording.
>
> ```powershell
> .\venv\Scripts\python.exe demo.py agent --job JOB-001
> ```

**Say while the nodes run:**

> LangGraph runs `discover → plan → act → plan → finalize`. Discover reads
> resources. Plan chooses a tool; act executes it through MCP. After planning
> finishes, finalize saves six match results, writes the report through
> `filesystem__write_file`, then reads it back through
> `workspace://file/...` to verify the write.

**Point at the progress lines:**

```
[agent] discovered JOB JOB-001 (Senior Python Engineer) and 6 resumes from 2 MCP servers
[agent] step 1: filesystem__batch_process
[agent] persisted 6/6 match results to jobs MCP server
[agent] wrote and verified reports/shortlist_JOB-001_<timestamp>.md via MCP resources
```

**Point at the answer block:** top three candidates, persisted count, report path,
trace path, elapsed time.

**If asked about the LLM:** “With a key, the LLM receives the 15 MCP tool schemas
in OpenAI function-calling format and chooses the calls itself. If the provider
fails, the agent logs a warning and falls back to the deterministic planner.”

---

## 5:05 – 5:40 — Artifacts and persistence

```powershell
Get-Content reports\latest.md
```

**Say:**

> The report is generated from the observed batch results, not hard-coded. It
> includes the ranking table, required skills, the tool trace, and how the score
> was produced.

Scroll to the ranking table and the “How this was produced” list.

```powershell
.\venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/jobs.db'); print(c.execute('select resume_name, score, run_id from match_results order by score desc').fetchall())"
```

**Say:** “Six rows in SQLite, written by the second MCP server. A rerun with the
same run id updates in place because of the unique constraint.”

Optionally show the trace:

```powershell
.\venv\Scripts\python.exe -c "import json; d=json.load(open('runs/latest.json')); print([e['type'] for e in d['events']])"
```

**Point at:** `['start','resource','resource',...,'plan','tool','plan','persist',...,'report','verify','finalize']`.

---

## 5:40 – 6:00 — Close

**Say:**

> This is the core MCP benefit: one agent, multiple standard providers. Adding a
> third server means adding a launch spec, not changing agent code. All 20 tests
> pass, including raw JSON-RPC checks, sandbox traversal, watch and batch, the
> SQLite server, and the full offline agent run. The architecture and test
> scenarios are in `ARCHITECTURE.md` and `TEST_SCENARIOS.md`.

**End screen:** repository tree with `filesystem_mcp_server.py`,
`job_board_mcp_server.py`, `matching_agent.py`, `mcp_client_manager.py`,
`ARCHITECTURE.md`, `TEST_SCENARIOS.md`, `DEMO_SCRIPT.md`.

---

## Fallbacks and troubleshooting

| Symptom | Fix |
| --- | --- |
| Groq key missing / rate limited | Run the offline agent command; it uses the same graph and MCP calls |
| Port or leftover process issue | stdio is used by default; no ports are opened. Restart the terminal |
| `data/jobs.db` locked | Close other Python processes; rerun with `--reset --seed` |
| Unicode boxes in PowerShell | The agent reconfigures stdout to UTF-8; use Windows Terminal, not legacy console |
| Report not found | Check the printed `report:` path and `runs/latest.json` for the `report` event |
| Slow PDF parsing | Expected on first read; batch concurrency is 4 and later runs reuse the OS file cache |

## Commands used in the recording (copy block)

```powershell
.\venv\Scripts\python.exe matching_agent.py --discover
.\venv\Scripts\python.exe demo.py resources
.\venv\Scripts\python.exe demo.py batch --job JOB-001
.\venv\Scripts\python.exe demo.py watch
.\venv\Scripts\python.exe demo.py agent --job JOB-001 --llm
Get-Content reports\latest.md
.\venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/jobs.db'); print(c.execute('select resume_name, score, run_id from match_results order by score desc').fetchall())"
```
