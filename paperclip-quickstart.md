# Paperclip Integration — Quick Start

[Paperclip](https://github.com/paperclipai/paperclip) is an open-source AI agent orchestration platform that lets you assign tasks to multiple AI agents via a Kanban-style board. This integration adds Paperclip as a **third input channel** alongside tkinter and Telegram.

## How it works

When an AI agent calls `get_remote_input` with a `paperclip_agent_id`, it registers itself as "waiting for input." Paperclip can then wake the agent up by sending a heartbeat with task instructions — just like a human answering from tkinter or Telegram.

### Three input channels, first-wins

| Channel | How the user responds |
|---|---|
| **Local tkinter dialog** | Type in the GUI window |
| **Telegram** | Reply from phone via bot or Mini App |
| **Paperclip heartbeat** | Board operator assigns a task in Paperclip's web UI |

Whichever channel delivers an answer first wins. The others are cancelled automatically.

### Reverse path

If a human answers via tkinter or Telegram (instead of Paperclip), the answer is posted back to the Paperclip issue as a comment tagged **"Human-Intercepted Input"**.

## Setup

### 1. Install & run Paperclip

```bash
npx paperclipai onboard --yes
```

Or manually:

```bash
git clone https://github.com/paperclipai/paperclip.git
cd paperclip
pnpm install
pnpm dev
```

This starts the API server at `http://localhost:3100`. An embedded PostgreSQL database is created automatically.

> Requirements: Node.js 20+, pnpm 9.15+

### 2. Create the configuration file

```bash
cp paperclip_config.example.json paperclip_config.json
```

Edit `paperclip_config.json`:

```json
{
  "enabled": true,
  "webhook_port": 8765,
  "paperclip_api_url": "http://localhost:3100/api",
  "agent_api_key": "<your-key-from-paperclip-dashboard>",
  "priority_mode": "first_wins",
  "reverse_path": true
}
```

| Field | Description |
|---|---|
| `enabled` | Master switch for the integration |
| `webhook_port` | Port for the webhook HTTP server (default: 8765) |
| `paperclip_api_url` | Paperclip API base URL |
| `agent_api_key` | JWT API key from the Paperclip dashboard |
| `priority_mode` | `"first_wins"` (default) — first channel to answer wins |
| `reverse_path` | Post human answers back to Paperclip issues |

### 3. Start the MCP server

```bash
python human_loop_server.py
```

If `paperclip_config.json` exists and `enabled` is `true`, the webhook server starts automatically on the configured port.

### 4. Configure Paperclip's HTTP adapter

In the Paperclip web dashboard, create an **HTTP adapter** pointing to:

```
http://localhost:8765/heartbeat
```

This tells Paperclip where to send heartbeats when agents need to wake up.

### 5. AI agents declare their identity

When calling `get_remote_input`, agents pass their Paperclip agent ID:

```python
result = await get_remote_input(
    title="Waiting for instructions",
    prompt="What should I work on next?",
    paperclip_agent_id="my-coding-agent",
)
```

This registers the agent as available for Paperclip task assignment.

## Webhook endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/heartbeat` | POST | Receives heartbeats from Paperclip |
| `/status` | GET | Lists currently pending prompts |
| `/health` | GET | Simple health check |

## Use cases

- **Multi-agent orchestration:** Run 5+ agents in parallel; assign tasks from Paperclip's board instead of typing in each dialog.
- **Human oversight at scale:** Require approval in Paperclip before agents proceed.
- **Task queuing:** Agents idle at `get_remote_input` and automatically wake up when new work is assigned.
- **Audit trail:** All interactions logged in `logs/paperclip_bridge.log` and in Paperclip.

## Graceful degradation

- If Paperclip is not configured, everything works exactly as before.
- If Paperclip fails at runtime, the MCP server continues operating — only Paperclip features are disabled.
- Agents that don't pass `paperclip_agent_id` are unaffected.
