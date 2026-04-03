# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-04-02

### Added

- MCP server with Claude Code Channel support (WebSocket-based inbound message polling)
- Tools: reply, fetch_messages, send_attachment, download_attachment, react, send_typing, list_groups, get_contacts
- Sender allowlist filtering for inbound messages
- Permission relay -- forward tool approval prompts to Signal, accept y/n verdicts from authorized senders
- Support for both DM and group channel types
- Comprehensive test suite (pytest + respx HTTP mocking)
