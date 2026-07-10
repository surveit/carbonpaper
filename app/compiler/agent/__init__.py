"""app.compiler.agent — the per-project editing agent that authors a workflow
interactively (chat-driven), the conversational counterpart to the one-shot batch
compile in `app.compiler`.

Split across three modules:
  - `prompt`  — the system prompt + stage-type catalog the agent is built with;
  - `tools`   — the in-process tools the agent calls (read/edit a project's
    workflow) and their claude_agent_sdk MCP wrapping;
  - `config`  — the cached agent/engine builders the web layer warms and drives.

The generic chat spine (streaming, turns, session store, the SDK engine that
drives claude_agent_sdk.query()) lives in `app.chat` and is reused verbatim."""
