import os

import httpx

from software_factory import config, store
from software_factory.queries import (
    ADD_LABEL_MUTATION,
    COMMENT_MUTATION,
    ISSUE_BY_LABEL_QUERY,
    ISSUE_CREATOR_QUERY,
    ISSUE_DETAIL_QUERY,
    ISSUE_STATES_QUERY,
    LABELS_BY_NAME_QUERY,
    SET_STATE_MUTATION,
)
from software_factory.utils import ssl_context

LINEAR_API_URL = "https://api.linear.app/graphql"


class LinearError(RuntimeError):
    pass


def linear_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"Authorization": str(os.getenv("LINEAR_API_KEY"))},
        verify=ssl_context(),
        timeout=30,
    )


async def linear_query(client: httpx.AsyncClient, query: str, variables: dict) -> dict:
    result = await client.post(LINEAR_API_URL, json={"query": query, "variables": variables})
    # GraphQL errors come back with a JSON body even on a 400, and say more than the status
    try:
        body = result.json()
    except ValueError:
        body = {}
    if errors := body.get("errors"):
        raise LinearError("; ".join(e.get("message", str(e)) for e in errors))
    result.raise_for_status()
    return body["data"]


async def list_todo_bugs():
    issues, after = [], None
    async with linear_client() as client:
        while True:
            data = await linear_query(
                client,
                ISSUE_BY_LABEL_QUERY,
                {"statusName": config.LINEAR_TODO_STATE, "labelName": config.LINEAR_BUG_LABEL, "after": after},
            )
            page = data["issues"]
            issues += page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                return issues
            after = page["pageInfo"]["endCursor"]


async def get_issue(issue_id: str):
    async with linear_client() as client:
        data = await linear_query(client, ISSUE_DETAIL_QUERY, {"id": issue_id})
    if data["issue"] is None:
        raise LinearError(f"Issue {issue_id} not found")
    return data["issue"]


async def set_state(issue_id: str, state_name: str):
    """Move the issue to its team's workflow state named state_name."""
    async with linear_client() as client:
        data = await linear_query(client, ISSUE_STATES_QUERY, {"id": issue_id})
        states = {s["name"]: s["id"] for s in data["issue"]["team"]["states"]["nodes"]}
        if state_name not in states:
            raise LinearError(f"The team has no {state_name!r} state (it has {', '.join(sorted(states))})")
        data = await linear_query(client, SET_STATE_MUTATION, {"id": issue_id, "stateId": states[state_name]})
    if not data["issueUpdate"]["success"]:
        raise LinearError(f"Linear refused the move to {state_name!r}")


async def add_label(issue_id: str, label_name: str):
    """Add the label named label_name to the issue, keeping its other labels.

    The issue's team label wins over a workspace label of the same name.
    """
    async with linear_client() as client:
        data = await linear_query(client, LABELS_BY_NAME_QUERY, {"id": issue_id, "name": label_name})
        team_id = data["issue"]["team"]["id"]
        labels = [n for n in data["issueLabels"]["nodes"] if n["team"] is None or n["team"]["id"] == team_id]
        if not labels:
            raise LinearError(f"There's no {label_name!r} label for the issue's team or the workspace")
        label = next((n for n in labels if n["team"] is not None), labels[0])
        data = await linear_query(client, ADD_LABEL_MUTATION, {"id": issue_id, "labelId": label["id"]})
    if not data["issueAddLabel"]["success"]:
        raise LinearError(f"Linear refused the {label_name!r} label")


async def report_state(issue_id: str, state_name: str):
    """set_state for the pipeline: a failure is recorded on the issue, never raised."""
    if not config.LINEAR_WRITEBACK:
        return
    try:
        await set_state(issue_id, state_name)
        store.add_event(issue_id, f"Linear status set to {state_name}")
    except (LinearError, httpx.HTTPError) as e:
        store.add_event(issue_id, f"Couldn't set the Linear status to {state_name}: {e}")


async def _comment(client: httpx.AsyncClient, issue_id: str, body: str):
    data = await linear_query(client, COMMENT_MUTATION, {"id": issue_id, "body": body})
    if not data["commentCreate"]["success"]:
        raise LinearError("Linear refused the comment")


async def comment(issue_id: str, body: str):
    async with linear_client() as client:
        await _comment(client, issue_id, body)


async def comment_to_reporter(issue_id: str, body: str) -> str | None:
    """Comment on the issue, mentioning the person who reported it so Linear notifies them.

    Returns their display name, or None when the issue has no creator and the comment
    went without a mention.
    """
    async with linear_client() as client:
        data = await linear_query(client, ISSUE_CREATOR_QUERY, {"id": issue_id})
        if data["issue"] is None:
            raise LinearError(f"Issue {issue_id} not found")
        creator = data["issue"]["creator"]
        await _comment(client, issue_id, f"{creator['url']} {body}" if creator else body)
    return creator["displayName"] if creator else None


async def report_comment(issue_id: str, body: str):
    """comment for the pipeline: a failure is recorded on the issue, never raised."""
    if not config.LINEAR_WRITEBACK:
        return
    try:
        await comment(issue_id, body)
        store.add_event(issue_id, "Linear comment posted")
    except (LinearError, httpx.HTTPError) as e:
        store.add_event(issue_id, f"Couldn't post the Linear comment: {e}")
