# TODO

What's still open after the review work on `claude/software-factory-review-99836a`. How
the factory works today is in `docs/running.md` and `docs/agent-permissions.md`.

## Outside the code

- [ ] **Dev deploys fail on the build agent.** Pulls from ghcr.io time out (builds 8872,
      8873) and the agent's disk is full (8874 onwards). Until both are fixed, merged issues
      wait at Pending Deployment (BEI-13, BEI-15 today).
- [ ] **`dev-awp-pr-check` hasn't run on PRs since 20 October** (last: PR 5476). Without
      it, PRs that merge cleanly but break the build go unnoticed.
- [ ] **Move BEI-12 to In Review in Linear by hand** (PR 5534). It merged before the deploy
      flow existed, and its In Review move failed because the state was missing then.
- [ ] **Remove the hard-coded ERP bearer token** from the target's main checkout:
      `packages/cpc_erp_client/src/cpc_erp_client/client.py` has an uncommitted change
      that sends a fixed JWT, which looks like a real person's, instead of `token`. Worktrees
      don't have it, but it's a secret in the working tree and anything run from that
      checkout calls ERP as that person.
- [ ] **Let the factory read Loki,** through Grafana (a service account token with the
      Viewer role) or with network access to Loki, then set `SF_LOKI_URL`, `LOKI_TOKEN` and
      `env` in `SF_LOKI_SELECTOR`. The logs are shipped (Alloy, 3 November).
- [ ] **Log rotation on the VM.** The compose files have no `logging:` rotation, so the
      VM's Docker logs grow until the disk fills.
- [ ] **Merge the prompt override hook in the target** (branch `feat/prompt-override-hook`):
      `PROMPT_OVERRIDE_DIR` in `agents/prompts.fetch_prompt`. Until it's on dev, replays
      and the browser check use Langfuse's prompts, not the worktree's `prompt-changes/`.
- [ ] **A test employee in UAT ERP for the test account.** The `AWP_TEST_*` accounts have
      no ERP record, so the slots serve ERP fixtures (`SF_SLOT_ERP_MOCK`). Bugs that
      depend on real ERP data won't reproduce until the ERP team maps a test employee to
      the test Keycloak account.

## Not built yet

- [ ] **Changes in Linear after an issue is queued.** Re-read the issue before each job:
      cancelled or duplicate means stop and release the slot; an edited description means
      plan again.
- [ ] **Duplicate bugs are planned in parallel.** Give the planner the titles and plan
      summaries of issues already in flight, so it can say "same root cause as BEI-12".
- [ ] **Track prompt fixes to apply in Langfuse.** A merged PR with `prompt-changes/*.md`
      should show up as "prompt to apply" until someone has.
- [ ] **Overlap warning noise.** Nearly every plan mentions
      `docs/agents/running-the-app.md`, so the warning flags every other issue. Ignore files
      that plans only read.

## Deferred on purpose

- [ ] **Sandbox and network limits for agents**, when this moves to a Linux VM (native
      Windows has no sandbox). Options are in `docs/agent-permissions.md`.
- [ ] **Planning can change files through shell commands** while `useAutoModeDuringPlan`
      is on; kept so planners can run tests. Revisit with the sandbox.
- [ ] **Review gate** (`review_and_gate` in `main.py`): restored but disabled in
      `implement_and_review` until its behaviour has been checked.

## Built, not yet run for real

- [ ] **Reproduce, verify, show evidence** (`docs/running.md`): `reproduction.json` and
      the not-reproduced gate with the QA question, `$SF_REPLAY`, the browser, the failing
      test first, `verifying-fix` with the factory's re-run, the before/after table in the
      PR and on the plan page, and Reproduced/Checked in the QA comment. Tested with fakes
      only; the first real chat bug and the first UI bug are the real tests. Check that
      replays work on a slot's database and that agents can sign in through the IdP headless.
- [ ] **Logs and `query_logs`.** Tested against a fake Loki; waits on the logs being
      shipped.
- [ ] **Metrics** (`/metrics`): the counts depend on event texts the pipeline writes; check
      them against a month of real history.
- [ ] **Root cause and fix in the Linear QA comment.** The implementer's structured output
      hasn't run in a real implementation yet.
- [ ] **The full deploy flow:** merge → Pending Deployment → In Review with the comment.
      It can't complete until a dev deploy succeeds.
- [ ] **Reviewer comments on the PR** (`/factory fix`, `/factory replan`). Tested against a
      fake Azure DevOps only. Check on a real PR that thread replies land under the right
      comment, that `commentType` and `publishedDate` read as expected on our server, and
      what our reviewers' `uniqueName` looks like, for `SF_PR_REVIEWERS`.
- [ ] **Refresh plan.** Not yet run on a real plan. BEI-26 and BEI-27 are in review with
      BEI-20 and BEI-25 merged since; a refresh of either is the first real test.

## Before others use the web app

- [ ] **HTTPS in front of the web app:** Basic auth sends passwords readably over plain HTTP.
- [ ] **Langfuse over HTTPS:** its token travels over plain HTTP on the internal network.
