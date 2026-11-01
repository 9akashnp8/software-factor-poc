import json

from conftest import git, seed_issue
from software_factory import dev_changes, environment, store
from software_factory.dev_changes import Merge


def factory_fix(identifier="BEI-17", pr_id=5540, qa=None):
    ws = seed_issue(store.DEPLOY_PENDING, issue_id=identifier.lower(), identifier=identifier, slot=None, pr_id=pr_id)
    if qa is not None:
        (ws / "qa.json").write_text(json.dumps(qa))


def test_merges_lists_each_pr_with_its_files_and_matches_factory_fixes(dev_repo):
    factory_fix(qa={"root_cause": "No email check", "fix": "Emails are validated"})
    worktree = dev_repo.worktree()
    base = git(worktree, "rev-parse", "HEAD")
    dev_repo.merge_pr(5540, "BEI-17: Email accepts test@123", ["apps/api/email.py", "apps/api/tests/test_email.py"])
    dev_repo.merge_pr(5544, "invitation email fixes", ["apps/web/invite.ts"])
    environment.fetch_base()

    found = dev_changes.merges(worktree, base, "origin/dev")

    assert [m.subject for m in found] == ["Merged PR 5540: BEI-17: Email accepts test@123", "Merged PR 5544: invitation email fixes"]
    assert found[0].files == ["apps/api/email.py", "apps/api/tests/test_email.py"]
    assert found[0].issue == {"identifier": "BEI-17", "root_cause": "No email check", "fix": "Emails are validated"}
    assert found[1].issue is None


def test_render_shows_root_cause_and_caps_merges_and_files():
    found = [Merge("a" * 40, "Merged PR 1: BEI-17: Email", [f"f{i}.py" for i in range(20)], {"identifier": "BEI-17", "root_cause": "No check", "fix": "Checked"})]
    found += [Merge(f"{i:040d}", f"Merged PR {i + 2}: other", ["x.py"]) for i in range(30)]

    text = dev_changes.render(found, "b" * 40, "c" * 40)

    assert text.startswith("<dev_changes>") and text.endswith("</dev_changes>")
    assert "against bbbbbbbbbb" in text and "31 merge(s) later" in text
    assert "- Merged PR 1: BEI-17: Email (a factory fix)\n  Root cause: No check\n  Fix: Checked" in text
    assert "f14.py and 5 more" in text
    assert "Merged PR 30: other" in text and "Merged PR 31: other" not in text
    assert "…and 1 more: run `git log --first-parent bbbbbbbbbb..HEAD`" in text


def test_label_names_the_factory_fixes():
    fix = lambda name: Merge("c", "s", [], {"identifier": name})  # noqa: E731
    assert dev_changes.label([Merge("c", "s")]) == "1 new merge to dev"
    assert dev_changes.label([fix("BEI-17"), Merge("c", "s")]) == "2 new merges to dev, including BEI-17"
    assert dev_changes.label([fix("BEI-17"), fix("BEI-20"), fix("BEI-8")]) == "3 new merges to dev, including BEI-17, BEI-20 and BEI-8"


def test_summary_counts_merges_since_the_plan_as_of_the_last_fetch(dev_repo):
    factory_fix()
    worktree = dev_repo.worktree()
    seed_issue(store.PLAN_READY, plan_base=git(worktree, "rev-parse", "HEAD"))
    dev_repo.merge_pr(5540, "BEI-17: Email accepts test@123", ["apps/api/email.py"])

    assert dev_changes.summary(store.get_issue("issue-1")) is None
    environment.fetch_base()
    assert dev_changes.summary(store.get_issue("issue-1")) == "1 new merge to dev, including BEI-17"


def test_summary_falls_back_to_the_worktree_head_and_needs_a_worktree(dev_repo):
    seed_issue(store.PLAN_READY)
    assert dev_changes.summary(store.get_issue("issue-1")) is None
    dev_repo.worktree()
    dev_repo.merge_pr(5544, "invitation email fixes", ["apps/web/invite.ts"])
    environment.fetch_base()
    assert dev_changes.summary(store.get_issue("issue-1")) == "1 new merge to dev"
