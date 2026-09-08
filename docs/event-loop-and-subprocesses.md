# The event loop and subprocesses

Spawning a child process is a capability of the *event loop*, not of the process. A chat turn
needs it: `app/core/agent/turns.py` starts the turn with `asyncio.create_task` on the server's
own loop, and `app/core/agent/sdk_engine.py` spawns the `claude` CLI from there.

## Why a loop can lack it

`asyncio.BaseEventLoop._make_subprocess_transport` is a bare `raise NotImplementedError`. A
loop class that never overrides it fails on the first spawn rather than at startup, and says
nothing about why: `str(NotImplementedError())` is `""`. The turn dies with

    CLIConnectionError: Failed to start Claude Code:

and nothing after the colon — once per turn, with no other symptom, on a server whose UI is
otherwise working.

On Windows, `asyncio.SelectorEventLoop` is one of these loops, and uvicorn hands a selector
loop to its subprocess supervisor — the process that runs under `--reload`, or with
`workers > 1`. So a Windows dev server started as plain `uvicorn app.main:app --reload` serves
a UI whose chat cannot run.

The workflow runtime is not exposed to this. It reaches the LLM through `run_sync`
(`app/core/llm_sdk.py`), which hops to its own thread and calls `asyncio.run`, getting the
platform default loop — a proactor loop on Windows.

## What the code does about it

| Name (`app/core/event_loop.py`) | What it does |
| --- | --- |
| `create_event_loop()` | The zero-argument uvicorn `--loop` factory: a `ProactorEventLoop` on Windows, `asyncio.new_event_loop()` elsewhere. `start` passes `--loop app.core.event_loop:create_event_loop`. |
| `can_spawn_subprocesses(loop_or_class)` | True when the loop's type carries a `_make_subprocess_transport` of its own, instead of the base class's unimplemented one. Asking the type answers the capability itself, unlike a platform test or a class-name test. |
| `validate_running_loop_can_spawn_subprocesses()` | Raises `EventLoopCannotSpawnSubprocesses` when the running loop fails that test. |

`app/main.py` calls the guard as the first statement of the FastAPI lifespan, so a server
whose loop cannot spawn refuses to start instead of serving a UI whose chat is broken.

## The trade-off

The guard is a hard failure, not a warning. Launching the app any way that does not pin the
factory — `uvicorn app.main:app --reload` on Windows, or an embedding host that builds its own
selector loop — now stops at startup with an error naming `--loop`, where before it started
and only chat was broken. That is the intent: the startup message is loud, names the fix, and
arrives before anyone spends a session on the empty `CLIConnectionError`.
