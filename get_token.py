#!/usr/bin/env python3
"""
get_token.py -- one-time OAuth helper that mints a static xoxp user token the
hosted Slack MCP endpoint (https://mcp.slack.com/mcp) accepts as a Bearer.

You run this once. It opens Slack's consent screen in your browser; you click
Allow; it catches the redirect on localhost:8080, exchanges the code, and prints
the xoxp- token. Paste that token into the proxy script.

The hosted endpoint does NOT support Dynamic Client Registration, so you bring
your own pre-registered Slack app. Do this in the Slack UI first:

  1. Create an app from scratch:      https://api.slack.com/apps?new_app=1
  2. OAuth & Permissions -> Redirect URLs -> add:  http://localhost:8080/callback
  3. Add the User Token Scopes listed in USER_SCOPES below.
  4. Turn MCP ON:  https://api.slack.com/apps/<APP_ID>/app-assistant
                   (enable "MCP server access" -- the step everyone misses; it's
                    what lets this app's xoxp token reach mcp.slack.com)
  5. Copy Client ID + Client Secret, then:

       export SLACK_CLIENT_ID=...
       export SLACK_CLIENT_SECRET=...
       python get_token.py

xoxp user tokens do not expire unless the app enables token rotation -- leave
rotation OFF so the proxy's static token stays valid.
"""
import os
import sys
import json
import webbrowser
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

CLIENT_ID = os.environ.get("SLACK_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET", "")
REDIRECT = "http://localhost:8080/callback"

# Starter scope set. Every tool the hosted MCP exposes maps to one of these;
# trim to least-privilege once the demo's tool surface is fixed.
USER_SCOPES = ",".join([
    "chat:write",
    "channels:read", "channels:history",
    "groups:read", "groups:history",
    "im:read", "im:history",
    "mpim:read", "mpim:history",
    "users:read", "users:read.email",
    "search:read.public", "search:read.private",
])

# Verified-working (classic) flow. If mcp.slack.com rejects the resulting token,
# swap these for Slack's documented user-token variant:
#   AUTHORIZE = "https://slack.com/oauth/v2_user/authorize"
#   TOKEN_URL = "https://slack.com/api/oauth.v2.user.access"
AUTHORIZE = "https://slack.com/oauth/v2/authorize"
TOKEN_URL = "https://slack.com/api/oauth.v2.access"

if not CLIENT_ID or not CLIENT_SECRET:
    sys.exit("Set SLACK_CLIENT_ID and SLACK_CLIENT_SECRET env vars first (see header).")

authorize_url = AUTHORIZE + "?" + urllib.parse.urlencode({
    "client_id": CLIENT_ID,
    "user_scope": USER_SCOPES,
    "redirect_uri": REDIRECT,
})

_result = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _result["code"] = params.get("code", [None])[0]
        _result["error"] = params.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Done. Close this tab and return to the terminal.")

    def log_message(self, *_):  # silence the default request logging
        pass


def exchange(code):
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "redirect_uri": REDIRECT,
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data)) as r:
        return json.load(r)


def main():
    print("Opening Slack's consent screen. If it doesn't open, paste this URL:\n")
    print(authorize_url, "\n")
    webbrowser.open(authorize_url)

    srv = HTTPServer(("localhost", 8080), Handler)
    print("Waiting for the redirect on", REDIRECT, "...")
    while "code" not in _result:
        srv.handle_request()

    if _result.get("error"):
        sys.exit("Slack returned an error: " + _result["error"])
    code = _result.get("code")
    if not code:
        sys.exit("No authorization code in the redirect. Did you click Allow?")

    print("Exchanging code for token...")
    resp = exchange(code)
    if not resp.get("ok"):
        sys.exit("Token exchange failed: " + json.dumps(resp))

    token = (resp.get("authed_user") or {}).get("access_token", "")
    if not token.startswith("xoxp-"):
        sys.exit("Expected an xoxp- user token, got: " + json.dumps(resp)[:400])

    print("\n=== SUCCESS -- static bearer for mcp.slack.com ===")
    print(token)
    print("\nPaste this as SLACK_XOXP_TOKEN in the proxy script.")


if __name__ == "__main__":
    main()
