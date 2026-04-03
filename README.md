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
git clone https://github.com/Matthew-IDKA/signal-mcp.git
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

## Running Tests

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT -- see [LICENSE](LICENSE).
