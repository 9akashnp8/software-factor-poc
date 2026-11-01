"""Watch the PRs the factory opened: clean up after merges, and send PRs that conflict with
the base branch or fail their build back to their agent."""

import httpx

from software_factory import cleanup, config, store
from software_factory.utils import ssl_context

# The project's API root; the repo API sits under it at /_apis/git/repositories/<repo>
ADO_PROJECT_API = config.ADO_REPO_API.split("/_apis/")[0] + "/_apis"
# Enough of a failed build's errors for the agent to start from
MAX_BUILD_ERRORS = 20


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


async def build_errors(client: httpx.AsyncClient, build_id: int) -> list[str]:
    result = await client.get(f"{ADO_PROJECT_API}/build/builds/{build_id}/timeline")
    result.raise_for_status()
    errors = [
        f"{record.get('name')}: {issue.get('message')}"
        for record in result.json().get("records") or []
        if record.get("result") == "failed"
        for issue in record.get("issues") or []
        if issue.get("type") == "error"
    ]
    return errors[:MAX_BUILD_ERRORS]


def _commit(pr: dict, key: str) -> str | None:
    return (pr.get(key) or {}).get("commitId")


async def check_pull_request(client: httpx.AsyncClient, issue) -> str | None:
    """Act on one PR's state. Returns what was done, for the log."""
    pr = await get_pull_request(client, issue["pr_id"])
    if pr["status"] == "completed":
        await cleanup.cleanup_after_merge(issue["id"])
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
    # A build of an older push says nothing about the branch as it is now
    built_sha = (build.get("triggerInfo") or {}).get("pr.sourceSha")
    if built_sha and source and built_sha != source:
        return None
    errors = await build_errors(client, build["id"])
    link = ((build.get("_links") or {}).get("web") or {}).get("href", "")
    details = "\n".join(f"- {e}" for e in errors) or "(the build reported no error messages)"
    store.queue_update(
        issue["id"],
        f"The PR build failed: {link}\nErrors:\n{details}",
        pr_build_id=build["id"],
    )
    return "build failed"


async def check_pull_requests():
    """Check every PR the factory opened that isn't merged yet."""
    issues = [
        issue
        for status in (store.PR_OPENED, store.UPDATE_QUEUED)
        for issue in store.list_issues(status)
        if issue["pr_id"] is not None
    ]
    if not issues:
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
            except httpx.HTTPError as e:
                print(f"{issue['identifier']}: couldn't check PR {issue['pr_id']}: {e}")
