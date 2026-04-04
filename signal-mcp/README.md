# signal-mcp

MCP server that bridges [Claude Code](https://claude.ai/claude-code) to [Signal Messenger](https://signal.org/) via [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api).

Operates as a **Claude Code Channel** -- it polls for inbound Signal messages over WebSocket and delivers them as channel notifications, so Claude Code can read and respond to Signal conversations in real time.

## Prerequisites

- Python 3.11+
- A running [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) instance (json-rpc mode)
- A registered Signal bot number linked to signal-cli-rest-api

## Installation

```bash
# Clone and install
git clone https://github.com/YOUR_ORG/signal-mcp.git
cd signal-mcp
pip install .

# Or install in development mode
pip install -e ".[dev]"
```

## Configuration

All configuration is via environment variables.

### Required

| Variable | Description |
|---|---|
| `SIGNAL_API_URL` | Base URL of your signal-cli-rest-api instance (e.g., `http://localhost:8093`) |
| `SIGNAL_BOT_NUMBER` | Phone number registered with signal-cli (e.g., `+15551234567`) |
| `SIGNAL_CHANNEL_TYPE` | `dm` for direct messages or `group` for a group chat |
| `SIGNAL_CHANNEL_ID` | Recipient phone number (for `dm`) or group ID (for `group`) |

### Optional

| Variable | Default | Description |
|---|---|---|
| `SIGNAL_ALLOWED_SENDERS` | *(empty -- all senders)* | Comma-separated phone numbers. Only messages from these senders are forwarded to Claude Code. Recommended for security. |
| `SIGNAL_APPROVAL_SENDERS` | *(empty -- disabled)* | Comma-separated phone numbers authorized to approve/deny tool permission requests via Signal. |
| `SIGNAL_POLL_INTERVAL` | `2` | Seconds between WebSocket reconnect attempts (used during backoff). |

## Claude Code Integration

Register the server with Claude Code:

```bash
claude mcp add signal-mcp \
  -e SIGNAL_API_URL=http://localhost:8093 \
  -e SIGNAL_BOT_NUMBER=+15551234567 \
  -e SIGNAL_CHANNEL_TYPE=dm \
  -e SIGNAL_CHANNEL_ID=+15559876543 \
  -e SIGNAL_ALLOWED_SENDERS=+15559876543 \
  -- signal-mcp
```

Or use an MCP config file (see `mcp-config.example.json`).

## Tools

| Tool | Description |
|---|---|
| `reply` | Send a text message to the current Signal channel |
| `fetch_messages` | Diagnostic check for channel connectivity (messages arrive automatically via WebSocket) |
| `send_attachment` | Send a file (up to 95 MB) to the channel |
| `download_attachment` | Download a received attachment to local disk |
| `react` | React to a message with an emoji |
| `send_typing` | Show or hide the typing indicator |
| `list_groups` | List Signal groups the bot belongs to |
| `get_contacts` | List contacts known to the bot's Signal account |

## How It Works

```
Signal app  -->  signal-cli-rest-api  --WebSocket-->  signal-mcp  --Channel notification-->  Claude Code
Claude Code  --tool call-->  signal-mcp  --HTTP POST-->  signal-cli-rest-api  -->  Signal app
```

Unlike a typical request/response MCP server, signal-mcp runs a **persistent WebSocket listener** alongside the MCP stdio transport. Inbound Signal messages arrive over the WebSocket, are filtered through the sender allowlist, and emitted as `notifications/claude/channel` events that Claude Code renders inline.

Outbound communication flows through MCP tool calls (`reply`, `react`, etc.), which signal-mcp translates to signal-cli-rest-api HTTP requests.

### Permission Relay

When `SIGNAL_APPROVAL_SENDERS` is configured, Claude Code tool permission prompts are forwarded to Signal. Approved senders can reply with `y <id>` or `n <id>` to allow or deny tool execution remotely.

## Channel Routing

signal-mcp uses **static routing**: every outbound message (`reply`, `react`, `send_attachment`) is always delivered to the channel configured at startup via `SIGNAL_CHANNEL_TYPE` and `SIGNAL_CHANNEL_ID`. The server does not dynamically route replies based on where an inbound message originated.

### Expected behavior: same bot number across multiple contexts

If the bot's phone number is a member of both a group and a DM thread, inbound messages from both contexts will be received — but **all replies go to the configured channel only**, regardless of whether the triggering message arrived from a DM or a group. This is not a bug; it is the expected consequence of static routing.

### Recommended pattern: one session per channel

The simplest way to support multiple channels simultaneously is one Claude Code session per channel, each launched from its own directory containing a channel-specific `.mcp.json`:

```
signal-sandbox/
  DM/
    .mcp.json        # SIGNAL_CHANNEL_TYPE=dm,    SIGNAL_CHANNEL_ID=+1XXXXXXXXXX
  Group-A/
    .mcp.json        # SIGNAL_CHANNEL_TYPE=group, SIGNAL_CHANNEL_ID=group.<id>
  Group-B/
    .mcp.json        # SIGNAL_CHANNEL_TYPE=group, SIGNAL_CHANNEL_ID=group.<id>
```

Each session is isolated: it only processes messages from allowed senders and always replies to its own configured channel. Launch each session with a channel-specific script (`cd` to the appropriate subdirectory before starting Claude Code).

### Roadmap: dynamic routing (Option A)

A future enhancement could support **dynamic routing**, where replies are directed to whichever channel the triggering message arrived from. This would require:

1. **Source channel tracking** — extract `dataMessage.groupInfo.groupId` from the inbound WebSocket envelope to distinguish DM vs. group messages. Store the active source as session state (`_active_channel`).

2. **`_get_reply_target()` helper** — resolve the reply destination from `_active_channel` at tool-call time, falling back to `cfg["channel_id"]` if no message has been received yet in the session.

3. **Updated tool implementations** — `reply`, `react`, `send_attachment`, and `send_typing` call `_get_reply_target()` instead of reading `cfg["channel_id"]` directly.

4. **`SIGNAL_ROUTING_MODE` env var** — a toggle between `static` (current behavior, default) and `dynamic` (source-based routing), making the two modes mutually exclusive and admin-selectable per deployment:

   ```json
   "SIGNAL_ROUTING_MODE": "dynamic"
   ```

   In `static` mode, `SIGNAL_CHANNEL_ID` and `SIGNAL_CHANNEL_TYPE` are authoritative as today. In `dynamic` mode, `SIGNAL_CHANNEL_ID` acts as a fallback only (used before the first inbound message arrives).

Dynamic routing is a focused addition (~100 lines of code including tests) — contributions welcome.

## Running Tests

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT -- see [LICENSE](LICENSE).
