"""app.compiler.agent — the per-project editing agent that authors a workflow
interactively (chat-driven), the conversational counterpart to the one-shot batch
compile in `app.compiler`.

Split across three modules:
  - `prompt`  — the system prompt + stage-type catalog the agent is built with;
  - `tools`   — the in-process tools the agent calls (read/edit a project's
    workflow) and their claude_agent_sdk MCP wrapping;
  - `config`  — the cached SDK-engine builder the web layer warms and drives.

The generic chat spine is reused verbatim: streaming lives in `app.agent`; turns,
the session store, and the SDK engine that drives claude_agent_sdk.query() live
in `app.core.agent`."""
