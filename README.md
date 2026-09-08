# PR Sentinel — Autonomous AI Code Review & Root Cause Engine

PR Sentinel is a production-grade, autonomous code review platform designed for GitHub Pull Requests. Built with **FastAPI**, **LangGraph**, **Groq (llama-3.1-8b-instant)**, and **PostgreSQL**, it goes beyond simple one-shot LLM wrappers by combining deterministic codebase retrieval, structured diff analysis, hallucination validation, autonomous root-cause investigation, durable worker scheduling, and an interactive operational dashboard.

---

## Architecture Overview

```mermaid
flowchart TD
    subgraph Intake ["1. Intake & Deduplication"]
        GH[GitHub Pull Request Event] -->|POST /webhook/github| WH[FastAPI Webhook Handler]
        WH -->|HMAC-SHA256| SIG[Signature Verification]
        SIG -->|Deduplicate & Claim| DB[(PostgreSQL Job Queue)]
        WH -.->|HTTP 202 Accepted <50ms| GH
    end

    subgraph WorkerLoop ["2. Durable Worker Engine"]
        DB -->|SELECT FOR UPDATE SKIP LOCKED| WORKER[Durable Worker Engine]
        WORKER -->|Heartbeat / Lease Fencing| DB
    end

    subgraph Pipeline ["3. LangGraph Review Graph"]
        WORKER --> RETRIEVER[1. Retriever: AST & Import Context]
        RETRIEVER --> ANALYZER[2. Analyzer: Groq llama-3.1-8b-instant]
        ANALYZER --> VALIDATOR[3. Validator: Diff Hunk Line Guard]
        VALIDATOR --> INVESTIGATOR[4. Investigator: Root Cause & Evidence]
        INVESTIGATOR --> AGGREGATOR[5. Aggregator: Deduplication & Verdict]
        AGGREGATOR --> POSTER[6. Poster: Atomic Review & Status]
    end

    subgraph GitHubDelivery ["4. Delivery & GitHub Sync"]
        POSTER -->|POST /pulls/{pr}/reviews| GH_REV[GitHub Pull Request Review]
        POSTER -->|POST /statuses/{sha}| GH_STAT[Commit Status Check]
        POSTER -->|Record Summary & Findings| DB
    end

    subgraph DashboardView ["5. Observability & Dashboard"]
        DASH[Developer Dashboard & REST API] -->|GET /jobs, /stats, /reviews/{id}| DB
        DASH -->|POST /jobs/{id}/retry| WORKER
    end

    classDef primary fill:#0284c7,stroke:#38bdf8,stroke-width:2px,color:#ffffff;
    classDef storage fill:#0f172a,stroke:#38bdf8,stroke-width:2px,color:#ffffff;
    classDef worker fill:#8b5cf6,stroke:#c084fc,stroke-width:2px,color:#ffffff;
    classDef gh fill:#10b981,stroke:#34d399,stroke-width:2px,color:#ffffff;

    class WH,RETRIEVER,ANALYZER,VALIDATOR,INVESTIGATOR,AGGREGATOR,POSTER primary;
    class DB storage;
    class WORKER,DASH worker;
    class GH,GH_REV,GH_STAT gh;
```

---

## Key Capabilities

- **Sub-50ms Webhook Intake**: Authenticates `X-Hub-Signature-256` payloads, atomically claims delivery IDs in PostgreSQL, and acknowledges GitHub with `202 Accepted` immediately without blocking on LLM inference.
- **PostgreSQL Durable Worker Queue**: Uses `SELECT ... FOR UPDATE SKIP LOCKED` for concurrent multi-worker job claiming with zero message broker overhead.
- **Lease Fencing & Active Heartbeats**: Workers renew execution leases every 10 seconds. Stale jobs are automatically recovered after 300 seconds. Pre-side-effect lease fencing ensures zombie or timed-out workers never publish duplicate reviews.
- **Durable Retries & Rate-Limit Handling**: Automatically parses delta-seconds and RFC 7231 HTTP-dates from `Retry-After` headers and `X-RateLimit-Reset` timestamps for GitHub and Groq APIs with exponential backoff.
- **Codebase-Aware Context Retrieval**: Deterministically extracts modified imports, symbols, class definitions, and call-sites across repository files using AST parsing and bounded regex extractors.
- **Strict Diff-Line Validation**: Validates that all LLM-reported line numbers strictly exist within actual diff hunks, completely eliminating out-of-bounds line comments.
- **Autonomous Root Cause Investigation**: Re-evaluates validated defect findings against repository context to enrich reports with **Root Cause**, **Supporting Evidence**, **Potential Impact**, and **Actionable Recommendations**.
- **Atomic GitHub Review Batching**: Posts all inline comments, formatted summary, and formal review verdict (`APPROVE`, `REQUEST_CHANGES`, `COMMENT`) in a single atomic GitHub API request, with automatic fallback for diff-line HTTP 422 errors.
- **Interactive Developer Dashboard**: Professional Dark/Light SaaS dashboard showing real-time queue telemetry, finding severity breakdown (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`), deep-dive finding inspection, and 1-click retry.

---

## Technical Stack

| Layer | Technologies |
|---|---|
| **API & Framework** | [FastAPI](https://fastapi.tiangolo.com/), [Pydantic v2](https://docs.pydantic.dev/), Starlette |
| **Orchestration & AI** | [LangGraph](https://langchain-ai.github.io/langgraph/), [ChatGroq](https://python.langchain.com/docs/integrations/chat/groq/) (`llama-3.1-8b-instant`) |
| **Database & ORM** | [PostgreSQL 14+](https://www.postgresql.org/), [SQLAlchemy 2.0 (Async)](https://docs.sqlalchemy.org/), [Alembic](https://alembic.sqlalchemy.org/), `asyncpg` |
| **HTTP & GitHub Client** | [HTTPX (Async)](https://www.python-httpx.org/), HMAC-SHA256, GitHub REST API v3 |
| **Frontend UI** | Vanilla Semantic HTML5, Modern CSS Design System (Custom Tokens, Dark/Light mode), Vanilla JavaScript (No heavy UI frameworks) |
| **Testing & Quality** | [Pytest](https://docs.pytest.org/), `pytest-asyncio`, `aiosqlite` (in-memory test isolation) |

---

## API Overview

All operational endpoints are served by FastAPI in [app/main.py](file:///c:/Users/91812/PR/app/main.py):

| Method | Route | Auth | Description |
|---|---|---|---|
| `GET` | `/` or `/dashboard` | None | Serves the interactive PR Sentinel Web Dashboard |
| `GET` | `/static/*` | None | Serves dashboard stylesheets and JavaScript application files |
| `GET` | `/health` | None | Basic liveness probe (`{"status": "ok"}`) |
| `GET` | `/health/ready` | None | Deep readiness probe verifying database connection and worker loop |
| `POST` | `/webhook/github` | `X-Hub-Signature-256` | GitHub webhook receiver (sub-50ms ACK with HTTP 202) |
| `GET` | `/jobs/stats` | `X-Admin-Key`* | Aggregated queue metrics (total, queued, in_progress, completed, failed) |
| `GET` | `/jobs` or `/reviews` | `X-Admin-Key`* | Returns list of recent review jobs with summary and status |
| `GET` | `/jobs/{job_id}` | `X-Admin-Key`* | Full lifecycle metadata, executive summary, and investigated findings |
| `GET` | `/reviews/{job_id}` | `X-Admin-Key`* | Alias for `/jobs/{job_id}` to retrieve completed review results |
| `POST` | `/jobs/{job_id}/retry` | `X-Admin-Key`* | Operator endpoint to re-queue failed jobs and notify worker |

*\* If `ADMIN_API_KEY` is not set in local development, admin endpoints allow unauthenticated access.*

---

## Project Structure

```text
PR/
├── app/
│   ├── main.py                  # FastAPI webhook entry point, dashboard routes, and REST API
│   ├── config.py                # Pydantic Settings and environment configuration
│   ├── github_client.py         # GitHub API client (diffs, contents, reviews, statuses, rate-limits)
│   ├── worker.py                # Durable worker loop, lease heartbeats, fencing, and drainage
│   ├── models.py                # Pydantic models for GitHub webhook payloads
│   ├── schemas.py               # API response models (ReviewDetailResponse, JobStatsResponse, etc.)
│   ├── agent/
│   │   ├── graph.py             # LangGraph StateGraph topology and compilation
│   │   ├── state.py             # ReviewState TypedDict definition
│   │   ├── schemas.py           # Pydantic schemas for findings, verdicts, and investigations
│   │   ├── retrieval.py         # AST/regex symbol extraction & bounded context slicer
│   │   ├── diff_parser.py       # Unified diff parser & hunk line validator
│   │   ├── prompts.py           # Structured analysis prompts & injection protection
│   │   └── nodes/
│   │       ├── retriever.py     # Codebase-aware retrieval node
│   │       ├── analyzer.py      # LLM diff analysis node via Groq
│   │       ├── validator.py     # Hunk line validation and path integrity checks
│   │       ├── investigator.py  # Autonomous root cause & evidence analysis node
│   │       ├── aggregator.py    # Deduplication, prioritization, and verdict determination
│   │       └── poster.py        # Atomic GitHub review publisher & lease fencing
│   ├── db/
│   │   ├── models.py            # SQLAlchemy Base and ReviewJob model
│   │   ├── session.py           # AsyncEngine and AsyncSession lifecycle management
│   │   └── job_store.py         # Durable state operations, atomic claims, and error sanitization
│   └── static/
│       ├── index.html           # Developer dashboard UI
│       ├── style.css            # Dark/Light theme design system
│       └── app.js               # Centralized ApiClient & reactive UI controller
├── migrations/                  # Alembic schema version history
│   ├── env.py
│   └── versions/                # 5 sequential migration revisions (head: b73ee4ae6cec)
├── tests/                       # 113 comprehensive unit, integration, API, and migration tests
├── alembic.ini                  # Alembic migration configuration
├── requirements.txt             # Project Python dependencies
└── .env.example                 # Environment configuration template
```

---

## Reliability & Engineering Highlights

PR Sentinel is built with strict distributed systems patterns:

1. **Durable Database-Backed Queue**: Review jobs survive application restarts and worker crashes without state loss.
2. **Atomic Idempotent Intake**: Deduplicates repeated GitHub webhook delivery IDs and concurrent PR updates via database constraints.
3. **Lease Fencing**: Prevents split-brain execution where a timed-out worker attempts to post comments after a retry worker has taken over.
4. **Prompt Injection & Secret Defense**: Sanitizes error messages to redact GitHub PATs, Groq API keys, and database passwords from logs and databases; instructs LLMs to treat all diffs as untrusted data.
5. **Diff Boundary Protection**: Every finding line number is checked against parsed unified diff hunks; findings outside diff hunks are automatically routed to the review summary.
6. **Graceful Worker Drainage**: On shutdown signals (`SIGINT`/`SIGTERM`), active workers are given a grace period to complete in-flight reviews, resetting uncompleted jobs back to `queued`.

---

## Local Setup & Quickstart

### 1. Prerequisites
- Python 3.11+
- PostgreSQL 14+ running locally (or in Docker)
- Groq API Key ([console.groq.com](https://console.groq.com))
- GitHub Personal Access Token (PAT) with `repo` scope

### 2. Clone & Install Dependencies
```powershell
git clone <repo-url>
cd PR

# Create and activate virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1   # On Windows
# source venv/bin/activate    # On Linux/macOS

# Install dependencies
pip install -r requirements.txt
```

### 3. Environment Configuration
Copy `.env.example` to `.env` and fill in your credentials:
```powershell
cp .env.example .env
```

Key variables in `.env`:
```ini
GITHUB_TOKEN=ghp_your_github_token_here
GITHUB_WEBHOOK_SECRET=your_webhook_secret_here
GROQ_API_KEY=gsk_your_groq_api_key_here
GROQ_MODEL=llama-3.1-8b-instant
DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/pr_sentinel
WORKER_MODE=hybrid
ADMIN_API_KEY=
ENABLE_COMMIT_STATUS=false
```

### 4. Apply Database Migrations
```powershell
alembic upgrade head
```

### 5. Start Application
```powershell
# Start FastAPI (handles webhooks, background worker, and serves dashboard)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open your browser and navigate to:
```text
http://localhost:8000/dashboard
```

---

## Testing & Quality Assurance

The project includes an automated test suite covering all nodes, APIs, GitHub interactions, worker fencing, and database migrations:

```powershell
# Run the complete test suite
pytest tests/ -v
```

**Verified Test Suite Status**:
```text
============================= 113 passed in 9.80s =============================
```

---

## Why PR Sentinel? (Portfolio & Architecture Impact)

Most LLM coding assistants are implemented as synchronous API wrappers that fail under production conditions (timeouts, rate limits, duplicate webhook deliveries, and hallucinated line comments).

PR Sentinel was engineered to demonstrate:
- **Resilient Asynchronous Architecture**: Decoupling HTTP webhook intake from background worker execution.
- **Graph-Based Agent Topology**: Multi-stage LangGraph pipeline with isolated validation and root-cause analysis steps.
- **Distributed Safety**: PostgreSQL-backed atomic job claiming (`FOR UPDATE SKIP LOCKED`), lease heartbeats, and idempotency guarantees.
- **Clean Developer Experience**: First-class REST observability APIs paired with a lightweight, accessible web dashboard.
