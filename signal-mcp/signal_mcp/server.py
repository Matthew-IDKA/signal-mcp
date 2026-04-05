"""Signal MCP Server -- bridges Claude Code to Signal via signal-cli-rest-api.

Operates as a Claude Code Channel: declares claude/channel capability,
polls signal-cli-rest-api for inbound messages via WebSocket, and emits
channel notifications. Claude Code tools (reply, react, etc.) handle
outbound communication.
"""

import base64
import hashlib
import logging
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
# Logging -- dual output: stderr (captured by Claude Code) + file (persistent)
#
# PII policy: E.164 phone numbers are replaced with a short SHA-256 hash
# ([pii:XXXXXXXX]) before any log record is written. The hash is stable, so
# the same number produces the same token across log lines (useful for
# correlating events) without exposing the number itself.
#
# Log directory: defaults to the system temp dir. Set SIGNAL_LOG_DIR to
# redirect logs to a user-owned directory (recommended for multi-user hosts).
# ---------------------------------------------------------------------------

_PII_PHONE_RE = re.compile(r'\+\d{7,15}')


def _redact_pii(text: str) -> str:
    """Replace E.164 phone numbers with a short non-reversible hash."""
    def _hash(m: re.Match) -> str:
        h = hashlib.sha256(m.group().encode()).hexdigest()[:8]
        return f"[pii:{h}]"
    return _PII_PHONE_RE.sub(_hash, text)


class _PiiFilter(logging.Filter):
    """Redact phone numbers from log records before writing."""
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = _redact_pii(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


_log_dir = Path(os.environ.get("SIGNAL_LOG_DIR", tempfile.gettempdir()))
LOG_PATH = _log_dir / "signal-mcp.log"

log = logging.getLogger("signal-mcp")
log.setLevel(logging.DEBUG)

_pii_filter = _PiiFilter()

_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
_stderr_handler.addFilter(_pii_filter)
log.addHandler(_stderr_handler)

_file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
_file_handler.addFilter(_pii_filter)
log.addHandler(_file_handler)

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
        log.fatal("Missing required environment variables: %s", ", ".join(missing))
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
        log.fatal("SIGNAL_CHANNEL_TYPE must be 'dm' or 'group', got '%s'", channel_type)
        sys.exit(1)


async def _check_signal_api(client: httpx.AsyncClient, api_url: str) -> None:
    """Verify signal-cli-rest-api is reachable."""
    try:
        resp = await client.get(f"{api_url}/v1/about", timeout=5)
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.ConnectError) as e:
        log.fatal("Cannot reach signal-cli-rest-api at %s: %s", api_url, e)
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

# Attachment ID format observed from signal-cli-rest-api: alphanumeric mixed-case
# with dots, underscores, and hyphens (e.g. "hj0OJjrh74jPo7rVNMdr.jpg").
# Blocks path separators (/ \) to prevent URL injection.
_ATTACHMENT_ID_RE = re.compile(r'^[a-zA-Z0-9._-]{1,200}$')

# Default directory for downloaded attachments. Set SIGNAL_ATTACHMENT_DIR to override.
ATTACHMENT_DIR = Path(
    os.environ.get("SIGNAL_ATTACHMENT_DIR", str(Path(tempfile.gettempdir()) / "signal-attachments"))
)

# ---------------------------------------------------------------------------
# Build send payload helper
# ---------------------------------------------------------------------------

def _send_payload(cfg: dict, message: str, attachments: list[str] | None = None) -> dict:
    """Build the JSON payload for POST /v2/send."""
    payload: dict = {
        "message": message,
        "number": cfg["bot_number"],
    }
    payload["recipients"] = [cfg["channel_id"]]
    if attachments:
        payload["base64_attachments"] = attachments
    return payload


def _safe_raise_for_status(resp: httpx.Response) -> None:
    """Raise HTTPStatusError on non-2xx responses without exposing the request URL.

    Standard httpx HTTPStatusError messages include the full request URL, which
    may contain the bot's phone number. This helper replaces that with a
    status-code-only message.
    """
    if resp.is_error:
        raise httpx.HTTPStatusError(
            f"Signal API error: HTTP {resp.status_code}",
            request=resp.request,
            response=resp,
        )


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
        timeout = float(os.environ.get("SIGNAL_API_TIMEOUT", "30"))
        _http = httpx.AsyncClient(timeout=timeout)
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
    _safe_raise_for_status(resp)
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
    _safe_raise_for_status(resp)
    return f"Attachment sent: {path.name} (timestamp: {resp.json().get('timestamp', 'unknown')})"


# ---------------------------------------------------------------------------
# Tool: download_attachment
# ---------------------------------------------------------------------------

@mcp.tool()
async def download_attachment(attachment_id: str, save_path: str = "") -> str:
    """Download an attachment from a received message to local disk.

    Files are saved to ATTACHMENT_DIR (default: system temp dir /
    signal-attachments). If save_path is provided it must resolve to a path
    within ATTACHMENT_DIR -- paths outside that directory are rejected to
    prevent traversal attacks.
    """
    if not _ATTACHMENT_ID_RE.match(attachment_id):
        log.warning("Rejected invalid attachment_id format")
        return "Error: invalid attachment_id format"

    cfg = await _get_config()
    client = await _get_client()

    try:
        resp = await client.get(f"{cfg['api_url']}/v1/attachments/{attachment_id}")
        _safe_raise_for_status(resp)
    except httpx.TimeoutException:
        log.warning("Timed out fetching attachment")
        return "Error: request timed out"
    except httpx.HTTPStatusError as e:
        log.warning("API error fetching attachment: HTTP %s", e.response.status_code)
        return f"Error: Signal API returned {e.response.status_code}"

    safe_dir = ATTACHMENT_DIR.resolve()
    safe_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    if save_path:
        resolved = Path(save_path).resolve()
        if not resolved.is_relative_to(safe_dir):
            log.warning("Rejected save_path outside attachment directory")
            return f"Error: save_path must be within {safe_dir}"
        final_path = resolved
    else:
        final_path = safe_dir / attachment_id

    final_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    final_path.write_bytes(resp.content)
    return f"Attachment saved to {final_path} ({len(resp.content)} bytes)"


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
    payload["recipient"] = cfg["channel_id"]

    resp = await client.put(
        f"{cfg['api_url']}/v1/reactions/{cfg['bot_number']}",
        json=payload,
    )
    _safe_raise_for_status(resp)
    return f"Reacted with {emoji}"


# ---------------------------------------------------------------------------
# Tool: send_typing
# ---------------------------------------------------------------------------

@mcp.tool()
async def send_typing(typing: bool = True) -> str:
    """Show or stop the typing indicator in the current channel."""
    cfg = await _get_config()
    client = await _get_client()

    payload = {"recipient": cfg["channel_id"]}
    resp = await client.put(
        f"{cfg['api_url']}/v1/typing-indicator/{cfg['bot_number']}",
        json=payload,
    )
    _safe_raise_for_status(resp)
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
    _safe_raise_for_status(resp)
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
    _safe_raise_for_status(resp)
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
            # Log client capabilities (available after handshake)
            client_params = getattr(session, "_client_params", None)
            if client_params:
                client_exp = getattr(client_params.capabilities, "experimental", None)
                log.info("Client experimental caps: %s", client_exp)
            else:
                log.warning("Client params not yet available (handshake pending?)")

            async with websockets.connect(ws_url) as ws:
                log.info("WebSocket connected to %s", ws_url)
                consecutive_errors = 0

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("Dropped malformed WebSocket message")
                        continue

                    envelope = msg.get("envelope", {})
                    source = envelope.get("sourceNumber", "")
                    data = envelope.get("dataMessage", {})
                    body = data.get("message", "") or ""
                    mentions = data.get("mentions", [])
                    raw_attachments = data.get("attachments", [])

                    # Channel routing: only process messages for this session's
                    # configured channel. signal-cli-rest-api broadcasts all
                    # inbound messages to every connected WebSocket; filtering
                    # here ensures each session only acts on its own channel.
                    group_info = data.get("groupInfo", {})
                    message_group_id = group_info.get("groupId", "")
                    if cfg["channel_type"] == "group":
                        if message_group_id != cfg["channel_id"]:
                            log.debug(
                                "Skipping message from unmatched group (got %r, want %r)",
                                message_group_id, cfg["channel_id"],
                            )
                            continue
                    elif cfg["channel_type"] == "dm":
                        if message_group_id:
                            log.debug("Skipping group message in DM session")
                            continue
                        if source != cfg["channel_id"]:
                            log.debug("Skipping DM from unconfigured sender %s", source)
                            continue

                    # Resolve @mention placeholders (U+FFFC) using mention metadata
                    if mentions and body:
                        chars = list(body)
                        for m in sorted(mentions, key=lambda x: x.get("start", 0), reverse=True):
                            start = m.get("start", 0)
                            length = m.get("length", 1)
                            name = m.get("name") or m.get("number", "unknown")
                            chars[start:start + length] = list(f"@{name}")
                        body = "".join(chars)

                    # Surface inbound attachment metadata so Claude can download them
                    if raw_attachments:
                        att_lines = []
                        for att in raw_attachments:
                            att_id = att.get("id", "")
                            ct = att.get("contentType", "")
                            fname = att.get("filename", "")
                            size = att.get("size", "")
                            att_lines.append(
                                f"[attachment id={att_id} type={ct} name={fname} size={size}]"
                            )
                        att_text = "\n".join(att_lines)
                        body = f"{body}\n{att_text}".strip() if body else att_text

                    if not body:
                        continue

                    log.info("Received message from %s: %r", source, body[:80])

                    # Sender allowlist gate -- prevents prompt injection
                    if allowed and source not in allowed:
                        log.warning("Dropped message from unlisted sender %s", source)
                        continue

                    # Check if this is a permission verdict (e.g., "y abcde")
                    verdict_match = VERDICT_PATTERN.match(body)
                    if verdict_match and approvers:
                        if source not in approvers:
                            log.warning("Permission verdict from non-approver %s", source)
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
                        log.info("Permission %s for %s from %s", behavior, request_id, source)
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
                    dumped = notif.model_dump(by_alias=True, mode="json", exclude_none=True)
                    log.info("Sending channel notification: %.200s", json.dumps(dumped))
                    try:
                        await session.send_notification(notif)  # type: ignore[arg-type]
                        log.info("send_notification returned OK")
                    except Exception:
                        log.exception("send_notification failed")

        except (
            websockets.ConnectionClosed,
            websockets.InvalidURI,
            OSError,
        ) as e:
            consecutive_errors += 1
            backoff = min(2 ** consecutive_errors, 60)
            log.warning("WebSocket error (%d): %s", consecutive_errors, e)
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
            timeout = float(os.environ.get("SIGNAL_API_TIMEOUT", "30"))
            _http = await stack.enter_async_context(httpx.AsyncClient(timeout=timeout))
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
                    log.warning("Failed to send permission request: %s", e)

            server.notification_handlers[
                "notifications/claude/channel/permission_request"
            ] = _handle_permission_request

            lifespan_context = await stack.enter_async_context(
                server.lifespan(server)
            )
            session = await stack.enter_async_context(
                ServerSession(read_stream, write_stream, init_options)
            )

            exp = init_options.capabilities.experimental if init_options.capabilities else None
            log.info("Server experimental caps offered: %s", exp)
            log.info("Log file: %s", LOG_PATH)

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
