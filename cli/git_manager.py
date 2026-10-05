"""
Phase 1 Git helper.

This module does not push anywhere automatically yet.
Phase 2 will connect repository creation through n8n.
"""

import subprocess
from pathlib import Path


def run_git(project_path: Path, args):
    result = subprocess.run(
        ["git", *args],
        cwd=project_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "success": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def git_available():
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result.returncode == 0
    except OSError:
        return False
