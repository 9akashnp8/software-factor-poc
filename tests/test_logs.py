import importlib
import json
from datetime import UTC, datetime

import anyio
import httpx
import pytest

from conftest import seed_issue
from software_factory import config, environment, logs, store

main = importlib.import_module("software_factory.main")

T0 = 1_793_000_000_000_000_000  # ns


@pytest.fixture
def loki(monkeypatch):
    """Answer Loki queries from responses[query substring], recording each request."""
    monkeypatch.setattr(config, "LOKI_URL", "http://loki.test")
    monkeypatch.setattr(config, "LOKI_SELECTOR", '{service="api"}')
    monkeypatch.setenv("LOKI_TOKEN", "tok")
    requests, responses = [], {}

    def handler(request: httpx.Request):
        requests.append(request)
        query = request.url.params["query"]
        for key, streams in responses.items():
            if key in query:
                return httpx.Response(200, json={"data": {"result": streams}})
        return httpx.Response(200, json={"data": {"result": []}})

    real_client = logs._client

    def client():
        made = real_client()
        return httpx.AsyncClient(base_url=made.base_url, headers=made.headers, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(logs, "_client", client)
    return requests, responses


def stream(*lines, start=T0):
    return [{"stream": {"service": "api"}, "values": [[str(start + i), line] for i, line in enumerate(lines)]}]


def test_logql_filters():
    assert logs.logql() == config.LOKI_SELECTOR
    assert logs.logql(contains='say "hi"') == config.LOKI_SELECTOR + ' |= "say \\"hi\\""'
    assert logs.logql(errors_only=True).endswith('| json | level=~"(?i)warn(ing)?|error|critical"')
    assert logs.logql(matching="a|b") == config.LOKI_SELECTOR + ' |~ "a|b"'


def test_scrub_removes_personal_data_and_tokens():
    line = '{"user": "jane@cpc.gov.ae", "phone": "+971501234567", "auth": "Bearer abcdefghijkl", "jwt": "eyJhbGciOi.eyJzdWIi.sig"}'
    scrubbed = logs.scrub(line)
    assert "jane@" not in scrubbed and "4567" not in scrubbed and "abcdefghijkl" not in scrubbed and "eyJ" not in scrubbed
    assert "Bearer <token>" in scrubbed and "<email>" in scrubbed


def test_query_sorts_scrubs_and_authenticates(loki):
    requests, responses = loki
    responses['service="api"'] = [*stream("second jane@cpc.gov.ae", start=T0 + 5), *stream("first")]
    start, end = datetime(2026, 11, 2, 9, tzinfo=UTC), datetime(2026, 11, 2, 10, tzinfo=UTC)
    entries = anyio.run(logs.query, start, end, logs.logql())
    assert [line for _, line in entries] == ["first", "second <email>"]
    (request,) = requests
    assert request.url.path == "/loki/api/v1/query_range"
    assert request.url.params["start"] == str(int(start.timestamp() * 1e9))
    assert request.headers["Authorization"] == "Bearer tok"


def test_query_errors(loki, monkeypatch):
    def failing():
        return httpx.AsyncClient(base_url="http://loki.test", transport=httpx.MockTransport(lambda r: httpx.Response(401, text="no")))

    monkeypatch.setattr(logs, "_client", failing)
    now = datetime.now(UTC)
    with pytest.raises(logs.LogsError, match="401"):
        anyio.run(logs.query, now, now, "q")
    monkeypatch.setattr(config, "LOKI_URL", "")
    with pytest.raises(logs.LogsError, match="isn't configured"):
        anyio.run(logs.query, now, now, "q")


def test_turn_logs_follow_the_conversation_to_its_requests(loki, tmp_path):
    requests, responses = loki
    responses["conv-1"] = stream('{"message": "chat request received", "conv_id": "conv-1", "request_id": "req-9"}')
    responses["|~"] = stream('{"message": "tool failed", "request_id": "req-9"}', start=T0 + 10_000_000)
    responses["level"] = stream('{"level": "ERROR", "message": "ERP timeout"}', start=T0 + 20_000_000)
    turn = {"turn": 2, "timestamp": "2026-10-29T11:59:04.929Z", "latency": 2.0}
    path, count = anyio.run(logs.turn_logs, tmp_path, "conv-1", turn)
    assert path == tmp_path / "logs" / "turn2.log" and count == 3
    text = path.read_text(encoding="utf-8")
    assert text.index("chat request received") < text.index("tool failed") < text.index("ERP timeout")
    start = int(requests[0].url.params["start"])
    assert start == int(datetime(2026, 10, 29, 11, 58, 4, 929000, tzinfo=UTC).timestamp() * 1e9)


def test_report_logs_look_back_from_the_report(loki, tmp_path, monkeypatch):
    requests, responses = loki
    monkeypatch.setattr(config, "LOKI_WINDOW_MINUTES", 30)
    path, count = anyio.run(logs.report_logs, tmp_path, "2026-11-02T10:00:00.000Z")
    assert path.name == "before-report.log" and count == 0
    params = requests[0].url.params
    assert (int(params["end"]) - int(params["start"])) == 30 * 60 * 10**9
    assert "level" in params["query"]


def test_query_logs_tool(loki, tmp_path):
    _, responses = loki
    responses['service="api"'] = stream("hello")
    tool = logs.query_logs_tool(tmp_path)
    result = anyio.run(tool.handler, {"since": "2026-11-02T09:00:00Z", "until": "2026-11-02T10:00:00Z"})
    text = result["content"][0]["text"]
    assert text.startswith("1 lines, saved to") and "hello" in text
    assert len(list((tmp_path / "logs").glob("query-*.log"))) == 1
    bad = anyio.run(tool.handler, {"since": "yesterday", "until": "2026-11-02T10:00:00Z"})
    assert bad["is_error"] and "Couldn't query" in bad["content"][0]["text"]


def test_agents_never_see_lokis_credentials(monkeypatch):
    monkeypatch.setenv("LOKI_TOKEN", "tok")
    seed_issue(store.PLANNING)
    env = environment.agent_env(store.get_issue("issue-1"))
    assert env["LOKI_TOKEN"] == "" and env["LOKI_PASSWORD"] == ""


def test_agents_get_the_log_tool_only_when_loki_is_set(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOKI_URL", "")
    assert "factory" not in main.mcp_servers(tmp_path)
    monkeypatch.setattr(config, "LOKI_URL", "http://loki.test")
    assert main.mcp_servers(tmp_path)["factory"]["type"] == "sdk"


def test_triage_prefetches_logs_into_the_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOKI_URL", "http://loki.test")
    seed_issue(store.GATHERING_CONTEXT)

    async def fake_report_logs(ws, reported_at):
        assert reported_at == "2026-11-02T10:00:00.000Z"
        return ws / "logs" / "before-report.log", 4

    monkeypatch.setattr(logs, "report_logs", fake_report_logs)
    monkeypatch.setattr(main, "download_uploads", lambda urls, dest, key: [])
    bug = {"id": "issue-1", "identifier": "BEI-1", "title": "t", "description": "d", "createdAt": "2026-11-02T10:00:00.000Z", "labels": {"nodes": []}}
    prompt = anyio.run(main.gather_context, bug, tmp_path)
    assert "<logs>" in prompt and "before-report.log (4 lines)" in prompt and "query_logs" in prompt


def test_loki_down_doesnt_stop_triage(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOKI_URL", "http://loki.test")
    seed_issue(store.GATHERING_CONTEXT)

    async def down(ws, reported_at):
        raise logs.LogsError("Loki unreachable: refused")

    monkeypatch.setattr(logs, "report_logs", down)
    monkeypatch.setattr(main, "download_uploads", lambda urls, dest, key: [])
    bug = {"id": "issue-1", "identifier": "BEI-1", "title": "t", "description": "d", "createdAt": "2026-11-02T10:00:00.000Z", "labels": {"nodes": []}}
    prompt = anyio.run(main.gather_context, bug, tmp_path)
    assert prompt and "<logs>" not in prompt
    assert store.events("issue-1")[-1]["detail"] == "Couldn't fetch the api's logs: Loki unreachable: refused"


def test_logs_command_saves_the_lines(loki, capsys):
    from argparse import Namespace

    from software_factory import worker

    _, responses = loki
    responses['service="api"'] = stream("hello")
    seed_issue(store.PLANNING)
    args = Namespace(issue="bei-1", since="2026-11-02T09:00:00Z", until=None, contains=None, errors_only=False)
    assert worker.logs_command(args) == 0
    out = capsys.readouterr().out
    assert out.startswith("1 lines, saved to") and str(store.issue_dir("issue-1") / "logs") in out
