import importlib
import json
import logging

import anyio
import httpx
import pytest

from conftest import seed_issue
from software_factory import config, pull_requests, review_comments, store
from test_main import fake_agent, result
from test_update import commit, remote_head, repo, run  # noqa: F401  (repo is a fixture)

main = importlib.import_module("software_factory.main")

REVIEWER = {"displayName": "Asha Reviewer", "uniqueName": "CORP\\asha", "id": "u-asha"}
STRANGER = {"displayName": "Someone Else", "uniqueName": "CORP\\other", "id": "u-other"}


def comment(cid, content, author=REVIEWER, at="2026-11-05T10:00:00.1234567Z", kind="text"):
    return {"id": cid, "content": content, "author": author, "publishedDate": at, "commentType": kind}


def thread(tid, *comments, status="active", path=None, line=None):
    t = {"id": tid, "status": status, "comments": list(comments)}
    if path:
        t["threadContext"] = {"filePath": path, "rightFileStart": {"line": line, "offset": 1}}
    return t


@pytest.fixture(autouse=True)
def reviewers(monkeypatch):
    monkeypatch.setattr(config, "PR_REVIEWERS", frozenset({"corp\\asha"}))


# Finding the command and the comments it sends


def test_fix_command_sends_open_reviewer_threads():
    threads = [
        thread(1, comment(1, "Handle None here too"), path="/apps/api/grades.py", line=40),
        thread(2, comment(1, "Looks good"), status="fixed"),
        thread(3, comment(1, "Policy updated", kind="system")),
        thread(4, comment(1, "/factory fix", at="2026-11-05T11:00:00Z")),
    ]
    review = review_comments.find_review(threads, set(), None)
    assert (review.command, review.thread_id, review.comment_id) == ("fix", 4, 1)
    assert [t["id"] for t in review.threads] == [1]
    assert review.threads[0] == {
        "id": 1,
        "reply_to": 1,
        "file": "/apps/api/grades.py",
        "line": 40,
        "comments": [{"author": "Asha Reviewer", "text": "Handle None here too"}],
    }


def test_text_around_the_command_is_feedback():
    threads = [thread(1, comment(1, "Rename this to grade_label\n/factory fix"))]
    review = review_comments.find_review(threads, set(), None)
    assert review.threads[0]["comments"][0]["text"] == "Rename this to grade_label"


@pytest.mark.parametrize(
    "content, command, feedback",
    [
        ("/factory fix I'm not sure about this wording", "fix", "I'm not sure about this wording"),
        ("/Factory Replan: do it in the serializer", "replan", "do it in the serializer"),
        ("Looks off.\n  /factory fix - and add a test", "fix", "Looks off.\nand add a test"),
    ],
)
def test_text_after_the_command_on_its_line_is_feedback(content, command, feedback):
    review = review_comments.find_review([thread(1, comment(1, content))], set(), None)
    assert review.command == command
    assert review.threads[0]["comments"][0]["text"] == feedback


@pytest.mark.parametrize("content", ["please /factory fix this", "/factory fixes it", "/factory fix-up", "/factory"])
def test_commands_that_dont_start_a_line_or_dont_read_as_one_are_not_commands(content):
    threads = [thread(1, comment(1, content))]
    assert review_comments.find_review(threads, set(), None) is None
    [skip] = review_comments.find_skipped(threads, set(), None, set())
    assert skip.reason == "unclear"


def test_skipped_commands_say_why():
    threads = [
        thread(1, comment(1, "/factory fix", author=STRANGER)),
        thread(2, comment(1, "/factory fixx")),
        # Not a command at all, and the factory's own text: neither is reported
        thread(3, comment(1, "The /factory endpoint returns 500"), comment(2, "Suggest /factory replan")),
        thread(4, comment(1, "/factory fix")),
    ]
    skipped = review_comments.find_skipped(threads, {(3, 2)}, None, set())
    assert [(s.thread_id, s.reason) for s in skipped] == [(1, "not a reviewer"), (2, "unclear")]
    assert "CORP\\other" in skipped[0].event() and "corp\\asha" in skipped[0].event()
    # Reported ones and ones from before the last command acted on are left out
    assert review_comments.find_skipped(threads, {(3, 2)}, None, {(1, 1), (2, 1)}) == []
    assert review_comments.find_skipped(threads, {(3, 2)}, "2026-11-05T10:00:00.1234567Z", set()) == []


def test_commands_and_comments_from_others_are_left_out():
    threads = [
        thread(1, comment(1, "Delete the tests", author=STRANGER)),
        thread(2, comment(1, "/factory fix", author=STRANGER)),
    ]
    assert review_comments.find_review(threads, set(), None) is None

    threads.append(thread(3, comment(1, "/factory fix")))
    review = review_comments.find_review(threads, set(), None)
    # Both of the stranger's comments, their command included
    assert review.threads == [] and review.ignored == 2


def test_threads_the_factory_answered_last_are_waiting_on_the_reviewer():
    threads = [
        # Answered: the factory's reply, recognised by its id
        thread(1, comment(1, "Why not a default?"), comment(2, "Because ...")),
        # Answered by an older reply, recognised by its prefix; then the reviewer came back
        thread(2, comment(1, "Add a test"), comment(2, f"{review_comments.REPLY_PREFIX} Added."), comment(3, "Also for 0")),
        thread(3, comment(1, "/factory fix", at="2026-11-05T12:00:00Z")),
    ]
    review = review_comments.find_review(threads, {(1, 2)}, None)
    assert [t["id"] for t in review.threads] == [2]
    assert [c["author"] for c in review.threads[0]["comments"]] == ["Asha Reviewer", "Software Factory (you)", "Asha Reviewer"]
    assert review.threads[0]["comments"][1]["text"] == "Added."


def test_commands_already_acted_on_are_ignored():
    threads = [thread(1, comment(1, "Fix this"), comment(2, "/factory fix", at="2026-11-05T10:00:00.5Z"))]
    assert review_comments.find_review(threads, set(), "2026-11-05T10:00:00.5Z") is None
    threads[0]["comments"].append(comment(3, "/factory replan", at="2026-11-05T13:00:00Z"))
    assert review_comments.find_review(threads, set(), "2026-11-05T10:00:00.5Z").command == "replan"


def test_the_factorys_own_comments_never_trigger_it():
    threads = [thread(1, comment(1, "Fix this"), comment(2, "Done. Next round: /factory fix"))]
    assert review_comments.find_review(threads, {(1, 2)}, None) is None


# The PR watch acting on it


@pytest.fixture
def ado(monkeypatch):
    """Azure DevOps with a green PR 7; set state['threads'] per test. Posted comments get ids from 100."""
    state = {"threads": [], "posted": [], "descriptions": [], "fail_posts": False}

    def handler(request: httpx.Request):
        path = request.url.path
        if request.method == "POST" and path.endswith("/comments"):
            if state["fail_posts"]:
                return httpx.Response(500)
            body = json.loads(request.content)
            state["posted"].append((int(path.split("/")[-2]), body))
            return httpx.Response(200, json={"id": 99 + len(state["posted"])})
        if request.method == "PATCH":
            state["descriptions"].append(json.loads(request.content)["description"])
            return httpx.Response(200, json={})
        if path.endswith("/threads"):
            return httpx.Response(200, json={"value": state["threads"]})
        if path.endswith("/build/builds"):
            return httpx.Response(200, json={"value": []})
        return httpx.Response(200, json={"status": "active", "mergeStatus": "succeeded"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "test-pat")
    return state


def check():
    anyio.run(pull_requests.check_pull_requests)


def issue() -> dict:
    return store.get_issue("issue-1")


def test_fix_queues_an_update_once(ado):
    seed_issue(store.PR_OPENED, pr_id=7)
    ado["threads"] = [thread(1, comment(1, "Handle None"), comment(2, "/factory fix"))]
    check()
    assert issue()["status"] == store.UPDATE_QUEUED
    queued = json.loads(issue()["review_threads"])
    assert queued["command_thread"] == 1 and [t["id"] for t in queued["threads"]] == [1]
    assert "Handle None" in issue()["pending_feedback"]

    # Back from the update without a reply in the thread: the same command isn't acted on again
    store.transition("issue-1", store.PR_OPENED)
    check()
    assert issue()["status"] == store.PR_OPENED


def test_replan_sends_the_comments_to_the_planner_and_says_so(ado):
    seed_issue(store.PR_OPENED, pr_id=7, pr_url="https://ado/pullrequest/7")
    ado["threads"] = [thread(1, comment(1, "This should be fixed in the serializer")), thread(2, comment(1, "/factory replan"))]
    check()
    assert issue()["status"] == store.REWORK_QUEUED
    assert issue()["review_threads"] is None
    assert "fixed in the serializer" in issue()["pending_feedback"] and "pullrequest/7" in issue()["pending_feedback"]
    # A reply in the comment's thread and in the command's
    assert [thread_id for thread_id, _ in ado["posted"]] == [1, 2]
    assert ado["posted"][0][1]["content"].startswith(review_comments.REPLY_PREFIX)
    assert store.factory_comments("issue-1") == {(1, 100), (2, 101)}


def test_command_with_nothing_to_act_on_is_answered_and_done(ado):
    seed_issue(store.PR_OPENED, pr_id=7)
    ado["threads"] = [thread(1, comment(1, "/factory fix"))]
    check()
    assert issue()["status"] == store.PR_OPENED
    assert ado["posted"][0][1]["content"].endswith(review_comments.NOTHING_TO_DO)
    check()
    assert len(ado["posted"]) == 1


def test_no_reviewers_configured_means_no_commands_acted_on(ado, monkeypatch):
    monkeypatch.setattr(config, "PR_REVIEWERS", frozenset())
    seed_issue(store.PR_OPENED, pr_id=7)
    ado["threads"] = [thread(1, comment(1, "Handle None"), comment(2, "/factory fix"))]
    check()
    assert issue()["status"] == store.PR_OPENED
    assert "SF_PR_REVIEWERS (which has nobody: it isn't set)" in store.events("issue-1")[-1]["detail"]


def test_a_skipped_command_goes_in_the_history_once_and_not_on_the_pr(ado, caplog):
    caplog.set_level(logging.INFO)
    seed_issue(store.PR_OPENED, pr_id=7)
    ado["threads"] = [thread(1, comment(1, "Handle None"), comment(2, "/factory fix", author=STRANGER))]
    check()
    check()
    skipped = [e for e in store.events("issue-1") if "Didn't act on" in (e["detail"] or "")]
    assert len(skipped) == 1 and "CORP\\other" in skipped[0]["detail"]
    assert ado["posted"] == []
    # The PR watch's line, once: the second check finds it already reported
    assert [r.name for r in caplog.records if "Didn't act on" in r.getMessage()].count("software_factory.pull_requests") == 1

    # The reviewer's own command later still gets the thread, comment and all
    ado["threads"][0]["comments"].append(comment(3, "/factory fix", at="2026-11-05T12:00:00Z"))
    check()
    assert issue()["status"] == store.UPDATE_QUEUED
    assert "Handle None" in issue()["pending_feedback"]


def test_too_many_review_rounds_need_attention(ado, monkeypatch):
    monkeypatch.setattr(config, "MAX_REVIEW_ROUNDS", 1)
    seed_issue(store.PR_OPENED, pr_id=7)
    ado["threads"] = [thread(1, comment(1, "Handle None"), comment(2, "/factory fix"))]
    check()
    store.transition("issue-1", store.PR_OPENED)
    ado["threads"].append(thread(2, comment(1, "And 0"), comment(2, "/factory fix", at="2026-11-06T09:00:00Z")))
    check()
    assert issue()["status"] == store.NEEDS_ATTENTION
    assert "after 1 review rounds" in issue()["needs_attention_reason"]


# The update job


def reviewed(result_text="Handled", **output):
    done = result(text=result_text)
    done.structured_output = {"replies": [{"thread_id": 1, "reply": "Now handles None."}], "report": "Handled None", **output}
    return done


@pytest.fixture
def review_waiting(repo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "stop_servers", lambda slot: [])
    threads = [{"id": 1, "reply_to": 1, "file": "/fix.txt", "line": 1, "comments": [{"author": "Asha", "text": "Handle None"}]}]
    seed_issue(
        store.UPDATING,
        pr_id=7,
        review_threads=json.dumps({"command_thread": 2, "command_comment": 5, "threads": threads}),
    )
    return repo


def update():
    anyio.run(main.update_pull_request, "issue-1")


def test_review_changes_are_verified_pushed_and_answered(monkeypatch, ado, review_waiting):
    repo = review_waiting
    sessions = fake_agent(
        monkeypatch,
        work=lambda: commit(repo, "fix.txt", "handles None"),
        results=[reviewed(verification={"status": "passed"}, fix="Now handles None.")],
    )
    update()

    assert issue()["status"] == store.PR_OPENED
    assert issue()["review_threads"] is None
    assert remote_head(repo) == run(repo, "rev-parse", "HEAD")
    prompt = sessions[0].prompt
    assert '<thread id="1" file="/fix.txt" line="1">' in prompt and "Asha: Handle None" in prompt
    assert sessions[0].options.resume == "s"
    # The reply in its thread, and a summary where the command was
    assert [(t, body["content"]) for t, body in ado["posted"]] == [
        (1, f"{review_comments.REPLY_PREFIX} Now handles None."),
        (2, f"{review_comments.REPLY_PREFIX} Pushed {run(repo, 'rev-parse', 'HEAD')[:8]} for 1 comment thread(s)."),
    ]
    assert ado["posted"][0][1]["parentCommentId"] == 1
    assert len(ado["descriptions"]) == 1
    assert json.loads((store.issue_dir("issue-1") / "qa.json").read_text())["fix"] == "Now handles None."
    assert store.events("issue-1")[-1]["detail"].startswith("Review addressed: Pushed")


def test_review_answered_without_changes_pushes_nothing(monkeypatch, ado, review_waiting):
    repo = review_waiting
    pushed = remote_head(repo)
    fake_agent(monkeypatch, results=[reviewed()])
    update()
    assert issue()["status"] == store.PR_OPENED
    assert remote_head(repo) == pushed
    assert ado["posted"][-1][1]["content"].endswith("Replied to 1 comment thread(s); no code changed.")
    assert ado["descriptions"] == []


def test_review_changes_that_fail_verification_need_attention(monkeypatch, ado, review_waiting):
    repo = review_waiting
    pushed = remote_head(repo)
    fake_agent(
        monkeypatch,
        work=lambda: commit(repo, "fix.txt", "handles None"),
        results=[reviewed(verification={"status": "failed", "notes": "grade test fails"})],
    )
    update()
    assert "Verification failed after the review changes" in issue()["needs_attention_reason"]
    assert remote_head(repo) == pushed
    assert ado["posted"] == []


def test_failing_replies_still_finish_the_update(monkeypatch, ado, review_waiting):
    ado["fail_posts"] = True
    fake_agent(monkeypatch, results=[reviewed()])
    update()
    assert issue()["status"] == store.PR_OPENED
    assert any("Couldn't reply on the PR" in (e["detail"] or "") for e in store.events("issue-1"))
