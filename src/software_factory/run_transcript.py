"""What an agent run's transcript (factory_log.Transcript) says, shaped for the transcript page.

The page has two views. Steps reads like the agent's side of a chat: each thing the agent
said, then the tool calls it made before it spoke again, folded into one line. All events
lists every line of the file. Both come from parse().
"""

import difflib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from software_factory import store

PLANNING_PHASES = frozenset({"plan", "rework", "refresh"})
# The CLI reports this for a planning run the worker stopped by refusing ExitPlanMode
# once the plan was saved (main.plan_phase_can_use_tool), so it is the normal ending
PLAN_STOP = "error_during_execution"

# Long outputs stay readable on the page; the raw file has the rest
OUTPUT_CHARS = 20_000
RAW_EVENT_CHARS = 4_000
# Unchanged lines kept around each change in an edit's diff
DIFF_CONTEXT = 2

BROWSER_PREFIX = "mcp__playwright__browser_"


def offset(seconds: float) -> str:
    """'+1:41' from the start of the run; '+1:02:03' past an hour."""
    s = max(0, int(seconds))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"+{h}:{m:02d}:{s:02d}" if h else f"+{m}:{s:02d}"


def span(seconds: float | None) -> str:
    """'0.8s', '16s', '1m02', '1h05': how long something took."""
    if seconds is None:
        return ""
    if seconds < 10:
        return f"{seconds:.1f}s"
    s = round(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}"
    return f"{s // 3600}h{s % 3600 // 60:02d}"


def _times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


@dataclass
class Call:
    """One tool call and its result."""

    id: str
    name: str
    input: dict
    at: datetime
    label: str = ""
    title: str = ""
    kind: str = "other"  # how the page draws it: shell, edit, write, read, browser, agent, other
    output: str = ""
    output_lines: int = 0
    truncated: int = 0
    images: int = 0
    is_error: bool = False
    done_at: datetime | None = None
    diff: list[tuple[str, str]] = field(default_factory=list)  # (" " | "-" | "+" | "…", line)
    added: int = 0
    removed: int = 0
    children: list = field(default_factory=list)  # a subagent's texts and calls
    anchor: str = ""
    t: str = ""

    @property
    def duration(self) -> str:
        return span((self.done_at - self.at).total_seconds()) if self.done_at else ""

    @property
    def path(self) -> str:
        return self.input.get("file_path") or self.input.get("notebook_path") or ""


def _short_path(path: str, bases) -> str:
    """A path relative to the run's worktree, or to the issue workspace or home folder
    with a prefix that says which; else as it is."""
    if not path:
        return ""
    norm = path.replace("\\", "/")
    for base, prefix in bases:
        base = (base or "").replace("\\", "/").rstrip("/")
        if base and norm.lower().startswith(base.lower() + "/"):
            return prefix + norm[len(base) + 1 :]
    return norm


def _humanize(name: str) -> str:
    return name.replace("_", " ").strip().capitalize()


def _first_line(text: str, limit: int = 120) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _describe(call: Call, bases):
    """Set the call's label, title and kind from its tool and input."""
    name, inp = call.name, call.input
    if name in ("Bash", "PowerShell"):
        call.kind, call.label = "shell", name
        call.title = inp.get("description") or _first_line(inp.get("command", ""))
    elif name in ("Edit", "MultiEdit", "NotebookEdit"):
        call.kind, call.label = "edit", "Edit"
        call.title = _short_path(call.path, bases)
    elif name == "Write":
        call.kind, call.label = "write", "Write"
        call.title = _short_path(call.path, bases)
    elif name == "Read":
        call.kind, call.label = "read", "Read"
        call.title = _short_path(call.path, bases)
    elif name in ("Grep", "Glob"):
        call.kind, call.label = "search", name
        where = _short_path(inp.get("path", ""), bases)
        call.title = (inp.get("pattern") or "") + (f" in {where}" if where else "")
    elif name.startswith(BROWSER_PREFIX):
        call.kind, call.label = "browser", "Browser"
        action = name[len(BROWSER_PREFIX) :]
        target = inp.get("element") or inp.get("text") or inp.get("regex") or ""
        call.title = {
            "navigate": f"Open {inp.get('url', '')}".strip(),
            "click": f"Click “{target}”" if target else "Click",
            "type": f"Type into “{target}”" if target else "Type",
            "find": f"Find “{target}”" if target else "Find",
            "snapshot": "Page snapshot",
            "take_screenshot": "Screenshot",
            "run_code_unsafe": "Run code in the page",
            "evaluate": "Run code in the page",
        }.get(action, _humanize(action))
    elif name in ("Task", "Agent"):
        call.kind, call.label = "agent", "Agent"
        call.title = inp.get("description") or _first_line(inp.get("prompt", ""))
    elif name == "TodoWrite":
        call.label, call.title = "To-dos", "Update the to-do list"
    elif name.startswith("mcp__"):
        _, server, tool = (name.split("__", 2) + ["", ""])[:3]
        call.label, call.title = server, _humanize(tool)
    else:
        call.label = name
        text = next((v for v in inp.values() if isinstance(v, str) and v.strip()), "")
        call.title = _first_line(text)
    call.title = call.title or call.label


def _edit_diff(call: Call):
    """The lines an Edit or MultiEdit took out and put in, with a little context."""
    edits = call.input.get("edits") or [call.input]
    for i, edit in enumerate(edits):
        if i:
            call.diff.append(("…", ""))
        old = (edit.get("old_string") or "").splitlines()
        new = (edit.get("new_string") or "").splitlines()
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if tag == "equal":
                same = old[i1:i2]
                if len(same) > 2 * DIFF_CONTEXT + 1:
                    same = same[:DIFF_CONTEXT] + ["…"] + same[-DIFF_CONTEXT:]
                call.diff.extend(("…", "") if line == "…" else (" ", line) for line in same)
                continue
            call.diff.extend(("-", line) for line in old[i1:i2])
            call.diff.extend(("+", line) for line in new[j1:j2])
            call.removed += i2 - i1
            call.added += j2 - j1


def _result_text(content) -> tuple[str, int]:
    """The text of a tool result, and how many images it held."""
    if isinstance(content, str):
        return content, 0
    if not isinstance(content, list):
        return "" if content is None else json.dumps(content, ensure_ascii=False, indent=1), 0
    texts, images = [], 0
    for block in content:
        if isinstance(block, dict) and block.get("type") == "image":
            images += 1
        elif isinstance(block, dict) and "text" in block:
            texts.append(str(block["text"]))
        else:
            texts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(texts), images


def _set_output(call: Call, content, is_error):
    text, call.images = _result_text(content)
    call.is_error = bool(is_error)
    call.output_lines = len(text.splitlines())
    if len(text) > OUTPUT_CHARS:
        call.truncated = len(text) - OUTPUT_CHARS
        text = text[:OUTPUT_CHARS]
    call.output = text


def summarize(calls: list[Call]) -> str:
    """'Edited 2 files, ran 3 commands': what a run of tool calls did."""
    counts: dict[str, int] = {}
    files: dict[str, set] = {}
    order: list[str] = []
    for call in calls:
        key = call.kind if call.kind != "other" else f"other:{call.label}"
        if key not in counts:
            order.append(key)
        counts[key] = counts.get(key, 0) + 1
        if call.kind in ("edit", "write", "read"):
            files.setdefault(key, set()).add(call.path or call.id)
    parts = []
    for key in order:
        n = counts[key]
        if key == "shell":
            parts.append(f"ran {_plural(n, 'command', 'commands')}")
        elif key in ("edit", "write", "read"):
            verb = {"edit": "edited", "write": "wrote", "read": "read"}[key]
            parts.append(f"{verb} {_plural(len(files[key]), 'file', 'files')}")
        elif key == "search":
            parts.append(f"searched {_times(n)}")
        elif key == "browser":
            parts.append(f"used the browser {_times(n)}")
        elif key == "agent":
            parts.append(f"ran {_plural(n, 'agent', 'agents')}")
        else:
            parts.append(f"used {key.split(':', 1)[1]} {_times(n)}")
    text = ", ".join(parts)
    return text[:1].upper() + text[1:]


def _titles(calls: list[Call]) -> str:
    titles = [c.title for c in calls]
    if len(titles) <= 2:
        return ", ".join(titles)
    return f"{titles[0]}, {titles[1]}, and {len(titles) - 2} more"


@dataclass
class Group:
    """The tool calls the agent made between two of its messages."""

    calls: list[Call]

    @property
    def summary(self) -> str:
        return summarize(self.calls)

    @property
    def titles(self) -> str:
        return _titles(self.calls)

    @property
    def errors(self) -> int:
        return sum(c.is_error for c in self.calls)

    @property
    def duration(self) -> str:
        ends = [c.done_at for c in self.calls if c.done_at]
        return span((max(ends) - self.calls[0].at).total_seconds()) if ends else ""


def _event_detail(line: dict, calls: dict[str, Call]) -> str:
    """One line saying what a transcript line is, for the All events view."""
    kind = line.get("type")
    data = line.get("data") if isinstance(line.get("data"), dict) else {}
    if kind == "run":
        return f"{line.get('phase')} run on {line.get('model')}" + (", resuming a session" if line.get("resume") else "")
    if kind == "HookEventMessage":
        return f"{line.get('subtype', '')}: {data.get('hook_name', '')}".strip(": ")
    if kind == "SystemMessage":
        sub = line.get("subtype")
        if sub == "init":
            return f"session {str(data.get('session_id', ''))[:8]}, {len(data.get('tools') or [])} tools"
        if sub == "thinking_tokens":
            return f"about {data.get('estimated_tokens')} thinking tokens"
        if sub == "permission_check_status":
            return f"{data.get('status', '')} {data.get('tool_use_id', '')}".strip()
        return ""
    if kind == "AssistantMessage":
        parts = []
        for block in line.get("content") or []:
            if "text" in block:
                parts.append(_first_line(block["text"], 100))
            elif "name" in block:
                call = calls.get(block.get("id"))
                parts.append(f"{call.label}: {call.title}" if call else block["name"])
            elif "thinking" in block:
                parts.append("thinking" if block.get("thinking") else "thinking (only a signature)")
        return "; ".join(parts)
    if kind == "UserMessage":
        content = line.get("content")
        if isinstance(content, str):
            return _first_line(content, 100)
        parts = []
        for block in content or []:
            if isinstance(block, dict) and "tool_use_id" in block:
                call = calls.get(block["tool_use_id"])
                what = f"{call.label}: {call.title}" if call else "a tool call"
                parts.append(("error from " if block.get("is_error") else "result of ") + what)
            elif isinstance(block, dict) and "text" in block:
                parts.append(_first_line(block["text"], 100))
        return "; ".join(parts)
    if kind == "RateLimitEvent":
        info = line.get("rate_limit_info") or {}
        windows = ((info.get("raw") or {}).get("unifiedWindows") or {})
        used = ", ".join(
            f"{name.replace('_', '-')} {round(w['utilization'] * 100)}%"
            for name, w in windows.items()
            if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float))
        )
        return f"{info.get('status', '')}" + (f"; {used} used" if used else "")
    if kind in ("TaskStartedMessage", "TaskNotificationMessage"):
        return " ".join(str(x) for x in (line.get("status"), line.get("description") or line.get("summary")) if x)
    if kind == "ResultMessage":
        return f"{line.get('subtype')}, {line.get('num_turns')} turns, ${line.get('total_cost_usd') or 0:.2f}"
    if kind == "stderr":
        return _first_line(line.get("line", ""), 140)
    if kind == "error":
        return _first_line(line.get("error", ""), 140)
    if kind == "end":
        return _first_line(str(line.get("outcome", "")), 140)
    return ""


def _structured(result: dict | None) -> dict:
    """The run's structured output (report, verification), if it gave one."""
    if not result:
        return {}
    output = result.get("structured_output")
    if isinstance(output, dict):
        return output
    try:
        output = json.loads(result.get("result") or "")
    except (TypeError, ValueError):
        return {}
    return output if isinstance(output, dict) else {}


def parse(path: Path, workspace: Path | None = None) -> dict:
    """Read a transcript file into what the page draws. workspace is the issue's folder,
    which paths are shown relative to when they're in it."""
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            line = json.loads(raw)
        except ValueError:
            line = {"type": "unreadable", "line": raw}
        if isinstance(line, dict):
            lines.append(line)

    head = next((line for line in lines if line.get("type") == "run"), {})
    cwd = head.get("cwd") or ""
    bases = [(cwd, ""), (str(workspace) if workspace else "", "workspace/"), (str(Path.home()), "~/")]

    def at(line) -> datetime | None:
        try:
            return datetime.fromisoformat(line["at"])
        except (KeyError, TypeError, ValueError):
            return None

    start = at(head) or next((at(line) for line in lines if at(line)), None)

    def since(moment: datetime | None) -> str:
        return offset((moment - start).total_seconds()) if moment and start else ""

    calls: dict[str, Call] = {}
    steps: list[dict] = []  # {"kind": "text"|"tools"|"note"|"plan", ...}
    group: list[Call] = []
    session = {}
    rate = ""
    result = None
    end = None
    errors, stderr = [], []
    texts = 0

    def close_group():
        nonlocal group
        if group:
            steps.append({"kind": "tools", "group": Group(group)})
            group = []

    for line in lines:
        kind, moment = line.get("type"), at(line)
        parent = line.get("parent_tool_use_id")
        owner = calls.get(parent) if parent else None
        if kind == "SystemMessage" and line.get("subtype") == "init":
            session = line.get("data") or {}
        elif kind == "RateLimitEvent" and not rate:
            rate = _event_detail(line, calls)
        elif kind == "AssistantMessage":
            for block in line.get("content") or []:
                if block.get("text", "").strip():
                    if owner:
                        owner.children.append({"kind": "text", "text": block["text"]})
                        continue
                    close_group()
                    texts += 1
                    steps.append({"kind": "text", "text": block["text"], "t": since(moment), "anchor": f"m{texts}"})
                elif "name" in block:
                    call = Call(id=block.get("id", ""), name=block["name"], input=block.get("input") or {}, at=moment or start)
                    _describe(call, bases)
                    if call.kind == "edit":
                        _edit_diff(call)
                    elif call.kind == "write":
                        call.added = len((call.input.get("content") or "").splitlines())
                    call.t = since(call.at)
                    call.anchor = f"c{len(calls) + 1}"
                    calls[call.id] = call
                    if owner:
                        owner.children.append({"kind": "call", "call": call})
                    elif call.name == "StructuredOutput":
                        pass  # the Result card shows it
                    elif call.name == "ExitPlanMode":
                        close_group()
                        steps.append({"kind": "plan", "call": call, "t": call.t, "text": call.input.get("plan") or ""})
                    else:
                        group.append(call)
        elif kind == "UserMessage":
            content = line.get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if "tool_use_id" in block and block["tool_use_id"] in calls:
                    call = calls[block["tool_use_id"]]
                    _set_output(call, block.get("content"), block.get("is_error"))
                    call.done_at = moment
                elif block.get("text", "").strip() and not owner:
                    close_group()
                    if block["text"].startswith("[Request interrupted") and steps and steps[-1]["kind"] == "plan":
                        # The worker refused ExitPlanMode with interrupt=True: it saved the plan and stopped
                        steps[-1]["stopped"] = True
                        continue
                    steps.append({"kind": "note", "text": block["text"], "t": since(moment)})
        elif kind == "ResultMessage":
            result = line
        elif kind == "end":
            end = line
        elif kind == "error":
            errors.append({**line, "t": since(moment)})
        elif kind == "stderr":
            stderr.append(line.get("line", ""))
    close_group()

    events = []
    for i, line in enumerate(lines, 1):
        kind = line.get("type", "")
        sub = line.get("subtype")
        raw = json.dumps(line, ensure_ascii=False, indent=1, default=str)
        events.append(
            {
                "n": i,
                "t": since(at(line)),
                "type": kind + (f" · {sub}" if sub and kind in ("SystemMessage", "HookEventMessage") else ""),
                "detail": _event_detail(line, calls),
                "raw": raw[:RAW_EVENT_CHARS],
                "cut": max(0, len(raw) - RAW_EVENT_CHARS),
                "is_error": kind == "error" or (kind == "ResultMessage" and bool(line.get("is_error"))),
            }
        )

    every_call = list(calls.values())
    tools: dict[str, int] = {}
    for call in every_call:
        label = {"StructuredOutput": "Report", "ExitPlanMode": "Plan"}.get(call.name, call.label)
        tools[label] = tools.get(label, 0) + 1
    error_calls = [c for c in every_call if c.is_error]
    error_tools: dict[str, int] = {}
    for call in error_calls:
        error_tools[call.label] = error_tools.get(call.label, 0) + 1

    last = max((m for m in (at(line) for line in lines) if m), default=None)
    usage = (result or {}).get("usage") or {}
    return {
        "head": head,
        "prompt": head.get("prompt") or "",
        "model": session.get("model") or head.get("model"),
        "session_id": (result or {}).get("session_id") or session.get("session_id") or head.get("resume"),
        "resumed": bool(head.get("resume")),
        "cwd": cwd,
        "worktree": cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] if cwd else "",
        "rate": rate,
        "steps": steps,
        "outline": [s for s in steps if s["kind"] == "text"],
        "calls": every_call,
        "error_calls": error_calls,
        "error_tools": sorted(error_tools.items(), key=lambda kv: -kv[1]),
        "tools": sorted(tools.items(), key=lambda kv: -kv[1]),
        "result": result,
        "result_t": since(at(result)) if result else "",
        "structured": _structured(result),
        "end": end,
        "errors": errors,
        "stderr": stderr,
        "events": events,
        "start": start,
        "last": last,
        "elapsed": span((last - start).total_seconds()) if last and start else "",
        "duration": span((result or {}).get("duration_ms", 0) / 1000) if result and result.get("duration_ms") else None,
        "api_duration": span(result["duration_api_ms"] / 1000) if result and result.get("duration_api_ms") else None,
        "usage": {
            "output": usage.get("output_tokens"),
            "thinking": (usage.get("output_tokens_details") or {}).get("thinking_tokens"),
            "cache_read": usage.get("cache_read_input_tokens"),
            "cache_write": usage.get("cache_creation_input_tokens"),
            "input": usage.get("input_tokens"),
        },
        "texts": texts,
    }


def _when(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def plan_saved(run, events) -> bool:
    """Whether a planning run ended by saving its plan, which the CLI reports as an error.

    The worker moves the issue to plan ready inside the ExitPlanMode check, before it stops
    the run, so an event to PLAN_READY while the run was going is the sign.
    """
    if run["phase"] not in PLANNING_PHASES or run["error"] or run["subtype"] != PLAN_STOP:
        return False
    started, ended = _when(run["started_at"]), _when(run["ended_at"])
    if not started or not ended:
        return False
    return any(
        e["to_status"] == store.PLAN_READY and (moment := _when(e["at"])) and started <= moment <= ended
        for e in events
    )


def outcome(run, events) -> tuple[str, str]:
    """(kind, label) a run's outcome is drawn with, on the issue page and the transcript page."""
    if run["error"]:
        return "attn", "failed"
    if not run["ended_at"]:
        return "run", "running"
    if plan_saved(run, events):
        return "review", "plan saved"
    if run["is_error"]:
        return "attn", (run["subtype"] or "failed").replace("_", " ")
    return "done", (run["subtype"] or "finished").replace("_", " ")


def evidence_image(call: Call, issue_dir: Path, screenshots: list[str]) -> str | None:
    """The evidence screenshot a Read call looked at, if it was one the page can show."""
    if call.kind != "read" or not call.path:
        return None
    name = os.path.basename(call.path.replace("\\", "/"))
    try:
        inside = Path(call.path).resolve().parent == (issue_dir / "evidence").resolve()
    except OSError:
        return None
    return name if inside and name in screenshots else None
