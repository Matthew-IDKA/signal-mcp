"""Tests for the Signal MCP server."""

import logging
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from signal_mcp import server

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ENV_DM = {
    "SIGNAL_API_URL": "http://signal-test:8093",
    "SIGNAL_BOT_NUMBER": "+15550001234",
    "SIGNAL_CHANNEL_TYPE": "dm",
    "SIGNAL_CHANNEL_ID": "+15559876543",
}

ENV_GROUP = {
    **ENV_DM,
    "SIGNAL_CHANNEL_TYPE": "group",
    "SIGNAL_CHANNEL_ID": "abc123groupid",
}


@pytest.fixture(autouse=True)
def reset_globals():
    """Reset module-level state between tests."""
    server._config = None
    server._http = None
    server._pending_approvals.clear()
    server._inbound_limiter._windows.clear()
    server._notify_limiter._windows.clear()
    yield
    if server._http:
        pass  # httpx.AsyncClient cleanup handled by test


@pytest.fixture
def dm_env(monkeypatch):
    for k, v in ENV_DM.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def group_env(monkeypatch):
    for k, v in ENV_GROUP.items():
        monkeypatch.setenv(k, v)


# ---------------------------------------------------------------------------
# Config validation tests
# ---------------------------------------------------------------------------

class TestConfig:
    def test_load_config_success(self, dm_env):
        cfg = server._load_config()
        assert cfg["api_url"] == "http://signal-test:8093"
        assert cfg["bot_number"] == "+15550001234"
        assert cfg["channel_type"] == "dm"
        assert cfg["channel_id"] == "+15559876543"

    def test_load_config_missing_var(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_API_URL", "http://test")
        # Missing the rest
        with pytest.raises(SystemExit):
            server._load_config()

    def test_load_config_strips_trailing_slash(self, monkeypatch):
        for k, v in ENV_DM.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("SIGNAL_API_URL", "http://test:8093/")
        cfg = server._load_config()
        assert cfg["api_url"] == "http://test:8093"

    def test_invalid_channel_type(self, monkeypatch):
        for k, v in ENV_DM.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("SIGNAL_CHANNEL_TYPE", "invalid")
        with pytest.raises(SystemExit):
            cfg = server._load_config()
            server._validate_channel_type(cfg["channel_type"])


# ---------------------------------------------------------------------------
# Send payload tests
# ---------------------------------------------------------------------------

class TestSendPayload:
    def test_dm_payload(self, dm_env):
        cfg = server._load_config()
        payload = server._send_payload(cfg, "hello")
        assert payload["message"] == "hello"
        assert payload["number"] == "+15550001234"
        assert payload["recipients"] == ["+15559876543"]
        assert "base64_attachments" not in payload

    def test_dm_payload_with_attachment(self, dm_env):
        cfg = server._load_config()
        payload = server._send_payload(cfg, "file", attachments=["base64data"])
        assert payload["base64_attachments"] == ["base64data"]


# ---------------------------------------------------------------------------
# Tool tests (mocked HTTP)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestReplyTool:
    @respx.mock
    async def test_reply_dm(self, dm_env):
        respx.post("http://signal-test:8093/v2/send").mock(
            return_value=httpx.Response(200, json={"timestamp": "123456"})
        )
        result = await server.reply("hello from test")
        assert "123456" in result

    @respx.mock
    async def test_reply_http_error_sanitized(self, dm_env):
        """HTTPStatusError message must not contain the request URL (which has bot number)."""
        respx.post("http://signal-test:8093/v2/send").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await server.reply("will fail")
        assert "+15550001234" not in str(exc_info.value)
        assert "HTTP 500" in str(exc_info.value)


@pytest.mark.asyncio
class TestFetchMessages:
    async def test_returns_channel_mode_message(self, dm_env):
        result = await server.fetch_messages()
        assert "automatically" in result
        assert "Channel" in result


@pytest.mark.asyncio
class TestSendAttachment:
    @respx.mock
    async def test_send_file(self, dm_env, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")

        respx.post("http://signal-test:8093/v2/send").mock(
            return_value=httpx.Response(200, json={"timestamp": "789"})
        )
        result = await server.send_attachment(str(test_file), "caption")
        assert "test.txt" in result
        assert "789" in result

    async def test_file_not_found(self, dm_env):
        result = await server.send_attachment("/nonexistent/file.txt")
        assert "Error" in result

    async def test_file_too_large(self, dm_env, tmp_path):
        test_file = tmp_path / "huge.bin"
        test_file.write_bytes(b"x")
        original_stat = Path.stat

        def fake_stat(self_path, *args, **kwargs):
            result = original_stat(self_path, *args, **kwargs)
            if self_path == test_file:
                # Return a modified stat with large size
                import os
                return os.stat_result((result.st_mode, result.st_ino, result.st_dev,
                    result.st_nlink, result.st_uid, result.st_gid,
                    100 * 1024 * 1024, int(result.st_atime),
                    int(result.st_mtime), int(result.st_ctime),
                ))
            return result

        with patch.object(Path, "stat", fake_stat):
            result = await server.send_attachment(str(test_file))
            assert "Error" in result
            assert "95 MB" in result


@pytest.mark.asyncio
class TestDownloadAttachment:
    @respx.mock
    async def test_happy_path(self, dm_env, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "ATTACHMENT_DIR", tmp_path / "attachments")
        respx.get("http://signal-test:8093/v1/attachments/abc123.jpg").mock(
            return_value=httpx.Response(200, content=b"fake-image-data")
        )
        result = await server.download_attachment("abc123.jpg")
        assert "abc123.jpg" in result
        assert "15" in result  # byte count

    @respx.mock
    async def test_invalid_id_format(self, dm_env):
        result = await server.download_attachment("../../etc/passwd")
        assert "Error" in result
        assert "invalid" in result

    @respx.mock
    async def test_invalid_id_with_slash(self, dm_env):
        result = await server.download_attachment("foo/bar")
        assert "Error" in result
        assert "invalid" in result

    @respx.mock
    async def test_path_traversal_blocked(self, dm_env, tmp_path, monkeypatch):
        safe_dir = tmp_path / "attachments"
        monkeypatch.setattr(server, "ATTACHMENT_DIR", safe_dir)
        safe_dir.mkdir()
        respx.get("http://signal-test:8093/v1/attachments/abc123.jpg").mock(
            return_value=httpx.Response(200, content=b"data")
        )
        # Attempt to write outside the attachment dir
        evil_path = str(tmp_path / "evil.txt")
        result = await server.download_attachment("abc123.jpg", save_path=evil_path)
        assert "Error" in result
        assert "save_path" in result
        assert not (tmp_path / "evil.txt").exists()

    @respx.mock
    async def test_custom_save_path_within_allowed_dir(self, dm_env, tmp_path, monkeypatch):
        safe_dir = tmp_path / "attachments"
        monkeypatch.setattr(server, "ATTACHMENT_DIR", safe_dir)
        safe_dir.mkdir()
        respx.get("http://signal-test:8093/v1/attachments/abc123.jpg").mock(
            return_value=httpx.Response(200, content=b"image-bytes")
        )
        save_to = str(safe_dir / "abc123.jpg")
        result = await server.download_attachment("abc123.jpg", save_path=save_to)
        assert "abc123.jpg" in result
        assert (safe_dir / "abc123.jpg").read_bytes() == b"image-bytes"

    @respx.mock
    async def test_api_error_sanitized(self, dm_env, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "ATTACHMENT_DIR", tmp_path / "attachments")
        respx.get("http://signal-test:8093/v1/attachments/abc123.jpg").mock(
            return_value=httpx.Response(404)
        )
        result = await server.download_attachment("abc123.jpg")
        assert "Error" in result
        assert "404" in result
        # Bot number must not appear in the error
        assert "+15550001234" not in result

    @respx.mock
    async def test_timeout_returns_clean_error(self, dm_env, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "ATTACHMENT_DIR", tmp_path / "attachments")
        respx.get("http://signal-test:8093/v1/attachments/abc123.jpg").mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        result = await server.download_attachment("abc123.jpg")
        assert "Error" in result
        assert "timed out" in result


@pytest.mark.asyncio
class TestSendTyping:
    @respx.mock
    async def test_start_typing(self, dm_env):
        respx.put("http://signal-test:8093/v1/typing-indicator/+15550001234").mock(
            return_value=httpx.Response(204)
        )
        result = await server.send_typing(True)
        assert "started" in result


@pytest.mark.asyncio
class TestReact:
    @respx.mock
    async def test_react_dm(self, dm_env):
        respx.put("http://signal-test:8093/v1/reactions/+15550001234").mock(
            return_value=httpx.Response(204)
        )
        result = await server.react("👍", "+15559876543", 123456)
        assert "👍" in result


@pytest.mark.asyncio
class TestListGroups:
    @respx.mock
    async def test_list_groups(self, dm_env):
        groups = [
            {"name": "Family", "id": "abc", "members": ["+1", "+2"]},
            {"name": "Work", "id": "def", "members": ["+1"]},
        ]
        respx.get("http://signal-test:8093/v1/groups/+15550001234").mock(
            return_value=httpx.Response(200, json=groups)
        )
        result = await server.list_groups()
        assert "Family" in result
        assert "Work" in result

    @respx.mock
    async def test_no_groups(self, dm_env):
        respx.get("http://signal-test:8093/v1/groups/+15550001234").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = await server.list_groups()
        assert "No groups" in result


@pytest.mark.asyncio
class TestGetContacts:
    @respx.mock
    async def test_get_contacts(self, dm_env):
        contacts = [
            {"name": "Alice", "number": "+1111"},
            {"profile_name": "Bob", "number": "+2222"},
        ]
        respx.get("http://signal-test:8093/v1/contacts/+15550001234").mock(
            return_value=httpx.Response(200, json=contacts)
        )
        result = await server.get_contacts()
        assert "Alice" in result
        assert "Bob" in result

    @respx.mock
    async def test_no_contacts(self, dm_env):
        respx.get("http://signal-test:8093/v1/contacts/+15550001234").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = await server.get_contacts()
        assert "No contacts" in result


# ---------------------------------------------------------------------------
# PII redaction tests
# ---------------------------------------------------------------------------

class TestPiiRedaction:
    def test_redact_pii_replaces_phone_number(self):
        result = server._redact_pii("Message from +14152739647")
        assert "+14152739647" not in result
        assert "[pii:" in result

    def test_redact_pii_preserves_non_pii(self):
        text = "No phone numbers here, just regular text."
        assert server._redact_pii(text) == text

    def test_redact_pii_consistent_hashing(self):
        """Same number must produce the same token (enables log correlation)."""
        r1 = server._redact_pii("+14152739647")
        r2 = server._redact_pii("+14152739647")
        assert r1 == r2

    def test_redact_pii_different_numbers_differ(self):
        r1 = server._redact_pii("+14152739647")
        r2 = server._redact_pii("+14155967114")
        assert r1 != r2

    def test_redact_pii_multiple_numbers_in_one_string(self):
        text = "From +14152739647 to +14155967114"
        result = server._redact_pii(text)
        assert "+14152739647" not in result
        assert "+14155967114" not in result
        assert result.count("[pii:") == 2

    def test_pii_filter_applied_to_log_record(self):
        """_PiiFilter must redact phone numbers before the record is written."""
        f = server._PiiFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="Received from %s", args=("+14152739647",), exc_info=None
        )
        f.filter(record)
        assert "+14152739647" not in record.getMessage()
        assert "[pii:" in record.getMessage()
        # args must be cleared so the formatter doesn't re-apply them
        assert record.args == ()


# ---------------------------------------------------------------------------
# Channel notification model tests
# ---------------------------------------------------------------------------

class TestChannelNotification:
    def test_notification_model_dump(self):
        notif = server.ChannelNotification(
            params=server.ChannelNotificationParams(
                content="hello from signal",
                meta={"sender": "+15559876543", "timestamp": "12345"},
            )
        )
        dumped = notif.model_dump(by_alias=True, mode="json", exclude_none=True)
        assert dumped["method"] == "notifications/claude/channel"
        assert dumped["params"]["content"] == "hello from signal"
        assert dumped["params"]["meta"]["sender"] == "+15559876543"

    def test_notification_empty_meta(self):
        notif = server.ChannelNotification(
            params=server.ChannelNotificationParams(content="test")
        )
        dumped = notif.model_dump(by_alias=True, mode="json", exclude_none=True)
        assert dumped["params"]["meta"] == {}


# ---------------------------------------------------------------------------
# Config: allowed senders and poll interval
# ---------------------------------------------------------------------------

class TestChannelConfig:
    def test_allowed_senders_parsed(self, dm_env, monkeypatch):
        monkeypatch.setenv("SIGNAL_ALLOWED_SENDERS", "+1111,+2222, +3333 ")
        cfg = server._load_config()
        assert cfg["allowed_senders"] == ["+1111", "+2222", "+3333"]

    def test_allowed_senders_empty(self, dm_env):
        cfg = server._load_config()
        assert cfg["allowed_senders"] == []

    def test_poll_interval_default(self, dm_env):
        cfg = server._load_config()
        assert cfg["poll_interval"] == 2

    def test_poll_interval_custom(self, dm_env, monkeypatch):
        monkeypatch.setenv("SIGNAL_POLL_INTERVAL", "5")
        cfg = server._load_config()
        assert cfg["poll_interval"] == 5

    def test_approval_senders_parsed(self, dm_env, monkeypatch):
        monkeypatch.setenv("SIGNAL_APPROVAL_SENDERS", "+1111, +2222")
        cfg = server._load_config()
        assert cfg["approval_senders"] == ["+1111", "+2222"]

    def test_approval_senders_empty(self, dm_env):
        cfg = server._load_config()
        assert cfg["approval_senders"] == []


# ---------------------------------------------------------------------------
# Permission relay tests
# ---------------------------------------------------------------------------

class TestPermissionRelay:
    def test_verdict_pattern_allow(self):
        m = server.VERDICT_PATTERN.match("y abcde")
        assert m is not None
        assert m.group(1) == "y"
        assert m.group(2) == "abcde"

    def test_verdict_pattern_deny(self):
        m = server.VERDICT_PATTERN.match("no fghij")
        assert m is not None
        assert m.group(1) == "no"
        assert m.group(2) == "fghij"

    def test_verdict_pattern_yes(self):
        m = server.VERDICT_PATTERN.match("  yes kwxyz  ")
        assert m is not None
        assert m.group(1) == "yes"
        assert m.group(2) == "kwxyz"

    def test_verdict_pattern_rejects_l(self):
        """Request IDs exclude 'l' to avoid ambiguity with '1'."""
        m = server.VERDICT_PATTERN.match("y abcle")
        assert m is None

    def test_verdict_pattern_rejects_normal_text(self):
        assert server.VERDICT_PATTERN.match("hello world") is None
        assert server.VERDICT_PATTERN.match("yes") is None
        assert server.VERDICT_PATTERN.match("y abc") is None  # too short

    def test_verdict_model_dump(self):
        verdict = server.PermissionVerdict(
            params=server.PermissionVerdictParams(
                request_id="abcde", behavior="allow"
            )
        )
        dumped = verdict.model_dump(by_alias=True, mode="json", exclude_none=True)
        assert dumped["method"] == "notifications/claude/channel/permission"
        assert dumped["params"]["request_id"] == "abcde"
        assert dumped["params"]["behavior"] == "allow"


# ---------------------------------------------------------------------------
# Group ID normalization tests
# ---------------------------------------------------------------------------

class TestGroupIdNormalization:
    """The REST API returns group IDs as "group." + base64(base64(masterKey)).
    The WebSocket delivers groupInfo.groupId as base64(masterKey) only.
    _load_config() must normalize channel_id_ws for inbound routing while
    preserving the original channel_id for outbound payloads.
    """

    def test_dm_channel_id_ws_unchanged(self, dm_env):
        cfg = server._load_config()
        assert cfg["channel_id"] == "+15559876543"
        assert cfg["channel_id_ws"] == "+15559876543"

    def test_group_channel_id_preserved_for_outbound(self, monkeypatch):
        import base64
        inner = "IVbVK5bnMUS8jRKvYBXjzZKTf7RG9URlhDJFIp5ZUV4="
        outer = base64.b64encode(inner.encode("ascii")).decode("ascii")
        rest_api_id = f"group.{outer}"
        for k, v in ENV_DM.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("SIGNAL_CHANNEL_TYPE", "group")
        monkeypatch.setenv("SIGNAL_CHANNEL_ID", rest_api_id)
        cfg = server._load_config()
        assert cfg["channel_id"] == rest_api_id  # outbound preserved

    def test_group_channel_id_ws_decoded(self, monkeypatch):
        import base64
        inner = "IVbVK5bnMUS8jRKvYBXjzZKTf7RG9URlhDJFIp5ZUV4="
        outer = base64.b64encode(inner.encode("ascii")).decode("ascii")
        rest_api_id = f"group.{outer}"
        for k, v in ENV_DM.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("SIGNAL_CHANNEL_TYPE", "group")
        monkeypatch.setenv("SIGNAL_CHANNEL_ID", rest_api_id)
        cfg = server._load_config()
        assert cfg["channel_id_ws"] == inner  # WebSocket-comparable form

    def test_group_channel_id_ws_matches_ws_envelope(self, monkeypatch):
        """Simulates the real Emily-group IDs from production."""
        # Actual values observed in signal-mcp.log
        ws_group_id = "IVbVK5bnMUS8jRKvYBXjzZKTf7RG9URlhDJFIp5ZUV4="
        rest_api_id = "group.SVZiVks1Ym5NVVM4alJLdllCWGp6WktUZjdSRzlVUmxoREpGSXA1WlVWND0="
        for k, v in ENV_DM.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("SIGNAL_CHANNEL_TYPE", "group")
        monkeypatch.setenv("SIGNAL_CHANNEL_ID", rest_api_id)
        cfg = server._load_config()
        assert cfg["channel_id_ws"] == ws_group_id


# ---------------------------------------------------------------------------
# HTTPS enforcement tests
# ---------------------------------------------------------------------------

class TestHttpsEnforcement:
    def test_http_url_logs_warning(self, dm_env, caplog):
        with caplog.at_level(logging.WARNING, logger="signal-mcp"):
            server._load_config()
        assert any("http://" in r.message and "cleartext" in r.message for r in caplog.records)

    def test_https_url_no_warning(self, monkeypatch, caplog):
        for k, v in ENV_DM.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("SIGNAL_API_URL", "https://signal-test:8093")
        with caplog.at_level(logging.WARNING, logger="signal-mcp"):
            server._load_config()
        assert not any("cleartext" in r.message for r in caplog.records)

    def test_allow_http_suppresses_warning(self, dm_env, monkeypatch, caplog):
        monkeypatch.setenv("SIGNAL_ALLOW_HTTP", "1")
        with caplog.at_level(logging.WARNING, logger="signal-mcp"):
            server._load_config()
        assert not any("cleartext" in r.message for r in caplog.records)

    def test_api_url_preserved_in_config(self, dm_env):
        cfg = server._load_config()
        assert cfg["api_url"] == "http://signal-test:8093"


# ---------------------------------------------------------------------------
# Approval expiry tests
# ---------------------------------------------------------------------------

class TestApprovalExpiry:
    def test_valid_verdict_accepted(self):
        server._pending_approvals["abcde"] = time.monotonic()
        status, msg = server._validate_verdict("abcde", ttl=300)
        assert status == "ok"
        assert msg is None
        assert "abcde" not in server._pending_approvals  # consumed

    def test_unknown_request_id_rejected(self):
        status, msg = server._validate_verdict("zzzzz", ttl=300)
        assert status == "unknown"
        assert "zzzzz" in msg

    def test_expired_verdict_rejected(self):
        server._pending_approvals["abcde"] = time.monotonic() - 301
        status, msg = server._validate_verdict("abcde", ttl=300)
        assert status == "expired"
        assert "abcde" in msg
        assert "abcde" not in server._pending_approvals  # consumed even when expired

    def test_double_reply_rejected(self):
        server._pending_approvals["abcde"] = time.monotonic()
        server._validate_verdict("abcde", ttl=300)  # first: consumed
        status, _ = server._validate_verdict("abcde", ttl=300)  # second: unknown
        assert status == "unknown"

    def test_custom_ttl_respected(self):
        server._pending_approvals["abcde"] = time.monotonic() - 11
        status, _ = server._validate_verdict("abcde", ttl=10)
        assert status == "expired"

    def test_within_custom_ttl_accepted(self):
        server._pending_approvals["abcde"] = time.monotonic() - 5
        status, _ = server._validate_verdict("abcde", ttl=10)
        assert status == "ok"


# ---------------------------------------------------------------------------
# Env file loader tests
# ---------------------------------------------------------------------------

class TestEnvFile:
    def test_loads_vars_into_environ(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".signal-mcp.env"
        env_file.write_text("SIGNAL_BOT_NUMBER=+15550009999\nSIGNAL_CHANNEL_ID=+15550001111\n")
        monkeypatch.delenv("SIGNAL_BOT_NUMBER", raising=False)
        monkeypatch.delenv("SIGNAL_CHANNEL_ID", raising=False)
        server._load_env_file(str(env_file))
        assert os.environ["SIGNAL_BOT_NUMBER"] == "+15550009999"
        assert os.environ["SIGNAL_CHANNEL_ID"] == "+15550001111"

    def test_existing_env_var_not_overwritten(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".signal-mcp.env"
        env_file.write_text("SIGNAL_BOT_NUMBER=+15550009999\n")
        monkeypatch.setenv("SIGNAL_BOT_NUMBER", "+15550001234")
        server._load_env_file(str(env_file))
        assert os.environ["SIGNAL_BOT_NUMBER"] == "+15550001234"

    def test_comments_and_blank_lines_skipped(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".signal-mcp.env"
        env_file.write_text("# this is a comment\n\nSIGNAL_BOT_NUMBER=+15550009999\n")
        monkeypatch.delenv("SIGNAL_BOT_NUMBER", raising=False)
        server._load_env_file(str(env_file))
        assert os.environ["SIGNAL_BOT_NUMBER"] == "+15550009999"

    def test_double_quoted_value(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".signal-mcp.env"
        env_file.write_text('SIGNAL_BOT_NUMBER="+15550009999"\n')
        monkeypatch.delenv("SIGNAL_BOT_NUMBER", raising=False)
        server._load_env_file(str(env_file))
        assert os.environ["SIGNAL_BOT_NUMBER"] == "+15550009999"

    def test_single_quoted_value(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".signal-mcp.env"
        env_file.write_text("SIGNAL_BOT_NUMBER='+15550009999'\n")
        monkeypatch.delenv("SIGNAL_BOT_NUMBER", raising=False)
        server._load_env_file(str(env_file))
        assert os.environ["SIGNAL_BOT_NUMBER"] == "+15550009999"

    def test_missing_file_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            server._load_env_file(str(tmp_path / "nonexistent.env"))

    def test_invalid_line_logged_and_skipped(self, tmp_path, monkeypatch, caplog):
        env_file = tmp_path / ".signal-mcp.env"
        env_file.write_text("NOTAVALIDLINE\nSIGNAL_BOT_NUMBER=+15550009999\n")
        monkeypatch.delenv("SIGNAL_BOT_NUMBER", raising=False)
        with caplog.at_level(logging.WARNING, logger="signal-mcp"):
            server._load_env_file(str(env_file))
        assert any("no '=' separator" in r.message for r in caplog.records)
        assert os.environ["SIGNAL_BOT_NUMBER"] == "+15550009999"

    def test_load_config_reads_env_file(self, tmp_path, monkeypatch):
        """_load_config() invokes _load_env_file when SIGNAL_ENV_FILE is set."""
        env_file = tmp_path / ".signal-mcp.env"
        env_file.write_text(
            "SIGNAL_BOT_NUMBER=+15550009999\n"
            "SIGNAL_CHANNEL_ID=+15559876543\n"
        )
        monkeypatch.setenv("SIGNAL_ENV_FILE", str(env_file))
        monkeypatch.setenv("SIGNAL_API_URL", "https://signal-test:8093")
        monkeypatch.setenv("SIGNAL_CHANNEL_TYPE", "dm")
        monkeypatch.delenv("SIGNAL_BOT_NUMBER", raising=False)
        monkeypatch.delenv("SIGNAL_CHANNEL_ID", raising=False)
        cfg = server._load_config()
        assert cfg["bot_number"] == "+15550009999"
        assert cfg["channel_id"] == "+15559876543"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix file permissions only")
    def test_world_readable_file_warns(self, tmp_path, monkeypatch, caplog):
        env_file = tmp_path / ".signal-mcp.env"
        env_file.write_text("SIGNAL_BOT_NUMBER=+15550009999\n")
        env_file.chmod(0o644)
        monkeypatch.delenv("SIGNAL_BOT_NUMBER", raising=False)
        with caplog.at_level(logging.WARNING, logger="signal-mcp"):
            server._load_env_file(str(env_file))
        assert any("group/others" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Rate limiter unit tests
# ---------------------------------------------------------------------------

class TestSlidingWindowRateLimiter:
    def test_allows_calls_within_limit(self):
        rl = server._SlidingWindowRateLimiter(max_calls=3, window_seconds=60)
        assert rl.is_allowed("k") is True
        assert rl.is_allowed("k") is True
        assert rl.is_allowed("k") is True

    def test_blocks_when_limit_reached(self):
        rl = server._SlidingWindowRateLimiter(max_calls=3, window_seconds=60)
        for _ in range(3):
            rl.is_allowed("k")
        assert rl.is_allowed("k") is False

    def test_independent_keys(self):
        rl = server._SlidingWindowRateLimiter(max_calls=1, window_seconds=60)
        assert rl.is_allowed("a") is True
        assert rl.is_allowed("b") is True  # separate bucket
        assert rl.is_allowed("a") is False  # a exhausted

    def test_window_expiry_allows_again(self):
        """Timestamps older than the window are evicted, freeing capacity."""
        rl = server._SlidingWindowRateLimiter(max_calls=2, window_seconds=1)
        rl.is_allowed("k")
        rl.is_allowed("k")
        # Manually backdate the timestamps to simulate window expiry
        dq = rl._windows["k"]
        for i in range(len(dq)):
            dq[i] = dq[i] - 2  # 2 seconds ago, outside 1-second window
        assert rl.is_allowed("k") is True

    def test_zero_max_calls_always_blocks(self):
        rl = server._SlidingWindowRateLimiter(max_calls=0, window_seconds=60)
        assert rl.is_allowed("k") is False

    def test_module_instances_exist(self):
        assert hasattr(server, "_inbound_limiter")
        assert hasattr(server, "_notify_limiter")
        assert isinstance(server._inbound_limiter, server._SlidingWindowRateLimiter)
        assert isinstance(server._notify_limiter, server._SlidingWindowRateLimiter)


# ---------------------------------------------------------------------------
# Log dir permission tests
# ---------------------------------------------------------------------------

class TestLogDirPermissions:
    @pytest.mark.skipif(sys.platform == "win32", reason="Unix mkdir mode only")
    def test_log_dir_created_with_restricted_permissions(self, tmp_path):
        log_dir = tmp_path / "signal-logs"
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stat = log_dir.stat()
        assert oct(stat.st_mode)[-3:] == "700"

    def test_log_dir_defaults_to_tempdir(self, monkeypatch):
        import tempfile
        monkeypatch.delenv("SIGNAL_LOG_DIR", raising=False)
        from pathlib import Path
        expected = Path(tempfile.gettempdir())
        assert server.LOG_PATH.parent == expected


# ---------------------------------------------------------------------------
# Content validation -- Tier 5
# ---------------------------------------------------------------------------

class TestValidateBody:
    def test_body_at_limit_passes(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_MAX_BODY_LENGTH", "4000")
        body = "x" * 4000
        result, reason = server._validate_body(body)
        assert result == body
        assert reason == ""

    def test_body_over_limit_drops(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_MAX_BODY_LENGTH", "4000")
        body = "x" * 4001
        result, reason = server._validate_body(body)
        assert result is None
        assert "4001" in reason

    def test_body_custom_limit(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_MAX_BODY_LENGTH", "10")
        result, _ = server._validate_body("x" * 11)
        assert result is None

    def test_control_chars_stripped(self, monkeypatch):
        monkeypatch.delenv("SIGNAL_MAX_BODY_LENGTH", raising=False)
        monkeypatch.delenv("SIGNAL_STRICT_UNICODE", raising=False)
        body = "hello\x01world\x0b!"
        result, reason = server._validate_body(body)
        assert result == "helloworld!"
        assert reason == ""

    def test_tab_newline_preserved(self, monkeypatch):
        monkeypatch.delenv("SIGNAL_MAX_BODY_LENGTH", raising=False)
        monkeypatch.delenv("SIGNAL_STRICT_UNICODE", raising=False)
        body = "line1\nline2\ttabbed"
        result, _ = server._validate_body(body)
        assert result == body

    def test_null_byte_stripped(self, monkeypatch):
        monkeypatch.delenv("SIGNAL_MAX_BODY_LENGTH", raising=False)
        monkeypatch.delenv("SIGNAL_STRICT_UNICODE", raising=False)
        result, _ = server._validate_body("hello\x00world")
        assert result == "helloworld"

    def test_body_empty_after_strip_drops(self, monkeypatch):
        monkeypatch.delenv("SIGNAL_MAX_BODY_LENGTH", raising=False)
        monkeypatch.delenv("SIGNAL_STRICT_UNICODE", raising=False)
        result, reason = server._validate_body("\x01\x02\x03")
        assert result is None
        assert "empty" in reason

    def test_strict_unicode_drops_format_char(self, monkeypatch):
        monkeypatch.delenv("SIGNAL_MAX_BODY_LENGTH", raising=False)
        monkeypatch.setenv("SIGNAL_STRICT_UNICODE", "1")
        # U+00AD SOFT HYPHEN is category Cf
        result, reason = server._validate_body("hello\u00adworld")
        assert result is None
        assert "Cf" in reason

    def test_strict_unicode_off_allows_format_char(self, monkeypatch):
        monkeypatch.delenv("SIGNAL_MAX_BODY_LENGTH", raising=False)
        monkeypatch.setenv("SIGNAL_STRICT_UNICODE", "0")
        result, _ = server._validate_body("hello\u00adworld")
        assert result is not None


class TestSanitizeAttachment:
    def test_valid_attachment(self):
        att = {"id": "abc123", "contentType": "image/jpeg", "filename": "photo.jpg", "size": 1024}
        result = server._sanitize_attachment(att)
        assert result is not None
        assert result["id"] == "abc123"
        assert result["contentType"] == "image/jpeg"
        assert result["filename"] == "photo.jpg"
        assert result["size"] == 1024

    def test_invalid_att_id_drops(self):
        att = {"id": "../etc/passwd", "contentType": "image/jpeg", "filename": "x.jpg", "size": 1}
        result = server._sanitize_attachment(att)
        assert result is None

    def test_empty_att_id_drops(self):
        att = {"id": "", "contentType": "image/jpeg", "filename": "x.jpg", "size": 1}
        result = server._sanitize_attachment(att)
        assert result is None

    def test_mime_type_normalized(self):
        att = {"id": "abc123", "contentType": "not-a-mime", "filename": "x.bin", "size": 1}
        result = server._sanitize_attachment(att)
        assert result is not None
        assert result["contentType"] == "application/octet-stream"

    def test_filename_path_traversal_sanitized(self):
        att = {
            "id": "abc123", "contentType": "text/plain",
            "filename": "../../../etc/passwd", "size": 1,
        }
        result = server._sanitize_attachment(att)
        assert result is not None
        assert "/" not in result["filename"]
        assert "\\" not in result["filename"]

    def test_filename_leading_dot_stripped(self):
        att = {"id": "abc123", "contentType": "text/plain", "filename": ".bashrc", "size": 1}
        result = server._sanitize_attachment(att)
        assert result is not None
        assert not result["filename"].startswith(".")

    def test_nonint_size_replaced(self):
        att = {"id": "abc123", "contentType": "text/plain", "filename": "x.txt", "size": "large"}
        result = server._sanitize_attachment(att)
        assert result is not None
        assert result["size"] == ""

    def test_absent_size_is_empty_string(self):
        att = {"id": "abc123", "contentType": "text/plain", "filename": "x.txt"}
        result = server._sanitize_attachment(att)
        assert result is not None
        assert result["size"] == ""


class TestResolveMentions:
    def test_basic_mention_resolved(self):
        body = "hello \ufffc!"
        mentions = [{"start": 6, "length": 1, "name": "Alice"}]
        result = server._resolve_mentions(body, mentions)
        assert result == "hello @Alice!"

    def test_non_list_mentions_passthrough(self):
        body = "hello world"
        result = server._resolve_mentions(body, "not-a-list")
        assert result == body

    def test_oob_start_skipped(self):
        body = "hi"
        mentions = [{"start": 99, "length": 1, "name": "Alice"}]
        result = server._resolve_mentions(body, mentions)
        assert result == body

    def test_negative_start_skipped(self):
        body = "hello"
        mentions = [{"start": -1, "length": 1, "name": "Alice"}]
        result = server._resolve_mentions(body, mentions)
        assert result == body

    def test_oversized_length_clamped(self):
        body = "abc"
        mentions = [{"start": 1, "length": 100, "name": "X"}]
        result = server._resolve_mentions(body, mentions)
        # Should replace chars 1-2 ("bc") with "@X"
        assert result == "a@X"

    def test_name_capped_at_64(self):
        body = "x\ufffc"
        long_name = "A" * 100
        mentions = [{"start": 1, "length": 1, "name": long_name}]
        result = server._resolve_mentions(body, mentions)
        assert len(result) == 1 + 1 + 64  # "x" + "@" + 64 chars

    def test_non_int_start_skipped(self):
        body = "hello"
        mentions = [{"start": "bad", "length": 1, "name": "Alice"}]
        result = server._resolve_mentions(body, mentions)
        assert result == body
