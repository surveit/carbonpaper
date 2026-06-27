"""
Deterministic: download each located doc URL to a local cache and record the
result. NO LLM, NO new connector kind — this is exactly what the research agents
did with urllib in Bash (the distillation's "download-then-parse" technique).

Input: one row per located doc (from the `locate` LLM stage), with a `url`.
Output: same rows + `local_path` (or null) + `fetch_status` / `content_type`.
Failures are recorded, not raised — a 403/timeout is data, not a crash (the runs
showed CB portals 403 and PROPER refusing connections).
"""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

# Present as a normal browser. A bare bot UA gets 403'd by corporate WAFs
# (Cargill, etc.) even on PUBLIC PDFs — verified: research-UA -> 403, browser-UA
# -> 200 on cargill.com/.../cargill-palm-mill-list.pdf. The research agents this
# DAG was distilled from used WebFetch (browser-like) and got those files; bare
# urllib lost that, silently under-collecting. These are public documents fetched
# at low volume for accountability research.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_CACHE = Path("build/palm_tier2_cache")
_TIMEOUT = 60

# Retry policy. The Musim Mas (musimmas.com) static PDF assets 504 transiently —
# SOURCE_MAP confirms the 504 is load/path-dependent, not a block, and a retry
# clears it. We retry on 5xx + transient transport errors with short backoff.
# 403/404 are real refusals/misses — do NOT retry those (waste).
_MAX_TRIES = 4
_BACKOFF_S = (1.5, 3.0, 6.0)  # sleep before retry 2, 3, 4
_RETRYABLE_STATUS = {500, 502, 503, 504, 408, 429}


def _raw_get(url: str) -> tuple[bytes, str]:
    """Single GET. Raises on any non-200 (HTTPError/URLError) so the caller can
    decide whether the failure is retryable."""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = resp.read()
        ctype = resp.headers.get("Content-Type", "")
    return data, ctype


def _wayback_url(url: str) -> str | None:
    """Ask the Wayback availability API for the newest archived snapshot of `url`.
    Returns a direct snapshot URL (with the `id_` raw-bytes flag so we get the
    original PDF, not a toolbar-wrapped HTML page) or None. Never raises."""
    try:
        api = "https://archive.org/wayback/available?url=" + urllib.parse.quote(url, safe="")
        req = urllib.request.Request(api, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            import json as _json
            meta = _json.loads(resp.read().decode("utf-8", errors="replace"))
        snap = (meta.get("archived_snapshots") or {}).get("closest") or {}
        if snap.get("available") and snap.get("url"):
            wb = snap["url"]
            # Insert the id_ modifier so the CDX serves raw original bytes:
            # http://web.archive.org/web/<ts>/<url>  ->  .../web/<ts>id_/<url>
            import re as _re
            return _re.sub(r"(/web/\d+)/", r"\1id_/", wb, count=1)
    except Exception:  # noqa: BLE001 — fallback only; absence is not an error
        return None
    return None


def _fetch_one(url: str) -> dict:
    if not isinstance(url, str) or not url.startswith("http"):
        return {"local_path": None, "fetch_status": "no_url", "content_type": None}
    _CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]

    last_label = "error:unknown"
    for attempt in range(_MAX_TRIES):
        try:
            data, ctype = _raw_get(url)
            ext = "pdf" if "pdf" in ctype or url.lower().endswith(".pdf") else "html"
            path = _CACHE / f"{key}.{ext}"
            path.write_bytes(data)
            status = f"ok:{len(data)}b" if attempt == 0 else f"ok:{len(data)}b:retry{attempt}"
            return {"local_path": str(path), "fetch_status": status, "content_type": ctype}
        except urllib.error.HTTPError as exc:
            last_label = f"error:HTTP{exc.code}"
            retryable = exc.code in _RETRYABLE_STATUS
        except Exception as exc:  # noqa: BLE001 — transport errors (timeout, reset, DNS)
            last_label = f"error:{type(exc).__name__}"
            # URLError often wraps a transient timeout/connection reset — worth a retry.
            retryable = isinstance(exc, urllib.error.URLError)
        if not retryable or attempt == _MAX_TRIES - 1:
            break
        time.sleep(_BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)])

    # Hard failure after retries — try a Wayback snapshot before giving up.
    wb = _wayback_url(url)
    if wb:
        try:
            data, ctype = _raw_get(wb)
            ext = "pdf" if "pdf" in ctype or url.lower().endswith(".pdf") else "html"
            path = _CACHE / f"{key}.{ext}"
            path.write_bytes(data)
            return {"local_path": str(path),
                    "fetch_status": f"ok:{len(data)}b:wayback", "content_type": ctype}
        except Exception as exc:  # noqa: BLE001
            return {"local_path": None,
                    "fetch_status": f"{last_label};wayback_{type(exc).__name__}",
                    "content_type": None}
    return {"local_path": None, "fetch_status": last_label, "content_type": None}


def transform(located: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in located.iterrows():
        res = _fetch_one(r.get("url"))
        rows.append({**r.to_dict(), **res})
    return pd.DataFrame(rows)
