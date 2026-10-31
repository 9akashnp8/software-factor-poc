import json
import threading
from pathlib import Path

import anyio
import markdown
from flask import Flask, render_template, redirect, url_for, request
from markupsafe import Markup

from software_factory.main import implement_and_review

app = Flask(__name__)
BUG_WORKSPACE_DIR = Path("./bug_workspace")

_running: set[str] = set()
_lock = threading.Lock()

def _worker(issue_id, is_plan_approved, remarks):
    try:
        anyio.run(implement_and_review, issue_id, is_plan_approved, remarks)
    finally:
        with _lock:
            _running.discard(issue_id)

@app.route("/")
def issue_home():
    issues = []

    for issue_folder in BUG_WORKSPACE_DIR.iterdir():
        if not issue_folder.is_dir():
            continue

        issue_id = issue_folder.name

        metadata_file = Path(issue_folder / "metadata.json")
        with open(metadata_file, "r", encoding="utf-8") as f:
            metadata = json.loads(f.read())
            issue_identifier = metadata["identifier"]
            status = metadata["status"]

        issues.append({"issue_id": issue_id, "identifier": issue_identifier, "status": status})

    return render_template("issue_list.html", issues=issues)

@app.route("/issue/<issue_id>/plan")
def issue_detail(issue_id):
    issue_dir = Path(BUG_WORKSPACE_DIR / issue_id)
    generated_plan = Path(issue_dir / "plan.md")
    metadata_file = Path(issue_dir / "metadata.json")

    html_plan = ""
    with open(generated_plan, "r", encoding="utf-8") as f:
        raw_markdown = f.read()
    parsed_plan = markdown.markdown(raw_markdown, extensions=["extra"])
    html_plan = Markup(parsed_plan)

    with open(metadata_file, "r", encoding="utf-8") as f:
        metadata = json.loads(f.read())
        issue_identifier = metadata["identifier"]
        status = metadata["status"]


    return render_template("issue_detail.html", html_content=html_plan, issue_id=issue_id, identifier=issue_identifier, issue_status=status, metadata=metadata)


@app.route("/issue/<issue_id>/issue_context")
def issue_context(issue_id):
    issue_dir = Path(BUG_WORKSPACE_DIR / issue_id)
    gathered_issue_ctx = Path(issue_dir / "issue.md")

    html_issue_ctx = ""
    with open(gathered_issue_ctx, "r", encoding="utf-8") as f:
        raw_markdown = f.read()

    parsed = markdown.markdown(raw_markdown, extensions=["extra"])

    html_issue_ctx = Markup(parsed)

    return render_template("issue_ctx_detail.html", html_content=html_issue_ctx, issue_id=issue_id)

@app.route("/issue/<issue_id>/approve", methods=["POST"])
def approve_plan(issue_id: str):
    issue_dir = Path(BUG_WORKSPACE_DIR / issue_id)
    metadata_file = Path(issue_dir / "metadata.json")

    with open(metadata_file, "r", encoding="utf-8") as f:
        metadata = json.loads(f.read())
        print(metadata)
        metadata["status"] = "plan-approved-agent-working"
        session_id = metadata["session_id"]

    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=4)

    with _lock:
        if issue_id not in _running:
            _running.add(issue_id) 
            threading.Thread(target=_worker, args=(issue_id, True, ""), daemon=True).start()

    return redirect(url_for("issue_detail", issue_id=issue_id))

@app.route("/issue/<issue_id>/rework-plan", methods=["POST"])
def rework_plan(issue_id: str):
    issue_dir = Path(BUG_WORKSPACE_DIR / issue_id)
    metadata_file = Path(issue_dir / "metadata.json")
    feedback = request.form.get("feedback", "").strip()

    with open(metadata_file, "r", encoding="utf-8") as f:
        metadata = json.loads(f.read())
        print(metadata)
        metadata["status"] = "plan-rework-requested-agent-planning"
        session_id = metadata["session_id"]

    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=4)

    with _lock:
        if issue_id not in _running:
            _running.add(issue_id) 
            threading.Thread(target=_worker, args=(issue_id, False, feedback), daemon=True).start()

    return redirect(url_for("issue_detail", issue_id=issue_id))