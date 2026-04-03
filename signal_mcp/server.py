"""Signal MCP Server -- bridges Claude Code to Signal via signal-cli-rest-api.

Operates as a Claude Code Channel: declares claude/channel capability,
polls signal-cli-rest-api for inbound messages via WebSocket, and emits
channel notifications. Claude Code tools (reply, react, etc.) handle
outbound communication.
"""

import base64
import os
import re
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

import anyio
import httpx
from mcp.server import FastMCP
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

REQUIRED_ENV = [
    "SIGNAL_API_URL",
    "SIGNAL_BOT_NUMBER",
    "SIGNAL_CHANNEL_TYPE",
    "SIGNAL_CHANNEL_ID",
]


def _load_config() -> dict:
    """Load and validate configuration from environment variables."""
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        msg = f"FATAL: Missing required environment variables: {', '.join(missing)}"
        print(msg, file=sys.stderr)
        sys.exit(1)

    allowed_raw = os.environ.get("SIGNAL_ALLOWED_SENDERS", "")
    allowed = [s.strip() for s in allowed_raw.split(",") if s.strip()]

    approvers_raw = os.environ.get("SIGNAL_APPROVAL_SENDERS", "")
    approvers = [s.strip() for s in approvers_raw.split(",") if s.strip()]

    return {
        "api_url": os.environ["SIGNAL_API_URL"].rstrip("/"),
        "bot_number": os.environ["SIGNAL_BOT_NUMBER"],
        "channel_type": os.environ["SIGNAL_CHANNEL_TYPE"],
        "channel_id": os.environ["SIGNAL_CHANNEL_ID"],
        "allowed_senders": allowed,
        "approval_senders": approvers,
        "poll_interval": int(os.environ.get("SIGNAL_POLL_INTERVAL", "2")),
    }


def _validate_channel_type(channel_type: str) -> None:
    if channel_type not in ("dm", "group"):
        msg = f"FATAL: SIGNAL_CHANNEL_TYPE must be 'dm' or 'group', got '{channel_type}'"
        print(msg, file=sys.stderr)
        sys.exit(1)


async def _check_signal_api(client: httpx.AsyncClient, api_url: str) -> None:
    """Verify signal-cli-rest-api is reachable."""
    try:
        resp = await client.get(f"{api_url}/v1/about", timeout=5)
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.ConnectError) as e:
        print(f"FATAL: Cannot reach signal-cli-rest-api at {api_url}: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Channel notification models
# ---------------------------------------------------------------------------

class ChannelNotificationParams(BaseModel):
    content: str
    meta: dict[str, str] = {}


class ChannelNotification(BaseModel):
    """Custom notification for claude/channel. Bypasses ServerNotification union
    at runtime -- send_notification only calls model_dump(), so any Pydantic
    model with method + params works."""
    method: str = "notifications/claude/channel"
    params: ChannelNotificationParams


class PermissionVerdictParams(BaseModel):
    request_id: str
    behavior: str  # "allow" or "deny"


class PermissionVerdict(BaseModel):
    """Permission verdict sent back to CC when a user approves/denies a tool call."""
    method: str = "notifications/claude/channel/permission"
    params: PermissionVerdictParams


# Regex for parsing permission verdicts from Signal messages: "y abcde" or "n abcde"
VERDICT_PATTERN = re.compile(r"^\s*(y|yes|n|no)\s+([a-km-z]{5})\s*$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Build send payload helper
# ---------------------------------------------------------------------------

def _send_payload(cfg: dict, message: str, attachments: list[str] | None = None) -> dict:
    """Build the JSON payload for POST /v2/send."""
    payload: dict = {
        "message": message,
        "number": cfg["bot_number"],
    }
    if cfg["channel_type"] == "dm":
        payload["recipients"] = [cfg["channel_id"]]
    else:
        payload["recipients"] = []
    if attachments:
        payload["base64_attachments"] = attachments
    return payload


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "signal-mcp",
    instructions=(
        "Messages from Signal Messenger arrive as <channel> tags with sender and "
        "timestamp attributes. Reply using the reply tool. You may also use react, "
        "send_attachment, and send_typing tools for richer interaction."
    ),
)

# Lazy-initialized at first tool call
_config: dict | None = None
_http: httpx.AsyncClient | None = None


async def _get_config() -> dict:
    global _config
    if _config is None:
        _config = _load_config()
        _validate_channel_type(_config["channel_type"])
    return _config


async def _get_client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=30)
    return _http


# ---------------------------------------------------------------------------
# Tool: reply
# ---------------------------------------------------------------------------

@mcp.tool()
async def reply(message: str) -> str:
    """Send a message to the current Signal channel (DM or group)."""
    cfg = await _get_config()
    client = await _get_client()
    payload = _send_payload(cfg, message)
    resp = await client.post(f"{cfg['api_url']}/v2/send", json=payload)
    resp.raise_for_status()
    return f"Message sent (timestamp: {resp.json().get('timestamp', 'unknown')})"


# ---------------------------------------------------------------------------
# Tool: fetch_messages
# ---------------------------------------------------------------------------

@mcp.tool()
async def fetch_messages(count: int = 15) -> str:
    """Check for recent messages from the current Signal channel.

    In Channel mode, inbound messages arrive automatically as <channel>
    notifications -- you do not need to call this tool for normal operation.
    This tool is available for diagnostics or to confirm connectivity.
    """
    return (
        "Messages arrive automatically via Channel notifications (WebSocket). "
        "Check the <channel> tags in your context for recent messages. "
        "If no messages have appeared, verify the WebSocket connection in stderr logs."
    )


# ---------------------------------------------------------------------------
# Tool: send_attachment
# ---------------------------------------------------------------------------

MAX_ATTACHMENT_BYTES = 95 * 1024 * 1024  # 95 MB


@mcp.tool()
async def send_attachment(file_path: str, message: str = "") -> str:
    """Send a file attachment to the current Signal channel. Max file size: 95 MB."""
    path = Path(file_path)
    if not path.is_file():
        return f"Error: file not found: {file_path}"
    if path.stat().st_size > MAX_ATTACHMENT_BYTES:
        return f"Error: file exceeds 95 MB limit ({path.stat().st_size} bytes)"

    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")

    cfg = await _get_config()
    client = await _get_client()
    payload = _send_payload(cfg, message, attachments=[b64])
    resp = await client.post(f"{cfg['api_url']}/v2/send", json=payload)
    resp.raise_for_status()
    return f"Attachment sent: {path.name} (timestamp: {resp.json().get('timestamp', 'unknown')})"


# ---------------------------------------------------------------------------
# Tool: download_attachment
# ---------------------------------------------------------------------------

@mcp.tool()
async def download_attachment(attachment_id: str, save_path: str = "") -> str:
    """Download an attachment from a received message to local disk."""
    cfg = await _get_config()
    client = await _get_client()

    resp = await client.get(f"{cfg['api_url']}/v1/attachments/{attachment_id}")
    resp.raise_for_status()

    if not save_path:
        save_dir = Path(tempfile.gettempdir()) / "signal-attachments"
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = str(save_dir / attachment_id)

    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    Path(save_path).write_bytes(resp.content)
    return f"Attachment saved to {save_path} ({len(resp.content)} bytes)"


# ---------------------------------------------------------------------------
# Tool: react
# ---------------------------------------------------------------------------

@mcp.tool()
async def react(emoji: str, target_sender: str, target_timestamp: int) -> str:
    """React to a message with an emoji."""
    cfg = await _get_config()
    client = await _get_client()

    payload = {
        "reaction": emoji,
        "target_author": target_sender,
        "timestamp": target_timestamp,
    }
    if cfg["channel_type"] == "dm":
        payload["recipient"] = cfg["channel_id"]

    resp = await client.put(
        f"{cfg['api_url']}/v1/reactions/{cfg['bot_number']}",
        json=payload,
    )
    resp.raise_for_status()
    return f"Reacted with {emoji}"


# ---------------------------------------------------------------------------
# Tool: send_typing
# ---------------------------------------------------------------------------

@mcp.tool()
async def send_typing(typing: bool = True) -> str:
    """Show or stop the typing indicator in the current channel."""
    cfg = await _get_config()
    client = await _get_client()

    payload = {"recipient": cfg["channel_id"]} if cfg["channel_type"] == "dm" else {}
    resp = await client.put(
        f"{cfg['api_url']}/v1/typing-indicator/{cfg['bot_number']}",
        json=payload,
    )
    resp.raise_for_status()
    action = "started" if typing else "stopped"
    return f"Typing indicator {action}"


# ---------------------------------------------------------------------------
# Tool: list_groups
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_groups() -> str:
    """List all Signal groups the bot is a member of."""
    cfg = await _get_config()
    client = await _get_client()

    resp = await client.get(f"{cfg['api_url']}/v1/groups/{cfg['bot_number']}")
    resp.raise_for_status()
    groups = resp.json()

    if not groups:
        return "No groups."

    lines = []
    for g in groups:
        name = g.get("name", "unnamed")
        gid = g.get("id", g.get("internal_id", "?"))
        members = len(g.get("members", []))
        lines.append(f"- {name} (id: {gid}, members: {members})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: get_contacts
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_contacts() -> str:
    """List contacts known to the bot's Signal account."""
    cfg = await _get_config()
    client = await _get_client()

    resp = await client.get(f"{cfg['api_url']}/v1/contacts/{cfg['bot_number']}")
    resp.raise_for_status()
    contacts = resp.json()

    if not contacts:
        return "No contacts."

    lines = []
    for c in contacts:
        name = c.get("name", c.get("profile_name", "unknown"))
        number = c.get("number", "?")
        lines.append(f"- {name}: {number}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Channel: inbound message polling
# ---------------------------------------------------------------------------

async def _poll_signal_messages(session: ServerSession, cfg: dict) -> None:
    """Listen for incoming Signal messages via WebSocket, emit channel notifications.

    Runs as a background task alongside the MCP message processor. Connects to
    signal-cli-rest-api's WebSocket endpoint (required in json-rpc mode -- the
    HTTP GET /v1/receive endpoint is WebSocket-only in this mode).

    Messages are non-destructive and buffered server-side (~100 messages).
    Reconnects with exponential backoff on connection loss.
    """
    import json

    import websockets

    allowed = set(cfg["allowed_senders"])
    approvers = set(cfg["approval_senders"])
    # Convert http:// URL to ws:// for WebSocket
    ws_base = cfg["api_url"].replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_base}/v1/receive/{cfg['bot_number']}"
    consecutive_errors = 0

    async def _notify(text: str) -> None:
        """Send a notice to the Signal channel (e.g., rejection messages)."""
        client = await _get_client()
        payload = _send_payload(cfg, text)
        await client.post(f"{cfg['api_url']}/v2/send", json=payload)

    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                print(f"INFO: WebSocket connected to {ws_url}", file=sys.stderr)
                consecutive_errors = 0

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    envelope = msg.get("envelope", {})
                    source = envelope.get("sourceNumber", "")
                    data = envelope.get("dataMessage", {})
                    body = data.get("message", "")

                    if not body:
                        continue

                    # Sender allowlist gate -- prevents prompt injection
                    if allowed and source not in allowed:
                        print(
                            f"WARN: Dropped message from unlisted sender {source}",
                            file=sys.stderr,
                        )
                        continue

                    # Check if this is a permission verdict (e.g., "y abcde")
                    verdict_match = VERDICT_PATTERN.match(body)
                    if verdict_match and approvers:
                        if source not in approvers:
                            print(
                                f"WARN: Permission verdict from non-approver {source}",
                                file=sys.stderr,
                            )
                            try:
                                await _notify(
                                    "Permission denied: only approved users "
                                    "can authorize tool calls."
                                )
                            except httpx.HTTPError:
                                pass
                            continue
                        answer = verdict_match.group(1).lower()
                        request_id = verdict_match.group(2)
                        behavior = "allow" if answer in ("y", "yes") else "deny"
                        verdict = PermissionVerdict(
                            params=PermissionVerdictParams(
                                request_id=request_id,
                                behavior=behavior,
                            )
                        )
                        await session.send_notification(verdict)  # type: ignore[arg-type]
                        print(
                            f"INFO: Permission {behavior} for {request_id} from {source}",
                            file=sys.stderr,
                        )
                        continue

                    ts = str(envelope.get("timestamp", ""))
                    notif = ChannelNotification(
                        params=ChannelNotificationParams(
                            content=body,
                            meta={
                                "sender": source,
                                "timestamp": ts,
                            },
                        )
                    )
                    await session.send_notification(notif)  # type: ignore[arg-type]

        except (
            websockets.ConnectionClosed,
            websockets.InvalidURI,
            OSError,
        ) as e:
            consecutive_errors += 1
            backoff = min(2 ** consecutive_errors, 60)
            print(f"WARN: WebSocket error ({consecutive_errors}): {e}", file=sys.stderr)
            await anyio.sleep(backoff)


# ---------------------------------------------------------------------------
# Channel server runner
# ---------------------------------------------------------------------------

async def run_channel_server() -> None:
    """Run the MCP server as a Claude Code Channel.

    Replaces FastMCP.run(transport="stdio") with a custom runner that:
    1. Declares claude/channel experimental capability
    2. Captures the session reference
    3. Starts message polling alongside MCP message processing
    """
    global _config, _http

    cfg = _load_config()
    _validate_channel_type(cfg["channel_type"])
    _config = cfg

    # FastMCP does not expose a public API for injecting experimental
    # capabilities, so we access the underlying Server instance directly.
    # Pin mcp<2.0 to guard against internal refactors.
    server = mcp._mcp_server

    async with stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options(
            experimental_capabilities={
                "claude/channel": {},
                "claude/channel/permission": {},
            },
        )

        async with AsyncExitStack() as stack:
            _http = await stack.enter_async_context(httpx.AsyncClient(timeout=30))
            perm_http = await stack.enter_async_context(
                httpx.AsyncClient(timeout=10)
            )

            # Register handler for permission request notifications from CC
            async def _handle_permission_request(notification: dict) -> None:
                """Forward tool permission prompts to Signal as messages."""
                params = notification.get("params", {})
                request_id = params.get("request_id", "?????")
                tool_name = params.get("tool_name", "unknown")
                description = params.get("description", "")
                preview = params.get("input_preview", "")

                msg = (
                    f"Permission [{request_id}]\n"
                    f"{tool_name}: {description}\n"
                    f"> {preview}\n"
                    f"Reply: y {request_id} / n {request_id}"
                )
                payload = _send_payload(cfg, msg)
                try:
                    await perm_http.post(f"{cfg['api_url']}/v2/send", json=payload)
                except httpx.HTTPError as e:
                    print(f"WARN: Failed to send permission request: {e}", file=sys.stderr)

            server.notification_handlers[
                "notifications/claude/channel/permission_request"
            ] = _handle_permission_request

            lifespan_context = await stack.enter_async_context(
                server.lifespan(server)
            )
            session = await stack.enter_async_context(
                ServerSession(read_stream, write_stream, init_options)
            )

            async with anyio.create_task_group() as tg:
                tg.start_soon(_poll_signal_messages, session, cfg)

                async for message in session.incoming_messages:
                    tg.start_soon(
                        server._handle_message,
                        message,
                        session,
                        lifespan_context,
                        False,  # raise_exceptions
                    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    _load_config()  # Fail fast on missing env vars
    anyio.run(run_channel_server)


if __name__ == "__main__":
    main()
