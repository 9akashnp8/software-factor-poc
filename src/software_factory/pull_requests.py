"""Watch the PRs the factory opened: clean up after merges, send PRs that conflict with the
base branch or fail their build back to their agent, and follow merged fixes to deployment."""

import re
from datetime import datetime, timedelta

import httpx

from software_factory import cleanup, config, deployments, store
from software_factory.utils import ssl_context

# The project's API root; the repo API sits under it at /_apis/git/repositories/<repo>
ADO_PROJECT_API = config.ADO_REPO_API.split("/_apis/")[0] + "/_apis"
# Enough of a failed build's errors and logs for the agent to start from
MAX_BUILD_ERRORS = 20
MAX_FAILED_LOGS = 2
LOG_TAIL_LINES = 60
# The deploy run for a merge is queued around when the PR completes, not exactly after it
DEPLOY_QUEUE_SLACK = timedelta(minutes=5)
# Azure Pipelines prefixes every log line with its time
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z ")


def ado_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        auth=("", config.ado_pat()),
        params={"api-version": config.ADO_API_VERSION},
        verify=ssl_context(),
        timeout=30,
    )


async def get_pull_request(client: httpx.AsyncClient, pr_id: int) -> dict:
    result = await client.get(f"{config.ADO_REPO_API}/pullrequests/{pr_id}")
    result.raise_for_status()
    return result.json()


async def latest_build(client: httpx.AsyncClient, pr_id: int) -> dict | None:
    """The newest build of the PR's merge ref, which is what build validation runs."""
    result = await client.get(
        f"{ADO_PROJECT_API}/build/builds",
        params={"branchName": f"refs/pull/{pr_id}/merge", "$top": 1, "queryOrder": "queueTimeDescending"},
    )
    result.raise_for_status()
    builds = result.json()["value"]
    return builds[0] if builds else None


async def build_failure(client: httpx.AsyncClient, build_id: int) -> str:
    """The failed steps' error messages and log tails.

    The messages alone are usually "Bash exited with code '1'"; the test or lint output
    that explains it is at the end of the step's log.
    """
    result = await client.get(f"{ADO_PROJECT_API}/build/builds/{build_id}/timeline")
    result.raise_for_status()
    if not result.content:
        # A build that fails before any job runs (a YAML error, a full agent disk) has no
        # timeline; its errors are the build's validation results
        build = await client.get(f"{ADO_PROJECT_API}/build/builds/{build_id}")
        build.raise_for_status()
        messages = [
            f"- {v.get('message', '').strip()}"
            for v in build.json().get("validationResults") or []
            if v.get("result") == "error"
        ]
        return "Errors:\n" + ("\n".join(messages) or "(none reported)")
    failed = [r for r in result.json().get("records") or [] if r.get("result") == "failed"]
    errors = [
        f"- {record.get('name')}: {issue.get('message')}"
        for record in failed
        for issue in record.get("issues") or []
        if issue.get("type") == "error"
    ][:MAX_BUILD_ERRORS]
    sections = ["Errors:\n" + ("\n".join(errors) or "(none reported)")]

    # Jobs and phases repeat their tasks' logs; the task is where the output is
    tasks = [r for r in failed if r.get("type") == "Task" and (r.get("log") or {}).get("url")]
    for task in tasks[:MAX_FAILED_LOGS]:
        log = await client.get(task["log"]["url"])
        log.raise_for_status()
        tail = [TIMESTAMP_RE.sub("", line) for line in log.text.splitlines()[-LOG_TAIL_LINES:]]
        sections.append(f"End of the {task.get('name')} log:\n```\n" + "\n".join(tail) + "\n```")
    return "\n\n".join(sections)


def _commit(pr: dict, key: str) -> str | None:
    return (pr.get(key) or {}).get("commitId")


async def check_pull_request(client: httpx.AsyncClient, issue) -> str | None:
    """Act on one PR's state. Returns what was done, for the log."""
    pr = await get_pull_request(client, issue["pr_id"])
    if pr["status"] == "completed":
        # The merge commit is what the deploy pipeline builds; closedDate is when it landed
        await cleanup.cleanup_after_merge(issue["id"], _commit(pr, "lastMergeCommit"), pr.get("closedDate"))
        return "merged"
    if pr["status"] == "abandoned":
        store.transition(issue["id"], store.NEEDS_ATTENTION, reason="The PR was abandoned in Azure DevOps")
        return "abandoned"
    # Waiting for its agent already; only a merge or an abandon changes that
    if issue["status"] != store.PR_OPENED:
        return None

    source, target = _commit(pr, "lastMergeSourceCommit"), _commit(pr, "lastMergeTargetCommit")
    if pr.get("mergeStatus") == "conflicts":
        # The same pair of commits conflicts the same way; act on each pair once
        key = f"{source}:{target}"
        if key != issue["pr_conflict_key"]:
            store.queue_update(
                issue["id"],
                f"Azure DevOps reports merge conflicts between this branch and {config.BASE_BRANCH}.",
                pr_conflict_key=key,
            )
            return "conflicts"
        return None

    build = await latest_build(client, issue["pr_id"])
    if not build or build.get("status") != "completed" or build.get("result") != "failed":
        return None
    if build["id"] == issue["pr_build_id"]:
        return None
    # A build of an older push says nothing about the branch as it is now. Not every server
    # reports the built commit; there, acting once per build id is what keeps a failure
    # from being sent back again after the agent pushed its fix.
    built_sha = (build.get("triggerInfo") or {}).get("pr.sourceSha")
    if built_sha and source and built_sha != source:
        return None
    details = await build_failure(client, build["id"])
    link = ((build.get("_links") or {}).get("web") or {}).get("href", "")
    store.queue_update(issue["id"], f"The PR build failed: {link}\n\n{details}", pr_build_id=build["id"])
    return "build failed"


def failure_reason(details: str) -> str:
    """One line saying why a build failed, from build_failure's text.

    Its error list starts with whatever the step wrote to stderr (Docker's progress lines,
    for one), so the explanation is the log's last ERROR line, or failing that the last
    error message.
    """
    lines = [line.removeprefix("##[error]").strip() for line in details.splitlines()]
    reason = next((line for line in reversed(lines) if "ERROR:" in line), None)
    if reason is None:
        errors = [line[2:].strip() for line in lines if line.startswith("- ") and line[2:].strip()]
        reason = errors[-1] if errors else "no error reported"
    return reason.rstrip(".")[:300]


def _time(value: str | None) -> datetime | None:
    """Azure DevOps times have up to seven fraction digits, which fromisoformat rejects."""
    if not value:
        return None
    value = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
    return datetime.fromisoformat(value)


async def deploy_runs(client: httpx.AsyncClient, since: datetime) -> list[dict]:
    """Runs of the deploy pipeline on the base branch queued from since on, oldest first."""
    result = await client.get(
        f"{ADO_PROJECT_API}/build/builds",
        params={
            "definitions": config.DEPLOY_PIPELINE_ID,
            "branchName": f"refs/heads/{config.BASE_BRANCH}",
            "minTime": since.isoformat(),
            "queryOrder": "queueTimeAscending",
            "$top": 50,
        },
    )
    result.raise_for_status()
    return result.json()["value"]


async def check_deploy(client: httpx.AsyncClient, issue) -> str | None:
    """Move a merged issue on once a deploy has shipped it. Returns what was done, for the log.

    The pipeline runs once per merge, so the fix ships with its merge commit's run or any
    later one: a failed run of ours is superseded by the next that succeeds.
    """
    merged_at = _time(issue["merged_at"])
    if merged_at is None:
        return None
    # The run for the merge commit is queued as the PR completes, give or take a little
    runs = await deploy_runs(client, merged_at - DEPLOY_QUEUE_SLACK)
    ours = [
        run for run in runs
        if run.get("sourceVersion") == issue["merge_commit"] or (_time(run.get("queueTime")) or merged_at) >= merged_at
    ]
    for run in ours:
        if run.get("status") != "completed":
            continue
        if run.get("result") == "succeeded":
            if store.transition(issue["id"], store.DEPLOYED, expected={store.DEPLOY_PENDING}):
                store.add_event(issue["id"], f"Deployed by build {run['id']}")
                await deployments.announce(issue["id"], run)
            return f"deployed by build {run['id']}"
        if run["id"] > (issue["deploy_build_seen"] or 0):
            details = await build_failure(client, run["id"])
            reason = failure_reason(details)
            store.add_event(issue["id"], f"Deploy build {run['id']} failed: {reason}. Waiting for the next deploy.")
            store.set_fields(issue["id"], deploy_build_seen=run["id"])
    return None


async def retry_cleanups():
    """Finish teardowns that failed after a merge: the issue holds its slot until they do."""
    for status in (store.MERGED, store.DEPLOY_PENDING, store.DEPLOYED):
        for issue in store.list_issues(status):
            if issue["slot"] is not None:
                await cleanup.teardown(issue["id"], None, "PR merged", delete_remote=True, flag=False)


async def check_pull_requests():
    """Check every PR the factory opened that isn't merged yet, and every merged fix that
    isn't deployed yet."""
    await retry_cleanups()
    issues = [
        issue
        for status in (store.PR_OPENED, store.UPDATE_QUEUED)
        for issue in store.list_issues(status)
        if issue["pr_id"] is not None
    ]
    awaiting_deploy = store.list_issues(store.DEPLOY_PENDING) if config.DEPLOY_PIPELINE_ID else []
    if not issues and not awaiting_deploy:
        return
    try:
        client = ado_client()
    except RuntimeError as e:
        print(f"Can't check PRs: {e}")
        return
    async with client:
        for issue in issues:
            try:
                if outcome := await check_pull_request(client, issue):
                    print(f"{issue['identifier']}: PR {issue['pr_id']} {outcome}")
            except httpx.HTTPStatusError as e:
                # Merges, abandons and conflicts were already handled; only the build check failed
                if "/_apis/build/" in e.request.url.path and e.response.status_code in (401, 403):
                    print(
                        f"{issue['identifier']}: can't read PR {issue['pr_id']}'s builds. Failed builds go "
                        "unnoticed until AZURE_DEVOPS_PAT has the Build (Read) scope."
                    )
                else:
                    print(f"{issue['identifier']}: couldn't check PR {issue['pr_id']}: {e}")
            except httpx.HTTPError as e:
                print(f"{issue['identifier']}: couldn't check PR {issue['pr_id']}: {e}")
        for issue in awaiting_deploy:
            try:
                if outcome := await check_deploy(client, issue):
                    print(f"{issue['identifier']}: {outcome}")
            except httpx.HTTPError as e:
                print(f"{issue['identifier']}: couldn't check its deploy: {e}")
