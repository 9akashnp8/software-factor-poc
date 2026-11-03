"""Tell QA in Linear that a fix has shipped: In Review, with the root cause, the fix, and how
it was reproduced and checked."""

import json

from software_factory import config, evidence, linear, store
from software_factory.trace_cleaner import EMAIL_RE, PHONE_RE


def scrub(text: str) -> str:
    # The agent is told never to copy user data; this catches what slips through
    return PHONE_RE.sub("<phone>", EMAIL_RE.sub("<email>", text))


def qa_summary(issue_id: str) -> dict:
    path = store.issue_dir(issue_id) / "qa.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def comment_body(issue, build: dict | None = None) -> str:
    """The comment for QA. build is the deploy pipeline run that shipped the fix, if any."""
    pr = f"[PR {issue['pr_id']}]({issue['pr_url']})" if issue["pr_url"] else None
    if build:
        link = ((build.get("_links") or {}).get("web") or {}).get("href")
        run = f"[build {build['id']}]({link})" if link else f"build {build['id']}"
        headline = " · ".join(filter(None, [f"**Fix deployed to {config.BASE_BRANCH}**", pr, run]))
    else:
        headline = " · ".join(filter(None, [f"**Fix merged into {config.BASE_BRANCH}**", pr]))

    sections = [headline]
    ws = store.issue_dir(issue["id"])
    qa = qa_summary(issue["id"])
    if reproduced := evidence.reproduced_sentence(ws):
        qa["reproduced"] = reproduced
    for key, label in (("root_cause", "Root cause"), ("fix", "Fix"), ("reproduced", "Reproduced"), ("checked", "Checked")):
        if qa.get(key):
            sections.append(f"**{label}:** {scrub(qa[key])}")
    return "\n\n".join(sections)


async def announce(issue_id: str, build: dict | None = None):
    """Comment for QA, then move Linear to In Review. Failures are recorded, never raised."""
    issue = store.get_issue(issue_id)
    await linear.report_comment(issue_id, comment_body(issue, build))
    await linear.report_state(issue_id, config.LINEAR_MERGED_STATE)
