from database.db import execute, fetch_all, fetch_one


def create_deployment(project_name, framework, build_command="", output_directory=""):
    deployment_id = execute(
        """
        INSERT INTO deployments
        (project_name, framework, build_command, output_directory, status)
        VALUES (?, ?, ?, ?, 'QUEUED')
        """,
        (project_name, framework, build_command, output_directory),
    )
    return get_deployment(deployment_id)


def get_deployment(deployment_id):
    return fetch_one(
        "SELECT * FROM deployments WHERE id = ?",
        (deployment_id,),
    )


def list_deployments(limit=100):
    return fetch_all(
        "SELECT * FROM deployments ORDER BY id DESC LIMIT ?",
        (limit,),
    )


def update_deployment(deployment_id, **fields):
    allowed = {
        "status",
        "github_url",
        "vercel_url",
        "error",
        "build_command",
        "output_directory",
        "framework",
    }

    clean = {key: value for key, value in fields.items() if key in allowed}

    if not clean:
        return get_deployment(deployment_id)

    assignments = ", ".join(f"{key} = ?" for key in clean)
    values = list(clean.values()) + [deployment_id]

    execute(
        f"""
        UPDATE deployments
        SET {assignments}, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        values,
    )

    return get_deployment(deployment_id)
