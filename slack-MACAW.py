"""
slack-MACAW -- MACAW-governed gateway in front of Slack's hosted MCP server.

Upstream is the clean hosted endpoint (https://mcp.slack.com/mcp). Auth is a
static xoxp bearer minted once by get_token.py. No OAuth logic lives here --
the proxy only carries the token.

Register with Claude Code:

    claude mcp add slack-MACAW --scope user \
      -- bash -lc 'source ~/finalised-demos/tvenv/bin/activate && \
          export MACAW_HOME="/home/itsadijmbt/finalised-demos/macaw-client-0.9.9.6-Linux-x86_64-py3.12" && \
          cd /home/itsadijmbt/finalised-demos/slack && python slack-MACAW.py'
"""
import os
import sys
import logging

import httpx as _httpx
from macaw_adapters.mcp import SecureMCPProxy, Client


logging.basicConfig(level=logging.INFO, stream=sys.stderr)

SLACK_MCP_URL = "https://mcp.slack.com/mcp"
SLACK_XOXP_TOKEN = os.environ.get("SLACK_XOXP_TOKEN", "")   # export SLACK_XOXP_TOKEN=xoxp-...


# SecureMCPProxy ships a 5s httpx timeout; a remote hosted endpoint needs a
# longer read budget. Patch the client factory -- transport only, not auth.
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
logging.info("slack-proxy: %d tools; serving native clients", len(proxy.list_tools()))

client = Client("slack-macaw-gateway")
bound = proxy.bind_to_user(client.macaw_client)


import macaw_adapters.mcp._endpoint as _endpoint

_StubClient = _endpoint.Client


def _bound_stub_client(name):
    stub = _StubClient(name)
    stub.macaw_client = bound.user_client
    return stub


_endpoint.Client = _bound_stub_client

transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
proxy.run(transport=transport, port=port)
