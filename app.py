import asyncio
import json
import os
from datetime import datetime

import httpx
import nh3
from markdown_it import MarkdownIt
from dotenv import load_dotenv
from flask import Flask, Response, abort, flash, render_template, redirect, send_from_directory, url_for, request
from flask_wtf.csrf import CSRFProtect
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

from software_factory import config, dev_changes, evidence, home, linear, metrics, reproduction, store
from software_factory.environment import branch_name
from software_factory.overlap import overlaps

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
CSRFProtect(app)
# Pages draw statuses and ages the way the home page does
app.add_template_filter(home.ago, "ago")
app.add_template_global(home.look, "look")
app.add_template_filter(metrics.duration, "duration")


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


# CommonMark, as agents write it: a list may follow a paragraph line directly, and nested
# lists indent by two spaces. Tables and strikethrough as in GitHub's flavour.
_MARKDOWN = MarkdownIt("commonmark").enable(["table", "strikethrough"])


def render_md(text: str) -> Markup:
    # issue.md holds QA text and users' chat messages, and Markdown passes raw HTML
    # through, so strip anything that could run script before marking it safe
    return Markup(nh3.clean(_MARKDOWN.render(text)))


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
    issues = store.list_issues()
    return render_template(
        "issue_list.html",
        issues=issues,
        groups=home.build_groups(issues),
        load=home.load(issues),
        activity=home.activity(),
        todo_state=config.LINEAR_TODO_STATE,
        bug_label=config.LINEAR_BUG_LABEL,
    )

@app.route("/api/attention")
def attention():
    # Issues waiting on a person; the page polls this to raise browser notifications
    items = [
        {
            "key": f"{i['id']}:{i['status']}",
            "status": i["status"],
            "identifier": i["identifier"],
            "title": i["title"],
            "reason": i["needs_attention_reason"],
            "url": url_for("issue_detail", issue_id=i["id"]),
        }
        for i in store.list_issues()
        if i["status"] in (store.PLAN_READY, store.NEEDS_ATTENTION)
    ]
    return {"issues": items}


METRIC_WINDOWS = (7, 30, 90)


@app.route("/metrics")
def factory_metrics():
    days = request.args.get("days", 30, type=int)
    if days not in METRIC_WINDOWS:
        days = 30
    return render_template("metrics.html", m=metrics.summary(days), windows=METRIC_WINDOWS)


@app.route("/issue/<issue_id>/plan")
def issue_detail(issue_id):
    issue = _issue_or_404(issue_id)
    plan = _read(issue_id, "plan.md")
    actions = store.allowed_actions(issue)
    events = store.events(issue_id)
    ws = store.issue_dir(issue_id)
    repro = reproduction.load(ws)
    verified = evidence.table(ws, for_pr=False)

    return render_template(
        "issue_detail.html",
        issue=issue,
        html_content=render_md(plan) if plan else None,
        actions=actions,
        # Only worth the git calls while the plan can still be approved
        overlaps=overlaps(issue) if "approve" in actions else [],
        dev_moved=dev_changes.summary(issue) if "refresh" in actions else None,
        runs=store.runs(issue_id),
        events=events,
        flow=home.flow(issue, events),
        pr=_pull_request(issue),
        repro=repro,
        verification=render_md(verified) if verified else None,
        screenshots=evidence.screenshots(ws),
        repro_look=REPRODUCTION_LOOK.get(repro["status"]) if repro else None,
        # A chat bug can be replayed, so anything else is weaker evidence
        replay_missing=plan is not None and (ws / "conversation.json").exists()
        and (repro is None or repro.get("method") != "replay"),
        linear_writeback=config.LINEAR_WRITEBACK,
        base_branch=config.BASE_BRANCH,
    )


REPRODUCTION_LOOK = {
    reproduction.REPRODUCED: ("done", "reproduced"),
    reproduction.NOT_REPRODUCED: ("attn", "not reproduced"),
    reproduction.NOT_ATTEMPTED: ("ship", "not attempted"),
}

MERGED_STATUSES = frozenset({store.MERGED, store.DEPLOY_PENDING, store.DEPLOYED})


def _pull_request(issue) -> dict | None:
    """The PR card: what the worker opened, with the root cause and fix it reported."""
    if not issue["pr_url"]:
        return None
    report = _read(issue["id"], "report.md")
    summary = _read(issue["id"], "qa.json")
    qa = json.loads(summary) if summary else {}
    return {
        "url": issue["pr_url"],
        "number": issue["pr_id"],
        "title": f"{issue['identifier']}: {issue['title']}".rstrip(": "),
        # The worker opens PRs as drafts; a person marks them ready and merges
        "state": "merged" if issue["status"] in MERGED_STATUSES else "draft",
        "branch": branch_name(issue["identifier"]),
        "base": config.BASE_BRANCH,
        "root_cause": qa.get("root_cause"),
        "fix": qa.get("fix"),
        "checked": qa.get("checked"),
        "report": render_md(report) if report else None,
    }


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
    return _queue(
        issue_id,
        "approve",
        plan_reviewed_by=request.authorization.username,
        browser_check=int(request.form.get("browser_check") == "1"),
    )

@app.route("/issue/<issue_id>/rework-plan", methods=["POST"])
def rework_plan(issue_id: str):
    feedback = request.form.get("feedback", "").strip()
    return _queue(issue_id, "rework", plan_reviewed_by=request.authorization.username, pending_feedback=feedback)

@app.route("/issue/<issue_id>/refresh-plan", methods=["POST"])
def refresh_plan(issue_id: str):
    return _queue(issue_id, "refresh")

@app.route("/issue/<issue_id>/retry", methods=["POST"])
def retry_triage(issue_id: str):
    return _queue(issue_id, "retry")

@app.route("/issue/<issue_id>/open-pr", methods=["POST"])
def open_pr(issue_id: str):
    return _queue(issue_id, "publish")

@app.route("/issue/<issue_id>/release", methods=["POST"])
def release_issue(issue_id: str):
    return _queue(issue_id, "release")

@app.route("/issue/<issue_id>/post-qa-question", methods=["POST"])
def post_qa_question(issue_id: str):
    """Post the planner's question for QA to Linear, as edited by the person who clicked."""
    _issue_or_404(issue_id)
    question = request.form.get("question", "").strip()
    if not question:
        flash("Write the question before posting it.")
    elif not config.LINEAR_WRITEBACK:
        flash("Posting to Linear is off (SF_LINEAR_WRITEBACK=0).")
    else:
        try:
            asyncio.run(linear.comment(issue_id, question))
        except (linear.LinearError, httpx.HTTPError) as e:
            flash(f"Couldn't post the question to Linear: {e}")
        else:
            actor = request.authorization.username
            # Off the review count, so new bugs aren't held up while QA answers. A follow-up
            # question, posted while already waiting, is only recorded.
            if not store.transition(
                issue_id, store.AWAITING_QA, expected={store.PLAN_READY}, actor=actor, reason=metrics.QA_QUESTION
            ):
                store.add_event(issue_id, metrics.QA_QUESTION, actor=actor)
            flash("Question posted to Linear. When QA answers, send the answer as rework.")
    return redirect(url_for("issue_detail", issue_id=issue_id))


@app.route("/issue/<issue_id>/evidence/<name>")
def evidence_file(issue_id: str, name: str):
    """A screenshot the agent saved while reproducing or checking the bug."""
    _issue_or_404(issue_id)
    # Only names the folder lists, so nothing outside it can be asked for
    if name not in evidence.screenshots(store.issue_dir(issue_id)):
        abort(404)
    return send_from_directory(store.issue_dir(issue_id) / evidence.EVIDENCE_DIR, name)
