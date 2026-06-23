"""
tests/test_semgrep_runner.py
-----------------------------
Full test suite for semgrep_runner.py.
subprocess.run is mocked throughout — no real semgrep binary required.

Run with:
    pytest tests/test_semgrep_runner.py -v
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from semgrep_runner import (
    ScanReport,
    SemgrepExecutionError,
    SemgrepNotInstalledError,
    SemgrepRunner,
    SemgrepTimeoutError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_file(path: str, content: str = "x = 1\n") -> SimpleNamespace:
    """Stand-in for github_fetcher.FileResult — only .path/.content are used."""
    return SimpleNamespace(path=path, content=content)


def fake_completed_process(returncode: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def semgrep_json(results=None, errors=None) -> str:
    return json.dumps({"results": results or [], "errors": errors or []})


def make_runner(**kwargs) -> SemgrepRunner:
    """Construct a SemgrepRunner with the binary check patched to succeed."""
    with patch("semgrep_runner.shutil.which", return_value="/usr/bin/semgrep"):
        return SemgrepRunner(**kwargs)


# ---------------------------------------------------------------------------
# 1. Construction / validation
# ---------------------------------------------------------------------------

class TestConstruction:

    def test_missing_semgrep_binary_raises(self):
        with patch("semgrep_runner.shutil.which", return_value=None):
            with pytest.raises(SemgrepNotInstalledError):
                SemgrepRunner()

    def test_rejects_invalid_config_string(self):
        with pytest.raises(ValueError):
            make_runner(config="auto; rm -rf /")

    def test_valid_config_accepted(self):
        runner = make_runner(config="auto")
        assert runner is not None


# ---------------------------------------------------------------------------
# 2. Input validation on scan()
# ---------------------------------------------------------------------------

class TestScanInputValidation:

    def test_empty_files_raises(self):
        runner = make_runner()
        with pytest.raises(ValueError, match="No files to scan"):
            runner.scan([])


# ---------------------------------------------------------------------------
# 3. Path safety
# ---------------------------------------------------------------------------

class TestPathSafety:

    def test_rejects_path_traversal(self):
        runner = make_runner()
        files = [
            make_file("../../etc/passwd", "evil"),
            make_file("good.py", "x = 1\n"),
        ]
        with patch(
            "semgrep_runner.subprocess.run",
            return_value=fake_completed_process(0, semgrep_json()),
        ):
            report = runner.scan(files)

        assert "../../etc/passwd" in report.skipped
        assert report.scanned == 1

    def test_rejects_absolute_path(self):
        # All files unsafe -> path_map empty -> scan() should still return
        # a report (no exception raised), with nothing actually scanned.
        runner = make_runner()
        files = [make_file("/etc/passwd", "evil")]
        with patch(
            "semgrep_runner.subprocess.run",
            return_value=fake_completed_process(0, semgrep_json()),
        ) as mock_run:
            report = runner.scan(files)
        assert report.scanned == 0
        assert "/etc/passwd" in report.skipped
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Output parsing
# ---------------------------------------------------------------------------

class TestOutputParsing:

    def test_clean_scan_no_findings(self):
        runner = make_runner()
        files = [make_file("clean.py", "x = 1\n")]
        with patch(
            "semgrep_runner.subprocess.run",
            return_value=fake_completed_process(0, semgrep_json()),
        ):
            report = runner.scan(files)
        assert report.findings == []
        assert report.scanned == 1

    def test_parses_findings_correctly(self):
        runner = make_runner()
        files = [make_file("bad.py", "eval(x)\n")]
        results = [
            {
                "path": "bad.py",
                "check_id": "python.lang.security.eval-detected",
                "start": {"line": 1},
                "end": {"line": 1},
                "extra": {
                    "severity": "ERROR",
                    "message": "Detected use of eval()",
                    "lines": "eval(x)",
                },
            },
            {
                "path": "bad.py",
                "check_id": "python.lang.security.exec-detected",
                "start": {"line": 2},
                "end": {"line": 2},
                "extra": {
                    "severity": "WARNING",
                    "message": "Detected use of exec()",
                    "lines": "exec(y)",
                },
            },
        ]
        with patch(
            "semgrep_runner.subprocess.run",
            return_value=fake_completed_process(1, semgrep_json(results=results)),
        ):
            report = runner.scan(files)

        assert len(report.findings) == 2
        f0 = report.findings[0]
        assert f0.path == "bad.py"
        assert f0.rule_id == "python.lang.security.eval-detected"
        assert f0.line_start == 1
        assert f0.message == "Detected use of eval()"

    def test_severity_normalized_uppercase(self):
        runner = make_runner()
        files = [make_file("warn.py", "x = 1\n")]
        results = [{
            "path": "warn.py",
            "check_id": "some.rule",
            "start": {"line": 1},
            "end": {"line": 1},
            "extra": {"severity": "warning", "message": "msg", "lines": "x = 1"},
        }]
        with patch(
            "semgrep_runner.subprocess.run",
            return_value=fake_completed_process(1, semgrep_json(results=results)),
        ):
            report = runner.scan(files)
        assert report.findings[0].severity == "WARNING"

    def test_resolves_absolute_tmp_path_to_original_relative_path(self):
        # Real semgrep echoes back the absolute path we passed it (the temp
        # sandbox dir), not the relative path we used internally. Regression
        # test for a bug where findings reported the raw sandbox path instead
        # of the caller's original file path.
        runner = make_runner()
        files = [make_file("src/bad.py", "eval(x)\n")]
        captured_tmp_dir = {}

        def fake_run(cmd, **kwargs):
            tmp_dir = cmd[-1]
            captured_tmp_dir["path"] = tmp_dir
            abs_path = f"{tmp_dir}/src/bad.py"
            results = [{
                "path": abs_path,
                "check_id": "rule.x",
                "start": {"line": 1},
                "end": {"line": 1},
                "extra": {"severity": "ERROR", "message": "m", "lines": "eval(x)"},
            }]
            return fake_completed_process(1, semgrep_json(results=results))

        with patch("semgrep_runner.subprocess.run", side_effect=fake_run):
            report = runner.scan(files)

        assert len(report.findings) == 1
        assert report.findings[0].path == "src/bad.py"
        assert captured_tmp_dir["path"] not in report.findings[0].path

    def test_skipped_files_from_errors_key(self):
        runner = make_runner()
        files = [make_file("broken.py", "def f(:\n")]
        errors = [{"path": "broken.py", "message": "parse error"}]
        with patch(
            "semgrep_runner.subprocess.run",
            return_value=fake_completed_process(0, semgrep_json(errors=errors)),
        ):
            report = runner.scan(files)
        assert "broken.py" in report.skipped
        assert report.findings == []


# ---------------------------------------------------------------------------
# 5. Exit code handling
# ---------------------------------------------------------------------------

class TestExitCodeHandling:

    def test_nonzero_exit_code_1_is_findings_not_error(self):
        runner = make_runner()
        files = [make_file("bad.py", "eval(x)\n")]
        results = [{
            "path": "bad.py",
            "check_id": "rule.x",
            "start": {"line": 1},
            "end": {"line": 1},
            "extra": {"severity": "ERROR", "message": "m", "lines": "eval(x)"},
        }]
        with patch(
            "semgrep_runner.subprocess.run",
            return_value=fake_completed_process(1, semgrep_json(results=results)),
        ):
            report = runner.scan(files)  # should not raise
        assert len(report.findings) == 1

    def test_nonzero_exit_code_other_raises(self):
        runner = make_runner()
        files = [make_file("x.py", "x = 1\n")]
        with patch(
            "semgrep_runner.subprocess.run",
            return_value=fake_completed_process(2, "", "boom"),
        ):
            with pytest.raises(SemgrepExecutionError) as exc_info:
                runner.scan(files)
        assert exc_info.value.returncode == 2

    def test_timeout_raises(self):
        runner = make_runner(timeout=5)
        files = [make_file("x.py", "x = 1\n")]
        with patch(
            "semgrep_runner.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="semgrep", timeout=5),
        ):
            with pytest.raises(SemgrepTimeoutError):
                runner.scan(files)

    def test_stderr_truncated_in_exception(self):
        runner = make_runner()
        files = [make_file("x.py", "x = 1\n")]
        huge_stderr = "E" * 5000
        with patch(
            "semgrep_runner.subprocess.run",
            return_value=fake_completed_process(2, "", huge_stderr),
        ):
            with pytest.raises(SemgrepExecutionError) as exc_info:
                runner.scan(files)
        # message embeds the truncated (2000-char) stderr plus a short prefix
        assert len(exc_info.value.message) < 2100
        assert "EEEE" in exc_info.value.message


# ---------------------------------------------------------------------------
# 6. Process safety / cleanup
# ---------------------------------------------------------------------------

class TestProcessSafety:

    def test_version_check_disabled_via_env(self):
        runner = make_runner()
        files = [make_file("x.py", "x = 1\n")]
        with patch(
            "semgrep_runner.subprocess.run",
            return_value=fake_completed_process(0, semgrep_json()),
        ) as mock_run:
            runner.scan(files)
        _, kwargs = mock_run.call_args
        assert kwargs["env"]["SEMGREP_ENABLE_VERSION_CHECK"] == "0"

    def test_symlinked_tmp_dir_does_not_crash(self, tmp_path):
        # Regression test: on macOS, TMPDIR / /tmp is a symlink to a
        # /private/... path. _safe_join() resolves symlinks but the old
        # code compared the resolved candidate against the *unresolved*
        # tmp_path, raising ValueError from relative_to(). Simulate that
        # by handing scan() a real symlinked temp directory and a path
        # containing a space (as seen in the real failure).
        real_dir = tmp_path / "real_target"
        real_dir.mkdir()
        symlink_dir = tmp_path / "symlinked_tmp"
        symlink_dir.symlink_to(real_dir, target_is_directory=True)

        runner = make_runner()
        files = [make_file("CLI projects/app.py", "x = 1\n")]

        class FakeTempDir:
            def __enter__(self):
                return str(symlink_dir)

            def __exit__(self, *exc):
                return False

        with patch("semgrep_runner.tempfile.TemporaryDirectory", return_value=FakeTempDir()):
            with patch(
                "semgrep_runner.subprocess.run",
                return_value=fake_completed_process(0, semgrep_json()),
            ):
                report = runner.scan(files)  # must not raise ValueError

        assert report.scanned == 1
        assert report.skipped == []

    def test_no_shell_true_used(self):
        runner = make_runner()
        files = [make_file("x.py", "x = 1\n")]
        with patch(
            "semgrep_runner.subprocess.run",
            return_value=fake_completed_process(0, semgrep_json()),
        ) as mock_run:
            runner.scan(files)
        _, kwargs = mock_run.call_args
        assert kwargs.get("shell", False) is False

    def test_temp_dir_cleaned_up(self):
        runner = make_runner()
        files = [make_file("x.py", "x = 1\n")]
        captured_dir = {}

        def fake_run(cmd, **kwargs):
            # last positional arg of the semgrep command is the temp dir
            captured_dir["path"] = cmd[-1]
            return fake_completed_process(0, semgrep_json())

        with patch("semgrep_runner.subprocess.run", side_effect=fake_run):
            runner.scan(files)

        from pathlib import Path
        assert "path" in captured_dir
        assert not Path(captured_dir["path"]).exists()


# ---------------------------------------------------------------------------
# 7. Scan report shape
# ---------------------------------------------------------------------------

class TestScanReportShape:

    def test_scan_report_has_duration(self):
        runner = make_runner()
        files = [make_file("x.py", "x = 1\n")]
        with patch(
            "semgrep_runner.subprocess.run",
            return_value=fake_completed_process(0, semgrep_json()),
        ):
            report = runner.scan(files)
        assert isinstance(report, ScanReport)
        assert report.duration_s >= 0
