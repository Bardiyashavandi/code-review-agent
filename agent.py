"""
agent.py
--------
Orchestrates github_fetcher -> semgrep_runner -> gemini_reviewer into a
single pipeline, and exposes it as a Google ADK 2.0 agent tool.

Usage:
    import os
    from agent import CodeReviewAgent

    agent = CodeReviewAgent(
        github_token=os.environ["GITHUB_TOKEN"],
        gemini_api_key=os.environ["GEMINI_API_KEY"],
    )
    result = agent.review_repo("https://github.com/owner/repo")
    for issue in result.review_report.issues:
        print(issue.severity, issue.path, issue.title)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from google.adk.agents import Agent
from google.adk.tools import FunctionTool

from gemini_reviewer import GeminiReviewer, GeminiReviewerError, ReviewReport
from github_fetcher import FetchResult, FileResult, GitHubFetcher
from semgrep_runner import Finding, ScanReport, SemgrepRunner, SemgrepRunnerError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class AgentError(Exception):
    """Orchestrator-level errors (e.g. bad constructor arguments).

    Errors raised by the underlying fetch/scan/review modules are NOT
    re-wrapped here: fetch-stage errors propagate unchanged, scan/review
    -stage errors are captured as StageError instead of raised.
    """
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class StageError:
    stage: str  # "fetch" | "scan" | "review"
    message: str


@dataclass
class PipelineResult:
    repo_url: str
    fetch_result: FetchResult
    scan_report: ScanReport
    review_report: ReviewReport
    stage_errors: list[StageError] = field(default_factory=list)
    duration_s: float = 0.0


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BRANCH = "main"
DEFAULT_MAX_FILES = 100
DEFAULT_MODEL = "gemini-3.1-flash-lite"
DEFAULT_SEMGREP_CONFIG = "auto"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class CodeReviewAgent:
    """
    Orchestrates the full review pipeline: fetch -> scan -> review.

    Only a fetch-stage failure is fatal (there is nothing to review without
    files). Scan and review failures are captured as StageError entries so
    the pipeline always returns a usable, possibly partial, PipelineResult.
    """

    def __init__(
        self,
        github_token: str,
        gemini_api_key: str,
        semgrep_config: str = DEFAULT_SEMGREP_CONFIG,
    ) -> None:
        if not github_token or not github_token.strip():
            raise ValueError("github_token must not be empty")
        if not gemini_api_key or not gemini_api_key.strip():
            raise ValueError("gemini_api_key must not be empty")

        self._fetcher = GitHubFetcher(token=github_token)
        self._semgrep = SemgrepRunner(config=semgrep_config)
        self._reviewer = GeminiReviewer(api_key=gemini_api_key)

    def review_repo(
        self,
        url: str,
        branch: str = DEFAULT_BRANCH,
        max_files: int = DEFAULT_MAX_FILES,
    ) -> PipelineResult:
        """Run the full fetch -> scan -> review pipeline for a single repo."""
        start = time.monotonic()
        stage_errors: list[StageError] = []

        # --- Fetch: fatal on failure -----------------------------------
        fetch_result = self._fetcher.fetch_python_files(url, branch=branch, max_files=max_files)
        logger.info("Fetched %d files from %s", len(fetch_result.files), url)

        # --- Scan: non-fatal on failure ---------------------------------
        try:
            scan_report = self._semgrep.scan(fetch_result.files)
        except (SemgrepRunnerError, ValueError) as exc:
            message = getattr(exc, "message", str(exc))
            logger.warning("Scan stage failed: %s", message)
            stage_errors.append(StageError(stage="scan", message=message))
            scan_report = ScanReport(
                findings=[],
                scanned=0,
                skipped=[f.path for f in fetch_result.files],
                duration_s=0.0,
            )

        # --- Review: non-fatal on failure --------------------------------
        try:
            review_report = self._reviewer.review(fetch_result.files, scan_report)
        except (GeminiReviewerError, ValueError) as exc:
            message = getattr(exc, "message", str(exc))
            logger.warning("Review stage failed: %s", message)
            stage_errors.append(StageError(stage="review", message=message))
            review_report = ReviewReport(
                issues=[],
                summary=f"Review unavailable: {message}",
                model=DEFAULT_MODEL,
                files_reviewed=0,
                duration_s=0.0,
            )

        duration = time.monotonic() - start
        logger.info(
            "Pipeline complete for %s in %.2fs (%d stage errors)",
            url, duration, len(stage_errors),
        )

        return PipelineResult(
            repo_url=url,
            fetch_result=fetch_result,
            scan_report=scan_report,
            review_report=review_report,
            stage_errors=stage_errors,
            duration_s=duration,
        )

    # --- Granular, single-stage entry points -----------------------------
    # These exist so the ADK agent can be given separate fetch/scan/review
    # tools instead of only the one-shot review_repo() pipeline, letting the
    # model itself plan and sequence multi-step tool calls. They delegate to
    # the exact same underlying clients as review_repo() — no new behavior,
    # just exposed individually.

    def fetch_files(
        self,
        url: str,
        branch: str = DEFAULT_BRANCH,
        max_files: int = DEFAULT_MAX_FILES,
    ) -> FetchResult:
        """Fetch a repo's Python files only — no scan, no review."""
        return self._fetcher.fetch_python_files(url, branch=branch, max_files=max_files)

    def scan_files(self, files: list[FileResult]) -> ScanReport:
        """Run Semgrep on an already-fetched list of files only."""
        return self._semgrep.scan(files)

    def generate_review(self, files: list[FileResult], scan_report: ScanReport) -> ReviewReport:
        """Ask Gemini to review an already-fetched list of files, optionally
        grounded by an already-computed ScanReport — no fetch, no scan."""
        return self._reviewer.review(files, scan_report)


# ---------------------------------------------------------------------------
# ADK tool wrapper
# ---------------------------------------------------------------------------

def _pipeline_result_to_dict(result: PipelineResult) -> dict:
    """
    Explicit field mapping from PipelineResult to a JSON-serializable dict.
    Never dumps dataclasses via vars()/__dict__ wholesale, so adding a new
    internal field later can't accidentally leak into the tool's output.
    """
    return {
        "repo_url": result.repo_url,
        "files_fetched": len(result.fetch_result.files),
        "truncated": result.fetch_result.truncated,
        "findings_count": len(result.scan_report.findings),
        "scan_skipped": list(result.scan_report.skipped),
        "issues": [
            {
                "path": issue.path,
                "line": issue.line,
                "severity": issue.severity,
                "title": issue.title,
                "description": issue.description,
                "suggested_fix": issue.suggested_fix,
                "rule_id": issue.rule_id,
            }
            for issue in result.review_report.issues
        ],
        "summary": result.review_report.summary,
        "model": result.review_report.model,
        "stage_errors": [
            {"stage": e.stage, "message": e.message} for e in result.stage_errors
        ],
        "duration_s": result.duration_s,
    }


def make_review_repo_tool(agent: CodeReviewAgent) -> Callable[..., dict]:
    """
    Build the ADK-callable tool function bound to a specific CodeReviewAgent
    instance. Real validation of the URL itself is delegated entirely to
    GitHubFetcher.parse_repo_url (single source of truth) — this function
    only checks that the basic argument shape is sane.
    """

    def review_repo_tool(repo_url: str, branch: str = DEFAULT_BRANCH) -> dict:
        """Review a GitHub repository's Python code and return a summary of findings."""
        if not isinstance(repo_url, str) or not repo_url.strip():
            raise ValueError("repo_url must be a non-empty string")

        result = agent.review_repo(repo_url, branch=branch)
        return _pipeline_result_to_dict(result)

    return review_repo_tool


def make_fetch_repo_files_tool(agent: CodeReviewAgent) -> Callable[..., dict]:
    """Build a standalone 'fetch only' ADK tool bound to a CodeReviewAgent instance."""

    def fetch_repo_files_tool(
        repo_url: str, branch: str = DEFAULT_BRANCH, max_files: int = DEFAULT_MAX_FILES
    ) -> dict:
        """Fetch a GitHub repository's Python files (path + content) without
        scanning or reviewing them. Use this when the user only wants to see
        what files exist, or as the first step of a multi-step review."""
        if not isinstance(repo_url, str) or not repo_url.strip():
            raise ValueError("repo_url must be a non-empty string")

        result = agent.fetch_files(repo_url, branch=branch, max_files=max_files)
        return {
            "repo_url": repo_url,
            "files": [{"path": f.path, "content": f.content} for f in result.files],
            "files_count": len(result.files),
            "truncated": result.truncated,
        }

    return fetch_repo_files_tool


def make_scan_code_tool(agent: CodeReviewAgent) -> Callable[..., dict]:
    """Build a standalone 'scan only' ADK tool bound to a CodeReviewAgent instance."""

    def scan_code_tool(files: list[dict]) -> dict:
        """Run Semgrep static analysis on a list of files, each given as
        {"path": ..., "content": ...}. Use this on files already fetched by
        fetch_repo_files_tool when the user wants static-analysis findings
        on their own, without an LLM review."""
        if not isinstance(files, list) or not files:
            raise ValueError("files must be a non-empty list of {path, content} objects")

        file_results = [
            FileResult(path=f["path"], content=f.get("content", ""), sha="", size=len(f.get("content", "")), url="")
            for f in files
        ]
        scan_report = agent.scan_files(file_results)
        return {
            "findings": [
                {
                    "path": finding.path,
                    "line_start": finding.line_start,
                    "line_end": finding.line_end,
                    "rule_id": finding.rule_id,
                    "severity": finding.severity,
                    "message": finding.message,
                    "snippet": finding.snippet,
                }
                for finding in scan_report.findings
            ],
            "scanned": scan_report.scanned,
            "skipped": list(scan_report.skipped),
        }

    return scan_code_tool


def make_generate_review_tool(agent: CodeReviewAgent) -> Callable[..., dict]:
    """Build a standalone 'review only' ADK tool bound to a CodeReviewAgent instance."""

    def generate_review_tool(files: list[dict], findings: list[dict] | None = None) -> dict:
        """Ask Gemini to produce a structured, severity-ranked code review for
        a list of files, each given as {"path": ..., "content": ...}, optionally
        grounded by Semgrep findings (each {"path", "line_start", "line_end",
        "rule_id", "severity", "message", "snippet"}) from scan_code_tool.
        Use this when files and/or findings were already gathered by the
        other tools and only the review step is still needed."""
        if not isinstance(files, list) or not files:
            raise ValueError("files must be a non-empty list of {path, content} objects")

        file_results = [
            FileResult(path=f["path"], content=f.get("content", ""), sha="", size=len(f.get("content", "")), url="")
            for f in files
        ]
        finding_objs = [
            Finding(
                path=finding["path"],
                line_start=finding.get("line_start", 0),
                line_end=finding.get("line_end", 0),
                rule_id=finding.get("rule_id", ""),
                severity=finding.get("severity", "MEDIUM"),
                message=finding.get("message", ""),
                snippet=finding.get("snippet", ""),
            )
            for finding in (findings or [])
        ]
        scan_report = ScanReport(findings=finding_objs, scanned=len(file_results), skipped=[], duration_s=0.0)

        review_report = agent.generate_review(file_results, scan_report)
        return {
            "issues": [
                {
                    "path": issue.path,
                    "line": issue.line,
                    "severity": issue.severity,
                    "title": issue.title,
                    "description": issue.description,
                    "suggested_fix": issue.suggested_fix,
                    "rule_id": issue.rule_id,
                }
                for issue in review_report.issues
            ],
            "summary": review_report.summary,
            "model": review_report.model,
        }

    return generate_review_tool


def build_adk_agent(
    github_token: str,
    gemini_api_key: str,
    semgrep_config: str = DEFAULT_SEMGREP_CONFIG,
) -> Agent:
    """Construct the Google ADK Agent definition wrapping the review pipeline.

    Exposes both a one-shot tool (review_repo_tool) and three granular
    single-stage tools (fetch_repo_files_tool, scan_code_tool,
    generate_review_tool), so the model can either run the whole pipeline
    in one call or plan and sequence the individual steps itself.
    """
    code_review_agent = CodeReviewAgent(
        github_token=github_token,
        gemini_api_key=gemini_api_key,
        semgrep_config=semgrep_config,
    )

    review_repo_tool = make_review_repo_tool(code_review_agent)
    review_repo_tool.__name__ = "review_repo_tool"

    fetch_repo_files_tool = make_fetch_repo_files_tool(code_review_agent)
    fetch_repo_files_tool.__name__ = "fetch_repo_files_tool"

    scan_code_tool = make_scan_code_tool(code_review_agent)
    scan_code_tool.__name__ = "scan_code_tool"

    generate_review_tool = make_generate_review_tool(code_review_agent)
    generate_review_tool.__name__ = "generate_review_tool"

    return Agent(
        name="code_review_agent",
        model=DEFAULT_MODEL,
        description=(
            "Reviews a GitHub repository's Python code for security and "
            "quality issues using static analysis and an LLM."
        ),
        instruction=(
            "When the user asks for a full review of a GitHub repository, call "
            "review_repo_tool with the repository URL (and branch, if given) — "
            "it runs fetch, scan, and review in one step and is the fastest path "
            "for a typical request. "
            "If the user explicitly asks for just one part of the process (e.g. "
            "'just show me the files', 'just run static analysis', 'just review "
            "this code I'm giving you'), instead use the individual "
            "fetch_repo_files_tool, scan_code_tool, and generate_review_tool, "
            "passing the files and findings returned by one tool into the next "
            "as needed. "
            "Always summarize the resulting issues for the user, prioritized by "
            "severity, and mention any stage_errors plainly if present."
        ),
        tools=[
            FunctionTool(review_repo_tool),
            FunctionTool(fetch_repo_files_tool),
            FunctionTool(scan_code_tool),
            FunctionTool(generate_review_tool),
        ],
    )


# --- Expose root_agent for the loader ---------------------------------------
import os
from dotenv import load_dotenv

# Ensure environment variables are loaded and override any invalid/expired shell values
load_dotenv(override=True)

github_token = os.environ.get("GITHUB_TOKEN", "")
gemini_api_key = os.environ.get("GEMINI_API_KEY", "")

root_agent = build_adk_agent(
    github_token=github_token,
    gemini_api_key=gemini_api_key,
)
