from __future__ import annotations

import json
from pathlib import Path


IGNORE_DIRS = {
    ".git",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".output",
    ".venv",
    "venv",
    "__pycache__",
}


SUPPORTED_FRAMEWORKS = {
    "static-html",
    "flask",
    "react-vite",
    "nextjs",
    "vue-vite",
}


def load_package_json(project_path: Path):
    package_file = project_path / "package.json"

    if not package_file.exists():
        return None, "package.json not found."

    try:
        # utf-8-sig transparently strips a byte-order mark. Editors on
        # Windows (and some generators) write package.json with a BOM,
        # which would otherwise make json.loads fail and silently
        # downgrade a React/Vite app to "static-html".
        raw = package_file.read_text(encoding="utf-8-sig")
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"Invalid package.json: {exc}"
    except UnicodeDecodeError:
        return None, "package.json is not valid UTF-8 text."


def detect_package_manager(project_path: Path):
    if (project_path / "pnpm-lock.yaml").exists():
        return "pnpm"

    if (project_path / "yarn.lock").exists():
        return "yarn"

    if (project_path / "bun.lockb").exists() or (
        project_path / "bun.lock"
    ).exists():
        return "bun"

    if (project_path / "package.json").exists():
        return "npm"

    if (
        (project_path / "requirements.txt").exists()
        or (project_path / "pyproject.toml").exists()
        or (project_path / "Pipfile").exists()
    ):
        return "pip"

    return None


def detect_python_framework(project_path: Path):
    """
    Detect Python web applications.

    Flask detection:
    - app.py
    - flask_app.py
    - application.py
    - requirements.txt containing Flask/flask
    """

    python_files = {
        file.name.lower()
        for file in project_path.glob("*.py")
        if file.is_file()
    }

    requirements = project_path / "requirements.txt"

    if requirements.exists():
        try:
            content = requirements.read_text(
                encoding="utf-8",
                errors="ignore"
            ).lower()

            if "flask" in content:
                return "flask"

            if "fastapi" in content:
                return "fastapi"

        except OSError:
            pass

    if {
        "app.py",
        "flask_app.py",
        "application.py",
    } & python_files:
        return "flask"

    if "main.py" in python_files:
        return "python"

    if python_files:
        return "python"

    return None


def detect_python_entry_file(project_path: Path, framework: str | None):
    if framework == "flask":
        for filename in [
            "app.py",
            "flask_app.py",
            "application.py",
        ]:
            if (project_path / filename).exists():
                return filename

    if framework in {"fastapi", "python"}:
        if (project_path / "main.py").exists():
            return "main.py"

    return None


def detect_static_html(project_path: Path):
    """
    Detect a plain static HTML project.

    A project is considered static HTML when it contains
    an index.html and does not have package.json or a
    Python application entry point.
    """

    index_file = project_path / "index.html"

    if index_file.exists() and index_file.is_file():
        return True

    return False


def detect_framework(package_json, project_path: Path):
    """
    Detect the project framework/type.
    """

    # JavaScript project
    if package_json:
        dependencies = {}

        dependencies.update(
            package_json.get("dependencies", {})
        )

        dependencies.update(
            package_json.get("devDependencies", {})
        )

        if "next" in dependencies:
            return "nextjs"

        if "vite" in dependencies and "react" in dependencies:
            return "react-vite"

        if "vite" in dependencies and "vue" in dependencies:
            return "vue-vite"

        if "react" in dependencies:
            return "react"

        if "vue" in dependencies:
            return "vue"

        return "javascript"

    # Python project
    python_framework = detect_python_framework(project_path)

    if python_framework:
        return python_framework

    # Static HTML project
    if detect_static_html(project_path):
        return "static-html"

    return "unknown"


def get_build_command(package_json, package_manager, framework):
    """
    Return the production build command.

    Static HTML and Flask projects do not require
    an npm production build.
    """

    if framework in {"static-html", "flask", "python", "fastapi"}:
        return None

    scripts = (package_json or {}).get("scripts", {})

    if "build" not in scripts:
        return None

    return {
        "npm": "npm run build",
        "pnpm": "pnpm run build",
        "yarn": "yarn build",
        "bun": "bun run build",
    }.get(package_manager, "npm run build")


def get_output_directory(framework):
    if framework in {"react-vite", "vue-vite"}:
        return "dist"

    if framework == "nextjs":
        return ".next"

    if framework in {
        "static-html",
        "flask",
        "python",
        "fastapi",
    }:
        return "."

    return "unknown"


def find_env_files(project_path: Path):
    names = []

    for name in [
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
    ]:
        if (project_path / name).exists():
            names.append(name)

    return names


def find_env_variable_names(project_path: Path):
    """
    Reads only environment variable names.

    IMPORTANT:
    Environment variable VALUES are never returned.
    """

    variables = []

    for env_file in [
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
    ]:
        path = project_path / env_file

        if not path.exists():
            continue

        try:
            lines = path.read_text(
                encoding="utf-8"
            ).splitlines()

        except UnicodeDecodeError:
            continue

        for line in lines:
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            if "=" in line:
                key = line.split("=", 1)[0].strip()

                if key and key not in variables:
                    variables.append(key)

    return variables


def scan_project(project_path):
    project_path = Path(project_path).resolve()

    package_json, package_error = load_package_json(
        project_path
    )

    package_manager = detect_package_manager(
        project_path
    )

    framework = detect_framework(
        package_json,
        project_path
    )

    build_command = get_build_command(
        package_json,
        package_manager,
        framework
    )

    output_directory = get_output_directory(
        framework
    )

    python_entry_file = detect_python_entry_file(
        project_path,
        framework
    )

    result = {
        "project_path": str(project_path),
        "project_name": project_path.name,

        "project_type": (
            "javascript"
            if package_json
            else (
                "python"
                if framework in {
                    "flask",
                    "fastapi",
                    "python",
                }
                else (
                    "static"
                    if framework == "static-html"
                    else "unknown"
                )
            )
        ),

        "package_json_found": package_json is not None,
        "package_manager": package_manager,

        "framework": framework,

        "entry_file": python_entry_file,

        "build_command": build_command,
        "output_directory": output_directory,

        "package_error": (
            None
            if package_json
            or framework in {
                "static-html",
                "flask",
                "fastapi",
                "python",
            }
            else package_error
        ),

        "env_files": find_env_files(project_path),

        "env_variable_names": find_env_variable_names(
            project_path
        ),

        "gitignore_found": (
            project_path / ".gitignore"
        ).exists(),

        "supported": framework in SUPPORTED_FRAMEWORKS,

        "has_build_script": False,
        "scripts": {},
    }

    if package_json:
        scripts = package_json.get(
            "scripts",
            {}
        )

        result["has_build_script"] = (
            "build" in scripts
        )

        result["scripts"] = scripts

    return result