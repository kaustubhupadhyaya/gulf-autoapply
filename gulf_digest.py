"""Gulf job digest: data/AI jobs that no bot applies to, from agency listings and employer boards. No browser, no login.

Reads the rules and sources in gulf_config.json ("digest"), fetches each source politely (public pages and public job-board
JSON), keeps the jobs that fit (title keep/drop words, Gulf location, years of experience <= max_years, no "UAE national" or
"Arabic required" text), and writes state/gulf_digest.json in the shape the board's shortlist page reads:
    {"updated": ..., "items": [{title, url, company, location, reason, posted, first_seen, new, identity}], "sources": {...}}
state/gulf_digest_seen.json remembers when each job was first seen, so "new" means first seen in the last day.

    gulf_digest.py              run once (what the daily task does)
    gulf_digest.py --show       print the current list
Exit codes: 0 ok (at least one source answered), 1 every source failed.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE, LOGS = HERE / "state", HERE / "logs"
STATE.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)
OUT, SEEN, LOG = STATE / "gulf_digest.json", STATE / "gulf_digest_seen.json", LOGS / "gulf_digest.log"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg: str, level: str = "INFO") -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


CONFIG_PATH = Path(os.environ.get("GULF_CONFIG") or (
    HERE / "gulf_config.json" if (HERE / "gulf_config.json").is_file()
    else HERE / "gulf_config.example.json"))


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["digest"]
    local = CONFIG_PATH.with_name("gulf_config.local.json")
    if local.exists():
        try:
            over = json.loads(local.read_text(encoding="utf-8")).get("digest", {})
            cfg.update({k: v for k, v in over.items() if k != "sources"})
            cfg["sources"] = cfg["sources"] + over.get("sources", [])
        except (OSError, ValueError) as e:
            log(f"gulf_config.local.json ignored: {e}", "WARN")
    return cfg


def fetch(url: str, as_json: bool = False):
    # Michael Page's CMS answers 404 to a page request that asks for JSON, so ask for exactly what is wanted
    accept = "application/json" if as_json else "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if as_json else body


def text_of(markup: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(markup or "")))).strip()


def has_word(text: str, words) -> str | None:
    low = text.lower()
    for w in words:
        if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low):
            return w
    return None


# ------------------------------------------------------------------ sources: each returns [{title, url, company, location, posted, desc}]
def src_michaelpage(s: dict, cfg: dict) -> list:
    """Only page 1 (15 jobs) is reachable without a browser: the site's ?page= answers 404 and ?pg= repeats page 1."""
    out = []
    for t in fetch(s["url"]).split('<li class="views-row">')[1:]:
        m = re.search(r'<h3><a href="([^"]+)"[^>]*>(.*?)</a></h3>', t, re.S)
        if not m:
            continue
        loc = re.search(r'class="job-location">(.*?)</div>', t, re.S)
        summ = re.search(r'class="job_advert__job-summary-text">(.*?)</div>', t, re.S)
        out.append({"title": text_of(m.group(2)), "url": "https://www.michaelpage.ae" + html.unescape(m.group(1)),
                    "company": s["company"], "location": text_of(loc.group(1)) if loc else "", "posted": "",
                    "desc": text_of(summ.group(1)) if summ else "", "partial": True})
    return out


def src_greenhouse(s: dict, cfg: dict) -> list:
    d = fetch(f"https://boards-api.greenhouse.io/v1/boards/{s['board']}/jobs", as_json=True)
    return [{"title": j.get("title", ""), "url": j.get("absolute_url", ""), "company": s["company"],
             "location": (j.get("location") or {}).get("name", ""), "posted": str(j.get("updated_at", ""))[:10], "desc": "",
             "detail": f"https://boards-api.greenhouse.io/v1/boards/{s['board']}/jobs/{j.get('id')}"} for j in d.get("jobs", [])]


def src_lever(s: dict, cfg: dict) -> list:
    d = fetch(f"https://api.lever.co/v0/postings/{s['company_token']}?mode=json", as_json=True)
    out = []
    for j in d if isinstance(d, list) else []:
        made = j.get("createdAt")
        out.append({"title": j.get("text", ""), "url": j.get("hostedUrl", ""), "company": s["company"],
                    "location": (j.get("categories") or {}).get("location", "") or "",
                    "posted": datetime.fromtimestamp(made / 1000).strftime("%Y-%m-%d") if made else "",
                    "desc": j.get("descriptionPlain", "") or ""})
    return out


def src_workable(s: dict, cfg: dict) -> list:
    d = fetch(f"https://apply.workable.com/api/v1/widget/accounts/{s['account']}?details=true", as_json=True)
    return [{"title": j.get("title", ""), "url": j.get("url") or j.get("shortlink", ""), "company": s["company"],
             "location": ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x),
             "posted": str(j.get("published_on", ""))[:10], "desc": text_of(j.get("description", ""))} for j in d.get("jobs", [])]


def src_ashby(s: dict, cfg: dict) -> list:
    d = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{s['board']}", as_json=True)
    return [{"title": j.get("title", ""), "url": j.get("jobUrl", ""), "company": s["company"], "location": j.get("location", "") or "",
             "posted": str(j.get("publishedAt", ""))[:10], "desc": j.get("descriptionPlain", "") or text_of(j.get("descriptionHtml", ""))}
            for j in d.get("jobs", [])]


SOURCES = {"michaelpage": src_michaelpage, "greenhouse": src_greenhouse, "lever": src_lever, "workable": src_workable, "ashby": src_ashby}


# ------------------------------------------------------------------ rules
def years_required(desc: str) -> int:
    """The largest 'N years' in the text (as the LinkedIn and NaukriGulf bots read it), ignoring figures above 12."""
    nums = [int(n) for n in re.findall(r"(\d{1,2})\s*\+?\s*(?:-|to|–)?\s*\d{0,2}\s*\+?\s*years?", desc.lower())]
    return max([n for n in nums if n <= 12], default=0)


def judge(j: dict, s: dict, cfg: dict):
    """(keep?, identity, reason) for one listing."""
    title = j["title"]
    if has_word(title, cfg["title_drop"]):
        return False, "", f"title has '{has_word(title, cfg['title_drop'])}'"
    identity = "data" if has_word(title, cfg["title_keep"]) else "ai" if has_word(title, cfg["title_keep_ai"]) else ""
    if not identity:
        return False, "", "title is not a data or AI engineering role"
    loc = (j.get("location") or "").lower()
    if loc:
        if not has_word(loc, cfg["location_words"]):
            return False, "", f"not in the Gulf ({j['location'][:40]})"
    elif s.get("location_required"):
        return False, "", "no location on a global board"
    desc = j.get("desc") or ""
    bad = has_word(f"{title} . {desc}", cfg["drop_text"])
    if bad:
        return False, "", f"text says '{bad}'"
    yrs = years_required(desc)
    if yrs > int(cfg["max_years"]):
        return False, "", f"asks for {yrs} years"
    return True, identity, ""


def enrich_greenhouse(j: dict) -> None:
    """The list has no description: read it for the jobs that already fit, so the years and nationality rules can apply."""
    try:
        d = fetch(j["detail"], as_json=True)
        j["desc"] = text_of(d.get("content", ""))
    except (urllib.error.URLError, ValueError, OSError):
        pass


def load_seen() -> dict:
    try:
        return json.loads(SEEN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def run(only: list | None = None) -> int:
    cfg = load_config()
    if only:
        cfg["sources"] = [s for s in cfg["sources"] if s["key"] in only]
    seen = load_seen()
    now = datetime.now()
    sources, items, ok_count = {}, [], 0
    for s in cfg["sources"]:
        info = {"ok": False, "fetched": 0, "matched": 0, "error": None}
        sources[s["key"]] = info
        try:
            jobs = SOURCES[s["type"]](s, cfg)
            info.update(ok=True, fetched=len(jobs))
            ok_count += 1
        except (urllib.error.URLError, ValueError, OSError, KeyError) as e:
            info["error"] = f"{type(e).__name__}: {str(e)[:100]}"
            log(f"{s['key']}: {info['error']}", "WARN")
            continue
        kept, why = [], {}
        for j in jobs:
            ok, identity, reason = judge(j, s, cfg)
            if ok and s["type"] == "greenhouse":
                enrich_greenhouse(j)
                ok, identity, reason = judge(j, s, cfg)
                time.sleep(random.uniform(1.0, 2.0))
            if ok:
                kept.append((j, identity))
            else:
                why[reason.split(" (")[0]] = why.get(reason.split(" (")[0], 0) + 1
        info["matched"] = len(kept)
        log(f"{s['key']}: {len(jobs)} fetched, {len(kept)} kept; dropped: {dict(sorted(why.items(), key=lambda kv: -kv[1])[:4])}")
        for j, identity in kept:
            ref = j["url"] or f"{s['key']}:{j['title']}"
            first = seen.setdefault(ref, now.isoformat(timespec="seconds"))
            items.append({"title": j["title"], "url": j["url"], "company": j["company"], "location": j["location"] or "Gulf",
                          "posted": j["posted"], "identity": identity, "first_seen": first,
                          "new": datetime.fromisoformat(first) > now - timedelta(days=1),
                          "reason": ("Employer-site apply" if s["type"] != "michaelpage" else "Agency listing; the agency applies for you")
                                    + f" ({s['key']}){'; AI role (for the AI Engineer profile)' if identity == 'ai' else ''}"
                                    + ("; requirements not read (summary only)" if j.get("partial") else "")})
        time.sleep(random.uniform(*cfg["pace_seconds"]))
    if not ok_count:
        log("every source failed", "ERROR")
        return 1
    if only:                                    # a partial run is for looking, never for replacing the full list
        for it in items:
            print(f"  {it['identity']:4} {it['company'][:22]:22} {it['title'][:60]:60} {it['location'][:24]}")
        log(f"partial run ({', '.join(only)}): {len(items)} jobs kept; nothing written")
        return 0
    seen = {k: v for k, v in seen.items() if datetime.fromisoformat(v) > now - timedelta(days=60)}
    uniq = {}
    for it in items:                                    # one posting per company + title + place
        uniq.setdefault((it["company"].lower(), it["title"].lower(), it["location"].lower()), it)
    items = sorted(uniq.values(), key=lambda it: (not it["new"], it["identity"] != "data", it["company"], it["title"]))
    total, new_count = len(items), sum(1 for it in items if it["new"])
    # Portals a bot cannot read (Cloudflare or a bot check answers any automated client): one saved-search link each, for a human.
    items += [{"title": m["title"], "url": m["url"], "company": m["company"], "location": "Gulf", "posted": "", "identity": "",
               "first_seen": "2000-01-01T00:00:00", "new": False, "manual": True,
               "reason": "Saved search: open it and apply by hand (no bot can read this portal)"} for m in cfg.get("manual_searches", [])]
    payload = {"updated": now.isoformat(timespec="minutes"), "total": total, "new_count": new_count,
               "sources": sources, "items": items}
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(OUT)
    SEEN.write_text(json.dumps(seen, indent=0), encoding="utf-8")
    log(f"digest written: {total} jobs, {new_count} new, {len(items) - total} saved searches, {ok_count} of {len(cfg['sources'])} sources answered")
    return 0


def show() -> int:
    try:
        d = json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("no digest yet")
        return 1
    print(f"{d['updated']}: {d['total']} jobs, {d['new_count']} new")
    for k, v in d["sources"].items():
        print(f"  {k:18} {'ok ' if v['ok'] else 'ERR'} fetched {v['fetched']:4} kept {v['matched']:3} {v['error'] or ''}")
    for it in d["items"][:60]:
        print(f"  {'NEW ' if it['new'] else '    '}{it['identity']:4} {it['company'][:22]:22} {it['title'][:58]:58} {it['location'][:24]}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--show", action="store_true", help="print the current list and exit")
    ap.add_argument("--only", nargs="+", metavar="KEY", help="fetch only these source keys (the list keeps the other sources' last jobs out)")
    args = ap.parse_args()
    sys.exit(show() if args.show else run(args.only))
