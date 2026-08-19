# Slack × MACAW — the demo (one place)

## The thesis

Your Databricks demo proved: *a pattern language (CEL) can't parse SQL — a deterministic
verifier can.* This demo is the same shape, one level up:

> **A pattern language (MAPL glob + string conditions) can't understand a natural-language
> task sent to a downstream agent — a MACAW verifier can.**

The twist Slack adds that SQL didn't: the in-workspace **Claude agent acts on GitHub
*outside* MACAW's enforcement** (it opens PRs, works on repos). So Slack is a **command bus
into an ungoverned autonomous agent**, and nobody governs that boundary today. This demo is
**MACAW governing agent-to-agent delegation** — allow safe tasks, gate risky ones, block
dangerous ones, *by understanding what is being asked*.

---

## The architecture

```
  AI agent (Claude Code)                    the in-workspace
        │  slack_send_message                Claude Slack agent
        ▼                                          ▲
  ┌───────────────┐   governs the      ┌───────────┴────────────┐
  │ SecureMCPProxy│   tasking boundary │  acts on GitHub OUTSIDE │
  │   (MACAW)     │───────────────────▶│  MACAW (opens PRs, etc.)│
  └───────┬───────┘                    └────────────────────────┘
          │ verifier stamps + MAPL policy gates
          ▼
   mcp.slack.com  (hosted Slack MCP, static xoxp bearer)
```

MACAW sits on the **send** path. When one agent tries to task the Claude agent via Slack,
MACAW's verifier reads the task, and the policy decides: allow / approve / deny.

---

## The verifier — `SlackTaskVerifier`

Deterministic Python at the PEP (proven Neon stamp pattern). Scoped to the send tools via
`scope.resource_patterns = ["*slack_send_message*"]`; guards on `invocation.tool_name` so it
**only stamps on sends, never reads** (this alone kills the read-DM over-gate). It normalizes
the message (lowercase, unicode-fold, collapse whitespace) and stamps three fields:

| Stamp | Values | How it's derived |
|---|---|---|
| `tasks_claude` | `yes` / `no` | message contains `<@U0BR5L6JSHF>` **or** `channel_id == U0BR5L6JSHF` |
| `github_intent` | `read` / `write` / `destructive` / `none` | verb classifier on normalized text (review/list → read; create-pr/push/comment → write; delete/force-push/merge-main/deploy → destructive) |
| `repo_scope` | `in` / `out` / `none` | extract `org/repo` tokens, resolve against allow-list `["itsadijmbt/sic"]` |

> **Honest:** `github_intent` is a **deterministic heuristic** (verb/pattern matching), not
> LLM understanding. Far more robust than glob, fully auditable — but clever phrasing can
> still fool it. Don't sell it as "AI understanding." The verifier can also `enforce=True`
> (deny in code) for the absolute cases instead of stamping.

**The bonus:** the stamps make the *policy* trivial and robust. The fragile compound
`NOT (message MATCHES …) AND NOT (channel_id MATCHES …)` collapses to `tasks_claude == 'no'`.

---

## The policy (gates on the stamps)

Evaluation order is `denied_resources → parameters → denied_parameters → attestations`, so
the hard blocks (parameters step) fire **before** any approval.

| Situation | Stamps | Verdict |
|---|---|---|
| Task Claude, destructive action | `github_intent = destructive` | **DENY** (allowed_values `[none,read,write]`) |
| Task Claude, out-of-scope repo | `repo_scope = out` | **DENY** (allowed_values `[none,in]`) |
| Task Claude, write, in-scope repo | `tasks_claude=yes, intent=write, repo=in` | **APPROVAL** (`claude_agent_approval`) |
| Task Claude, read | `tasks_claude=yes, intent=read` | **ALLOW** silently |
| Normal send (not Claude) | `tasks_claude = no` | **APPROVAL** (`send_message_approval`) |
| Any read / search | (verifier didn't stamp) | **ALLOW** |

Attestations become clean:
```
send_message_approval::{params.message != '' AND params.tasks_claude == 'no'}
claude_agent_approval::{params.tasks_claude == 'yes' AND params.github_intent == 'write'}
```
Hard blocks via `allowed_values` on the stamped params (deny at the parameters step):
```
github_intent: allowed_values ["none","read","write"]      # denies "destructive"
repo_scope:    allowed_values ["none","in"]                # denies "out"
```

---

## The live script (the beats)

Drive it through Claude Code (the proxy), **not** by typing in Slack — MACAW only governs
agent sends, not your keystrokes. Each message becomes a `slack_send_message` with the
encoded mention `<@U0BR5L6JSHF>`.

1. **Safe delegation flows.** `@claude summarize the open PRs on itsadijmbt/sic`
   → `read`, `in` → **allowed silently**. Claude replies. *(governed ≠ obstructive)*
2. **Risky delegation is gated.** `@claude open a PR on itsadijmbt/sic fixing the null check`
   → `write`, `in` → **blocks for your approval**. Approve → Claude does it.
3. **Dangerous delegation is blocked.** `@claude delete the main branch of itsadijmbt/sic`
   → `destructive` → **hard-denied before Claude ever sees it.**
4. **Out-of-scope repo is blocked.** `@claude push a change to macawsecurity/prod`
   → `repo_scope = out` → **hard-denied.**
5. **The money shot — glob vs verifier.** `@claude work on MACAW-Security / prod`
   → the old `*macawsecurity*` **glob misses it** (casing + space); the verifier
   **normalizes and catches it**. Same payload, two outcomes. This is the whole thesis in
   one slide.

Every verdict carries its reason in the audit trail (`intent=destructive, repo=out`) —
**deterministic and provable**, not an LLM guessing.

---

## Honest boundaries

- **Stamp-and-gate is proven** (Neon `salary_access`). Beats 1–5 build on it — real.
- **`github_intent` is heuristic**, not NLU. Robust, auditable, bypassable by clever phrasing.
- **Result-side governance** (redacting Claude's *reply* if it leaks a secret) is the dream
  bidirectional version, but the result-processor hook is **untested end-to-end** — aspirational.
- **Enforcement prerequisites** (why you saw "no result" earlier): the policy must be
  **uploaded to the MACAW console** for tenant `mw_47d461eb…58ba`, and an **approver client**
  must run as `user:adibhatt2203@gmail.com` to approve blocked sends. Without both, a gated
  send just hangs.

---

## Build checklist

- [ ] `slack_task_verifier.py` — `SlackTaskVerifier` (scope=send tools, stamps the 3 fields) + self-test
- [ ] `server_policy_v0.5.0.json` — gates on the stamps (tables above); retire the fragile MATCHES conditions
- [ ] `slack_approver.py` — approver client as `adibhatt2203@gmail.com` (needs a JWT for that identity)
- [ ] wire the verifier into `slack-MACAW.py` (`verification_pipeline.add_verifier(...)`)
- [ ] extend `slack_verdict_test.py` with beats 1–5 + the glob-vs-verifier money shot
- [ ] upload policy to console → run the verdict table → record results
