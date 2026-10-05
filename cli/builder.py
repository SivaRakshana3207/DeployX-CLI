from __future__ import annotations

import subprocess
from pathlib import Path


def run_build(project_path: Path, command: str):
    if not command:
        return {
            "success": False,
            "returncode": None,
            "output": "No build script was found in package.json.",
        }

    try:
        completed = subprocess.run(
            command,
            cwd=project_path,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        combined = (completed.stdout or "") + "\n" + (completed.stderr or "")

        return {
            "success": completed.returncode == 0,
            "returncode": completed.returncode,
            "output": combined[-12000:],
        }

    except Exception as exc:
        return {
            "success": False,
            "returncode": None,
            "output": str(exc),
        }
