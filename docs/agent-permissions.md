# Agent permissions

How the Agent SDK decides whether a tool call runs, and how the factory uses that in each
phase. Checked against the Claude Code docs in November 2026 (SDK 0.2.174); links at the end.

## Evaluation order

For every tool call the SDK checks, in order:

1. **Hooks** (`hooks=`). A `deny` blocks the call in every mode. An `allow` doesn't skip
   the deny and ask rules below.
2. **Deny rules** (`disallowed_tools=`, settings files). A match blocks the call and
   `can_use_tool` is never called. A bare tool name (`"Edit"`) removes the tool entirely.
3. **Ask rules**. A match goes to `can_use_tool`.
4. **Permission mode** (`permission_mode=`).
5. **Allow rules** (`allowed_tools=`), plus calls that need no approval: reads inside the
   working directories and Claude Code's built-in read-only Bash commands (`ls`, `cat`,
   `grep`, read-only `git`...).
6. **`can_use_tool`**, for anything still undecided.

So a check in `can_use_tool` only sees calls that nothing earlier resolved. A check that
must apply to every call belongs in a PreToolUse hook.

## Plan mode (planning and rework phases)

- The agent reads files, runs shell commands to explore, and writes a plan. It can't edit
  source files.
- **File edits always go to `can_use_tool`**, even when an allow rule or a hook says
  `allow`. That includes the plan itself: the agent writes it with `Write` to
  `~/.claude/plans/<name>.md` (or the `plansDirectory` setting), and our callback allows
  only that directory, plus the issue's `reproduction.json` in its workspace.
- **Shell commands go to the auto mode classifier, not to `can_use_tool`,** when auto mode
  is available to the session and the `useAutoModeDuringPlan` setting is on (the default).
  The classifier approves or blocks each command. It can also approve read-only calls such
  as `WebFetch` that would otherwise reach the callback.
- When auto mode isn't available (an unsupported model or provider) or
  `useAutoModeDuringPlan` is off, commands outside the read-only set reach `can_use_tool`.
- `ExitPlanMode` reaches `can_use_tool`. Without a valid `reproduction.json` the callback
  denies it, without interrupting, and says what's missing. Otherwise the factory copies
  the plan to the workspace, then denies the call with `interrupt=True`, which ends the run
  so a person can review it.
- MCP tools reach `can_use_tool`. The callback allows the browser (`mcp__playwright__*`)
  and the factory's own tools (`mcp__factory__*`, the log search), and denies the rest.
- `AskUserQuestion` reaches `can_use_tool`. Nobody can answer in a headless run, so the
  callback denies it and tells the agent to record its assumption in the plan.
- Reads outside the working directory (the issue's attachments and `conversation.json`
  live in the workspace, not the worktree) reach `can_use_tool`. The callback allows
  `Read`, `Glob` and `Grep` inside the issue's workspace folder.
- Everything else that reaches the callback is denied with a message, so a new or
  unexpected tool fails closed.

The callback is `plan_phase_can_use_tool` in `src/software_factory/main.py`.

### What we observed (haiku, SDK 0.2.174, November 2026)

A live planning session was told to read a workspace file, `Edit` a source file, run
`echo changed > app.txt`, then write its plan and call `ExitPlanMode`:

| Call | Default (`useAutoModeDuringPlan` on) | `useAutoModeDuringPlan: false` |
| :- | :- | :- |
| `Read` of a workspace file (outside the worktree) | Approved before the callback | Reached the callback, allowed |
| `Edit` of a source file | Reached the callback, denied | Reached the callback, denied |
| `echo changed > app.txt` | **Approved by the classifier; the file was overwritten** | Reached the callback, denied |
| `Write` of the plan file | Approved before the callback | Approved before the callback |
| `ExitPlanMode` | Reached the callback | Reached the callback |

So with the default setting, **planning isn't read-only for shell commands**: whatever the
classifier approves runs, writes included. Turning `useAutoModeDuringPlan` off (for example
`ClaudeAgentOptions(settings='{"useAutoModeDuringPlan": false}')`) sends every shell command
outside the read-only set to the callback, at the cost of the planner not being able to run
tests or start the stack.

**Current choice:** the factory keeps the default (classifier on), so the planner can run
tests and start the stack while it investigates. Plan review is the check on what it did.
Revisit this when sandboxing is available, or if planners start changing files.

An `ExitPlanMode` call denied with `interrupt=True` ends the run as
`subtype="error_during_execution"`, `is_error=True`. That's why a planning run's success is
judged by whether the plan was saved, not by the result subtype.

## Models and caching

Planning and rework run on `SF_PLAN_MODEL` (Opus), implementation and fixes on
`SF_IMPLEMENT_MODEL` (Sonnet), in the same resumed session. Each model has its own prompt
cache, so the first implementation request reads the planning transcript uncached. That
costs nothing extra in practice: a person reviews the plan in between, which takes longer
than the cache lives (5 minutes with an API key, 1 hour on a subscription), so the cache
would have expired on the same model too. See
[How Claude Code uses prompt caching](https://code.claude.com/docs/en/prompt-caching).

## Environment

Agents inherit the factory's environment, with `ClaudeAgentOptions.env` layered on top.
The factory passes the issue's slot variables (`docs/running.md`) and blanks its own
credentials, `config.FACTORY_SECRETS`, so an agent can't read the Linear key or the Azure
DevOps PAT.

## Implementation phase

`permission_mode="auto"`: the classifier reviews shell commands and network requests, and
file edits inside the working directory are allowed. There's no `can_use_tool`.

## MCP servers

Planning and implementation get, when configured:

- **`playwright`**: a headless Chrome (`@playwright/mcp`, started with `npx`), with an
  isolated profile and screenshots saved to the issue's `evidence/`.
- **`factory`**: an SDK server running inside the worker, with `query_logs`. It reads Loki
  with the worker's own credentials, which are blanked in the agent's environment.

After implementation the worker runs the reproducing test the agent named, with
`shell=True`, in the worktree and the agent's environment. That's no more than the agent
could run itself.

## Blocking `git push`

The factory pushes after its own checks, so agents may only commit. Every phase has:

- **Deny rules** `Bash(git push *)` and `PowerShell(git push *)`.
- **A PreToolUse hook**, `deny_git_push`, that matches the full command text.

Both are needed because a Bash rule matches the command only as written. `git -C . push`,
`git -c push.default=current push` and `git 'push'` all get past `Bash(git push *)`. The
hook catches those forms. Neither stops a script the agent writes from pushing, because
the agent runs with your Git credentials. Only a sandbox or removing the credentials does
that.

## Sandboxing (not set up yet)

Claude Code's sandbox isolates the filesystem and network for Bash and PowerShell commands
and their child processes. It runs on macOS, Linux and WSL2. **Native Windows isn't
supported**, and commands there run unsandboxed. Options for later, on a Linux VM:

- **Sandbox settings** via `ClaudeAgentOptions(sandbox=...)`: `enabled`,
  `failIfUnavailable: true`, `allowUnsandboxedCommands: false`, and
  `network.allowedDomains` (empty by default). Docker doesn't work inside the sandbox,
  so the stack would need `excludedCommands`, and localhost is blocked by default on Linux.
- **A container per issue**: the worktree mounted in, no network interface, a proxy on
  the host that allows specific domains, and no Git or SSH credentials inside.

## Sources

- [Configure permissions](https://code.claude.com/docs/en/permissions)
- [Permission modes](https://code.claude.com/docs/en/permission-modes)
- [Agent SDK: configure permissions](https://code.claude.com/docs/en/agent-sdk/permissions)
- [Sandboxing](https://code.claude.com/docs/en/sandboxing)
- [Agent SDK: secure deployment](https://platform.claude.com/docs/en/agent-sdk/secure-deployment)
