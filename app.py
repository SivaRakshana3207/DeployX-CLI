from flask import Flask, render_template, request, jsonify
from database.db import init_db
from services.deployment_service import create_deployment, get_deployment, list_deployments

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

init_db()


@app.route("/")
def index():
    deployments = list_deployments(limit=10)
    return render_template("index.html", deployments=deployments)


@app.route("/dashboard")
def dashboard():
    deployments = list_deployments(limit=50)
    return render_template("dashboard.html", deployments=deployments)


@app.route("/deployments")
def history():
    deployments = list_deployments(limit=100)
    return render_template("history.html", deployments=deployments)


@app.route("/deployment/<int:deployment_id>")
def deployment_page(deployment_id):
    deployment = get_deployment(deployment_id)
    if not deployment:
        return render_template("error.html", message="Deployment not found."), 404
    return render_template("deployment.html", deployment=deployment)


@app.post("/api/deployments")
def api_create_deployment():
    data = request.get_json(silent=True) or {}

    project_name = str(data.get("project_name", "")).strip()
    framework = str(data.get("framework", "unknown")).strip()
    build_command = str(data.get("build_command", "")).strip()
    output_directory = str(data.get("output_directory", "")).strip()

    if not project_name:
        return jsonify({"success": False, "error": "project_name is required"}), 400

    deployment = create_deployment(
        project_name=project_name,
        framework=framework,
        build_command=build_command,
        output_directory=output_directory,
    )

    return jsonify({"success": True, "deployment": deployment}), 201


@app.get("/api/deployments")
def api_list_deployments():
    return jsonify({"success": True, "deployments": list_deployments(limit=100)})


@app.get("/api/deployments/<int:deployment_id>")
def api_get_deployment(deployment_id):
    deployment = get_deployment(deployment_id)
    if not deployment:
        return jsonify({"success": False, "error": "Deployment not found"}), 404
    return jsonify({"success": True, "deployment": deployment})


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "DeployX Phase 1"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
