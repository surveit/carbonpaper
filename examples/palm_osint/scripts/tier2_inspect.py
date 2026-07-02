"""
Tier-2 LLM inspector / REPL.

The Tier-2 extract returns mostly "unknown" because the default backend asks the
model a tool-less question about mills it has never seen. This tool lets you SEE
that pulling and improve it: render the exact prompt, run it, and watch the agent
think and (optionally) search the web LIVE — then tweak the prompt / model /
tools and rerun, or just talk to the model freeform.

Run from the repo root:

    # interactive
    python -m examples.palm_osint.scripts.tier2_inspect

    # one-shot: research one facility WITH web search, stream the trace
    python -m examples.palm_osint.scripts.tier2_inspect --facility palm:PO1000000058 --tools

    # freeform question to the model (with or without --tools)
    python -m examples.palm_osint.scripts.tier2_inspect --ask "What is the Sungai Lilin palm oil mill and who operates it?" --tools

Commands inside the REPL: :list  :pick <n|id>  :tools on|off  :model <m>
    :show  :sys <text>  :run  :ask <text>  :raw  :help  :q
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows consoles default to cp1252 and choke on the model's unicode + our
# decorations. Force UTF-8 and never crash on an un-encodable char.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Make `app` and `examples` importable when run as a script or module.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from app.runtime import llm, llm_agent_sdk  # noqa: E402
from examples.palm_osint.code import flatten_facilities, select_for_enrichment  # noqa: E402

METH_DIR = REPO_ROOT / "examples" / "palm_osint"
EXTRACT_YAML = METH_DIR / "compiled" / "07_tier2_extract.yaml"

DIM, RESET, BOLD, CYAN, GREEN, YELLOW, RED = (
    "\033[2m", "\033[0m", "\033[1m", "\033[36m", "\033[32m", "\033[33m", "\033[31m"
)


def _load_facilities(limit: int = 5) -> pd.DataFrame:
    """Reproduce the select_for_enrichment set straight from facilities.jsonl
    (no prior run required)."""
    raw = pd.read_json(METH_DIR / "data" / "facilities.jsonl", lines=True)
    flat = flatten_facilities.transform(raw)
    sel = select_for_enrichment.transform(flat)
    return sel.head(limit).reset_index(drop=True)


def _load_prompt_template() -> str:
    import yaml
    stage = yaml.safe_load(EXTRACT_YAML.read_text(encoding="utf-8"))
    return stage["llm"]["prompt_template"]


def _print_event(ev: dict) -> None:
    k = ev.get("kind")
    if k == "thinking":
        print(f"{DIM}  . thinking: {ev['text'][:200].strip()}{RESET}")
    elif k == "tool_use":
        print(f"{CYAN}  -> {ev['name']}({ev['input']}){RESET}")
    elif k == "tool_result":
        print(f"{DIM}  <- {str(ev['content']).strip()[:240]}{RESET}")
    elif k == "text":
        # narration before the final JSON; show dim so the JSON stands out
        t = ev["text"].strip()
        if t:
            print(f"{DIM}  {t[:200]}{RESET}")
    elif k == "result":
        print(f"{DIM}  [done: {ev.get('num_turns')} turns, error={ev.get('is_error')}]{RESET}")
    elif k == "error":
        print(f"{RED}  [error: {ev['text'][:200]}]{RESET}")


class Session:
    def __init__(self, tools: bool, model: str):
        self.facilities = _load_facilities()
        self.template = _load_prompt_template()
        self.idx = 0
        self.tools = tools
        self.model = model
        self.system = None
        self.last_text = ""

    @property
    def row(self) -> dict:
        return self.facilities.iloc[self.idx].to_dict()

    def rendered_prompt(self) -> str:
        return llm.render_prompt(self.template, self.row)

    def _tools_arg(self):
        return llm_agent_sdk.RESEARCH_TOOLS if self.tools else None

    def run_prompt(self, prompt: str) -> None:
        mode = f"{GREEN}WEB RESEARCH{RESET}" if self.tools else f"{YELLOW}no tools{RESET}"
        print(f"\n{BOLD}running {self.model} [{mode}]{RESET} ...\n")
        try:
            res = llm_agent_sdk.run_query(
                prompt, self.model, tools=self._tools_arg(), on_event=_print_event
            )
        except Exception as exc:
            print(f"{RED}call failed: {exc}{RESET}")
            return
        self.last_text = res["text"]
        parsed = llm._parse_text_result(res["text"])
        print(f"\n{BOLD}-- parsed result --{RESET}")
        if isinstance(parsed, (list, dict)):
            print(json.dumps(parsed, indent=2, ensure_ascii=False))
            self._summarize(parsed)
        else:
            print(f"{YELLOW}(not valid JSON — raw text below){RESET}\n{res['text'][:1200]}")

    @staticmethod
    def _summarize(parsed) -> None:
        items = parsed if isinstance(parsed, list) else [parsed]
        cited = sum(1 for it in items if isinstance(it, dict) and it.get("evidence_urls"))
        present = sum(1 for it in items if isinstance(it, dict)
                      and str(it.get("asserted_present")).lower() in ("true", "false"))
        print(f"\n{BOLD}{len(items)} features · {present} with a definite stance · "
              f"{cited} with cited evidence_urls{RESET}")

    def run_extract(self) -> None:
        f = self.row
        print(f"\n{BOLD}facility:{RESET} {f['name']} ({f['facility_id']}) — "
              f"{f.get('owner')}, {f.get('country')}")
        self.run_prompt(self.rendered_prompt())


def _cmd_loop(s: Session) -> None:
    def show_facilities():
        for i, r in s.facilities.iterrows():
            mark = ">" if i == s.idx else " "
            print(f" {mark} [{i}] {r['name']:<16} {r['facility_id']:<22} {r.get('country')}")

    print(f"{BOLD}Tier-2 inspector{RESET}  —  :help for commands, :q to quit")
    print(f"backend: {llm.resolve_backend()} · model: {s.model} · "
          f"tools: {'on' if s.tools else 'off'}")
    show_facilities()
    while True:
        try:
            line = input(f"\n{BOLD}tier2>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in (":q", ":quit", ":exit"):
            return
        elif line in (":help", "?"):
            print(__doc__.split("Commands inside")[1] if "Commands inside" in __doc__ else "")
            print("  :list  :pick <n|id>  :tools on|off  :model <m>  :show  "
                  ":sys <text>  :run  :ask <text>  :raw  :q")
        elif line == ":list":
            show_facilities()
        elif line.startswith(":pick"):
            arg = line[5:].strip()
            ids = list(s.facilities["facility_id"])
            if arg.isdigit() and int(arg) < len(s.facilities):
                s.idx = int(arg)
            elif arg in ids:
                s.idx = ids.index(arg)
            else:
                print(f"{RED}no such facility{RESET}"); continue
            print(f"picked {s.row['name']} ({s.row['facility_id']})")
        elif line.startswith(":tools"):
            arg = line[6:].strip().lower()
            s.tools = arg in ("on", "true", "1", "yes", "")
            print(f"web research tools: {'on' if s.tools else 'off'}")
        elif line.startswith(":model"):
            s.model = line[6:].strip() or s.model
            print(f"model: {s.model}")
        elif line == ":show":
            print(f"\n{DIM}{s.rendered_prompt()}{RESET}")
        elif line.startswith(":sys"):
            s.system = line[4:].strip() or None
            print(f"system override {'set' if s.system else 'cleared'}")
        elif line == ":run":
            s.run_extract()
        elif line.startswith(":ask"):
            q = line[4:].strip()
            if q:
                s.run_prompt(q)
        elif line == ":raw":
            print(s.last_text[:4000] or "(nothing yet)")
        else:
            print(f"{RED}unknown command{RESET} (try :help)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Tier-2 LLM inspector / REPL")
    ap.add_argument("--facility", help="facility_id to run once (non-interactive)")
    ap.add_argument("--ask", help="freeform question to run once (non-interactive)")
    ap.add_argument("--tools", action="store_true", help="grant WebSearch/WebFetch")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--list", action="store_true", help="list facilities and exit")
    args = ap.parse_args()

    if not llm_agent_sdk.available():
        print(f"{RED}agent SDK backend unavailable: {llm_agent_sdk.status()}{RESET}")
        return 1

    s = Session(tools=args.tools, model=args.model)

    if args.list:
        for i, r in s.facilities.iterrows():
            print(f"[{i}] {r['facility_id']:<22} {r['name']:<18} {r.get('country')}")
        return 0
    if args.ask:
        s.run_prompt(args.ask)
        return 0
    if args.facility:
        ids = list(s.facilities["facility_id"])
        if args.facility not in ids:
            print(f"{RED}unknown facility {args.facility}; options:{RESET} {ids}")
            return 1
        s.idx = ids.index(args.facility)
        s.run_extract()
        return 0

    _cmd_loop(s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
