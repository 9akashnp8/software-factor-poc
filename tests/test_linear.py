import json

import anyio
import httpx
import pytest

from conftest import seed_issue
from software_factory import config, linear, store

STATES = {"data": {"issue": {"team": {"states": {"nodes": [{"id": "s-todo", "name": "Todo"}, {"id": "s-prog", "name": "In Progress"}]}}}}}


def mock_linear(monkeypatch, handler) -> list:
    """Route Linear calls to handler; returns the GraphQL bodies sent."""
    sent = []
    real_client = httpx.AsyncClient

    def recording(request):
        sent.append(json.loads(request.content))
        return handler(request)

    monkeypatch.setattr(linear.httpx, "AsyncClient", lambda **kw: real_client(**kw, transport=httpx.MockTransport(recording)))
    return sent


def test_list_todo_bugs_follows_pages(monkeypatch):
    def handler(request):
        variables = json.loads(request.content)["variables"]
        assert (variables["statusName"], variables["labelName"]) == (config.LINEAR_TODO_STATE, config.LINEAR_BUG_LABEL)
        page = {"nodes": [{"id": "a"}], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}} if variables["after"] is None \
            else {"nodes": [{"id": "b"}], "pageInfo": {"hasNextPage": False, "endCursor": None}}
        return httpx.Response(200, json={"data": {"issues": page}})

    mock_linear(monkeypatch, handler)
    assert [i["id"] for i in anyio.run(linear.list_todo_bugs)] == ["a", "b"]


def test_graphql_errors_raise_linear_error(monkeypatch):
    mock_linear(monkeypatch, lambda request: httpx.Response(400, json={"errors": [{"message": "Bad filter"}]}))
    with pytest.raises(linear.LinearError, match="Bad filter"):
        anyio.run(linear.list_todo_bugs)


def test_missing_issue_raises_linear_error(monkeypatch):
    mock_linear(monkeypatch, lambda request: httpx.Response(200, json={"data": {"issue": None}}))
    with pytest.raises(linear.LinearError, match="not found"):
        anyio.run(linear.get_issue, "nope")


def test_set_state_looks_up_the_team_state_then_updates(monkeypatch):
    def handler(request):
        if "issueUpdate" in json.loads(request.content)["query"]:
            return httpx.Response(200, json={"data": {"issueUpdate": {"success": True}}})
        return httpx.Response(200, json=STATES)

    sent = mock_linear(monkeypatch, handler)
    anyio.run(linear.set_state, "issue-1", "In Progress")
    assert sent[1]["variables"] == {"id": "issue-1", "stateId": "s-prog"}


def test_unknown_state_is_recorded_not_raised(monkeypatch):
    seed_issue(store.GATHERING_CONTEXT)
    monkeypatch.setattr(config, "LINEAR_WRITEBACK", True)
    sent = mock_linear(monkeypatch, lambda request: httpx.Response(200, json=STATES))
    anyio.run(linear.report_state, "issue-1", "In Review")
    assert len(sent) == 1
    assert "has no 'In Review' state" in store.events("issue-1")[-1]["detail"]
    assert store.get_issue("issue-1")["status"] == store.GATHERING_CONTEXT


def test_failed_write_back_is_recorded_not_raised(monkeypatch):
    seed_issue(store.GATHERING_CONTEXT)
    monkeypatch.setattr(config, "LINEAR_WRITEBACK", True)
    mock_linear(monkeypatch, lambda request: httpx.Response(503))
    anyio.run(linear.report_state, "issue-1", "In Progress")
    assert "Couldn't set the Linear status" in store.events("issue-1")[-1]["detail"]


def test_write_back_off_sends_nothing(monkeypatch):
    seed_issue(store.GATHERING_CONTEXT)
    sent = mock_linear(monkeypatch, lambda request: httpx.Response(500))
    anyio.run(linear.report_state, "issue-1", "In Progress")
    assert sent == []
