import os
from datetime import datetime

import markdown
import nh3
from dotenv import load_dotenv
from flask import Flask, Response, abort, flash, render_template, redirect, url_for, request
from flask_wtf.csrf import CSRFProtect
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

from software_factory import store
from software_factory.overlap import overlaps

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
CSRFProtect(app)


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

@app.context_processor
def worker_pause():
    # The worker holds off while a rate limit is in force; every page says until when
    until = store.paused_until()
    return {"paused_until": datetime.fromtimestamp(until).strftime("%H:%M") if until else None}


def render_md(text: str) -> Markup:
    # issue.md holds QA text and users' chat messages, and Markdown passes raw HTML
    # through, so strip anything that could run script before marking it safe
    return Markup(nh3.clean(markdown.markdown(text, extensions=["extra"])))


def _read(issue_id: str, name: str) -> str | None:
    path = store.issue_dir(issue_id) / name
    return path.read_text(encoding="utf-8") if path.exists() else None


def _issue_or_404(issue_id: str):
    issue = store.get_issue(issue_id)
    if issue is None:
        abort(404)
    return issue


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
    return render_template("issue_list.html", issues=store.list_issues())

@app.route("/issue/<issue_id>/plan")
def issue_detail(issue_id):
    issue = _issue_or_404(issue_id)
    plan = _read(issue_id, "plan.md")
    actions = store.allowed_actions(issue)

    return render_template(
        "issue_detail.html",
        issue=issue,
        html_content=render_md(plan) if plan else None,
        actions=actions,
        # Only worth the git calls while the plan can still be approved
        overlaps=overlaps(issue) if "approve" in actions else [],
        runs=store.runs(issue_id),
        events=store.events(issue_id),
    )


@app.route("/issue/<issue_id>/issue_context")
def issue_context(issue_id):
    issue = _issue_or_404(issue_id)
    issue_ctx = _read(issue_id, "issue.md")

    return render_template(
        "issue_ctx_detail.html",
        issue=issue,
        html_content=render_md(issue_ctx) if issue_ctx else None,
    )


def _queue(issue_id: str, action: str, **fields):
    """Put the issue on the worker's queue for action; the worker runs it."""
    issue = _issue_or_404(issue_id)
    # Fields first, so the worker never claims the issue without them
    if action in store.allowed_actions(issue) and fields:
        store.set_fields(issue_id, **fields)
    # The status change claims the issue, so a second click can't queue it twice
    if action not in store.allowed_actions(issue) or not store.transition(
        issue_id, store.ACTION_QUEUES[action], expected=store.ACTIONS[action], actor=request.authorization.username
    ):
        flash(f"Can't {action} while the issue is {issue['status']}.")
    return redirect(url_for("issue_detail", issue_id=issue_id))


@app.route("/issue/<issue_id>/approve", methods=["POST"])
def approve_plan(issue_id: str):
    return _queue(issue_id, "approve", plan_reviewed_by=request.authorization.username)

@app.route("/issue/<issue_id>/rework-plan", methods=["POST"])
def rework_plan(issue_id: str):
    feedback = request.form.get("feedback", "").strip()
    return _queue(issue_id, "rework", plan_reviewed_by=request.authorization.username, pending_feedback=feedback)

@app.route("/issue/<issue_id>/retry", methods=["POST"])
def retry_triage(issue_id: str):
    return _queue(issue_id, "retry")

@app.route("/issue/<issue_id>/open-pr", methods=["POST"])
def open_pr(issue_id: str):
    return _queue(issue_id, "publish")

@app.route("/issue/<issue_id>/release", methods=["POST"])
def release_issue(issue_id: str):
    return _queue(issue_id, "release")
