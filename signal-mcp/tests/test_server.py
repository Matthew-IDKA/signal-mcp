"""Tests for the Signal MCP server."""

import logging
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
