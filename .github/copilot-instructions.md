# AI Agent Instructions

## Human-in-the-Loop Tool Usage

The `get_remote_input` MCP tool (exposed as `mcp_human-in-the-_get_remote_input`) is the primary channel for all human interaction in this workspace. The following rules are **mandatory and non-negotiable**.

### Core Rules

1. **Use `get_remote_input` for every interaction** — questions, answers, clarifications, confirmations, status updates, and follow-ups must all go through this tool.

2. **Always close with a follow-up prompt** — at the end of every response, ask the user whether they need any further assistance using `get_remote_input`.

3. **Ask before concluding** — before delivering any final answer, summary, or ending the session, use `get_remote_input` to ask the user if there is anything else they would like to add or clarify.

4. **Never stop on a canceled or interrupted dialog** — if a dialog is canceled, times out, or ends unexpectedly, do NOT stop. Keep prompting the user via `get_remote_input` until they explicitly respond with `"end"` or `"stop"`.

5. **Ask instead of assuming** — if any context, information, or clarification is missing, use `get_remote_input` to request it. Do not make assumptions or guesses about information you do not have access to.

### When to Use `get_remote_input` vs Other Tools

| Situation | Action |
|---|---|
| Need user input, confirmation, or feedback | Use `get_remote_input` |
| Need a short single-line answer | Use `get_remote_input` |
| Ending any response or work session | Use `get_remote_input` to ask if further help is needed before you actually end the session |
| Dialog was dismissed or timed out | Use `get_remote_input` again — do not give up |
| Missing information to proceed | Use `get_remote_input` to ask — never guess |

### Telegram Remote Answering

`get_remote_input` supports dual-channel answering: the user can respond from either the local GUI dialog or from Telegram on their phone. Both channels are synchronized — answering on one automatically closes the other. If Telegram is not configured, the tool falls back to the local GUI dialog only.

### Summary

> The `get_remote_input` tool is the bridge between this AI agent and the human. Treat it as the **only** valid communication channel for all interactions with the user.