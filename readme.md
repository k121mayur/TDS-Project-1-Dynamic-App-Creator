# TDS LLM Code Deployment Orchestrator (FastAPI + Celery + Redis)

This service receives **task requests** at `POST /app`, immediately responds **200 OK**, then—within **10 minutes**—does all of the following **asynchronously**:

1) **Generates a dynamic web app (only HTML/CSS/JS)** based on the task brief using LLM 
2) **Creates a new GitHub repo** and commits the app,
3) **Publishes to GitHub Pages** via GitHub Actions,
4) **Posts the evaluation callback** with `{ repo_url, commit_sha, pages_url }` to the given `evaluation_url`.

> **Hard rule:** The generated app must be a **pure static site** (HTML + CSS + JS only), suitable for GitHub Pages. No servers or build steps in the generated repo.

---

## Table of Contents
- [Architecture](#architecture)
- [API Contract](#api-contract)
- [Ten-Minute SLO (Design)](#ten-minute-slo-design)
- [Prerequisites](#prerequisites)
- [Environment Variables](#environment-variables)
- [Quick Start (Docker Compose)](#quick-start-docker-compose)
- [Local Dev (without Docker)](#local-dev-without-docker)
- [How It Works](#how-it-works)
- [Revision Flow](#revision-flow)
- [LLM Generation vs Deterministic Templates](#llm-generation-vs-deterministic-templates)
- [GitHub Pages Workflow File](#github-pages-workflow-file)
- [Logging & Monitoring](#logging--monitoring)
- [Retries & Failure Modes](#retries--failure-modes)
- [Security Notes](#security-notes)
- [Runbook](#runbook)
- [License](#license)

---

## Architecture

- **FastAPI** exposes `POST /app` and **does not block**.  
- **Celery + Redis** handle the heavy lifting: codegen, repository ops, and status polling.  
- **GitHub Actions → Pages** hosts the generated static site.  
- **Evaluation callback** is sent **within 10 minutes** with repo and pages details.

---

## API Contract

### Endpoint
`POST /app`  
`Content-Type: application/json`

### Request body (example)
```json
{
  "email": "student@example.com",
  "secret": "...",
  "task": "captcha-solver-001",
  "round": 1,
  "nonce": "ab12-xyz",
  "brief": "Create a captcha solver that handles ?url=https://.../image.png. Default to attached sample.",
  "checks": [
    "Repo has MIT license",
    "README.md is professional",
    "Page displays captcha URL passed at ?url=...",
    "Page displays solved captcha text within 15 seconds"
  ],
  "evaluation_url": "https://example.com/notify",
  "attachments": [
    { "name": "sample.png", "url": "data:image/png;base64,iVBORw..." }
  ]
}
```

### Immediate response (synchronous)
```json
{ "ok": true, "received_at": "2025-10-17T13:14:15.123Z" }
```

### Asynchronous callback (within 10 minutes)
`POST evaluation_url` (`Content-Type: application/json`)
```json
{
  "email": "student@example.com",
  "task": "captcha-solver-001",
  "round": 1,
  "nonce": "ab12-xyz",
  "repo_url": "https://github.com/<owner>/<repo>",
  "commit_sha": "abc123def456...",
  "pages_url": "https://<owner>.github.io/<repo>/"
}
```

---

## Ten-Minute SLO (Design)

We budget the 10 minutes as follows (typical completes in ~2–6 minutes):

- **LLM/template generation:** ≤ 45–90s (LLM), ≤ 2s (template)
- **Repo create + push files:** 5–20s
- **Pages deploy (first build):** 1–5 minutes (varies by GitHub load)
- **Polling Pages URL:** up to 6–7 minutes max
- **Callback** POST: < 2s

Fail-safe: if Pages isn’t 200 yet but the URL is predictable (e.g., `https://<owner>.github.io/<repo>/`), we still **callback before 10 min** with that URL and note the deploy is in flight (logged). Evaluator can retry after a minute.

---

## Prerequisites

- **GitHub Personal Access Token (PAT)** with scopes: `repo`, `workflow`, `pages`.
- **Redis** accessible by the app and workers.
- Optional LLM provider (OpenAI or AI Pipe) credentials.

---

## Environment Variables

Create a `.env` file or configure your process manager/host with:

```
# Shared secret required on requests (set to a strong value in prod)
APP_SECRET=changeme-shared-secret

# GitHub automation (leave DRY_RUN=true when developing without GitHub access)
GITHUB_TOKEN=ghp_xxx
GITHUB_OWNER=your-github-username-or-org
DRY_RUN=true

# Redis broker used by Celery & FastAPI
REDIS_URL=redis://redis:6379/0

# LLM provider (required for dynamic code generation)
OPENAI_API_KEY=
AI_PIPE_TOKEN=
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=
OPENAI_TEMPERATURE=0.2

# Logging verbosity
LOG_LEVEL=INFO
```

> When `DRY_RUN=true` the worker skips GitHub calls and writes generated repos
> to `./artifacts/` locally. Set it to `false` and provide a valid PAT to enable
> full automation.
> Provide either `OPENAI_API_KEY` or (`AI_PIPE_TOKEN` + `OPENAI_BASE_URL=https://aipipe.org/openai/v1`)
> so the worker can leverage the LLM for bespoke apps. Without valid credentials
> the worker falls back to a minimal deterministic template.

---

## Quick Start (Docker Compose)

`dockercompose.yml` runs **FastAPI**, the **Celery worker**, and **Redis**:

```yaml
version: "3.9"
services:
  api:
    build: .
    command: uvicorn app:app --host 0.0.0.0 --port 8000
    env_file:
      - .env
    environment:
      REDIS_URL: redis://redis:6379/0
    depends_on:
      - redis
    ports:
      - "8000:8000"

  worker:
    build: .
    command: celery -A tasks.celery_app worker --loglevel=info
    env_file:
      - .env
    environment:
      REDIS_URL: redis://redis:6379/0
    depends_on:
      - redis

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
```

**Build & run**
```bash
docker compose up --build
```

**Test the endpoint**
```bash
curl -X POST http://127.0.0.1:8000/app   -H 'Content-Type: application/json'   -d @sample-request.json
```

---

## Local Dev (without Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Start Redis separately if needed (e.g., brew services start redis)
export REDIS_URL=redis://localhost:6379/0

# API
uvicorn app:app --host 0.0.0.0 --port 8000

# Worker
celery -A tasks.celery_app worker --loglevel=info
```

Or launch both services with the helper script (installs deps on first run):

```bash
./startapp.sh
```

---

## How It Works

**Directory structure (orchestrator repo)**

```
.
├─ app.py                # FastAPI app with POST /app (acknowledge & enqueue)
├─ tasks.py              # Celery orchestration (LLM → GitHub → callback)
├─ config.py             # Pydantic settings layer
├─ codegen.py            # Helpers to supplement/validate generated repos
├─ utils.py              # Slugging, attachment decoding, shared helpers
├─ services/
│  ├─ github_service.py  # GitHub REST API wrapper
│  └─ llm_generator.py   # OpenAI-compatible static site generator
├─ dockercompose.yml     # API + worker + Redis stack
├─ dockerfile
├─ requirements.txt
├─ startapp.sh           # Local bootstrap script
└─ README.md
```

**Flow**

1. **/app** receives the request, validates schema, enforces the shared secret, enqueues a Celery task, and responds immediately.
2. **Celery worker**:
   - Persists attachments under `assets/` and summarises them for prompting.
   - Prompts the configured LLM to emit a JSON manifest of static files (HTML/CSS/JS only).
   - Validates paths, file sizes, and required artefacts; supplements missing essentials (MIT LICENSE, Pages workflow, README) if necessary.
   - Bundles attachments, `task.json`, and `automation/llm_raw_output.json` for traceability.
   - Pushes the repo to GitHub (or `./artifacts/` in dry-run), enables Pages, and polls for readiness.
   - Posts the evaluation callback with repo metadata and Pages status.
3. Structured logs capture each stage for observability.

---

## Revision Flow

- **Round tracking:** Every successful build stores `{repo_name, owner, pages_url, last_commit, last_round}` in Redis keyed by `task`.
- **Round 2+ requests:** The worker reloads that state, reuses the existing GitHub repository, and applies the new brief on the saved branch instead of creating a new repo.
- **Deployment:** Updated assets overwrite previous versions and trigger the Pages workflow automatically.
- **Callback:** The evaluation POST mirrors round-specific metadata (including the new `round`/`nonce`) within the 10-minute SLO.

State entries are overwritten after each successful deployment, so the latest repo metadata is always available for future revise calls.

---

## LLM Generation vs Deterministic Templates

- **LLM-first pipeline** (default behaviour):
  - The worker prompts the configured OpenAI-compatible model to return a JSON manifest of repo files.
  - The prompt enforces: static assets only, professional README, MIT license, GitHub Pages workflow, and attachment handling.
  - Generated files are validated (path safety, size caps, required artifacts) before publishing.
  - The raw LLM response is archived under `automation/llm_raw_output.json` inside each generated repo for traceability.
- **Safety fallback** (only on LLM failure/malformed output):
  - Falls back to a minimal deterministic template so evaluators still receive a working repo.
  - The callback still posts within the 10-minute SLO with status notes indicating the fallback.

> Tweak `services/llm_generator.py` if you need different prompt instructions or file validation rules.

---

## GitHub Pages Workflow File

The generated repo includes `.github/workflows/pages.yml`:

```yaml
name: Deploy to GitHub Pages
on:
  push:
    branches: [ main ]
permissions:
  contents: read
  pages: write
  id-token: write
concurrency:
  group: "pages"
  cancel-in-progress: true
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: mkdir -p dist && cp -r * dist || true
      - uses: actions/upload-pages-artifact@v3
        with:
          path: dist
  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - id: deployment
        uses: actions/deploy-pages@v4
```

This ensures any push to `main` deploys the static site. No build tools required.

---

## Logging & Monitoring

- **API** logs: request ID, enqueue time, basic payload metadata (never log secrets/raw attachments).
- **Worker** logs: timing for generation, GitHub calls, Pages polling, callback status.
- Optional **Flower** at `http://localhost:5555` for task visibility (queued/running/succeeded/failed).

---

## Retries & Failure Modes

- **LLM timeouts** (e.g., 60s): one retry; then fallback to deterministic template if applicable.
- **GitHub API** transient errors: exponential backoff (e.g., 1s, 2s, 4s; max 3–5 tries).
- **Pages not live within wait window**: still **send callback** with predictable `pages_url` and log `"pages_status":"pending"`.
- **Callback POST** failure: retry with backoff up to the SLO boundary; log the final outcome.
- **Attachments** that are invalid data-URIs: skip and log.

---

## Security Notes

- **Secrets**: Use environment variables (no hardcoding). The generated repos contain **no server secrets**.
- **Request `secret`**: Optionally validate against an allowlist or HMAC scheme before enqueuing.
- **CORS**: Not exposed (server is backend only).
- **Attachments**: Written to `assets/` as provided; do not execute or interpret them server-side.

---

## Runbook

**To deploy/upgrade**
1. Update `.env` with valid GitHub PAT and owner.
2. `docker compose up --build -d` (or your infra equivalent).
3. Post a sample request to `/app` and watch logs.

**To scale**
- Increase Celery **concurrency** or run multiple worker containers.
- Redis can be moved to a managed service.
- Add a Celery queue per task class if needed (e.g., `q:captcha`, `q:generic`).

**To debug a task**
- Open Flower UI to inspect recent tasks.
- Check GitHub repo actions for Pages build logs.
- Manually hit `pages_url` to verify content once deployed.

---

## License

- Orchestrator: your choice (MIT recommended).
- Generated repos: **MIT** by default (author/year injected automatically).

---

### Quick cURL to test

```bash
cat > sample-request.json <<'JSON'
{
  "email": "student@example.com",
  "secret": "s3cr3t",
  "task": "captcha-solver-001",
  "round": 1,
  "nonce": "ab12-xyz",
  "brief": "Create a captcha solver that handles ?url=https://.../image.png. Default to attached sample.",
  "checks": [
    "Repo has MIT license",
    "README.md is professional",
    "Page displays captcha URL passed at ?url=...",
    "Page displays solved captcha text within 15 seconds"
  ],
  "evaluation_url": "https://example.com/notify",
  "attachments": [
    { "name": "sample.png", "url": "data:image/png;base64,iVBORw0K..." }
  ]
}
JSON

curl -X POST http://127.0.0.1:8000/app   -H 'Content-Type: application/json'   -d @sample-request.json
```
> You should get `{ "ok": true, ... }` immediately. Within 10 minutes, your `evaluation_url` will receive the callback with repo and pages details.

