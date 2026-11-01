import os

import httpx

from software_factory import config, store
from software_factory.queries import ISSUE_BY_LABEL_QUERY, ISSUE_DETAIL_QUERY, ISSUE_STATES_QUERY, SET_STATE_MUTATION
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


async def report_state(issue_id: str, state_name: str):
    """set_state for the pipeline: a failure is recorded on the issue, never raised."""
    if not config.LINEAR_WRITEBACK:
        return
    try:
        await set_state(issue_id, state_name)
        store.add_event(issue_id, f"Linear status set to {state_name}")
    except (LinearError, httpx.HTTPError) as e:
        store.add_event(issue_id, f"Couldn't set the Linear status to {state_name}: {e}")
