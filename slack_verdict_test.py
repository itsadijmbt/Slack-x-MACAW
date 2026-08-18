"""
slack_verdict_test.py -- red-team verdict table for the app:slack-proxy MAPL policy.

Runs each denied tool + each constraint probe through the proxy's PEP and prints
ALLOW/DENY vs expected. Denied-resource probes should be blocked at the PEP before
reaching Slack.

Prereq: server_policy_v0.1.0.json must be uploaded to the MACAW console for the
tenant whose api_key is in $MACAW_HOME/.macaw/config.json, then synced. Run:

    source ../tvenv/bin/activate
    export MACAW_HOME=".../macaw-client-0.9.9.6-Linux-x86_64-py3.12"
    python slack_verdict_test.py

Safety: probes use an invalid channel id, so even if the policy were NOT active,
a send/draft could not actually deliver to the workspace (upstream rejects it).
"""
import os
import sys
import logging

import httpx as _httpx
from macaw_adapters.mcp import SecureMCPProxy

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

SLACK_MCP_URL = "https://mcp.slack.com/mcp"
SLACK_XOXP_TOKEN = os.environ.get("SLACK_XOXP_TOKEN", "")   # export SLACK_XOXP_TOKEN=xoxp-...

BAD_CHANNEL = "C0000000000"        # non-existent: no send can deliver even if allowed
TS = "1700000000.000100"


def _timed_create_http_client(self):
    ua = self.upstream_auth
    headers = {}
    if getattr(ua, "type", None) == "bearer" and getattr(ua, "token", None):
        headers["Authorization"] = f"Bearer {ua.token}"
    return _httpx.AsyncClient(
        headers=headers or None,
        timeout=_httpx.Timeout(connect=60, read=600, write=30, pool=30),
    )
SecureMCPProxy._create_http_client = _timed_create_http_client

proxy = SecureMCPProxy(
    app_name="slack-proxy",
    upstream_url=SLACK_MCP_URL,
    upstream_auth={"type": "bearer", "token": SLACK_XOXP_TOKEN},
)

# (label, tool, params, expected)
PROBES = [
    ("denied-resource: slack_send_message",             "slack_send_message",             {"channel_id": BAD_CHANNEL, "message": "probe"}, "DENY"),
    ("denied-resource: slack_schedule_message",         "slack_schedule_message",         {"channel_id": BAD_CHANNEL, "message": "probe", "post_at": 9999999999}, "DENY"),
    ("denied-resource: slack_search_public_and_private","slack_search_public_and_private",{"query": "probe"}, "DENY"),
    ("constraint: draft message secret (xoxb-)",        "slack_send_message_draft",       {"channel_id": BAD_CHANNEL, "message": "leak xoxb-999"}, "DENY"),
    ("constraint: draft message > 4000 chars",          "slack_send_message_draft",       {"channel_id": BAD_CHANNEL, "message": "a" * 4001}, "DENY"),
    ("constraint: draft bad channel_id (space)",        "slack_send_message_draft",       {"channel_id": "C 12", "message": "hi"}, "DENY"),
    ("constraint: search limit 50 (>20)",               "slack_search_public",            {"query": "probe", "limit": 50}, "DENY"),
    ("constraint: search sort=banana",                  "slack_search_public",            {"query": "probe", "sort": "banana"}, "DENY"),
    ("constraint: read_channel format=verbose",         "slack_read_channel",             {"channel_id": BAD_CHANNEL, "response_format": "verbose"}, "DENY"),
    ("constraint: read_thread bad message_ts",          "slack_read_thread",              {"channel_id": BAD_CHANNEL, "message_ts": "not-a-ts"}, "DENY"),
    ("allow: search_public valid",                      "slack_search_public",            {"query": "probe", "limit": 5}, "ALLOW"),
    ("allow: read_user_profile self",                   "slack_read_user_profile",        {}, "ALLOW"),
    ("allow: members ids_only",                         "slack_list_channel_members",     {"channel_id": BAD_CHANNEL, "response_format": "ids_only"}, "ALLOW"),
]


def classify(tool, params):
    try:
        proxy.call_tool(tool, params)
        return "ALLOW"
    except Exception as e:
        msg = str(e).lower()
        if isinstance(e, PermissionError) or any(
            k in msg for k in ("denied", "not allowed", "policy", "forbidden", "constraint", "violat")
        ):
            return "DENY"
        return f"ALLOW*({type(e).__name__})"   # policy let it through; upstream errored


def main():
    print(f"\n{'PROBE':<48}{'EXPECT':<8}{'ACTUAL':<22}{'RESULT'}")
    print("-" * 90)
    passed = 0
    for label, tool, params, expect in PROBES:
        actual = classify(tool, params)
        ok = actual.startswith(expect)
        passed += ok
        print(f"{label:<48}{expect:<8}{actual:<22}{'PASS' if ok else 'FAIL'}")
    print("-" * 90)
    print(f"{passed}/{len(PROBES)} probes matched expectation "
          f"(ALLOW* = policy allowed, upstream errored on the fake channel -- still ALLOW).\n")


if __name__ == "__main__":
    main()
