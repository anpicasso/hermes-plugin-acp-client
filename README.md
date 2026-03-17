# hermes-plugin-acp-client

Connect Hermes to [ACP (Agent Client Protocol)](https://agentclientprotocol.com/) agents via stdio transport. Currently supports **OpenCode** and any ACP-compatible server that speaks NDJSON over stdin/stdout.

## What it does

Spawns `opencode acp` as a subprocess and communicates via NDJSON (JSON-RPC 2.0 over stdio). This gives Hermes access to all agents/modes configured in OpenCode as native tools — the plugin discovers them automatically.

## Tools

| Tool | Description |
|------|-------------|
| `acp_agents` | List available agents/modes and models |
| `acp_send` | Send a prompt to an agent, get response with tool calls and usage |
| `acp_sessions` | List active sessions for multi-turn conversations |

## Install

```bash
hermes plugins install anpicasso/hermes-plugin-acp-client
```

Then copy and edit the config:

```bash
cd ~/.hermes/plugins/acp-client
cp config.yaml.example config.yaml
# Edit config.yaml — set default_cwd to your workspace
```

Restart the gateway.

## Configuration

`config.yaml`:

```yaml
# Path to the OpenCode binary
opencode_binary: ~/.opencode/bin/opencode

# Default workspace directory for new sessions
default_cwd: ~/omo

# Auto-approve permission requests for autonomous operation
auto_approve: true
```

## How it works

1. On first tool call, spawns `opencode acp` as a subprocess
2. Performs the ACP initialize handshake (protocol version 1)
3. Creates sessions per workspace with full agent/mode discovery
4. Sends prompts, collects streaming responses (thoughts, messages, tool calls, plans)
5. Handles agent→client requests automatically:
   - **Permissions** — auto-approved (configurable)
   - **File I/O** — reads/writes files on the local filesystem
   - **Terminal** — executes commands in subprocesses
6. Returns assembled response with text, tool call summary, execution plan, and usage

The subprocess stays alive between calls for session continuity and efficiency.

## Multi-turn conversations

Pass `session_id` from a previous response to continue the conversation:

```
# First call — creates a new session
acp_send(prompt="Create a FastAPI app with auth", agent="sisyphus")
# → returns session_id: "ses_abc123"

# Follow-up — continues the same session
acp_send(prompt="Add rate limiting to the auth endpoints", session_id="ses_abc123")
```

## Available agents

Agents depend entirely on your OpenCode configuration. Use `acp_agents` to discover what's available in your workspace — the plugin is agent-agnostic and surfaces whatever modes OpenCode exposes.

## Requirements

- [OpenCode](https://github.com/opencode-ai/opencode) v1.2.27+ installed
- OpenCode providers configured (`opencode auth login`)
- PyYAML (included with Hermes)

## Protocol reference

This plugin implements the ACP stdio transport:
- **Wire format**: NDJSON (newline-delimited JSON) over stdin/stdout
- **Protocol**: JSON-RPC 2.0, protocol version 1
- **Bidirectional**: client sends requests to agent, agent sends requests back (permissions, file I/O, terminal)
- **Streaming**: `session/update` notifications carry thoughts, message chunks, tool calls, and plans during prompt processing

See [agentclientprotocol.com](https://agentclientprotocol.com/protocol/overview) for the full spec.
