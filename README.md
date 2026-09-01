# Carbon Paper — reviewable AI workflows

A chat is a good place to investigate and a terrible place to review. Carbon Paper
turns open-ended work into a constrained workflow where deterministic steps are
predictable and judgment calls are visible. The same structure that makes review
faster makes the investigation easy to rerun and reuse.

Some questions you only ask once. How much did firms spend lobbying about AI last
year. Which of these 400 contracts name the same shell company. What changed
between two editions of a public register.

You are not building a product. You want the answer, and you need it to hold up.

Carbon Paper turns the investigation's method into a workflow: a chain of small named
steps running against the original files. Most steps are ordinary deterministic code.
A step that genuinely needs judgement calls a model, and it is the only one that does.

Every step is then open to expert review:

- it states in plain English what it does, and shows what it changed in your rows
- a step needing judgement can halt the run and wait for a reviewer to decide
- every published figure traces back to the source rows it was computed from
- the whole run exports as a folder that opens in a browser with no app and no network

The goal is not perfect code. It is an answer you can stand behind, a record that
proves it, and a workflow you can run again when next quarter's file lands.

## Try it without installing anything

- **[Read one worked through, step by step](https://carbonpaper.fly.dev/intro)** — one
  lobbying question, from the filings to the published figure. Served from `intro/` in
  this repo, so a local server has it at `/intro` too.
- **[Take the guided tour](https://carbonpaper.fly.dev)** — it seeds a seven-stage
  workflow over real, sourced advocacy records and runs it for real, so the first
  workflow you read is one you watched run.

That instance has no login and anyone can delete anything on it, so treat what you put
there as public and keep real work local.

## Getting started

```
git clone --branch production --depth 1 https://github.com/surveit/carbonpaper
cd carbonpaper && ./start
```

That is the whole install. The machine brings `git`, `curl` and `bash`; `./start`
installs uv if it is missing, builds the venv from `uv.lock`, migrates the store, and
serves <http://127.0.0.1:8765>. Run it again after a `git pull`.

`production` is the branch the public instance runs, so the code you install is the
code you just tried. `master` is where work lands and is not promoted until it is
green.

The server boots and serves with no credential. What fails without one is the steps
that call a model — `claude auth login` once is the only part `./start` will not do
for you. [docs/getting-started.md](docs/getting-started.md) covers signing in, the two
environment variables that override it, and where your state lives.

Your local server opens on the same tour, in place of the project list, until you have
taken it once.

### Your own question

1. **＋ New project** opens a chat with the editing agent. Tell it how the
   investigation works, or upload the write-up and the data it runs on, and it
   creates the project.
2. Author the stages by talking to the agent in the same chat. The project's five
   sections — Overview, Document, Terms, Workflow, Runs — are the left sidebar.
3. **Get your data file onto the server.** A run reads its inputs off the server's
   disk by absolute path, so the run form's Browse… posts the file and hands the path
   back. [Getting a data file in](docs/self-hosting.md#getting-a-data-file-in) covers
   the endpoint, which takes any caller that can run `curl`.
4. **▶ Run workflow**, from the Workflow section or a stored version's page. The run
   form is where a version is picked and each input is pointed at its file.
5. Read the run: the walkthrough, the issue index, and a panel per step showing what
   that step changed. Then export the review packet and hand it to whoever checks it.

## Where the rest is

- Install, signing in, where state lives, the test commands:
  [docs/getting-started.md](docs/getting-started.md)
- What & why: [docs/overview.md](docs/overview.md) — the mission, the locked
  vocabulary, and the three features.
- Code map: [docs/architecture.md](docs/architecture.md)
- Hosting it for other people: [docs/self-hosting.md](docs/self-hosting.md) — the
  upload endpoint and its quotas, and the Fly.io deploy.
- Contributor guide / conventions: [AGENTS.md](AGENTS.md)
