from dataclasses import dataclass
from typing import Optional


@dataclass
class Deployment:
    id: Optional[int] = None
    project_name: str = ""
    framework: str = "unknown"
    build_command: str = ""
    output_directory: str = ""
    status: str = "QUEUED"
    github_url: str = ""
    vercel_url: str = ""
    error: str = ""
