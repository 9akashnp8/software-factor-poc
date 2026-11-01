import json
import os
import threading
from pathlib import Path

import anyio
import markdown
import nh3
from dotenv import load_dotenv
from flask import Flask, Response, render_template, redirect, url_for, request
from flask_wtf.csrf import CSRFProtect
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

from software_factory.main import implement_and_review

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
CSRFProtect(app)
BUG_WORKSPACE_DIR = Path("./bug_workspace")


def _load_users(spec: str) -> dict[str, str]:
    """Parse SF_USERS, "name:hash,name:hash", where each hash comes from generate_password_hash."""
    users = {}
    for entry in filter(None, (e.strip() for e in spec.split(","))):
        name, _, password_hash = entry.partition(":")
        users[name] = password_hash
    return users


USERS = _load_users(os.getenv("SF_USERS", ""))
# Checked for unknown users too, so a wrong name takes as long as a wrong password
_DUMMY_HASH = generate_password_hash("unused")

_running: set[str] = set()
_lock = threading.Lock()

def _worker(issue_id, is_plan_approved, remarks):
    try:
        anyio.run(implement_and_review, issue_id, is_plan_approved, remarks)
    finally:
        with _lock:
            _running.discard(issue_id)


def render_md(text: str) -> Markup:
    # issue.md holds QA text and users' chat messages, and Markdown passes raw HTML
    # through, so strip anything that could run script before marking it safe
    return Markup(nh3.clean(markdown.markdown(text, extensions=["extra"])))


@app.before_request
def require_login():
    # Fail closed: approving starts an agent that commits and pushes code
    if not USERS or not app.config["SECRET_KEY"]:
        return Response("Set SF_USERS and FLASK_SECRET_KEY in .env", 503)
    auth = request.authorization
    if auth and auth.type == "basic":
        password_hash = USERS.get(auth.username or "", _DUMMY_HASH)
        if check_password_hash(password_hash, auth.password or "") and auth.username in USERS:
            return None
    return Response(
        "Login required", 401, {"WWW-Authenticate": 'Basic realm="Software Factory"'}
    )


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

    with open(generated_plan, "r", encoding="utf-8") as f:
        html_plan = render_md(f.read())

    with open(metadata_file, "r", encoding="utf-8") as f:
        metadata = json.loads(f.read())
        issue_identifier = metadata["identifier"]
        status = metadata["status"]


    return render_template("issue_detail.html", html_content=html_plan, issue_id=issue_id, identifier=issue_identifier, issue_status=status, metadata=metadata)


@app.route("/issue/<issue_id>/issue_context")
def issue_context(issue_id):
    issue_dir = Path(BUG_WORKSPACE_DIR / issue_id)
    gathered_issue_ctx = Path(issue_dir / "issue.md")

    with open(gathered_issue_ctx, "r", encoding="utf-8") as f:
        html_issue_ctx = render_md(f.read())

    return render_template("issue_ctx_detail.html", html_content=html_issue_ctx, issue_id=issue_id)

@app.route("/issue/<issue_id>/approve", methods=["POST"])
def approve_plan(issue_id: str):
    issue_dir = Path(BUG_WORKSPACE_DIR / issue_id)
    metadata_file = Path(issue_dir / "metadata.json")

    with open(metadata_file, "r", encoding="utf-8") as f:
        metadata = json.loads(f.read())
        print(metadata)
        metadata["status"] = "plan-approved-agent-working"
        metadata["plan_reviewed_by"] = request.authorization.username
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
        metadata["plan_reviewed_by"] = request.authorization.username
        session_id = metadata["session_id"]

    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=4)

    with _lock:
        if issue_id not in _running:
            _running.add(issue_id)
            threading.Thread(target=_worker, args=(issue_id, False, feedback), daemon=True).start()

    return redirect(url_for("issue_detail", issue_id=issue_id))
