import os
import threading

import anyio
import markdown
import nh3
from dotenv import load_dotenv
from flask import Flask, Response, abort, flash, render_template, redirect, url_for, request
from flask_wtf.csrf import CSRFProtect
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

from software_factory import store
from software_factory.main import implement_and_review

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

# Runs this process started and never finished can't still be going after a restart
store.reap_interrupted("web")


def _worker(issue_id, is_plan_approved, remarks):
    anyio.run(implement_and_review, issue_id, is_plan_approved, remarks, "web")


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

    return render_template(
        "issue_detail.html",
        issue=issue,
        html_content=render_md(plan) if plan else None,
        actions=store.allowed_actions(issue),
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


def _start(issue_id: str, action: str, to_status: str, is_plan_approved: bool, remarks: str):
    issue = _issue_or_404(issue_id)
    # The status change claims the issue, so a second click or a run in progress can't start another agent
    if action not in store.allowed_actions(issue) or not store.transition(
        issue_id, to_status, expected=store.ACTIONS[action], actor=request.authorization.username
    ):
        flash(f"Can't {action} while the issue is {issue['status']}.")
        return redirect(url_for("issue_detail", issue_id=issue_id))

    store.set_fields(issue_id, plan_reviewed_by=request.authorization.username)
    threading.Thread(target=_worker, args=(issue_id, is_plan_approved, remarks), daemon=True).start()
    return redirect(url_for("issue_detail", issue_id=issue_id))


@app.route("/issue/<issue_id>/approve", methods=["POST"])
def approve_plan(issue_id: str):
    return _start(issue_id, "approve", store.IMPLEMENTING, True, "")

@app.route("/issue/<issue_id>/rework-plan", methods=["POST"])
def rework_plan(issue_id: str):
    feedback = request.form.get("feedback", "").strip()
    return _start(issue_id, "rework", store.REWORKING, False, feedback)
