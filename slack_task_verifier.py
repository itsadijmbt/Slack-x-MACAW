"""
slack_task_verifier.py -- SlackTaskVerifier

Deterministic PEP verifier that reads an outbound Slack message and stamps three facts the
MAPL policy gates on, so MACAW can govern one agent tasking the in-workspace Claude agent
(which acts on GitHub OUTSIDE MACAW). Stamps ONLY on the send tools, never on reads.

  tasks_claude  = yes | no            -- message mentions <@CLAUDE_ID> or is a DM to it
  github_intent = read | write | destructive | none   -- verb classifier on normalized text
  repo_scope    = public | private | none              -- LIVE GitHub visibility (cached,
                                                          fail-closed) + denied-orgs block

This verifier ONLY stamps; the MAPL policy decides, and it DEFAULTS TO APPROVAL:
  repo_scope == private                   -> DENY     (un-rephrasable: private/denied repo)
  tasks_claude == yes AND intent != read  -> APPROVAL (human reviews the exact message, which
                                             rides on the invocation -- so a synonym-evaded
                                             destructive task, classed 'none', still hits a human)
  intent == read                          -> ALLOW
  normal send (tasks_claude == no)        -> APPROVAL
The intent label (read/write/destructive) is context for the approver; only READ vs not-read
changes the verdict, so evasion lands in APPROVAL, never a silent ALLOW.

The destructive-operation set is grounded in real sources, not guessed:
  - GitHub repository "Danger Zone" irreversible actions: delete repository, transfer
    ownership, change visibility, archive.                      (docs.github.com)
  - github-mcp-server destructiveHint tools: delete_file, delete_repository,
    create_or_update_file (overwrite), push_files, merge_pull_request, label_write(delete).
                                                                (github.com/github/github-mcp-server)
  - Git-level destructive pushes: force-push / --force / --mirror; history rewrite
    (reset --hard, filter-branch).        (github.blog "Block potentially destructive pushes")
  - MCP spec destructiveHint = "operations that can irreversibly destroy or overwrite data".
  Note: `git revert` is additive (inverse commit) -> classified WRITE, not destructive.

Config is user-provided (constructor args OR env), nothing repo-specific hardcoded:
  SLACK_CLAUDE_USER_ID   the Claude agent's Slack user id (default below)
  SLACK_DENIED_ORGS      comma-separated GitHub orgs to always deny (default: macawsecurity)
  SLACK_ALLOWED_REPOS    comma-separated org/repo allow-list (default: none = any PUBLIC repo)
  GITHUB_TOKEN           optional; accurate 3-way visibility. Without it, 404 fails closed.

Verified against SDK 0.9.9.6: Verifier base; self.scope.resource_patterns + compile_patterns();
verify(invocation, context) -> VerificationResult(success, message); invocation.tool_name;
invocation.params (the tool-args dict the policy reads/stamps).
"""
import os
import re
import json
import unicodedata
import urllib.request
import urllib.error

from macaw_client.macaw.security.verification.verifier import Verifier
from macaw_client.macaw.security.verification.result import VerificationResult


DEFAULT_CLAUDE_USER_ID = "U0BR5L6JSHF"
DEFAULT_DENIED_ORGS = {"macawsecurity"}

# ---- intent vocabulary (matched as whole tokens; see _matches) -----------------------
# DESTRUCTIVE: irreversible / data-destroying, grounded in the sources in the module header.
DESTRUCTIVE_WORDS = (
    "delete",                                   # delete repo/branch/file/tag/release/issue/comment/label
    "force push", "force-push", "push -f", "push --force", "--force", "--mirror",
    "overwrite",                                # create_or_update_file overwrite
    "reset --hard", "filter-branch", "filter-repo", "rewrite history",
    "rm -rf", "wipe", "purge",
)
# Danger-Zone / protected ops as (verb-ish, object-ish): a repo name often sits between them.
DZ_RULES = (
    (("transfer",), ("repo", "repository", "ownership")),                # transfer ownership
    (("archive",), ("repo", "repository")),                              # archive repository
    (("make", "change", "set", "switch"), ("private", "public", "visibility")),  # change visibility
    (("merge",), ("main", "master", "prod", "production")),              # merge into a protected base
)
# DDL (schema-defining SQL) -> destructive, matching config.yaml (Create/Drop: false).
DDL_STANDALONE = ("truncate", "migration", "migrate")
DDL_VERBS = ("create", "alter", "drop", "truncate", "add", "rename", "modify")
DDL_OBJECTS = ("table", "tables", "schema", "schemas", "database", "databases",
               "migration", "migrations", "index", "indexes", "indices",
               "column", "columns", "view", "views", "constraint", "constraints")
# WRITE: reversible mutations.
WRITE = ("open a pr", "create a pr", "create pr", "raise a pr", "create a branch",
         "push", "commit", "comment", "make a change", "edit", "add", "fix", "update",
         "modify", "rename", "merge", "revert", "fork")
READ = ("review", "list", "show", "summarize", "summary", "read", "what", "status",
        "explain", "describe", "look at", "check", "find", "search", "who", "how")

COMMON_PAIRS = {"and/or", "read/write", "n/a", "24/7", "he/she", "she/he", "yes/no",
                "either/or", "pass/fail", "input/output", "i/o", "tcp/ip", "on/off"}

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"), None)
_REPO_RE = re.compile(r"[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*")


def _normalize(s):
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFKC", s).translate(_ZERO_WIDTH)
    return re.sub(r"\s+", " ", s).strip().lower()


def _split_env(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


class SlackTaskVerifier(Verifier):
    def __init__(self, allowed_repos=None, denied_orgs=None, claude_user_id=None,
                 github_token=None, timeout=10.0):
        super().__init__(name="slack_task_verifier")
        self.claude_user_id = (claude_user_id or os.environ.get("SLACK_CLAUDE_USER_ID")
                               or DEFAULT_CLAUDE_USER_ID)
        self.denied_orgs = (denied_orgs if denied_orgs is not None
                            else _split_env("SLACK_DENIED_ORGS", set(DEFAULT_DENIED_ORGS)))
        self.allowed_repos = (allowed_repos if allowed_repos is not None
                              else _split_env("SLACK_ALLOWED_REPOS", None))
        self.github_token = github_token or os.environ.get("GITHUB_TOKEN")
        self.timeout = timeout
        self._cache = {}
        self.scope.resource_patterns = ["*slack_send_message*"]
        self.scope.compile_patterns()

    # ---- pure classifiers ------------------------------------------------------------
    @staticmethod
    def _matches(norm, verbs):
        # whole-token match: a verb must not be flanked by letters, so the noun "commits"
        # does not trip "commit", nor "PRs" the phrase "open a pr".
        return any(re.search(r"(?<![a-z])" + re.escape(v) + r"(?![a-z])", norm) for v in verbs)

    @classmethod
    def _cooccur(cls, norm, rule):
        verbs, objs = rule
        return cls._matches(norm, verbs) and cls._matches(norm, objs)

    def _tasks_claude(self, norm, channel_id):
        return "yes" if (f"<@{self.claude_user_id.lower()}>" in norm
                         or channel_id == self.claude_user_id) else "no"

    @classmethod
    def _intent(cls, norm):
        # destructive first: DDL, Danger-Zone ops, then plain destructive words
        if cls._matches(norm, DDL_STANDALONE):
            return "destructive"
        if cls._matches(norm, DDL_VERBS) and cls._matches(norm, DDL_OBJECTS):
            return "destructive"
        if any(cls._cooccur(norm, r) for r in DZ_RULES):
            return "destructive"
        if cls._matches(norm, DESTRUCTIVE_WORDS):
            return "destructive"
        if cls._matches(norm, WRITE):
            return "write"
        if cls._matches(norm, READ):
            return "read"
        return "none"

    @staticmethod
    def _candidate_repos(norm):
        return {r for r in _REPO_RE.findall(norm) if r not in COMMON_PAIRS}

    # ---- GitHub visibility (side-effecting, cached, fail-closed) ----------------------
    def _visibility(self, org_repo):
        org = org_repo.split("/", 1)[0]
        if org in self.denied_orgs:
            return "private"
        if org_repo in self._cache:
            return self._cache[org_repo]
        vis = self._lookup(org_repo)
        self._cache[org_repo] = vis
        return vis

    def _lookup(self, org_repo):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "macaw-slack-verifier"}
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        req = urllib.request.Request(f"https://api.github.com/repos/{org_repo}", headers=headers)
        for attempt in range(2):                                  # retry once so a transient
            try:                                                  # blip/cold start doesn't fail closed
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return "private" if json.load(r).get("private", True) else "public"
            except urllib.error.HTTPError as e:                   # deterministic -> no retry
                if e.code == 404:
                    # tokened: 404 = not a real repo (skip). tokenless: 404 = private/missing (deny).
                    return "not_a_repo" if self.github_token else "private"
                return "private"                                  # 401/403/5xx -> fail closed
            except Exception:                                     # timeout / network blip
                if attempt == 0:
                    continue                                      # try once more
                return "private"                                  # still failing -> fail closed

    def _repo_scope(self, norm):
        real = []
        for r in self._candidate_repos(norm):
            v = self._visibility(r)
            if v == "not_a_repo":
                continue
            real.append((r, v))
        if not real:
            return "none"
        for r, v in real:
            if v != "public":
                return "private"
            if self.allowed_repos is not None and r not in self.allowed_repos:
                return "private"
        return "public"

    # ---- the PEP hook ----------------------------------------------------------------
    def verify(self, invocation, context):
        # Scope (should_verify) already restricts this to the send tools -- no tool_name guard,
        # so a prefixed resource name (tool:slack-proxy/slack_send_message) cannot bypass it.
        params = invocation.params if invocation.params is not None else {}
        norm = _normalize(params.get("message", ""))
        chan = params.get("channel_id", "")
        tc = self._tasks_claude(norm, chan)
        params["tasks_claude"] = tc
        # Only classify intent/repo for tasks aimed at the Claude agent. A normal send is
        # never blocked by these gates, and no GitHub call is made for it.
        if tc == "yes":
            params["github_intent"] = self._intent(norm)
            params["repo_scope"] = self._repo_scope(norm)
        else:
            params["github_intent"] = "none"
            params["repo_scope"] = "none"
        # The attestation card summarizes the FIRST param; move `message` to the front so the
        # approver sees the task text instead of channel_id. (Revert if the card is unchanged.)
        if "message" in params:
            _m = params.pop("message")
            _rest = dict(params)
            params.clear()
            params["message"] = _m
            params.update(_rest)
        return VerificationResult(
            True,
            f"tasks_claude={tc} intent={params['github_intent']} repo_scope={params['repo_scope']}",
        )


# --------------------------------------------------------------------------------------
def demo():
    """Offline self-check with a pre-seeded visibility cache (no network), plus one live probe.
    Run: python slack_task_verifier.py"""
    import types

    v = SlackTaskVerifier()
    v._cache.update({"itsadijmbt/sic": "public", "itsadijmbt/secret-internal": "private"})

    def run(message, channel_id=""):
        inv = types.SimpleNamespace(tool_name="slack_send_message",
                                    params={"message": message, "channel_id": channel_id})
        v.verify(inv, None)
        return inv.params

    M = f"<@{DEFAULT_CLAUDE_USER_ID}>"

    cases = [
        # (message, channel_id, tasks_claude, github_intent, repo_scope)
        (f"{M} summarize the open PRs on itsadijmbt/sic", "", "yes", "read", "public"),
        (f"{M} open a PR on itsadijmbt/sic fixing the null check", "", "yes", "write", "public"),
        (f"{M} revert the last commit on itsadijmbt/sic", "", "yes", "write", "public"),      # revert = write (safe)
        (f"{M} push a change to macawsecurity/prod", "", "yes", "write", "private"),          # denied org
        (f"{M} review itsadijmbt/secret-internal", "", "yes", "read", "private"),             # private repo
        ("hey team standup at 3pm", "", "no", "none", "none"),                                 # normal send
        ("delete the old file please", "", "no", "none", "none"),                              # normal send, NOT blocked
        (f"{M} do the read/write refactor", "", "yes", "read", "none"),                        # common-pair skipped
        ("do it now", DEFAULT_CLAUDE_USER_ID, "yes", "none", "none"),                          # DM to Claude
        # --- real destructive ops (Danger Zone / MCP destructiveHint / git force-push) ---
        (f"{M} delete the itsadijmbt/sic repository", "", "yes", "destructive", "public"),     # delete repo
        (f"{M} delete the config file in itsadijmbt/sic", "", "yes", "destructive", "public"), # delete_file
        (f"{M} force push to itsadijmbt/sic", "", "yes", "destructive", "public"),             # force-push
        (f"{M} transfer ownership of itsadijmbt/sic to acme", "", "yes", "destructive", "public"),  # transfer
        (f"{M} archive the itsadijmbt/sic repository", "", "yes", "destructive", "public"),    # archive
        (f"{M} make the itsadijmbt/sic repo private", "", "yes", "destructive", "public"),     # visibility change
        (f"{M} run the migration on itsadijmbt/sic", "", "yes", "destructive", "public"),      # DDL
        (f"{M} drop me a quick note", "", "yes", "none", "none"),                              # "drop" w/o DB obj: NOT DDL
    ]
    for msg, chan, et, ei, er in cases:
        p = run(msg, chan)
        assert p["tasks_claude"] == et, (msg, "tasks_claude", p["tasks_claude"], "!=", et)
        assert p["github_intent"] == ei, (msg, "intent", p["github_intent"], "!=", ei)
        assert p["repo_scope"] == er, (msg, "repo_scope", p["repo_scope"], "!=", er)
        print(f"  OK  tasks_claude={p['tasks_claude']:<3} intent={p['github_intent']:<11} "
              f"repo={p['repo_scope']:<7} | {msg[:50]}")

    print("\nall offline cases passed.\n--- live GitHub probe (itsadijmbt/sic) ---")
    live = SlackTaskVerifier()
    print("  itsadijmbt/sic ->", live._visibility("itsadijmbt/sic"),
          "(token:", "yes" if live.github_token else "no", ")")


if __name__ == "__main__":
    demo()
