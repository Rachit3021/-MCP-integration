# Candidate Shortlist - JOB-001

**Role:** Senior Python Engineer  
**Company:** FinStack Technologies  
**Location:** Bengaluru, India  
**Generated:** 2026-11-16T14:53:50  
**Run:** `run-20261116-145343`  
**Planner:** llm (openai/gpt-oss-120b)

## Ranking

| Rank | Candidate | Score | Matched skills | Missing skills |
| ---: | --- | ---: | --- | --- |
| 1 | `resume_john_doe.pdf` | 100.0% | Python, FastAPI, PostgreSQL, Docker, Kubernetes, AWS, scikit-learn, Airflow | - |
| 2 | `resume_rahul_verma.txt` | 50.0% | Python, Docker, Kubernetes, AWS | FastAPI, PostgreSQL, scikit-learn, Airflow |
| 3 | `resume_alex_chen.docx` | 12.5% | Python | FastAPI, PostgreSQL, Docker, Kubernetes, AWS, scikit-learn, Airflow |
| 4 | `resume_emily_watson.txt` | 0.0% | - | Python, FastAPI, PostgreSQL, Docker, Kubernetes, AWS, scikit-learn, Airflow |
| 5 | `resume_maria_garcia.docx` | 0.0% | - | Python, FastAPI, PostgreSQL, Docker, Kubernetes, AWS, scikit-learn, Airflow |
| 6 | `resume_priya_sharma.pdf` | 0.0% | - | Python, FastAPI, PostgreSQL, Docker, Kubernetes, AWS, scikit-learn, Airflow |

## Required skills

`Python`, `FastAPI`, `PostgreSQL`, `Docker`, `Kubernetes`, `AWS`, `scikit-learn`, `Airflow`

## Tool trace

| Server | Tool | OK | ms |
| --- | --- | --- | ---: |
| filesystem | `filesystem__batch_process` | yes | 111 |
| jobs | `jobs__save_match_result` | yes | 13 |
| jobs | `jobs__save_match_result` | yes | 13 |
| jobs | `jobs__save_match_result` | yes | 12 |
| jobs | `jobs__save_match_result` | yes | 12 |
| jobs | `jobs__save_match_result` | yes | 12 |
| jobs | `jobs__save_match_result` | yes | 13 |

## How this was produced

1. `matching_agent.py` started both MCP servers over stdio.
2. LangGraph ran `discover -> plan -> act -> plan -> finalize`.
3. Candidate scores came from `filesystem__batch_process` over MCP.
4. Scores were persisted with `jobs__save_match_result` on the job-board server.
5. This report was written with `filesystem__write_file` and read back through `workspace://file/...`.
