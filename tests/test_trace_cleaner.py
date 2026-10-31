import copy
import json
from pathlib import Path

import pytest

from software_factory.trace_cleaner import (
    clean_conversation,
    clean_trace,
    redact,
    render_markdown,
    select_main_trace_ids,
)

FIXTURES = Path(__file__).parent / "fixtures"
SESSION_ID = "11c9970b-7dd3-4bf4-9b1f-97c5d851e90b"


@pytest.fixture
def session() -> dict:
    return json.loads((FIXTURES / "session.json").read_text(encoding="utf-8"))


@pytest.fixture
def trace() -> dict:
    return json.loads((FIXTURES / "trace.json").read_text(encoding="utf-8"))


def steps_of(turn: dict, node_name: str) -> list[dict]:
    return next(n for n in turn["nodes"] if n["name"] == node_name)["steps"]


def test_select_main_trace_ids_skips_title_trace(session):
    assert select_main_trace_ids(session) == ["6f4fb634545422426b97b67100b58663"]


def test_clean_trace_groups_steps_by_graph_node(trace):
    turn = clean_trace(trace)

    assert turn["user_query"] == "what is my mobile number"
    assert turn["result"] == {
        "classification": "employee_records",
        "standalone_query": "What is my mobile number?",
        "answer": "Your mobile number is **abc123**.",
    }
    assert [n["name"] for n in turn["nodes"]] == ["classify", "employee_records"]
    assert [s["type"] for s in steps_of(turn, "classify")] == ["llm", "agent"]
    assert [s["type"] for s in steps_of(turn, "employee_records")] == ["llm", "tool", "llm"]
    assert turn["issues"] == []


def test_clean_trace_separates_tool_schemas_from_tool_results(trace):
    first_call, tool, second_call = steps_of(clean_trace(trace), "employee_records")

    assert first_call["tools_available"] == [
        "get_employee_personal_details",
        "search_organization_structure",
    ]
    assert first_call["new_messages"] == [
        {
            "role": "user",
            "text": "what is my mobile number\n\nReply in English, the language of this question.",
        }
    ]
    assert first_call["output"] == {
        "role": "assistant",
        "tool_calls": [{"name": "get_employee_personal_details", "args": {}}],
    }
    assert tool["status"] == "success"
    assert tool["result"]["mobile"] == "abc123"
    # The resent user message, tool call and tool result are already shown elsewhere
    assert second_call["new_messages"] == []
    assert second_call["output"] == {
        "role": "assistant",
        "text": "Your mobile number is **abc123**.",
    }


def test_clean_conversation_lists_each_system_prompt_once(trace):
    conversation = clean_conversation(SESSION_ID, [trace], redact_data=False)
    turn = conversation["turns"][0]

    assert list(conversation["prompts"]) == [
        "awp/classifier@19",
        "awp/self_service_agents/employee_records_agent@8",
    ]
    assert [s["prompt"] for s in steps_of(turn, "employee_records") if s["type"] == "llm"] == [
        "awp/self_service_agents/employee_records_agent@8",
        "awp/self_service_agents/employee_records_agent@8",
    ]
    assert all("system_prompt" not in s for s in steps_of(turn, "classify"))


def test_changed_prompt_text_is_kept_as_a_variant(trace):
    later = copy.deepcopy(trace)
    later["id"] = "later-turn"
    later["timestamp"] = "2026-10-29T12:05:00.000Z"
    for obs in later["observations"]:
        if obs.get("promptName") == "awp/classifier":
            obs["input"][0]["content"] = "# CPCGPT - Classifier Agent\nCurrent time: 12:05"

    prompts = clean_conversation(SESSION_ID, [later, trace], redact_data=False)["prompts"]

    assert "awp/classifier@19" in prompts
    assert prompts["awp/classifier@19 (variant 2)"].endswith("12:05")


def follow_up_turn(trace: dict, query: str, history: list[dict]) -> dict:
    """A later turn whose LLM calls replay `history` between the system prompt and the query."""
    later = copy.deepcopy(trace)
    later["id"] = "follow-up"
    later["timestamp"] = "2026-10-29T12:05:00.000Z"
    later["input"] = {"query": query}
    for obs in later["observations"]:
        if obs["type"] == "GENERATION":
            system, user, *rest = obs["input"]
            user = {**user, "content": user["content"].replace("what is my mobile number", query)}
            obs["input"] = [system, *history, user, *rest]
    return later


def test_replayed_chat_history_collapses_to_one_line(trace):
    history = [
        {"role": "user", "content": "what is my mobile number"},
        {"role": "assistant", "content": "Your mobile number is **abc123**."},
    ]
    later = follow_up_turn(trace, "and my email?", history)

    turn = clean_conversation(SESSION_ID, [trace, later], redact_data=False)["turns"][1]
    classify_call = steps_of(turn, "classify")[0]
    first_agent_call = steps_of(turn, "employee_records")[0]

    marker = {"role": "history", "text": "2 messages from turn 1, shown above"}
    assert classify_call["new_messages"] == [marker, {"role": "user", "text": "and my email?"}]
    assert first_agent_call["new_messages"][0] == marker
    assert "(chat history: 2 messages from turn 1, shown above)" in render_markdown(
        clean_conversation(SESSION_ID, [trace, later])
    )


def test_history_that_differs_from_earlier_turns_is_kept(trace):
    history = [{"role": "user", "content": "a message the app invented"}]
    later = follow_up_turn(trace, "and my email?", history)

    turn = clean_conversation(SESSION_ID, [trace, later], redact_data=False)["turns"][1]

    assert steps_of(turn, "classify")[0]["new_messages"][0] == {
        "role": "user",
        "text": "a message the app invented",
    }


def test_prompts_differing_only_by_timestamp_are_listed_once(trace):
    later = copy.deepcopy(trace)
    later["id"] = "later-turn"
    later["timestamp"] = "2026-10-29T12:05:00.000Z"
    for t, time in ((trace, "2026-10-29T11:59:04.928766"), (later, "2026-10-29T12:05:00.123")):
        for obs in t["observations"]:
            if obs.get("promptName") == "awp/classifier":
                obs["input"][0]["content"] += f"\nCurrent System Time: {time}"

    prompts = clean_conversation(SESSION_ID, [trace, later], redact_data=False)["prompts"]

    assert [label for label in prompts if label.startswith("awp/classifier")] == ["awp/classifier@19"]
    assert prompts["awp/classifier@19"].endswith("Current System Time: <timestamp>")


def test_redact_replaces_personal_data_consistently(trace):
    conversation = clean_conversation(SESSION_ID, [trace])
    turn = conversation["turns"][0]
    tool = steps_of(turn, "employee_records")[1]

    assert tool["result"] == {
        "employee_number": "401",
        "name": "<NAME_1>",
        "department": "Private Marine Center",
        "mobile": "<MOBILE_1>",
        "work_phone": "<WORK_PHONE_1>",
        "email": "<EMAIL_1>",
    }
    assert turn["result"]["answer"] == "Your mobile number is **<MOBILE_1>**."
    assert steps_of(turn, "employee_records")[2]["output"]["text"] == (
        "Your mobile number is **<MOBILE_1>**."
    )
    assert "abc123" not in json.dumps(conversation)


def test_redact_catches_emails_and_phone_numbers_in_free_text():
    conversation = {
        "session_id": "s",
        "prompts": {"p@1": "Example: change my email to a.b@cpc.ae"},
        "turns": [
            {
                "user_query": "change my mobile to 0501234567 and email to new@cpc.ae",
                "result": {"answer": "Updated 0501234567 on 2026-10-29"},
                "issues": [],
                "nodes": [],
            }
        ],
    }

    redacted = redact(conversation)

    assert redacted["turns"][0]["user_query"] == "change my mobile to <PHONE_1> and email to <EMAIL_1>"
    assert redacted["turns"][0]["result"]["answer"] == "Updated <PHONE_1> on 2026-10-29"
    assert redacted["prompts"] == conversation["prompts"]


def test_abnormal_finish_reason_and_error_level_become_issues(trace):
    for obs in trace["observations"]:
        if obs["id"] == "ab4de2f703620df1":
            obs["output"][0]["update"]["messages"][0]["response_metadata"]["finish_reason"] = "length"
        if obs["type"] == "TOOL":
            obs["level"] = "ERROR"
            obs["statusMessage"] = "HR system timeout"

    issues = clean_trace(trace)["issues"]

    assert {"observation": "model", "type": "CHAIN", "finish_reason": "length"} in issues
    assert {
        "observation": "get_employee_personal_details",
        "type": "TOOL",
        "level": "ERROR",
        "status_message": "HR system timeout",
    } in issues


def test_cleaning_does_not_mutate_inputs(trace):
    original = copy.deepcopy(trace)
    clean_conversation(SESSION_ID, [trace])
    assert trace == original


def test_render_markdown_is_compact_and_redacted(trace):
    markdown = render_markdown(
        clean_conversation(SESSION_ID, [trace]), base_url="http://langfuse.local"
    )

    assert "## Turn 1 · 2026-10-29T11:59:04.929Z · 2.12s" in markdown
    assert "Trace: http://langfuse.local/project/" in markdown
    assert "### classify" in markdown
    assert "- Agent `route_to_agents`" in markdown
    assert "call `get_employee_personal_details`({})" in markdown
    assert "### awp/self_service_agents/employee_records_agent@8" in markdown
    assert markdown.count("# CPCGPT - Employee Records Agent") == 1
    for noise in ("resourceAttributes", "public_key", "langgraph_path", "abc123"):
        assert noise not in markdown


def test_render_markdown_points_truncated_values_at_the_data_file(trace):
    markdown = render_markdown(
        clean_conversation(SESSION_ID, [trace]),
        max_value_chars=20,
        data_path=".bug-context/conversation.json",
    )

    assert "Values marked [truncated] are complete in .bug-context/conversation.json" in markdown
    assert "chars; full value in .bug-context/conversation.json]" in markdown
