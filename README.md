# Slack × MACAW

> Slack's hosted MCP server, fronted by MACAW.
> One static `xoxp` token in, one MAPL policy on top.
> Reads and public search stay open.
> Every outbound message stops for your approval.
> Scheduled sends and private-channel search: denied.
> The Slack credential never leaves the proxy.

---

## Part A — Slack app (one-time, in the Slack UI)

Slack's hosted MCP does **not** support Dynamic Client Registration, so you bring
your own pre-registered app.

1. **Create the app:** https://api.slack.com/apps?new_app=1
   → From scratch → name it (e.g. `MACAW-test`) → pick your test workspace.

2. **OAuth & Permissions → Redirect URLs:**
   → Add New Redirect URL: `http://localhost:8080/callback`
   (http, **not** https; no trailing slash — must match exactly)
   → **Save URLs** ← easy to miss; the URL doesn't count until saved.

3. **OAuth & Permissions → Scopes → USER Token Scopes** (not Bot token scopes;
   the user flow mints an `xoxp` user token). Add:
   ```
   chat:write
   channels:read        channels:history
   groups:read          groups:history
   im:read              im:history
   mpim:read            mpim:history
   users:read           users:read.email
   search:read.public   search:read.private
   ```

4. **Enable MCP server access for the app:**
   `https://api.slack.com/apps/<APP_ID>/app-assistant`
   Without this, `mcp.slack.com` rejects the token even though it is valid.

5. **Basic Information → App Credentials** — copy the Client ID and Client Secret.

---

## Part B — mint the token (one-time)

```bash
cd <this-dir>
export SLACK_CLIENT_ID=<Client ID from step 5>
export SLACK_CLIENT_SECRET=<Client Secret from step 5>
python get_token.py
```

A browser opens Slack's consent screen → pick workspace → **Allow**.
The script catches the `localhost:8080` redirect, exchanges the code, and prints
an `xoxp-` token. Copy it.

---

## Part C — wire the proxy

The token is read from the environment — never hardcoded. Run:

```bash
source <venv>/bin/activate
export MACAW_HOME="<macaw-client-dir>"
export SLACK_XOXP_TOKEN="xoxp-...."     # the token from Part B
python slack-MACAW.py
```

On start it connects to `mcp.slack.com` and logs the discovered tool count.
That tool surface is what the MAPL policy is written against.

---

## Part D — register with Claude Code (optional)

```bash
claude mcp add slack-MACAW --scope user \
  -- bash -lc 'source <venv>/bin/activate && \
     export MACAW_HOME="<macaw-client-dir>" && \
     export SLACK_XOXP_TOKEN="xoxp-...." && \
     cd <this-dir> && python slack-MACAW.py'
```
