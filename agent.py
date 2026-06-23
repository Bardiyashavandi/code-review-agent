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
from dataclasses import dataclass, field
from typing import Callable

from google.adk.agents import Agent
from google.adk.tools import FunctionTool

from github_fetcher import FetchResult, GitHubFetcher
from semgrep_runner import ScanReport, SemgrepRunner, SemgrepRunnerError
from gemini_reviewer import GeminiReviewer, GeminiReviewerError, ReviewReport

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


def build_adk_agent(
    github_token: str,
    gemini_api_key: str,
    semgrep_config: str = DEFAULT_SEMGREP_CONFIG,
) -> Agent:
    """Construct the Google ADK Agent definition wrapping the review pipeline."""
    code_review_agent = CodeReviewAgent(
        github_token=github_token,
        gemini_api_key=gemini_api_key,
        semgrep_config=semgrep_config,
    )
    tool_fn = make_review_repo_tool(code_review_agent)
    tool_fn.__name__ = "review_repo_tool"

    return Agent(
        name="code_review_agent",
        model=DEFAULT_MODEL,
        description=(
            "Reviews a GitHub repository's Python code for security and "
            "quality issues using static analysis and an LLM."
        ),
        instruction=(
            "When the user asks you to review a GitHub repository, call the "
            "review_repo_tool with the repository URL (and branch, if given). "
            "Summarize the resulting issues for the user, prioritized by "
            "severity, and mention any stage_errors plainly if present."
        ),
        tools=[FunctionTool(tool_fn)],
    )
