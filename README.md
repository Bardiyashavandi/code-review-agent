<div align="center">

# AI Code Review Agent

**Give it a GitHub URL. Get back a prioritized, fix-it-now code review.**

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-107%20passing-brightgreen)
![ADK](https://img.shields.io/badge/Google%20ADK-2.0-orange)
![ADK Tools](https://img.shields.io/badge/ADK%20tools-8-blueviolet)
![Cost](https://img.shields.io/badge/cost-%240-success)

Kaggle 5-Day AI Agents Capstone — track: **Agents for Business**

</div>

---

## Contents

- [The idea](#the-idea)
- [Architecture](#architecture)
- [What a run actually looks like](#what-a-run-actually-looks-like)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Security, by design](#security-by-design)
- [Testing](#testing)
- [Real-world verification](#real-world-verification-not-just-mocks)
- [Project structure](#project-structure)
- [Known limitations](#known-limitations)
- [License](#license)

## The idea

Static analyzers find patterns but can't explain why they matter. LLMs can explain things but hallucinate when given no real grounding. This agent closes that gap: it fetches your actual repository, runs real Semgrep static analysis on it, and hands both the code and the findings to Gemini 3.1 Flash Lite — so every issue in the final report is backed by either a deterministic rule or a model that's actually looking at your code, never a guess.

Only a fetch failure is treated as fatal — there's nothing to review without files. A Semgrep or Gemini hiccup is captured as a non-fatal `StageError` instead, so the pipeline always returns a usable result, degraded but never empty-handed. This isn't theoretical: during real testing, Gemini intermittently threw transient `503` errors under load, and the retry logic kept the run going without dropping it.

## Architecture

```
                       ┌────────────────────┐
   repo URL ──────────►│   github_fetcher   │── GitHub API
                       └──────────┬─────────┘
                                  │ Python files
                                  ▼
                       ┌────────────────────┐
                       │   semgrep_runner   │── sandboxed subprocess
                       └──────────┬─────────┘
                                  │ files + findings
                                  ▼
                       ┌────────────────────┐
                       │   gemini_reviewer  │── Gemini 3.1 Flash Lite
                       └──────────┬─────────┘
                                  │ structured issues
                                  ▼
                       ┌────────────────────┐
                       │  report_generator  │── review_report.md
                       └────────────────────┘

   agent.py orchestrates the above AND exposes it as a
   Google ADK 2.0 Agent + FunctionTool, so an LLM-driven
   agent runtime can decide on its own when to call it.
```

| Stage | Module | Job |
|---|---|---|
| 1. Fetch | `github_fetcher.py` | Walks the repo tree via the GitHub API, pulls every Python file, skips venvs/build noise |
| 2. Scan | `semgrep_runner.py` | Writes files into an isolated sandbox, runs Semgrep, parses JSON into typed findings |
| 3. Review | `gemini_reviewer.py` | Batches code + findings into prompts, asks Gemini 3.1 Flash Lite for a structured, severity-ranked review |

### The agent's tool graph

`agent.py` doesn't just run that pipeline once — it exposes **eight separate tools** to the ADK agent, all as flat siblings under the agent, so the model plans its own path through them instead of always running the whole thing:

```
                              code_review_agent
                                     |
   +---------------+---------------+----------------+----------------+
   |               |               |                |                |
review_repo_   fetch_repo_     scan_code_      generate_       get_repo_
tool           files_tool      tool            review_tool     metadata_tool
(one-shot:     (fetch only)    (Semgrep        (Gemini review  (language/size/
 fetch+scan+                    only)           only)           stars, no fetch)
 review)

   |               |               |
search_code_   explain_         generate_
in_files_tool  finding_tool     report_file_tool
(grep fetched  (deep-dive on    (save review as
 files)         one issue)       a real .md file)
```

A one-line request like *"review this repo"* collapses to a single tool call. A narrower request — *"just show me the files,"* *"find every place using eval,"* *"explain that issue further,"* *"save this as a file"* — makes the model pick (and chain) the right tool(s) itself, which is the actual point of using an agent framework instead of one big function.

## What a run actually looks like

```
$ python3 main.py https://github.com/owner/repo --branch main --out review_report.md -v

Files fetched: 25  |  Semgrep findings: 2  |  Review issues: 23  |  Duration: 96.3s

### CRITICAL
Flask Debug Mode Enabled in Production (app.py:115)
  Running with debug=True in production exposes tracebacks, environment
  variables, and an interactive debugger capable of arbitrary code execution.
  Suggested fix: set debug=False and gate it behind an environment-driven config.

Hardcoded Mock API Key (agent.py:95)
  A string matching a real credential's prefix format is hardcoded. Even
  "mock" keys risk being mistaken for real ones or copied into production.
  Suggested fix: load all keys from environment variables, never literals.
```

That's a real run against a real, unmodified repository, not a cherry-picked fixture — see [Real-world verification](#real-world-verification-not-just-mocks) below.

## Quick start

```bash
git clone https://github.com/Bardiyashavandi/code-review-agent
cd code-review-agent
python3 -m pip install -r requirements.txt
pipx install semgrep   # isolated — see "Why pipx" below
```

Create a `.env` file in the project root:

```
GITHUB_TOKEN=ghp_your_token_here
GEMINI_API_KEY=your_gemini_key_here
```

Run it:

```bash
python3 main.py https://github.com/owner/repo --branch main --out review_report.md -v
```

`--max-files` (default `10`) caps how many Python files get reviewed per run — kept conservative by default since Gemini's free tier caps requests per day; raise it if you have a higher quota.

## How it works

`agent.py` orchestrates all three stages behind a single `CodeReviewAgent.review_repo()` call, and also exposes the same pipeline as a Google ADK 2.0 `Agent` + `FunctionTool` (via a module-level `root_agent`) — so a Gemini-powered ADK agent can decide for itself, from a plain-language request, to call `review_repo_tool`. `report_generator.py` renders the result to Markdown, and `main.py` is the CLI entry point.

**Use it programmatically:**

```python
import os
from agent import CodeReviewAgent

agent = CodeReviewAgent(
    github_token=os.environ["GITHUB_TOKEN"],
    gemini_api_key=os.environ["GEMINI_API_KEY"],
)
result = agent.review_repo("https://github.com/owner/repo")
for issue in result.review_report.issues:
    print(issue.severity, issue.path, issue.title)
```

**Use it as an ADK agent** — the model decides on its own which tool(s) to call:

```python
from agent import build_adk_agent

adk_agent = build_adk_agent(
    github_token=os.environ["GITHUB_TOKEN"],
    gemini_api_key=os.environ["GEMINI_API_KEY"],
)
```

Run `adk_agent` through any ADK `Runner` (e.g. `google.adk.runners.InMemoryRunner`) — or just run `python3 adk_demo.py` for a ready-made example. The agent exposes eight tools, not one:

| Tool | Does |
|---|---|
| `review_repo_tool` | One-shot: fetch + scan + review a repo URL in a single call |
| `fetch_repo_files_tool` | Fetch a repo's Python files only |
| `scan_code_tool` | Run Semgrep on a given set of files only |
| `generate_review_tool` | Ask Gemini to review a given set of files (+ optional findings) only |
| `get_repo_metadata_tool` | Look up a repo's language, size, stars, default branch — no file fetch |
| `search_code_in_files_tool` | Regex/keyword search across already-fetched files |
| `explain_finding_tool` | Ask Gemini for a focused, deeper explanation of one already-known issue |
| `generate_report_file_tool` | Render an already-produced review as Markdown and save it to disk |

For a typical request like *"review https://github.com/owner/repo and summarize the top issues,"* the model calls `review_repo_tool` directly. For a narrower request — *"just show me the files in this repo,"* *"just run static analysis,"* *"find every place using eval,"* *"explain issue #3 in more detail,"* or *"save that as a file"* — the model instead reaches for the appropriate single tool, or plans a multi-step call sequence using the granular pipeline tools, passing each tool's output into the next itself. The agent's instructions also keep it in scope: asked something unrelated to code review, it declines and redirects rather than forcing an unrelated tool call. All of this was exercised and verified live in the ADK Dev UI playground, where the tool graph now shows eight distinct nodes branching from the agent.

This was verified two independent ways: once via the standalone `adk_demo.py` script, and again interactively in Google's own ADK Dev UI playground (`adk web`), which loads `agent.py`'s module-level `root_agent` and lets you chat with the agent directly in a browser, complete with a visual graph of all eight tool nodes. Both surfaced the same correct behavior — the model picking the right tool (or chaining several) for the request and returning accurate, well-formed output — which is stronger evidence than either check alone, since the Dev UI is Google's own tooling, not code this project wrote.

## Security, by design

- Every subprocess call (Semgrep) uses explicit argument lists — never `shell=True`.
- File paths from a fetched repo are validated against path traversal before touching disk.
- Semgrep's `--config` argument is allow-listed by regex against argument injection.
- Gemini's system prompt instructs the model to treat all file contents and Semgrep output as **untrusted data, not instructions** — a malicious commit containing "ignore previous instructions" can't redirect the review. Tested directly with an injected payload.
- No credentials are ever hardcoded. Both API keys load from environment variables only, and `test_secrets_never_logged` asserts a key never leaks into a log line or exception message.
- Model output is never evaluated as code or interpolated unsafely into the report — tested with an injected `__import__` payload.

## Testing

```bash
pytest -v
```

107 tests across all five modules. Every external dependency — GitHub's API, the Semgrep subprocess, the Gemini SDK — is mocked, so the suite runs in about a second with no network access or credentials.

## Real-world verification, not just mocks

A real end-to-end run (not a test fixture) fetched 25 files, ran a live Semgrep scan, called Gemini 3.1 Flash Lite, and produced a 23-issue report in 96 seconds with genuine findings — a Flask app left in debug mode, a hardcoded mock API key, an endpoint trusting a client-supplied ID. That run also surfaced three real integration bugs no mock could have caught, all now fixed and covered by regression tests:

1. **Dependency conflict** — `google-adk` and `semgrep` pin incompatible `opentelemetry` ranges. Fixed by isolating Semgrep into its own `pipx` environment.
2. **Stale shell env var** — `python-dotenv` never overrides an already-exported variable, so an old `GEMINI_API_KEY` from a previous test silently beat the correct `.env` value. Fixed by loading `.env` with `override=True`.
3. **macOS symlink bug** — macOS resolves its temp dir through a `/private/...` symlink; a path comparison that worked fine on Linux raised `ValueError` on a real Mac.

The ADK agent itself was also verified two ways — once via the `adk_demo.py` terminal script, and again live in Google's ADK Dev UI playground (`adk web`) — both producing the same correct tool-calling behavior and accurate review output.

### Why `pipx` for Semgrep

`google-adk` and `semgrep` pin incompatible ranges of `opentelemetry-api`/`opentelemetry-sdk` — installing both into one environment breaks one of them. `pipx` gives Semgrep its own isolated venv; `semgrep_runner.py` only ever shells out to the `semgrep` binary on `PATH`, so the isolation is invisible to the rest of the project.

## Project structure

```
code-review-agent/
├── agent.py                  # orchestrator + ADK Agent/FunctionTool (exposes root_agent)
├── github_fetcher.py         # stage 1: fetch
├── semgrep_runner.py         # stage 2: scan
├── gemini_reviewer.py        # stage 3: review
├── report_generator.py       # Markdown rendering
├── main.py                   # CLI entry point
├── adk_demo.py                # standalone ADK tool-calling demo
├── *_spec.md                  # spec written before each module's code
├── tests/                     # 107 tests, one file per module
├── deploy/                    # optional cloud-deployment scaffold (not used for this
│                               # submission — see "Known limitations"), generated by
│                               # `agents-cli`: Dockerfile, FastAPI wrapper, uv-based build
├── KAGGLE_WRITEUP.md           # full capstone writeup
└── VIDEO_SCRIPT.md             # demo video script
```

## Known limitations

`--config auto` requires reaching `semgrep.dev`'s rule registry over the network; locked-down CI runners or sandboxes with restrictive egress will need a local or registry-pinned ruleset instead. Gemini occasionally returns a transient `503` under high demand — `gemini_reviewer.py` retries automatically with exponential backoff, but a sustained outage still surfaces as a non-fatal `StageError` rather than blocking the run. Free-tier Gemini keys also cap total requests per day (not just per minute) — `--max-files` defaults to `10` and batches include a short inter-batch delay specifically to stretch a free-tier quota further.

The `deploy/` folder contains a scaffolded Cloud Run/FastAPI deployment target generated while exploring Google's `agents-cli` tooling. It is not built, run, or required for this submission — real cloud deployment would typically need a billing-enabled Google Cloud project, which conflicts with this project's no-paid-services constraint, so it's kept here only as a documented next step.

## What this demonstrates

Every module started as a written spec (interface, behavior, error hierarchy, test table) before any implementation code — the `*_spec.md` files in this repo are the visible record of that. The orchestrator is a genuine Google ADK 2.0 tool, with the agent runtime itself deciding when to invoke the pipeline. No paid services are used anywhere — Semgrep's `--config auto`, Gemini, and the GitHub API are all free-tier, by hard constraint from day one.

Full writeup: [`KAGGLE_WRITEUP.md`](./KAGGLE_WRITEUP.md). Demo video script: [`VIDEO_SCRIPT.md`](./VIDEO_SCRIPT.md).

## License

MIT — see [`LICENSE`](./LICENSE).
