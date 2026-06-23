#!/usr/bin/env python3
"""
main.py
-------
CLI entry point for the AI Code Review Agent.

Usage:
    python main.py https://github.com/owner/repo [--branch main] [--out review_report.md]

Reads GITHUB_TOKEN and GEMINI_API_KEY from the environment (and from a
local .env file, if present, via python-dotenv). Neither value is ever
printed or logged.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from agent import CodeReviewAgent
from report_generator import write_report


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Review a GitHub repository's Python code with static analysis + Gemini."
    )
    parser.add_argument("repo_url", help="GitHub repository URL, e.g. https://github.com/owner/repo")
    parser.add_argument("--branch", default="main", help="Branch to review (default: main)")
    parser.add_argument(
        "--max-files", type=int, default=10,
        help=(
            "Max Python files to review (default: 10). Kept low by default "
            "because Gemini's free tier caps requests per day; raise this "
            "with --max-files if you have a higher quota or paid billing."
        ),
    )
    parser.add_argument("--out", default="review_report.md", help="Output Markdown report path")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable INFO-level logging")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    load_dotenv(override=True)  # loads .env into os.environ, overriding existing env vars

    github_token = os.environ.get("GITHUB_TOKEN", "")
    gemini_api_key = os.environ.get("GEMINI_API_KEY", "")

    if not github_token:
        print("Error: GITHUB_TOKEN is not set (check your environment or .env file).", file=sys.stderr)
        return 1
    if not gemini_api_key:
        print("Error: GEMINI_API_KEY is not set (check your environment or .env file).", file=sys.stderr)
        return 1

    try:
        agent = CodeReviewAgent(github_token=github_token, gemini_api_key=gemini_api_key)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        result = agent.review_repo(args.repo_url, branch=args.branch, max_files=args.max_files)
    except Exception as exc:
        # Fetch-stage failures propagate unchanged from github_fetcher (e.g.
        # RepoNotFoundError, AuthenticationError, ValueError on a bad URL).
        print(f"Review failed: {exc}", file=sys.stderr)
        return 1

    path = write_report(result, args.out)

    print(f"Files fetched: {len(result.fetch_result.files)}")
    print(f"Semgrep findings: {len(result.scan_report.findings)}")
    print(f"Review issues: {len(result.review_report.issues)}")
    if result.stage_errors:
        print(f"Stage errors: {[e.stage for e in result.stage_errors]}")
    print(f"Report written to: {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
