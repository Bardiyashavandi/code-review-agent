"""
tests/test_agent.py
---------------------
Tests for agent.py's orchestration logic. GitHubFetcher, SemgrepRunner, and
GeminiReviewer are all mocked at the agent module level — these tests verify
only the orchestration (sequencing, partial-failure handling, ADK tool
shape), not the underlying modules, which have their own test suites.

Run with:
    pytest tests/test_agent.py -v
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import (
    CodeReviewAgent,
    PipelineResult,
    make_fetch_repo_files_tool,
    make_generate_review_tool,
    make_review_repo_tool,
    make_scan_code_tool,
)
from gemini_reviewer import GeminiRateLimitError
from semgrep_runner import SemgrepExecutionError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_fetch_result(paths=("a.py", "b.py"), truncated=False) -> SimpleNamespace:
    files = [SimpleNamespace(path=p, content="x = 1\n") for p in paths]
    return SimpleNamespace(files=files, truncated=truncated)


def make_scan_report(findings_count=0) -> SimpleNamespace:
    findings = [
        SimpleNamespace(path="a.py", rule_id=f"rule.{i}", severity="WARNING",
                         line_start=1, line_end=1, message="m", snippet="x = 1")
        for i in range(findings_count)
    ]
    return SimpleNamespace(findings=findings, scanned=2, skipped=[], duration_s=0.1)


def make_review_report(issue_count=0) -> SimpleNamespace:
    issues = [
        SimpleNamespace(path="a.py", line=1, severity="HIGH", title=f"issue {i}",
                         description="d", suggested_fix="f", rule_id=None)
        for i in range(issue_count)
    ]
    return SimpleNamespace(issues=issues, summary="ok", model="gemini-2.5-flash",
                            files_reviewed=2, duration_s=0.1)


def make_agent(fetch_result=None, scan_result=None, review_result=None,
               scan_side_effect=None, review_side_effect=None):
    """
    Construct a CodeReviewAgent with all three underlying clients mocked.
    Returns (agent, mock_fetcher_instance, mock_semgrep_instance, mock_reviewer_instance).
    """
    with patch("agent.GitHubFetcher") as MockFetcher, \
         patch("agent.SemgrepRunner") as MockSemgrep, \
         patch("agent.GeminiReviewer") as MockReviewer:

        mock_fetcher = MagicMock()
        mock_fetcher.fetch_python_files.return_value = fetch_result or make_fetch_result()
        MockFetcher.return_value = mock_fetcher

        mock_semgrep = MagicMock()
        if scan_side_effect is not None:
            mock_semgrep.scan.side_effect = scan_side_effect
        else:
            mock_semgrep.scan.return_value = scan_result or make_scan_report()
        MockSemgrep.return_value = mock_semgrep

        mock_reviewer = MagicMock()
        if review_side_effect is not None:
            mock_reviewer.review.side_effect = review_side_effect
        else:
            mock_reviewer.review.return_value = review_result or make_review_report()
        MockReviewer.return_value = mock_reviewer

        agent = CodeReviewAgent(github_token="ghp_faketoken", gemini_api_key="gem_fakekey")

    return agent, mock_fetcher, mock_semgrep, mock_reviewer


# ---------------------------------------------------------------------------
# 1. Construction
# ---------------------------------------------------------------------------

class TestConstruction:

    def test_empty_github_token_raises(self):
        with pytest.raises(ValueError, match="github_token"):
            CodeReviewAgent(github_token="", gemini_api_key="gem_fakekey")

    def test_empty_gemini_key_raises(self):
        with patch("agent.GitHubFetcher"), patch("agent.SemgrepRunner"):
            with pytest.raises(ValueError, match="gemini_api_key"):
                CodeReviewAgent(github_token="ghp_faketoken", gemini_api_key="")


# ---------------------------------------------------------------------------
# 2. Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:

    def test_happy_path_runs_all_three_stages(self):
        agent, mock_fetcher, mock_semgrep, mock_reviewer = make_agent(
            review_result=make_review_report(issue_count=1)
        )

        result = agent.review_repo("https://github.com/owner/repo")

        mock_fetcher.fetch_python_files.assert_called_once()
        mock_semgrep.scan.assert_called_once()
        mock_reviewer.review.assert_called_once()

        assert isinstance(result, PipelineResult)
        assert result.stage_errors == []
        assert len(result.review_report.issues) == 1

    def test_pipeline_result_has_duration(self):
        agent, *_ = make_agent()
        result = agent.review_repo("https://github.com/owner/repo")
        assert result.duration_s >= 0


# ---------------------------------------------------------------------------
# 3. Fatal vs non-fatal failures
# ---------------------------------------------------------------------------

class TestFailureHandling:

    def test_fetch_failure_is_fatal(self):
        class FakeNotFound(Exception):
            pass

        agent, mock_fetcher, *_ = make_agent()
        mock_fetcher.fetch_python_files.side_effect = FakeNotFound("repo not found")

        with pytest.raises(FakeNotFound):
            agent.review_repo("https://github.com/owner/repo")

    def test_scan_failure_is_non_fatal(self):
        agent, mock_fetcher, mock_semgrep, mock_reviewer = make_agent(
            scan_side_effect=SemgrepExecutionError("boom", returncode=2)
        )

        result = agent.review_repo("https://github.com/owner/repo")

        assert len(result.stage_errors) == 1
        assert result.stage_errors[0].stage == "scan"
        mock_reviewer.review.assert_called_once()

    def test_scan_failure_falls_back_empty_report(self):
        agent, mock_fetcher, mock_semgrep, mock_reviewer = make_agent(
            scan_side_effect=SemgrepExecutionError("boom", returncode=2)
        )

        agent.review_repo("https://github.com/owner/repo")

        call_args = mock_reviewer.review.call_args
        scan_report_passed = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("scan_report")
        assert scan_report_passed.findings == []

    def test_review_failure_is_non_fatal(self):
        agent, *_ = make_agent(
            review_side_effect=GeminiRateLimitError("rate limited")
        )

        result = agent.review_repo("https://github.com/owner/repo")

        assert len(result.stage_errors) == 1
        assert result.stage_errors[0].stage == "review"
        assert result.review_report.issues == []

    def test_both_scan_and_review_fail(self):
        agent, *_ = make_agent(
            scan_side_effect=SemgrepExecutionError("boom", returncode=2),
            review_side_effect=GeminiRateLimitError("rate limited"),
        )

        result = agent.review_repo("https://github.com/owner/repo")

        stages = {e.stage for e in result.stage_errors}
        assert stages == {"scan", "review"}
        assert isinstance(result, PipelineResult)


# ---------------------------------------------------------------------------
# 4. ADK tool wrapper
# ---------------------------------------------------------------------------

class TestAdkToolWrapper:

    def test_review_repo_tool_returns_json_serializable_dict(self):
        agent, *_ = make_agent(review_result=make_review_report(issue_count=2))
        tool = make_review_repo_tool(agent)

        output = tool("https://github.com/owner/repo")

        json.dumps(output)  # should not raise

    def test_review_repo_tool_does_not_leak_internal_fields(self):
        agent, *_ = make_agent(review_result=make_review_report(issue_count=1))
        tool = make_review_repo_tool(agent)

        output = tool("https://github.com/owner/repo")

        expected_keys = {
            "repo_url", "files_fetched", "truncated", "findings_count",
            "scan_skipped", "issues", "summary", "model", "stage_errors",
            "duration_s",
        }
        assert set(output.keys()) == expected_keys


# ---------------------------------------------------------------------------
# 4b. Granular single-stage entry points + their ADK tool wrappers
# ---------------------------------------------------------------------------

class TestGranularEntryPoints:

    def test_fetch_files_delegates_to_fetcher(self):
        agent, mock_fetcher, mock_semgrep, mock_reviewer = make_agent()

        result = agent.fetch_files("https://github.com/owner/repo")

        mock_fetcher.fetch_python_files.assert_called_once()
        mock_semgrep.scan.assert_not_called()
        mock_reviewer.review.assert_not_called()
        assert result is mock_fetcher.fetch_python_files.return_value

    def test_scan_files_delegates_to_semgrep(self):
        agent, mock_fetcher, mock_semgrep, mock_reviewer = make_agent()

        result = agent.scan_files([SimpleNamespace(path="a.py", content="x = 1\n")])

        mock_semgrep.scan.assert_called_once()
        mock_fetcher.fetch_python_files.assert_not_called()
        mock_reviewer.review.assert_not_called()
        assert result is mock_semgrep.scan.return_value

    def test_generate_review_delegates_to_reviewer(self):
        agent, mock_fetcher, mock_semgrep, mock_reviewer = make_agent()

        files = [SimpleNamespace(path="a.py", content="x = 1\n")]
        scan_report = make_scan_report()
        result = agent.generate_review(files, scan_report)

        mock_reviewer.review.assert_called_once_with(files, scan_report)
        mock_fetcher.fetch_python_files.assert_not_called()
        mock_semgrep.scan.assert_not_called()
        assert result is mock_reviewer.review.return_value


class TestGranularAdkTools:

    def test_fetch_repo_files_tool_returns_json_serializable_dict(self):
        agent, *_ = make_agent(fetch_result=make_fetch_result(paths=("a.py",)))
        tool = make_fetch_repo_files_tool(agent)

        output = tool("https://github.com/owner/repo")

        json.dumps(output)
        assert set(output.keys()) == {"repo_url", "files", "files_count", "truncated"}
        assert output["files_count"] == 1

    def test_fetch_repo_files_tool_rejects_empty_url(self):
        agent, *_ = make_agent()
        tool = make_fetch_repo_files_tool(agent)

        with pytest.raises(ValueError, match="repo_url"):
            tool("")

    def test_scan_code_tool_returns_json_serializable_dict(self):
        agent, *_ = make_agent(scan_result=make_scan_report(findings_count=1))
        tool = make_scan_code_tool(agent)

        output = tool([{"path": "a.py", "content": "x = 1\n"}])

        json.dumps(output)
        assert set(output.keys()) == {"findings", "scanned", "skipped"}
        assert len(output["findings"]) == 1

    def test_scan_code_tool_rejects_empty_files(self):
        agent, *_ = make_agent()
        tool = make_scan_code_tool(agent)

        with pytest.raises(ValueError, match="files"):
            tool([])

    def test_generate_review_tool_returns_json_serializable_dict(self):
        agent, *_ = make_agent(review_result=make_review_report(issue_count=1))
        tool = make_generate_review_tool(agent)

        output = tool(
            [{"path": "a.py", "content": "x = 1\n"}],
            findings=[{"path": "a.py", "rule_id": "r1", "severity": "WARNING", "message": "m"}],
        )

        json.dumps(output)
        assert set(output.keys()) == {"issues", "summary", "model"}
        assert len(output["issues"]) == 1

    def test_generate_review_tool_works_without_findings(self):
        agent, *_ = make_agent(review_result=make_review_report(issue_count=0))
        tool = make_generate_review_tool(agent)

        output = tool([{"path": "a.py", "content": "x = 1\n"}])

        json.dumps(output)
        assert output["issues"] == []

    def test_generate_review_tool_rejects_empty_files(self):
        agent, *_ = make_agent()
        tool = make_generate_review_tool(agent)

        with pytest.raises(ValueError, match="files"):
            tool([])


# ---------------------------------------------------------------------------
# 5. Secret hygiene
# ---------------------------------------------------------------------------

class TestSecretHygiene:

    def test_secrets_never_logged(self, caplog):
        agent, *_ = make_agent(
            scan_side_effect=SemgrepExecutionError("boom", returncode=2),
            review_side_effect=GeminiRateLimitError("rate limited"),
        )

        with caplog.at_level(logging.DEBUG):
            agent.review_repo("https://github.com/owner/repo")

        for record in caplog.records:
            assert "ghp_faketoken" not in record.getMessage()
            assert "gem_fakekey" not in record.getMessage()
