"""
semgrep_runner.py
------------------
Runs Semgrep static analysis over FileResult objects produced by github_fetcher,
and returns structured Finding/ScanReport data ready for the Gemini review step.

Usage:
    from github_fetcher import GitHubFetcher
    from semgrep_runner import SemgrepRunner

    runner = SemgrepRunner(config="auto")
    report = runner.scan(files)  # files: list[FileResult]
    for finding in report.findings:
        print(finding.path, finding.rule_id, finding.severity)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SemgrepRunnerError(Exception):
    """Base error for all semgrep_runner failures."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class SemgrepNotInstalledError(SemgrepRunnerError):
    """Raised when the semgrep binary cannot be found on PATH."""


class SemgrepTimeoutError(SemgrepRunnerError):
    """Raised when the semgrep subprocess exceeds the configured timeout."""


class SemgrepExecutionError(SemgrepRunnerError):
    """Raised when semgrep exits with an unexpected non-zero status."""
    def __init__(self, message: str, returncode: int):
        super().__init__(message)
        self.returncode = returncode


class UnsafeFilePathError(SemgrepRunnerError):
    """Raised when a FileResult.path looks like a path-traversal attempt."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    path: str
    line_start: int
    line_end: int
    rule_id: str
    severity: str
    message: str
    snippet: str


@dataclass
class ScanReport:
    findings: list[Finding] = field(default_factory=list)
    scanned: int = 0
    skipped: list[str] = field(default_factory=list)
    duration_s: float = 0.0


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = "auto"
DEFAULT_TIMEOUT = 60
MAX_STDERR_CHARS = 2000
MAX_SNIPPET_CHARS = 500

# Allow-list for the --config argument: registry ids, local paths, "auto".
_CONFIG_PATTERN = re.compile(r"^[a-zA-Z0-9_\-./:]+$")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class SemgrepRunner:
    """
    Runs Semgrep against a set of in-memory source files inside an isolated
    temporary directory, and parses the results into typed Finding objects.

    Parameters
    ----------
    config : str
        Semgrep ruleset: "auto", a registry id, or a local ruleset path.
    timeout : int
        Max seconds to allow the semgrep subprocess to run.
    """

    def __init__(self, config: str = DEFAULT_CONFIG, timeout: int = DEFAULT_TIMEOUT) -> None:
        if shutil.which("semgrep") is None:
            raise SemgrepNotInstalledError(
                "semgrep binary not found on PATH. Install it with `pip install semgrep`."
            )
        if not _CONFIG_PATTERN.match(config):
            raise ValueError(
                f"Invalid semgrep config string: {config!r}. "
                "Only alphanumerics, '_', '-', '.', '/', ':' are allowed."
            )
        self._config = config
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self, files: list) -> ScanReport:
        """
        Scan the given FileResult-like objects (must have .path and .content).
        Returns a ScanReport. Raises on infrastructure failures; per-file
        problems (unsafe paths, parse errors) are recorded in the report
        instead of aborting the whole scan.
        """
        if not files:
            raise ValueError("No files to scan")

        start = time.monotonic()
        skipped: list[str] = []

        with tempfile.TemporaryDirectory(prefix="semgrep_runner_") as tmpdir:
            # Resolve once, up front. On macOS (and some Linux setups) the
            # temp dir lives under a symlink (e.g. /tmp -> /private/tmp,
            # or TMPDIR under /var/folders -> /private/var/folders).
            # _safe_join() below calls .resolve() on every candidate path,
            # which follows that symlink; if tmp_path itself weren't also
            # resolved, relative_to(tmp_path) would raise ValueError because
            # the two sides disagree on the real (canonical) prefix.
            tmp_path = Path(tmpdir).resolve()
            path_map: dict[str, str] = {}  # tmp-relative path -> original path

            for f in files:
                try:
                    rel_path = self._safe_join(tmp_path, f.path)
                except UnsafeFilePathError as exc:
                    logger.warning("Skipping unsafe path %s: %s", f.path, exc.message)
                    skipped.append(f.path)
                    continue

                rel_path.parent.mkdir(parents=True, exist_ok=True)
                rel_path.write_text(f.content, encoding="utf-8")
                path_map[str(rel_path.relative_to(tmp_path))] = f.path

            if not path_map:
                duration = time.monotonic() - start
                return ScanReport(findings=[], scanned=0, skipped=skipped, duration_s=duration)

            raw_output = self._run_semgrep(tmp_path)
            findings, parse_skipped = self._parse_output(raw_output, path_map)
            skipped.extend(parse_skipped)

        duration = time.monotonic() - start
        logger.info(
            "Semgrep scan complete: %d files scanned, %d findings, %d skipped, %.2fs",
            len(path_map), len(findings), len(skipped), duration,
        )

        return ScanReport(
            findings=findings,
            scanned=len(path_map),
            skipped=skipped,
            duration_s=duration,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_join(base: Path, relative_path: str) -> Path:
        """
        Join `relative_path` onto `base`, rejecting any path that attempts
        to escape the sandbox directory (traversal, absolute paths, etc.).
        """
        if not relative_path or relative_path.startswith(("/", "\\")):
            raise UnsafeFilePathError(f"Absolute or empty path not allowed: {relative_path!r}")

        if ".." in Path(relative_path).parts:
            raise UnsafeFilePathError(f"Path traversal detected: {relative_path!r}")

        if "\\" in relative_path:
            raise UnsafeFilePathError(f"Backslashes not allowed in path: {relative_path!r}")

        candidate = (base / relative_path).resolve()
        base_resolved = base.resolve()
        if base_resolved not in candidate.parents and candidate != base_resolved:
            raise UnsafeFilePathError(f"Resolved path escapes sandbox: {relative_path!r}")

        return candidate

    def _run_semgrep(self, tmp_path: Path) -> str:
        """Invoke the semgrep CLI as a subprocess and return raw stdout."""
        cmd = [
            "semgrep", "scan",
            "--config", self._config,
            "--json",
            "--timeout", str(self._timeout),
            str(tmp_path),
        ]
        # Semgrep does a version-check phone-home on every run by default,
        # which hangs/retries in network-restricted environments. Disabling
        # it is safe and doesn't affect scan results. Note: "auto" config
        # still requires a network call to fetch rules from the registry;
        # metrics are intentionally left at their default (required for
        # --config auto) rather than forced off.
        env = os.environ.copy()
        env["SEMGREP_ENABLE_VERSION_CHECK"] = "0"
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise SemgrepTimeoutError(
                f"Semgrep scan exceeded timeout of {self._timeout}s"
            )

        # Exit code 0 = clean scan, 1 = findings present (not an error).
        if result.returncode not in (0, 1):
            stderr = (result.stderr or "")[:MAX_STDERR_CHARS]
            raise SemgrepExecutionError(
                f"Semgrep exited with code {result.returncode}: {stderr}",
                returncode=result.returncode,
            )

        return result.stdout

    def _parse_output(
        self, raw_output: str, path_map: dict[str, str]
    ) -> tuple[list[Finding], list[str]]:
        """Parse semgrep's --json stdout into Finding objects."""
        try:
            data = json.loads(raw_output) if raw_output else {}
        except json.JSONDecodeError:
            logger.warning("Could not parse semgrep JSON output.")
            return [], []

        findings: list[Finding] = []
        for item in data.get("results", []):
            tmp_rel_path = item.get("path", "")
            original_path = self._resolve_original_path(tmp_rel_path, path_map)

            start = item.get("start", {}) or {}
            end = item.get("end", {}) or {}
            extra = item.get("extra", {}) or {}

            findings.append(Finding(
                path=original_path,
                line_start=start.get("line", 0),
                line_end=end.get("line", 0),
                rule_id=item.get("check_id", ""),
                severity=str(extra.get("severity", "")).upper(),
                message=extra.get("message", ""),
                snippet=str(extra.get("lines", ""))[:MAX_SNIPPET_CHARS],
            ))

        skipped: list[str] = []
        for err in data.get("errors", []):
            err_path = err.get("path", "")
            original_path = self._resolve_original_path(err_path, path_map)
            skipped.append(original_path or err_path)

        return findings, skipped

    @staticmethod
    def _resolve_original_path(tmp_rel_path: str, path_map: dict[str, str]) -> str:
        """Map a semgrep-reported path back to the caller's original path."""
        if tmp_rel_path in path_map:
            return path_map[tmp_rel_path]
        # Semgrep may report paths with a leading "./" or as absolute paths
        # within the temp dir (it echoes back whatever target path we passed
        # it, which is the absolute tmp_path). Normalize and check whether
        # the *reported* path ends with a known relative path-map key,
        # since the reported path is the longer/absolute side here.
        normalized = tmp_rel_path.lstrip("./")
        normalized_parts = Path(normalized).as_posix()
        for key, original in path_map.items():
            if key == normalized or normalized_parts.endswith("/" + key) or normalized_parts == key:
                return original
        return tmp_rel_path
