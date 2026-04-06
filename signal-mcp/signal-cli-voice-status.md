# Voice Call Support in signal-cli — Status as of April 2026

## Summary

Voice calling was merged into signal-cli on **April 1, 2026** (v0.14.2, PR #1932). This is a newly landed feature after years of being unsupported.

## What's Supported

| Feature | Status |
|---------|--------|
| 1:1 voice calls (initiate) | Supported (v0.14.2+) |
| 1:1 voice calls (receive) | Supported (v0.14.2+) |
| Group voice calls | Not implemented |
| Video calls | Not implemented |

### CLI Commands (v0.14.2+)

```
startCall       Start an outgoing voice call
acceptCall      Accept an incoming voice call
hangupCall      Hang up an active voice call
rejectCall      Reject an incoming voice call
listCalls       List active voice calls
subscribeCallEvents  Subscribe to call event notifications
```

## Architecture

signal-cli (Java) handles Signal protocol signaling. Audio is offloaded to a companion Rust subprocess — `signal-call-tunnel` (repo: `visigoth/signal-call-tunnel`) — which wraps Signal's RingRTC/WebRTC library.

Audio I/O flows through platform virtual audio devices:
- **Linux:** PulseAudio null sinks
- **macOS:** BlackHole virtual audio driver

This means any external process can pipe audio in/out using standard platform audio APIs, enabling use cases like answering machines or call routing.

## REST API Wrapper (signal-cli-rest-api)

The `bbernhard/signal-cli-rest-api` project (which this session uses) has **no current plans** to expose call support. The blocker is architectural: the REST API is stateless HTTP, while voice calls are stateful and audio-bound. Meaningful support would require WebSockets, streaming audio endpoints, or a sidecar process — none of which are in progress.

- Relevant issue: [bbernhard/signal-cli-rest-api#465](https://github.com/bbernhard/signal-cli-rest-api/issues/465)

## Path to Voice in This Integration

To use voice calls programmatically, the options are:

1. **JSON-RPC mode** — Run signal-cli directly in `--output=json` daemon mode and communicate over its JSON-RPC socket. This bypasses the REST wrapper and exposes all v0.14.2 commands including call control.
2. **Wait for REST API support** — No active development; timeline unknown.
3. **Direct signal-cli subprocess** — Shell out to signal-cli per call. Workable for simple use cases but not suitable for a persistent session.

Option 1 (JSON-RPC) is the most viable path for integrating voice into signal-mcp.

## References

- [signal-cli v0.14.2 release](https://github.com/AsamK/signal-cli/releases/tag/v0.14.2)
- [PR #1932: Add voice calling support](https://github.com/AsamK/signal-cli/pull/1932)
- [signal-call-tunnel (Rust/RingRTC companion)](https://github.com/visigoth/signal-call-tunnel)
- [signal-cli-rest-api issue #465](https://github.com/bbernhard/signal-cli-rest-api/issues/465)
