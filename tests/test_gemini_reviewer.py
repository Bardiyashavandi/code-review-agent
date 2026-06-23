"""
tests/test_gemini_reviewer.py
-------------------------------
Full test suite for gemini_reviewer.py.
The Gemini SDK client is fully mocked — no live API calls, no real key
required.

Run with:
    pytest tests/test_gemini_reviewer.py -v
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from google.genai import errors as genai_errors

from gemini_reviewer import (
    SYSTEM_INSTRUCTION,
    GeminiAuthenticationError,
    GeminiRateLimitError,
    GeminiReviewer,
    ReviewReport,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_file(path: str, content: str = "x = 1\n") -> SimpleNamespace:
    """Stand-in for github_fetcher.FileResult — only .path/.content are used."""
    return SimpleNamespace(path=path, content=content)


def make_finding(path: str, rule_id: str = "rule.x", severity: str = "WARNING",
                  line_start: int = 1, message: str = "msg") -> SimpleNamespace:
    """Stand-in for semgrep_runner.Finding."""
    return SimpleNamespace(path=path, rule_id=rule_id, severity=severity,
                            line_start=line_start, message=message)


def make_scan_report(findings=None) -> SimpleNamespace:
    """Stand-in for semgrep_runner.ScanReport."""
    return SimpleNamespace(findings=findings or [])


def response_text(summary: str = "ok", issues: list | None = None) -> SimpleNamespace:
    """Build a fake Gemini response object with a .text attribute."""
    payload = {"summary": summary, "issues": issues or []}
    return SimpleNamespace(text=json.dumps(payload))


def make_reviewer(**kwargs) -> tuple[GeminiReviewer, MagicMock]:
    """Construct a GeminiReviewer with genai.Client mocked out entirely."""
    with patch("gemini_reviewer.genai.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        reviewer = GeminiReviewer(api_key="fake-key-123", **kwargs)
    return reviewer, mock_client


# ---------------------------------------------------------------------------
# 1. Construction / input validation
# ---------------------------------------------------------------------------

class TestConstruction:

    def test_empty_api_key_raises(self):
        with pytest.raises(ValueError, match="GEMINI_API_KEY must not be empty"):
            GeminiReviewer(api_key="")

    def test_empty_files_raises(self):
        reviewer, _ = make_reviewer()
        with pytest.raises(ValueError, match="No files to review"):
            reviewer.review([], make_scan_report())


# ---------------------------------------------------------------------------
# 2. Batching
# ---------------------------------------------------------------------------

class TestBatching:

    def test_batches_respect_max_files(self):
        reviewer, mock_client = make_reviewer(max_files_per_batch=10, max_chars_per_batch=1_000_000)
        files = [make_file(f"f{i}.py") for i in range(25)]
        mock_client.models.generate_content.return_value = response_text()

        reviewer.review(files, make_scan_report())

        assert mock_client.models.generate_content.call_count == 3

    def test_batches_respect_max_chars(self):
        reviewer, mock_client = make_reviewer(max_files_per_batch=100, max_chars_per_batch=100)
        big_content = "x" * 80
        files = [make_file("a.py", big_content), make_file("b.py", big_content)]
        mock_client.models.generate_content.return_value = response_text()

        reviewer.review(files, make_scan_report())

        assert mock_client.models.generate_content.call_count == 2

    def test_findings_filtered_per_batch(self):
        reviewer, mock_client = make_reviewer(max_files_per_batch=1, max_chars_per_batch=1_000_000)
        files = [make_file("a.py"), make_file("b.py")]
        findings = [make_finding("b.py", rule_id="rule.b")]
        mock_client.models.generate_content.return_value = response_text()

        reviewer.review(files, make_scan_report(findings))

        calls = mock_client.models.generate_content.call_args_list
        assert len(calls) == 2
        prompt_a = calls[0].kwargs["contents"]
        prompt_b = calls[1].kwargs["contents"]
        assert "rule.b" not in prompt_a
        assert "rule.b" in prompt_b


# ---------------------------------------------------------------------------
# 3. Output parsing
# ---------------------------------------------------------------------------

class TestOutputParsing:

    def test_parses_issues_correctly(self):
        reviewer, mock_client = make_reviewer()
        files = [make_file("a.py")]
        issues_payload = [
            {
                "path": "a.py", "line": 3, "severity": "HIGH",
                "title": "SQL injection risk", "description": "desc",
                "suggested_fix": "use parameterized queries", "rule_id": "rule.sql",
            },
            {
                "path": "a.py", "line": 10, "severity": "LOW",
                "title": "Unused import", "description": "desc2",
                "suggested_fix": "remove it", "rule_id": None,
            },
        ]
        mock_client.models.generate_content.return_value = response_text(issues=issues_payload)

        report = reviewer.review(files, make_scan_report())

        assert len(report.issues) == 2
        first = [i for i in report.issues if i.title == "SQL injection risk"][0]
        assert first.path == "a.py"
        assert first.line == 3
        assert first.severity == "HIGH"
        assert first.rule_id == "rule.sql"

    def test_severity_unknown_defaults_medium(self):
        reviewer, mock_client = make_reviewer()
        files = [make_file("a.py")]
        issues_payload = [{
            "path": "a.py", "line": 1, "severity": "urgent",
            "title": "t", "description": "d", "suggested_fix": "f", "rule_id": None,
        }]
        mock_client.models.generate_content.return_value = response_text(issues=issues_payload)

        report = reviewer.review(files, make_scan_report())

        assert report.issues[0].severity == "MEDIUM"

    def test_issues_sorted_by_severity(self):
        reviewer, mock_client = make_reviewer()
        files = [make_file("a.py")]
        issues_payload = [
            {"path": "a.py", "line": 1, "severity": "LOW", "title": "low", "description": "", "suggested_fix": "", "rule_id": None},
            {"path": "a.py", "line": 2, "severity": "CRITICAL", "title": "crit", "description": "", "suggested_fix": "", "rule_id": None},
            {"path": "a.py", "line": 3, "severity": "MEDIUM", "title": "med", "description": "", "suggested_fix": "", "rule_id": None},
        ]
        mock_client.models.generate_content.return_value = response_text(issues=issues_payload)

        report = reviewer.review(files, make_scan_report())

        assert [i.title for i in report.issues] == ["crit", "med", "low"]

    def test_malformed_json_batch_dropped(self):
        reviewer, mock_client = make_reviewer(max_files_per_batch=1, max_chars_per_batch=1_000_000)
        files = [make_file("bad.py"), make_file("good.py")]

        good_payload = [{
            "path": "good.py", "line": 1, "severity": "HIGH",
            "title": "real issue", "description": "", "suggested_fix": "", "rule_id": None,
        }]
        mock_client.models.generate_content.side_effect = [
            SimpleNamespace(text="not valid json {{{"),
            response_text(issues=good_payload),
        ]

        report = reviewer.review(files, make_scan_report())

        assert len(report.issues) == 1
        assert report.issues[0].title == "real issue"


# ---------------------------------------------------------------------------
# 4. Error handling / retries
# ---------------------------------------------------------------------------

class TestErrorHandling:

    def test_401_raises_auth_error(self):
        reviewer, mock_client = make_reviewer()
        files = [make_file("a.py")]
        mock_client.models.generate_content.side_effect = genai_errors.APIError(
            code=401, response_json={"message": "invalid api key ABC123"}
        )

        with pytest.raises(GeminiAuthenticationError):
            reviewer.review(files, make_scan_report())

    def test_api_key_not_in_exception_message(self):
        reviewer, mock_client = make_reviewer()
        files = [make_file("a.py")]
        mock_client.models.generate_content.side_effect = genai_errors.APIError(
            code=401, response_json={"message": "Bad credentials"}
        )

        with pytest.raises(GeminiAuthenticationError) as exc_info:
            reviewer.review(files, make_scan_report())

        assert "fake-key-123" not in exc_info.value.message
        assert "fake-key-123" not in str(exc_info.value)

    def test_429_retries_then_succeeds(self):
        reviewer, mock_client = make_reviewer()
        files = [make_file("a.py")]
        rate_limit_error = genai_errors.APIError(code=429, response_json={"message": "quota exceeded"})
        mock_client.models.generate_content.side_effect = [
            rate_limit_error,
            response_text(),
        ]

        with patch("gemini_reviewer.time.sleep"):
            report = reviewer.review(files, make_scan_report())

        assert mock_client.models.generate_content.call_count == 2
        assert isinstance(report, ReviewReport)

    def test_429_exhausted_raises(self):
        reviewer, mock_client = make_reviewer()
        files = [make_file("a.py")]
        rate_limit_error = genai_errors.APIError(code=429, response_json={"message": "quota exceeded"})
        mock_client.models.generate_content.side_effect = rate_limit_error

        with patch("gemini_reviewer.time.sleep"):
            with pytest.raises(GeminiRateLimitError):
                reviewer.review(files, make_scan_report())

    def test_503_retries_then_succeeds(self):
        # Real-world case: "This model is currently experiencing high
        # demand" -- transient, should retry rather than fail immediately.
        reviewer, mock_client = make_reviewer()
        files = [make_file("a.py")]
        overload_error = genai_errors.APIError(code=503, response_json={"message": "high demand"})
        mock_client.models.generate_content.side_effect = [
            overload_error,
            response_text(),
        ]

        with patch("gemini_reviewer.time.sleep"):
            report = reviewer.review(files, make_scan_report())

        assert mock_client.models.generate_content.call_count == 2
        assert isinstance(report, ReviewReport)

    def test_503_exhausted_raises_api_error(self):
        from gemini_reviewer import GeminiAPIError
        reviewer, mock_client = make_reviewer()
        files = [make_file("a.py")]
        overload_error = genai_errors.APIError(code=503, response_json={"message": "high demand"})
        mock_client.models.generate_content.side_effect = overload_error

        with patch("gemini_reviewer.time.sleep"):
            with pytest.raises(GeminiAPIError):
                reviewer.review(files, make_scan_report())


# ---------------------------------------------------------------------------
# 5. Prompt safety
# ---------------------------------------------------------------------------

class TestPromptSafety:

    def test_prompt_instructs_against_injection(self):
        lowered = SYSTEM_INSTRUCTION.lower()
        assert "ignore" in lowered
        assert "untrusted data" in lowered or "not as instructions" in lowered

    def test_no_eval_of_model_output(self):
        reviewer, mock_client = make_reviewer()
        files = [make_file("a.py")]
        malicious_title = "__import__('os').system('echo pwned')"
        issues_payload = [{
            "path": "a.py", "line": 1, "severity": "LOW",
            "title": malicious_title, "description": "", "suggested_fix": "", "rule_id": None,
        }]
        mock_client.models.generate_content.return_value = response_text(issues=issues_payload)

        report = reviewer.review(files, make_scan_report())

        # The string is stored verbatim as data — never executed.
        assert report.issues[0].title == malicious_title
