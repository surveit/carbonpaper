"""
Fetch the source documents behind the ingested LobbyMap evidence — for the
broader document-tier (Level-2) analysis.

Policy (human-authorized, non-commercial research):
  - We fetch DOCUMENT FILES only (the /site/data/ attachments = their archived
    copies of third-party documents, plus original-publisher URLs). We never
    touch /evidence/ or /score/ pages and never crawl.
  - Polite by construction: one request at a time, RATE_DELAY seconds apart,
    an identifying User-Agent with a contact email, no retries hammering.
  - Resumable: everything is cached under source_docs/ keyed by URL hash;
    re-running skips completed fetches. Failures are recorded, not retried
    endlessly.

Output:
  source_docs/<sha1>.<ext>     — raw bytes
  source_docs/index.jsonl      — {url, file, status, content_type, chars} per URL
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

HERE = Path(__file__).resolve().parent
GT = HERE.parent.parent / "lobbymap" / "eval" / "ground_truth"
DOCS = HERE / "source_docs"
RATE_DELAY = 2.5  # seconds between requests — be a polite guest
UA = ("prototype-one research fetcher (journalism/eval, non-commercial; "
      "contact: shuhanbao@gmail.com)")


def absolute(url: str) -> str:
    if url.startswith("/site/") or url.startswith("/site//"):
        return "https://lobbymap.org" + url
    return url


def extract_text(data: bytes, content_type: str, url: str) -> str | None:
    if data[:4] == b"%PDF" or "pdf" in content_type or url.lower().endswith(".pdf"):
        try:
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
        except Exception:  # noqa: BLE001
            return None
    soup = BeautifulSoup(data.decode("utf-8", errors="replace"), "lxml")
    for t in soup(["script", "style", "nav", "footer", "header"]):
        t.decompose()
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))


def main() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    index_path = DOCS / "index.jsonl"
    done: dict[str, dict] = {}
    if index_path.exists():
        for line in index_path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            done[r["url"]] = r

    # One URL per evidence row: prefer the original publisher; fall back to the
    # lobbymap-hosted archived copy.
    wanted: list[str] = []
    for line in (GT / "gt_scored_evidence.jsonl").read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        links = e.get("source_links") or []
        publisher = [u for u in links if "lobbymap.org" not in u and not u.startswith("/site")]
        hosted = [u for u in links if u not in publisher]
        # publisher first; if the publisher fetch already FAILED (per the index),
        # fall back to InfluenceMap's archived copy — that is what it exists for.
        primary = (publisher or hosted or [None])[0]
        if primary is None:
            continue
        primary = absolute(primary)
        prior = done.get(primary)
        if prior is not None and prior.get("status") != "ok" and hosted:
            wanted.append(absolute(hosted[0]))
        else:
            wanted.append(primary)
    todo = [u for u in dict.fromkeys(wanted) if u not in done]
    print(f"{len(set(wanted))} unique doc URLs; {len(done)} cached; {len(todo)} to fetch "
          f"(~{len(todo) * RATE_DELAY / 60:.0f} min at {RATE_DELAY}s spacing)")

    session = requests.Session()
    session.headers["User-Agent"] = UA
    with index_path.open("a", encoding="utf-8") as idx:
        for i, url in enumerate(todo, 1):
            time.sleep(RATE_DELAY)
            rec: dict = {"url": url}
            try:
                r = session.get(url, timeout=60)
                rec["http_status"] = r.status_code
                if r.ok and r.content:
                    ctype = r.headers.get("content-type", "")
                    ext = ".pdf" if (r.content[:4] == b"%PDF" or "pdf" in ctype) else ".html"
                    name = hashlib.sha1(url.encode()).hexdigest()[:16] + ext
                    (DOCS / name).write_bytes(r.content)
                    text = extract_text(r.content, ctype, url)
                    rec.update({"status": "ok" if text and len(text) >= 200 else "no_text",
                                "file": name, "content_type": ctype,
                                "chars": len(text) if text else 0})
                else:
                    rec["status"] = "http_error"
            except Exception as exc:  # noqa: BLE001
                rec["status"] = "fetch_error"
                rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
            idx.write(json.dumps(rec, ensure_ascii=False) + "\n")
            idx.flush()
            print(f"[{i}/{len(todo)}] {rec.get('status'):11} {url[:90]}")

    rows = [json.loads(l) for l in index_path.read_text(encoding="utf-8").splitlines()]
    ok = sum(1 for r in rows if r.get("status") == "ok")
    print(f"\nindex: {len(rows)} URLs, {ok} with usable text")


if __name__ == "__main__":
    main()
