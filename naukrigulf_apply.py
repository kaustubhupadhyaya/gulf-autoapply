"""NaukriGulf auto-applier: finds Gulf data-engineering jobs that take a one-click "Easy Apply" and lists them
(dry run, the default). Applying is built but locked: it needs --live AND the environment variable
NAUKRIGULF_APPLY_LIVE=1, and nothing in this repo sets that variable.

One account, NaukriGulf A (expected_email in the config), in its own Chrome profile g6-apply (a copy of the
Google-signed-in google-6 profile, so the user's own browser is never touched). Each run:
  1. Starts headless Chrome on that profile over the DevTools protocol (edge_cdp.py, no WebDriver) and opens
     the dashboard, so the site's own scripts refresh the session. Before anything else it confirms through
     GET /ngprofile/user/self that the signed-in user is the expected email. Nothing here types a password:
     a signed-out profile stops the run with exit 42 and a marker file.
  2. Reads the account's applied jobs (GET /spapply/jobapplier/user/v1/easyApplyList and .../webdetails, the
     lists behind https://www.naukrigulf.com/mnj/userProfile/whtma).
  3. Discovery: GET /spapi/jobapi/search once per keyword (curated titles + the Naukri bot's relevance
     keywords) over the six GCC countries in one query, Easy Apply and last-30-days filtered on the server,
     newest first, a few pages each, with jittered pacing. Then GET /spapi/jobs/<jobId> for every job that
     passes the cheap filters, for the nationality field and the job page's own flags.
  4. Filters (fail closed): Easy Apply from an enabled source (default: jobs the employer posted on
     NaukriGulf; external-feed FBA and Naukri-India IMS listings are off until their apply path has been
     seen once); minimum experience <= 5 years; posted within 30 days; one of the six countries; nationality open to an
     Indian national ("Any" or India); title relevance on word boundaries (the Naukri bot's relevance
     keywords, off-role and too-senior titles excluded, same logic family as instahyre_apply.py); not
     applied (the site's flag, the Applied list, the local DB); one posting per company + title.
  5. --dry-run: writes state/naukrigulf_dryrun_<YYYYMMDD>.json and .md. Nothing is clicked or posted.
     --live (armed): opens each job page and presses the site's own "Easy Apply" button, so the site builds
     its own request (POST /spapply/jobapplier/apply/v2). A job that answers with a layer instead (custom
     questions 4001, profile update 4030-4044) is left unanswered and recorded. An application counts as
     confirmed only once it appears in the Applied list; state/naukrigulf_progress.json carries the
     confirmed count and the last-progress time for a progress-based watcher.

Exit codes: 0 finished, 44 daily cap reached, 42 needs the user (signed out, wrong account, profile not
apply-ready), 47 throttled or blocked by the site, 3 --live without NAUKRIGULF_APPLY_LIVE=1, 1 other error.
"""
from __future__ import annotations

import argparse
import base64
import json
import msvcrt
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import websocket

from edge_cdp import CDPError, EdgeSession

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("NAUKRIGULF_CONFIG") or (
    HERE / "naukrigulf_config.json" if (HERE / "naukrigulf_config.json").is_file()
    else HERE / "naukrigulf_config.example.json"))
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
STATE_DIR = HERE / "state"
LOG_DIR = HERE / "logs"
STATE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
LOG_PATH = LOG_DIR / "naukrigulf_apply.log"
DB_PATH = STATE_DIR / "naukrigulf_apply.db"
PROGRESS_PATH = STATE_DIR / "naukrigulf_progress.json"

SITE = "https://www.naukrigulf.com"
PAGE_SIZE = 30
SEARCH = "/spapi/jobapi/search"
JOB = "/spapi/jobs/{jid}?seo=1&locationId={loc}&nationality=&xz=&source="
SELF = "/ngprofile/user/self?section=basicDetails,applyReadyDetails"
APPLIED_LISTS = (("easy", "/spapply/jobapplier/user/v1/easyApplyList?limit=50&offset={off}&dashboardActivity=false&xz=",
                  "totalEasyApplies"),
                 ("web", "/spapply/jobapplier/user/v1/webdetails?limit=50&offset={off}", "totalWebApplies"))
APPLY_FRAGMENT = "/jobapplier/apply/v2"     # the site posts to /spapply/jobapplier/apply/v2 (jobDescription bundle)
# jobSource values, from the site's own bundle: POSTED, PJP, FBA (form based), EMAIL and IMS show "Easy Apply";
# WEB and TAG send the user to the employer's site. Seen live 2026-10-01: FBA jobs come from external feeds
# (isWebJob true, contact.website = the feed's own page) and IMS jobs are Naukri India listings (contact.website =
# naukri.com). Their apply path has not been observed, so only the sources in config "allowed_sources" are used.
EASY_SOURCES = {"POSTED", "PJP", "FBA", "EMAIL", "IMS"}
SOURCE_REASONS = {"IMS": "IMS job (Naukri India listing, source not enabled)",
                  "FBA": "FBA job (external form-based listing, source not enabled)"}
# Layer codes the site's manageLayers() handles after an HTTP 400 from the apply call.
LAYER_QUESTIONS, LAYER_DONE, LAYER_NOT_READY, LAYER_HARD_BOUNCE = 4001, 4012, 4014, 4043
PROFILE_LAYERS = set(range(4030, 4045)) - {LAYER_HARD_BOUNCE}
# Decisions that end a job for good; a "failed" job is tried once more on a later run, then marked "failed twice".
FINAL_DECISIONS = {"applied", "already-applied", "questionnaire", "profile-layer", "ims", "no-easy-button",
                   "failed twice"}
LIVE_ENV = "NAUKRIGULF_APPLY_LIVE"

EXIT_OK, EXIT_ERROR, EXIT_NOT_ARMED, EXIT_NEEDS_USER, EXIT_CAP, EXIT_THROTTLED = 0, 1, 3, 42, 44, 47
TRIM_FLAGS = (   # memory-starved laptop: one small renderer budget, no GPU, no background services
    "--renderer-process-limit=2", "--disable-gpu", "--disable-background-networking", "--disable-default-apps",
    "--no-pings", "--disk-cache-size=33554432", "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
    "--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion,Translate,OptimizationHints,"
    "MediaRouter,InterestFeedContentSuggestions",
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_live_armed = False   # set only by main() when --live and NAUKRIGULF_APPLY_LIVE=1 are both present


class Stop(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def log(msg: str, level: str = "INFO") -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def pace(bounds) -> None:
    time.sleep(random.uniform(*bounds))


def mask_email(e: str) -> str:
    name, _, dom = (e or "").partition("@")
    return (name[:2] + "***@" + dom) if dom else "(none)"


# ------------------------------------------------------------------ browser
class ChromeSession(EdgeSession):
    """edge_cdp.EdgeSession on chrome.exe that keeps the DevTools events it reads. EdgeSession drops events
    while it waits for a reply; the live path needs Network/Page events to see what the site answered."""

    KEEP = ("Network.requestWillBeSent", "Network.responseReceived", "Network.loadingFinished",
            "Network.loadingFailed", "Page.frameNavigated", "Page.navigatedWithinDocument", "Page.windowOpen",
            "Page.javascriptDialogOpening")

    def __init__(self, profile_dir: str, headless: bool, exe: str):
        super().__init__(profile_dir, headless=headless, extra_args=TRIM_FLAGS, exe=exe)
        self.events: list[dict] = []
        self.keep_events = False

    def start(self, timeout: float = 90) -> "ChromeSession":
        super().start(timeout)
        if self.headless:
            # EdgeSession drops "Headless" from the UA string, but an override without client-hint metadata leaves
            # navigator.userAgentData empty and sends no sec-ch-ua headers, unlike the user's normal Chrome
            # ("Chromium";v="154", "Google Chrome";v="154", "Not A(Brand";v="99", seen 2026-10-01). Send matching hints.
            full = str(self.call("Browser.getVersion").get("product", "")).split("/")[-1] or "0.0.0.0"
            major = full.split(".")[0]
            meta = {"brands": [{"brand": "Chromium", "version": major}, {"brand": "Google Chrome", "version": major},
                               {"brand": "Not A(Brand", "version": "99"}],
                    "fullVersionList": [{"brand": "Chromium", "version": full}, {"brand": "Google Chrome", "version": full},
                                        {"brand": "Not A(Brand", "version": "99.0.0.0"}],
                    "platform": "Windows", "platformVersion": "19.0.0", "architecture": "x86", "model": "",
                    "mobile": False, "bitness": "64", "wow64": False}
            self.call("Network.setUserAgentOverride", {"userAgent": str(self.eval("navigator.userAgent")),
                                                       "userAgentMetadata": meta})
        return self

    def _stash(self, msg: dict) -> None:
        if self.keep_events and msg.get("method") in self.KEEP:
            self.events.append(msg)

    def call(self, method: str, params: dict | None = None, timeout: float = 60) -> dict:
        mid = next(self._ids)
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(1.0, deadline - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                break
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CDPError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
            self._stash(msg)
        raise CDPError(f"{method}: no reply within {timeout:.0f}s")

    def pump(self, seconds: float) -> None:
        """Read (and keep) DevTools events for up to `seconds`."""
        end = time.time() + seconds
        while time.time() < end:
            self.ws.settimeout(max(0.2, min(1.0, end - time.time())))
            try:
                self._stash(json.loads(self.ws.recv()))
            except websocket.WebSocketTimeoutException:
                continue


def kill_profile_chromes(profile_dir: str) -> None:
    needle = f"--user-data-dir={Path(profile_dir)}".replace("'", "''")
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object { $_.CommandLine -and "
          f"$_.CommandLine.Contains('{needle}') }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force "
          "-ErrorAction SilentlyContinue }")
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    time.sleep(2)


def ng_get(s: ChromeSession, path: str, spapi: bool = False, timeout: float = 60) -> dict:
    """Same-origin GET from inside the signed-in page, with the headers the site's own app sends."""
    headers = dict(CONFIG["site_headers"])
    if spapi:
        headers.update(CONFIG["spapi_headers"])
    js = f"""(async () => {{
        const r = await fetch({json.dumps(path)}, {{credentials: 'same-origin', headers: {json.dumps(headers)}}});
        const text = await r.text();
        let data = null; try {{ data = JSON.parse(text); }} catch (e) {{}}
        return JSON.stringify({{status: r.status, retryAfter: r.headers.get('retry-after'),
                                data: data, text: data === null ? text.slice(0, 300) : null}});
    }})()"""
    try:
        return json.loads(s.eval(js, await_promise=True, timeout=timeout))
    except CDPError as e:            # "Failed to fetch" on a network blip: one retry, then let it surface
        log(f"in-page fetch {path[:80]} failed ({str(e)[:100]}); retrying once", "WARN")
        time.sleep(5)
        return json.loads(s.eval(js, await_promise=True, timeout=timeout))


def marker(kind: str) -> Path:
    return STATE_DIR / f"NAUKRIGULF_{kind}"


def check_markers() -> None:
    m = marker("THROTTLED")
    if m.exists():
        try:
            until = datetime.fromisoformat(json.loads(m.read_text(encoding="utf-8"))["until"])
        except Exception:
            until = datetime.fromtimestamp(m.stat().st_mtime) + timedelta(minutes=30)
        if datetime.now() < until:
            raise Stop(EXIT_THROTTLED, f"NaukriGulf throttled/blocked us; waiting until {until:%H:%M} (marker {m.name})")
        m.unlink(missing_ok=True)


def check_throttle(res: dict, what: str) -> None:
    if res["status"] not in (429, 403):
        return
    try:
        wait = int(res.get("retryAfter") or 0)
    except ValueError:
        wait = 0
    until = datetime.now() + timedelta(seconds=max(wait, 30 * 60))
    marker("THROTTLED").write_text(json.dumps({"until": until.isoformat(timespec="seconds"), "what": what,
                                               "status": res["status"]}), encoding="utf-8")
    raise Stop(EXIT_THROTTLED, f"{what}: HTTP {res['status']} from NaukriGulf - pausing until {until:%H:%M}")


def login_required(reason: str) -> Stop:
    marker("LOGIN_REQUIRED").write_text(datetime.now().isoformat(timespec="seconds") + " " + reason, encoding="utf-8")
    return Stop(EXIT_NEEDS_USER, reason)


def open_session(visible: bool) -> tuple[ChromeSession, dict]:
    def new() -> ChromeSession:
        return ChromeSession(CONFIG["profile_dir"], headless=not visible, exe=CONFIG["chrome_exe"])
    s = new()
    try:
        s.start()
    except CDPError as e:
        # A Chrome still holding the profile makes the new one hand off and exit.
        log(f"Chrome start failed ({e}); closing stale Chrome on this profile and retrying once", "WARN")
        kill_profile_chromes(CONFIG["profile_dir"])
        s = new().start()
    try:
        s.navigate(f"{SITE}/mnj/userProfile/myHome", timeout=90)
        time.sleep(6)   # the dashboard's own scripts refresh the session cookie (POST /ngoauth/oauth/sessions/refresh)
        href = str(s.eval("location.href"))
        if "/login" in href:
            raise login_required(f"profile {CONFIG['profile_dir']} is signed out (landed on {href[:80]}); sign in once "
                                 "through Google in that profile (naukrigulf_apply.cmd setup)")
        res = ng_get(s, SELF)
        check_throttle(res, "user/self")
        data = res.get("data") or {}
        basic = data.get("basicDetails") or {}
        user = str(basic.get("username") or "")
        if res["status"] in (401, 403) or not user:
            raise login_required(f"user/self answered HTTP {res['status']} without a user; the profile needs a sign-in")
        if user.lower() != CONFIG["expected_email"].lower():
            raise Stop(EXIT_NEEDS_USER, f"profile is signed in as {mask_email(user)}, expected "
                                        f"{mask_email(CONFIG['expected_email'])}; refusing to continue")
        marker("LOGIN_REQUIRED").unlink(missing_ok=True)
        me = {"apply_ready": bool((data.get("applyReadyDetails") or {}).get("isApplyReady")),
              "total_experience": basic.get("totalExperience"),
              "user_agent": str(s.eval("navigator.userAgent")),
              "ua_brands": str(s.eval("navigator.userAgentData ? navigator.userAgentData.brands.map(b => b.brand)"
                                      ".join(', ') : ''"))}
        return s, me
    except BaseException:
        s.close()
        raise


# ------------------------------------------------------------------ state
def open_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (           -- every job discovery has seen, with its latest verdict
            job_id      TEXT PRIMARY KEY,           -- text: ids like 020926000843 keep their leading zero
            title       TEXT,
            company     TEXT,
            company_id  TEXT,
            location    TEXT,
            exp_min     REAL,
            exp_max     REAL,
            posted      TEXT,                       -- latestPostedDate as YYYY-MM-DD
            job_source  TEXT,                       -- POSTED | PJP | FBA | EMAIL | IMS | WEB | TAG
            easy_apply  INTEGER,
            nationality TEXT,
            url         TEXT,
            score       REAL,
            verdict     TEXT,                       -- candidate | skip
            reason      TEXT,
            detail      TEXT,
            queries     TEXT,
            first_seen  TEXT NOT NULL,
            last_seen   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS applications (   -- live outcomes only; a dry run never writes here
            job_id      TEXT PRIMARY KEY,
            title       TEXT,
            company     TEXT,
            location    TEXT,
            url         TEXT,
            decision    TEXT NOT NULL,              -- applied | already-applied | questionnaire | profile-layer |
                                                    -- ims | no-easy-button | skipped | failed
            reason      TEXT,
            http_status INTEGER,
            layer_code  INTEGER,
            site_flag   INTEGER DEFAULT 0,          -- the job page's own isApplied flag right after applying
            verified    INTEGER DEFAULT 0,          -- 1 = found in the site's Applied list (easyApplyList)
            verified_at TEXT,
            attempts    INTEGER DEFAULT 1,
            ts          TEXT NOT NULL               -- local time of our decision
        );
        CREATE TABLE IF NOT EXISTS runs (
            run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            started     TEXT NOT NULL,
            ended       TEXT,
            mode        TEXT,                       -- dry-run | live
            exit_code   INTEGER,
            fetched     INTEGER,
            candidates  INTEGER,
            applied     INTEGER DEFAULT 0,
            note        TEXT
        );
    """)
    return con


def applied_today(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) FROM applications WHERE decision='applied' AND substr(ts,1,10)=?",
                       (datetime.now().strftime("%Y-%m-%d"),)).fetchone()[0]


def write_progress(con: sqlite3.Connection, run: dict) -> dict:
    """Progress file for a watcher: progress means a NEW application confirmed in the site's Applied list,
    never "the process is alive" or "a run finished"."""
    try:
        prev = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except Exception:
        prev = {}
    today = datetime.now().strftime("%Y-%m-%d")
    total = con.execute("SELECT COUNT(*) FROM applications WHERE verified=1").fetchone()[0]
    confirmed_today = con.execute("SELECT COUNT(*) FROM applications WHERE verified=1 AND substr(verified_at,1,10)=?",
                                  (today,)).fetchone()[0]
    accepted_today = applied_today(con)
    state = {
        "account": CONFIG["account_label"],
        "day": today,
        "confirmed_total": total,
        "confirmed_today": confirmed_today,
        "accepted_today": accepted_today,          # the site answered 200 today (counted against the daily cap)
        "unconfirmed_total": con.execute("SELECT COUNT(*) FROM applications WHERE decision='applied' AND verified=0")
                                .fetchone()[0],
        "daily_cap": int(CONFIG["daily_cap"]),
        "last_progress_at": prev.get("last_progress_at"),
        "last_run": run,
        "last_live_run": prev.get("last_live_run"),
        "last_dry_run": prev.get("last_dry_run"),
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    if total > int(prev.get("confirmed_total") or 0):
        state["last_progress_at"] = datetime.now().isoformat(timespec="seconds")
    state["last_live_run" if run.get("mode") == "live" else "last_dry_run"] = run
    PROGRESS_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


# ------------------------------------------------------------------ filters
def wb(term: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])")


def alt(words) -> re.Pattern:
    words = [w.lower() for w in words if w]
    if not words:
        return re.compile(r"(?!x)x")
    return re.compile(r"(?<![a-z0-9])(" + "|".join(map(re.escape, words)) + r")(?![a-z0-9])")


def identity_keywords() -> list[str]:
    """The Naukri bot's job_search.relevance_keywords (only that key is used from its config).
    A config that does not exist yet simply contributes no extra keywords."""
    try:
        js = json.loads(Path(CONFIG["identity_config"]).read_text(encoding="utf-8")).get("job_search") or {}
    except (OSError, ValueError):
        js = {}
    return [str(k).lower().strip() for k in (js.get("relevance_keywords") or []) if str(k).strip()]


def dedupe(items) -> list[str]:
    out, seen = [], set()
    for i in items:
        k = i.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def num(v) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def posted_ts(job: dict) -> int | None:
    v = num(job.get("latestPostedDate"))
    return int(v) if v else None


class Filters:
    def __init__(self, identity: list[str]):
        self.tokens = dedupe(identity + CONFIG.get("extra_title_keywords", []))
        self.tok_res = [(t, wb(t)) for t in self.tokens]
        self.role_re = alt(CONFIG["generic_role_words"])
        self.off_re = alt(CONFIG["off_role_words"])
        self.senior_re = alt(CONFIG["senior_title_words"])
        self.tag_excluded_re = alt(CONFIG.get("tag_match_excluded_title_words", []))
        self.allowed = {x.upper() for x in CONFIG.get("allowed_sources", ["POSTED"])}
        self.avoid_res = [wb(c) for c in CONFIG.get("avoid_companies", [])]
        self.country_re = alt(CONFIG["country_words"])
        self.nat_self = alt(CONFIG["nationality_self_words"])
        self.nat_any = alt(CONFIG["nationality_any_words"])
        self.nat_restricted = alt(CONFIG["nationality_restricted_words"])
        self.max_exp = float(CONFIG["max_experience_min_years"])
        self.entry_max = float(CONFIG.get("min_experience_max_years", 0))
        self.min_tag_hits = int(CONFIG.get("min_tag_hits", 1))
        self.max_days = float(CONFIG["max_posted_days"])

    def title_hits(self, title: str) -> list[str]:
        t = title.lower()
        return [tok for tok, r in self.tok_res if r.search(t)]

    def source_reason(self, src: str, easy: bool, web: bool) -> str | None:
        if not easy or src not in EASY_SOURCES:
            return "not Easy Apply (employer site)"
        if src not in self.allowed:
            return SOURCE_REASONS.get(src, f"{src} job (source not enabled)")
        if web and src not in ("FBA", "IMS"):
            return "not Easy Apply (employer site)"
        return None

    def cheap(self, j: dict, applied_ids: set[str], known: dict[str, str],
              any_source: bool = False) -> tuple[str | None, str]:
        """Filters on the search result alone. Returns (reason, detail); reason None = keep. any_source=True skips
        the source rule (used only to report how many FBA/IMS jobs would pass otherwise)."""
        jid = str(j.get("jobId"))
        title = (j.get("designation") or "").strip()
        tl = title.lower()
        src = (j.get("jobSource") or "").upper()
        reason = None if any_source else self.source_reason(src, bool(j.get("isEasyApply")), bool(j.get("isWebJob")))
        if reason:
            return reason, src
        if j.get("isApplied"):
            return "already applied (site flag)", ""
        if jid in applied_ids:
            return "already applied (Applied list)", ""
        if known.get(jid) in FINAL_DECISIONS:
            return "already handled (local DB)", known[jid]
        if j.get("isArchived"):
            return "expired/archived", ""
        if not self.country_re.search((j.get("location") or "").lower()):
            return "location outside the six GCC countries", j.get("location") or ""
        ts = posted_ts(j)
        if ts is None:
            return "posted date missing", ""
        age = (time.time() - ts) / 86400
        if age > self.max_days:
            return f"posted over {self.max_days:g} days ago", f"{age:.0f} days"
        emin = num((j.get("experience") or {}).get("min"))
        if emin is None:
            return "minimum experience not stated", ""
        if emin > self.max_exp:
            return f"minimum experience above {self.max_exp:g} years", f"min {emin:g}"
        emax = num((j.get("experience") or {}).get("max"))
        if emax is not None and emax < self.entry_max:
            return f"entry-level role (maximum experience below {self.entry_max:g} years)", f"max {emax:g}"
        if self.off_re.search(tl):
            return "off-role title", self.off_re.search(tl).group(1)
        if self.senior_re.search(tl):
            return "too-senior title", self.senior_re.search(tl).group(1)
        if not self.title_hits(tl):
            # Same rule as instahyre_apply.py: the job's skill tags count only for a generic build title
            # (engineer/developer...). NaukriGulf's tags are looser still: in the 2026-10-01 dry run one "Kafka" tag
            # let a Java backend and two full-stack jobs through, and an "AI/ML Engineer" lists "Data Engineering".
            # So the tags must name at least min_tag_hits different relevance keywords, and never for AI/ML titles.
            tags = (j.get("keywords") or "").lower()
            tag_hits = sum(1 for _, r in self.tok_res if r.search(tags))
            if self.tag_excluded_re.search(tl) or not (self.role_re.search(tl) and tag_hits >= self.min_tag_hits):
                return "title not a data-engineering role", f"{tag_hits} tag keyword(s)" if tag_hits else ""
        company = ((j.get("company") or {}).get("name") or "").lower()
        if any(r.search(company) for r in self.avoid_res):
            return "avoided company", company
        return None, ""

    def nationality_ok(self, nat: str) -> bool:
        n = (nat or "").strip().lower()
        if not n or n in ("not mentioned", "nm", "any"):
            return True
        if self.nat_self.search(n):
            return True
        # "Any Non-GCC National" is open to Indians: drop negated groups before looking for restricted ones
        n2 = re.sub(r"non[- ]?(gcc|arabs?|locals?)(?![a-z0-9])", " ", n)
        return bool(self.nat_any.search(n2)) and not self.nat_restricted.search(n2)

    def detail(self, d: dict) -> tuple[str | None, str]:
        """Filters on the job page's own JSON (GET /spapi/jobs/<id>)."""
        o = d.get("other") or {}
        src = (d.get("jobSource") or "").upper()
        reason = self.source_reason(src, bool(d.get("isEasyApply")), bool(o.get("isWebJob")))
        if reason:
            return reason, f"{src} (job page)"
        if o.get("isNaukriEasyApplyJob") == 1:
            return "Naukri apply flow (apply.naukri.com)", src
        if o.get("isApplied"):
            return "already applied (site flag)", "job page"
        if d.get("isExpired") or o.get("isArchived"):
            return "expired/archived", ""
        nat = ((d.get("desiredCandidate") or {}).get("nationality") or "").strip()
        if not self.nationality_ok(nat):
            return "nationality not open to an Indian national", nat
        emin = num(((d.get("desiredCandidate") or {}).get("experience") or {}).get("min"))
        if emin is not None and emin > self.max_exp:
            return f"minimum experience above {self.max_exp:g} years", f"min {emin:g} (job page)"
        return None, ""

    def score(self, j: dict) -> float:
        """Rank for the daily budget: title keyword hits first, then experience fit, freshness, activity."""
        sc = 3.0 * len(self.title_hits(j.get("designation") or ""))
        emin = num((j.get("experience") or {}).get("min")) or 0
        sc += 2 if emin <= 3 else (1 if emin <= 4 else 0)
        age = (time.time() - (posted_ts(j) or 0)) / 86400
        sc += 2 if age <= 7 else (1 if age <= 15 else 0)
        sc += 1 if j.get("recruiterActive") else 0
        sc += 1 if (j.get("jobSource") or "").upper() == "POSTED" else 0
        return sc


# ------------------------------------------------------------------ site reads
def read_applied(s: ChromeSession) -> tuple[set[str], dict]:
    ids: set[str] = set()
    totals = {}
    for kind, path, total_key in APPLIED_LISTS:
        off = 0
        for _ in range(40):
            res = ng_get(s, path.format(off=off))
            check_throttle(res, f"applied list ({kind})")
            if res["status"] in (401,):
                raise login_required("applied list answered 401; the profile needs a sign-in")
            if res["status"] != 200:
                raise Stop(EXIT_ERROR, f"applied list ({kind}): HTTP {res['status']} {res.get('text') or ''}")
            data = res.get("data") or {}
            page = data.get("jobs") or []
            totals[kind] = int(data.get(total_key) or 0)
            ids.update(str(j.get("jobId")) for j in page if j.get("jobId"))
            off += len(page)
            if not page or off >= totals[kind]:
                break
    return ids, totals


def search_path(keyword: str, page: int) -> str:
    p = {"Experience": "", "Keywords": keyword, "KeywordsAr": "", "Limit": PAGE_SIZE,
         "Location": ",".join(c.lower() for c in CONFIG["countries"]), "LocationAr": "",
         "Offset": PAGE_SIZE * (page - 1), "SortPreference": "date", "breadcrumb": 1, "geoIpCityName": "",
         "geoIpCountryName": "", "locationId": CONFIG["location_id"], "nationality": "", "nationalityLabel": "",
         "pageNo": page, "seo": 1, "showBellyFilters": "false", "showSponsoredJobs": "false", "srchId": "",
         "topEmployer": "true"}
    p.update(CONFIG.get("server_filters") or {})
    return SEARCH + "?" + urlencode(p)


def keyword_list(override: str | None) -> list[str]:
    if override:
        return dedupe(override.split(";"))
    kws = list(CONFIG["search_keywords"])
    if CONFIG.get("add_identity_keywords_to_search"):
        kws += identity_keywords()
    # One phrase per query: the search splits a comma list into single words (2026-10-01: "data engineer,
    # data platform engineer,..." matched 2815 jobs on any of the words "data", "engineer", "platform"...).
    return dedupe(k.replace(",", " ") for k in kws)


def discover(s: ChromeSession, keywords: list[str]) -> tuple[dict[str, dict], list[dict], int]:
    jobs: dict[str, dict] = {}
    per_query = []
    calls = 0
    for kw in keywords:
        got, total, pages = 0, None, 0
        for page in range(1, int(CONFIG["max_pages_per_query"]) + 1):
            if calls:
                pace(CONFIG["search_pace_seconds"])
            calls += 1
            res = ng_get(s, search_path(kw, page), spapi=True)
            check_throttle(res, f"search {kw!r}")
            if res["status"] == 204:
                total = total or 0
                break
            if res["status"] != 200:
                log(f"search {kw!r} page {page}: HTTP {res['status']} {res.get('text') or ''}", "WARN")
                break
            data = res.get("data") or {}
            page_jobs = data.get("jobs") or []
            total = data.get("totalJobsCount") if total is None else total
            pages = page
            for j in page_jobs:
                jid = str(j.get("jobId") or "")
                if not jid:
                    continue
                if jid in jobs:
                    jobs[jid]["_queries"].append(kw)
                else:
                    keep = {k: v for k, v in j.items() if k not in ("description", "keywordsAr", "logoUrl", "tELogoUrl",
                                                                     "whitelistedkeywords", "email")}
                    keep["_queries"] = [kw]
                    jobs[jid] = keep
            got += len(page_jobs)
            if len(page_jobs) < PAGE_SIZE or (total is not None and got >= int(total)):
                break
        per_query.append({"keyword": kw, "server_total": total, "fetched": got, "pages": pages})
        log(f"search {kw!r}: {got} fetched (server total {total}), {len(jobs)} unique so far")
    return jobs, per_query, calls


def job_detail(s: ChromeSession, jid: str) -> dict:
    res = ng_get(s, JOB.format(jid=jid, loc=CONFIG["location_id"]), spapi=True)
    check_throttle(res, f"job {jid}")
    if res["status"] != 200 or not isinstance(res.get("data"), dict):
        return {"_error": f"HTTP {res['status']}"}
    return res["data"]


def row_for(j: dict, f: Filters) -> dict:
    exp = j.get("experience") or {}
    ts = posted_ts(j)
    return {
        "job_id": str(j.get("jobId")),
        "title": (j.get("designation") or "").strip(),
        "company": ((j.get("company") or {}).get("name") or "").strip(),
        "company_id": str((j.get("company") or {}).get("id") or ""),
        "location": j.get("location") or "",
        "experience": f"{exp.get('min', '?')}-{exp.get('max', '?')} yrs",
        "exp_min": num(exp.get("min")),
        "exp_max": num(exp.get("max")),
        "posted": datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else None,
        "age_days": round((time.time() - ts) / 86400, 1) if ts else None,
        "url": f"{SITE}/{j.get('jdURL')}" if j.get("jdURL") else None,
        "easy_apply": bool(j.get("isEasyApply")),
        "job_source": (j.get("jobSource") or "").upper(),
        "nationality": None,
        "title_keywords": f.title_hits(j.get("designation") or ""),
        "score": f.score(j),
        "queries": j.get("_queries") or [],
        "decision": None,
        "reason": None,
        "detail": "",
    }


def evaluate(s: ChromeSession, jobs: dict[str, dict], f: Filters, applied_ids: set[str],
             known: dict[str, str]) -> tuple[list[dict], int]:
    rows = {jid: row_for(j, f) for jid, j in jobs.items()}
    passing = []
    for jid, j in jobs.items():
        reason, detail = f.cheap(j, applied_ids, known)
        if reason:
            if reason in SOURCE_REASONS.values() and f.cheap(j, applied_ids, known, any_source=True)[0] is None:
                detail = f"{detail}; passes the other search filters"   # sizes the "enable FBA/IMS?" decision
            rows[jid].update(decision="skip", reason=reason, detail=detail)
        else:
            passing.append(jid)
    # one posting per company + title: keep the newest (reposts and multi-city copies of one opening)
    passing.sort(key=lambda x: posted_ts(jobs[x]) or 0, reverse=True)
    first: dict[tuple, str] = {}
    unique = []
    for jid in passing:
        r = rows[jid]
        key = (r["company_id"] or r["company"].lower(), re.sub(r"[^a-z0-9]+", " ", r["title"].lower()).strip())
        if key in first:
            r.update(decision="skip", reason="duplicate posting (same company + title)", detail=f"kept {first[key]}")
        else:
            first[key] = jid
            unique.append(jid)
    unique.sort(key=lambda x: (rows[x]["score"], posted_ts(jobs[x]) or 0), reverse=True)
    budget = int(CONFIG["max_detail_fetches"])
    details = 0
    for jid in unique:
        if details >= budget:
            rows[jid].update(decision="skip", reason="not checked (job-page budget)", detail="")
            continue
        if details:
            pace(CONFIG["detail_pace_seconds"])
        details += 1
        d = job_detail(s, jid)
        if "_error" in d:
            rows[jid].update(decision="skip", reason="job page read failed", detail=d["_error"])
            continue
        rows[jid]["nationality"] = ((d.get("desiredCandidate") or {}).get("nationality") or "").strip()
        reason, detail = f.detail(d)
        if reason:
            rows[jid].update(decision="skip", reason=reason, detail=detail)
        else:
            rows[jid].update(decision="would-apply", reason="passes all filters",
                             detail=f"score {rows[jid]['score']:g}; title keywords: "
                                    f"{', '.join(rows[jid]['title_keywords']) or '(skill tags only)'}")
    ordered = sorted(rows.values(), key=lambda r: (r["decision"] != "would-apply", -r["score"],
                                                    r["age_days"] if r["age_days"] is not None else 999))
    return ordered, details


def save_jobs(con: sqlite3.Connection, rows: list[dict]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    con.executemany("""INSERT INTO jobs (job_id, title, company, company_id, location, exp_min, exp_max, posted, job_source,
                                         easy_apply, nationality, url, score, verdict, reason, detail, queries,
                                         first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(job_id) DO UPDATE SET title=excluded.title, company=excluded.company,
                         location=excluded.location, exp_min=excluded.exp_min, exp_max=excluded.exp_max,
                         posted=excluded.posted, job_source=excluded.job_source, easy_apply=excluded.easy_apply,
                         nationality=COALESCE(excluded.nationality, jobs.nationality), url=excluded.url,
                         score=excluded.score, verdict=excluded.verdict, reason=excluded.reason,
                         detail=excluded.detail, queries=excluded.queries, last_seen=excluded.last_seen""",
                    [(r["job_id"], r["title"], r["company"], r["company_id"], r["location"], r["exp_min"], r["exp_max"],
                      r["posted"], r["job_source"], int(r["easy_apply"]), r["nationality"], r["url"], r["score"],
                      "candidate" if r["decision"] == "would-apply" else "skip", r["reason"], r["detail"],
                      json.dumps(r["queries"]), now, now) for r in rows])
    con.commit()


# ------------------------------------------------------------------ reports
def write_reports(meta: dict, rows: list[dict], per_query: list[dict]) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d")
    hist = Counter(r["reason"] for r in rows if r["decision"] != "would-apply")
    would = [r for r in rows if r["decision"] == "would-apply"]
    out_json = STATE_DIR / f"naukrigulf_dryrun_{stamp}.json"
    out_md = STATE_DIR / f"naukrigulf_dryrun_{stamp}.md"
    out_json.write_text(json.dumps({"meta": meta, "skip_histogram": dict(hist.most_common()), "per_query": per_query,
                                    "would_apply": would, "all_jobs": rows}, indent=1, ensure_ascii=False),
                        encoding="utf-8")

    def cell(v) -> str:
        return str(v if v is not None else "").replace("|", "/").replace("\n", " ")

    lines = [f"# NaukriGulf dry run {meta['started'][:16].replace('T', ' ')}",
             "",
             f"Account {meta['account']} ({mask_email(CONFIG['expected_email'])}), profile apply-ready: "
             f"{meta['apply_ready']}. Nothing was clicked or posted.",
             "",
             f"- Runtime: {meta['runtime_s']:.0f} s; {meta['search_calls']} search calls over "
             f"{len(per_query)} keywords x {len(CONFIG['countries'])} countries, {meta['detail_calls']} job pages read",
             f"- Server-side filters: {json.dumps(CONFIG.get('server_filters'))}",
             f"- Unique jobs fetched: {meta['fetched']}; passing every filter: {meta['would_apply']}; held back only by "
             f"their source (FBA/IMS): {meta['held_back_by_source']}",
             f"- Already applied on this account (Applied list): {meta['applied_list'].get('easy', 0)} Easy Apply, "
             f"{meta['applied_list'].get('web', 0)} employer-site",
             f"- Daily cap {CONFIG['daily_cap']}, per-run cap {CONFIG['per_run_cap']}",
             "",
             f"## Top {min(30, len(would))} would-apply jobs (ranked)",
             "",
             "| # | Job ID | Title | Company | Location | Exp | Posted | Source | Nationality | Score |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for n, r in enumerate(would[:30], 1):
        title = f"[{cell(r['title'])}]({r['url']})" if r["url"] else cell(r["title"])
        lines.append(f"| {n} | {r['job_id']} | {title} | {cell(r['company'])} | {cell(r['location'])} | "
                     f"{cell(r['experience'])} | {cell(r['posted'])} | {cell(r['job_source'])} | "
                     f"{cell(r['nationality'])} | {r['score']:g} |")
    lines += ["", "## Skip reasons (first failing filter per unique job)", "", "| Reason | Jobs |", "|---|---|"]
    lines += [f"| {cell(k)} | {v} |" for k, v in hist.most_common()]
    held = [r for r in rows if "passes the other search filters" in (r["detail"] or "")]
    lines += ["", f"## Held back only by their source: {len(held)} FBA/IMS jobs",
              "", "These pass every search-level filter but come from sources that are not enabled (`allowed_sources`); "
              "their job pages (nationality) were not read.", "",
              "| Job ID | Title | Company | Exp | Posted | Source |", "|---|---|---|---|---|---|"]
    for r in sorted(held, key=lambda x: -x["score"])[:20]:
        title = f"[{cell(r['title'])}]({r['url']})" if r["url"] else cell(r["title"])
        lines.append(f"| {r['job_id']} | {title} | {cell(r['company'])} | {cell(r['experience'])} | {cell(r['posted'])} | "
                     f"{cell(r['job_source'])} |")
    lines += ["", "## Per keyword (server total = Easy Apply jobs posted in the last 30 days, six countries)", "",
              "| Keyword | Server total | Fetched | Pages |", "|---|---|---|---|"]
    lines += [f"| {cell(q['keyword'])} | {cell(q['server_total'])} | {q['fetched']} | {q['pages']} |" for q in per_query]
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_json, out_md


# ------------------------------------------------------------------ live path (locked)
def assert_armed() -> None:
    if not (_live_armed and os.environ.get(LIVE_ENV) == "1"):
        raise Stop(EXIT_NOT_ARMED, f"live apply is not armed (--live and {LIVE_ENV}=1 are both required)")


BUTTON_JS = """(() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const txt = e => (e.innerText || '').trim().replace(/\\s+/g, ' ');
  const buttons = Array.from(document.querySelectorAll('button')).filter(vis);
  const easy = buttons.filter(b => txt(b) === 'Easy Apply');
  const plain = buttons.filter(b => txt(b) === 'Apply');
  const applied = Array.from(document.querySelectorAll('.applied-text')).filter(vis).length > 0;
  let rect = null;
  if (easy.length) {
    easy[0].scrollIntoView({block: 'center'});
    const r = easy[0].getBoundingClientRect();
    rect = {x: r.left + r.width / 2, y: r.top + r.height / 2};
  }
  return JSON.stringify({easy: easy.length, plain: plain.length, applied: applied, rect: rect,
                         url: location.href, ready: document.readyState});
})()"""

HIT_JS = """(([x, y]) => {
  const e = document.elementFromPoint(x, y);
  const b = e && e.closest('button');
  return !!b && (b.innerText || '').trim().replace(/\\s+/g, ' ') === 'Easy Apply';
})(%s)"""


def wait_for_button(s: ChromeSession, seconds: float = 25) -> dict:
    end = time.time() + seconds
    st = {}
    while time.time() < end:
        st = json.loads(s.eval(BUTTON_JS))
        if st.get("easy") or st.get("applied") or (st.get("plain") and st.get("ready") == "complete"):
            time.sleep(1.0)        # let React finish wiring the handlers before reading the final state
            return json.loads(s.eval(BUTTON_JS))
        time.sleep(1.0)
    return st


def press_easy_apply(s: ChromeSession, rect: dict) -> None:
    """A real mouse click on the site's own Easy Apply button: the site builds and sends its own request."""
    assert_armed()
    x, y = float(rect["x"]), float(rect["y"])
    if not s.eval(HIT_JS % json.dumps([x, y])):
        raise Stop(EXIT_ERROR, "the point to click is not the Easy Apply button; refusing to click")
    s.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    time.sleep(random.uniform(0.2, 0.6))
    s.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    time.sleep(random.uniform(0.05, 0.15))
    s.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})


def watch_apply(s: ChromeSession, seconds: float = 40) -> dict:
    """Passively watch what the click caused: the apply request and its answer, navigations, new windows."""
    out = {"request_id": None, "status": None, "body": None, "failed": None, "ims": False, "nav": [], "windows": [],
           "dialog": None}
    end = time.time() + seconds
    seen = 0
    while time.time() < end:
        s.pump(1.0)
        new, seen = s.events[seen:], len(s.events)
        for ev in new:
            m, p = ev.get("method"), ev.get("params") or {}
            if m == "Network.requestWillBeSent":
                req = p.get("request") or {}
                url = req.get("url", "")
                if APPLY_FRAGMENT in url and req.get("method") == "POST":
                    out["request_id"] = p.get("requestId")
                elif "apply.naukri.com" in url:
                    out["ims"] = True
            elif m == "Network.responseReceived" and p.get("requestId") == out["request_id"]:
                out["status"] = (p.get("response") or {}).get("status")
            elif m == "Network.loadingFinished" and p.get("requestId") == out["request_id"]:
                try:
                    b = s.call("Network.getResponseBody", {"requestId": out["request_id"]}, timeout=15)
                    text = base64.b64decode(b["body"]).decode("utf-8", "replace") if b.get("base64Encoded") else b.get("body")
                    out["body"] = json.loads(text) if text else None
                except (CDPError, ValueError, KeyError) as e:
                    out["body_error"] = str(e)[:120]
                return out
            elif m == "Network.loadingFailed" and p.get("requestId") == out["request_id"]:
                out["failed"] = p.get("errorText") or "loading failed"
                return out
            elif m == "Page.frameNavigated" and not (p.get("frame") or {}).get("parentId"):
                out["nav"].append((p.get("frame") or {}).get("url", ""))
            elif m == "Page.navigatedWithinDocument":      # React Router push/replace (e.g. the 4014 register redirect)
                out["nav"].append(p.get("url", ""))
            elif m == "Page.windowOpen":
                out["windows"].append(p.get("url"))
            elif m == "Page.javascriptDialogOpening":
                out["dialog"] = (p.get("message") or "")[:200]
                try:
                    s.call("Page.handleJavaScriptDialog", {"accept": False})
                except CDPError:
                    pass
        if out["ims"] or out["windows"] or any("/register" in u or "/login" in u for u in out["nav"]):
            return out
    return out


def apply_one(s: ChromeSession, row: dict, f: Filters) -> dict:
    """Open the job page and press its Easy Apply button once. Returns {decision, reason, status, layer}."""
    assert_armed()
    d = job_detail(s, row["job_id"])            # fresh read right before acting
    if "_error" in d:
        return {"decision": "failed", "reason": f"job page read failed ({d['_error']})"}
    reason, detail = f.detail(d)
    if reason:
        return {"decision": "skipped", "reason": f"re-check: {reason} {detail}".strip()}
    s.navigate(row["url"], timeout=90)
    st = wait_for_button(s)
    if "/login" in str(st.get("url", "")):
        raise login_required("job page redirected to the login page; the profile needs a sign-in")
    if st.get("applied"):
        return {"decision": "already-applied", "reason": "the job page already shows Applied"}
    if not st.get("easy") or not st.get("rect"):
        return {"decision": "no-easy-button", "reason": f"no Easy Apply button on the page ({st.get('plain', 0)} plain Apply)"}
    s.events.clear()
    s.keep_events = True
    s.call("Network.enable", {"maxTotalBufferSize": 10_000_000, "maxResourceBufferSize": 2_000_000})
    try:
        press_easy_apply(s, st["rect"])
        out = watch_apply(s)
    finally:
        s.keep_events = False
        try:
            s.call("Network.disable")
        except CDPError:
            pass
    body = out.get("body") if isinstance(out.get("body"), dict) else {}
    status, layer = out.get("status"), body.get("layerCode")
    res = {"status": status, "layer": layer}
    if any("/register" in u for u in out["nav"]):
        raise login_required("the site sent the apply to its register page: the profile is not apply-ready")
    if any("/login" in u for u in out["nav"]):
        raise login_required("the site sent the apply to its login page; the profile needs a sign-in")
    if out["dialog"] is not None:
        return {**res, "decision": "failed", "reason": f"page dialog: {out['dialog']}"}
    if out["ims"]:
        return {**res, "decision": "ims", "reason": "the button opened the apply.naukri.com flow; left alone"}
    if out["windows"]:
        return {**res, "decision": "failed", "reason": f"the button opened a window: {str(out['windows'][0])[:100]}"}
    if status == 200:
        return {**res, "decision": "applied", "reason": "apply/v2 answered 200"}
    if status == 400 and layer == LAYER_QUESTIONS:
        n = len(((body.get("cqData") or {}).get("list")) or [])
        return {**res, "decision": "questionnaire", "reason": f"employer asks {n} custom question(s); not answered"}
    if status == 400 and layer == LAYER_DONE:
        return {**res, "decision": "applied", "reason": "apply/v2 answered layer 4012 (the site treats it as applied)"}
    if status == 400 and layer == LAYER_HARD_BOUNCE:
        raise Stop(EXIT_NEEDS_USER, "the site reports the account email as bouncing (layer 4043); fix it on the site")
    if status == 400 and layer == LAYER_NOT_READY:
        raise login_required("the site says the profile is not apply-ready (layer 4014)")
    if status == 400 and layer in PROFILE_LAYERS:
        return {**res, "decision": "profile-layer", "reason": f"the site asks for a profile update first (layer {layer})"}
    if status == 401:
        raise login_required("apply/v2 answered 401; the profile needs a sign-in")
    if status in (403, 429):
        check_throttle({"status": status, "retryAfter": None}, "apply/v2")
    if status is None:
        return {**res, "decision": "failed", "reason": out.get("failed") or "no apply request seen within 40 s"}
    return {**res, "decision": "failed", "reason": f"apply/v2 HTTP {status} layer {layer}"}


def record_application(con: sqlite3.Connection, row: dict, res: dict, site_flag: bool = False) -> None:
    con.execute("""INSERT INTO applications (job_id, title, company, location, url, decision, reason, http_status,
                                             layer_code, site_flag, ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_id) DO UPDATE SET decision=excluded.decision, reason=excluded.reason,
                     http_status=excluded.http_status, layer_code=excluded.layer_code, site_flag=excluded.site_flag,
                     attempts=applications.attempts + 1, ts=excluded.ts""",
                (row["job_id"], row["title"], row["company"], row["location"], row["url"], res["decision"],
                 res.get("reason"), res.get("status"), res.get("layer"), int(site_flag),
                 datetime.now().isoformat(timespec="seconds")))
    con.commit()


def verify_applied(s: ChromeSession, con: sqlite3.Connection) -> int:
    """Confirm our unverified applications against the site's own Applied list (easyApplyList, the list
    behind /mnj/userProfile/whtma), the way instahyre_apply.py confirms against interest_facet=1."""
    pending = {r[0] for r in con.execute("SELECT job_id FROM applications WHERE decision='applied' AND verified=0")}
    if not pending:
        return 0
    listed, _ = read_applied(s)
    confirmed = pending & listed
    now = datetime.now().isoformat(timespec="seconds")
    for jid in confirmed:
        con.execute("UPDATE applications SET verified=1, verified_at=? WHERE job_id=?", (now, jid))
    con.commit()
    missing = pending - confirmed
    log(f"read-back: {len(confirmed)} confirmed in the Applied list"
        + (f", {len(missing)} not listed yet ({sorted(missing)[:5]})" if missing else ""),
        "WARN" if missing else "INFO")
    return len(confirmed)


def apply_queue(s: ChromeSession, con: sqlite3.Connection, queue: list[dict], f: Filters) -> int:
    assert_armed()
    applied = fails = layers = 0
    for n, row in enumerate(queue):
        if n:
            pace(CONFIG["pace_seconds"])
        label = f"{row['title']} @ {row['company']} (job {row['job_id']}, score {row['score']:g})"
        res = apply_one(s, row, f)
        site_flag = False
        if res["decision"] == "applied":
            time.sleep(4)
            d = job_detail(s, row["job_id"])
            site_flag = bool((d.get("other") or {}).get("isApplied"))
        record_application(con, row, res, site_flag)
        if res["decision"] == "applied":
            applied += 1
            fails = layers = 0
            log(f"APPLIED {label}: {res['reason']}; job page flag isApplied={site_flag}")
        elif res["decision"] == "failed":
            fails += 1
            log(f"apply failed for {label}: {res['reason']}", "WARN")
            if fails >= int(CONFIG["max_failures_in_row"]):
                raise Stop(EXIT_ERROR, f"{fails} apply failures in a row; stopping this run")
        else:
            if res["decision"] == "profile-layer":
                layers += 1
                if layers >= int(CONFIG["max_profile_layers_in_row"]):
                    raise Stop(EXIT_NEEDS_USER, f"{layers} jobs in a row asked for a profile update (layer "
                                                f"{res.get('layer')}); update the NaukriGulf profile")
            log(f"left {label}: {res['decision']} - {res['reason']}")
    return applied


# ------------------------------------------------------------------ main flow
def run(dry_run: bool, limit: int | None, visible: bool, keywords_override: str | None) -> int:
    con = open_db()
    started = datetime.now()
    mode = "dry-run" if dry_run else "live"
    run_id = con.execute("INSERT INTO runs (started, mode) VALUES (?, ?)",
                         (started.isoformat(timespec="seconds"), mode)).lastrowid
    con.commit()
    code, note = EXIT_OK, ""
    fetched = candidates = applied = 0
    queue_len = 0
    s = None
    try:
        check_markers()
        cap = int(CONFIG["daily_cap"])
        done_today = applied_today(con)
        budget = min(cap - done_today, int(CONFIG["per_run_cap"]))
        if limit is not None:
            budget = min(budget, limit)
        if not dry_run and budget <= 0:
            raise Stop(EXIT_CAP, f"daily cap reached ({done_today}/{cap} applied today)")
        f = Filters(identity_keywords())
        keywords = keyword_list(keywords_override)
        log(f"{CONFIG['account_label']}: starting {mode.upper()} ({done_today}/{cap} applied today, budget "
            f"{max(budget, 0)}; {len(keywords)} keywords x {len(CONFIG['countries'])} countries)")

        s, me = open_session(visible)
        log(f"signed in as the expected account; apply-ready={me['apply_ready']}, total experience "
            f"{me['total_experience']}")
        if not me["apply_ready"] and not dry_run:
            raise login_required("the NaukriGulf profile is not apply-ready (isApplyReady false); complete it on the site")
        applied_ids, applied_totals = read_applied(s)
        log(f"Applied list: {applied_totals.get('easy', 0)} Easy Apply + {applied_totals.get('web', 0)} employer-site "
            f"applications on the account")
        known = {r[0]: r[1] for r in con.execute("SELECT job_id, decision FROM applications")}
        attempts = {r[0]: r[1] for r in con.execute("SELECT job_id, attempts FROM applications WHERE decision='failed'")}
        known.update({jid: "failed twice" for jid, n in attempts.items() if n >= 2})

        jobs, per_query, search_calls = discover(s, keywords)
        fetched = len(jobs)
        rows, detail_calls = evaluate(s, jobs, f, applied_ids, known)
        save_jobs(con, rows)
        would = [r for r in rows if r["decision"] == "would-apply"]
        candidates = len(would)
        hist = Counter(r["reason"] for r in rows if r["decision"] != "would-apply")
        log(f"{fetched} unique jobs, {candidates} pass every filter; skips: "
            + "; ".join(f"{k} {v}" for k, v in hist.most_common()))

        if dry_run:
            meta = {"started": started.isoformat(timespec="seconds"), "account": CONFIG["account_label"],
                    "apply_ready": me["apply_ready"], "runtime_s": (datetime.now() - started).total_seconds(),
                    "search_calls": search_calls, "detail_calls": detail_calls, "fetched": fetched,
                    "would_apply": candidates, "applied_list": applied_totals, "keywords": keywords,
                    "held_back_by_source": sum(1 for r in rows if "passes the other search filters" in (r["detail"] or "")),
                    "headless": not visible, "user_agent": me["user_agent"], "ua_brands": me["ua_brands"]}
            js, md = write_reports(meta, rows, per_query)
            for r in would[:budget if budget > 0 else 0]:
                log(f"DRY RUN would apply: {r['title']} @ {r['company']} (job {r['job_id']}, {r['experience']}, "
                    f"posted {r['posted']}, score {r['score']:g})")
            note = f"dry run: {candidates} would-apply of {fetched}; report {md.name}"
        else:
            queue = would[:budget]
            queue_len = len(queue)
            applied = apply_queue(s, con, queue, f)
            verify_applied(s, con)
            note = f"applied {applied} of {queue_len} queued"
            if applied_today(con) >= cap:
                code, note = EXIT_CAP, f"daily cap reached ({cap}); {note}"
    except Stop as e:
        code, note = e.code, e.reason
        log(e.reason, "ERROR" if e.code in (EXIT_ERROR, EXIT_NEEDS_USER, EXIT_THROTTLED, EXIT_NOT_ARMED) else "INFO")
    except Exception as e:
        code, note = EXIT_ERROR, f"{type(e).__name__}: {e}"
        log(f"run crashed: {note}", "ERROR")
    finally:
        if s is not None:
            s.close()
        ended = datetime.now()
        con.execute("UPDATE runs SET ended=?, exit_code=?, fetched=?, candidates=?, applied=?, note=? WHERE run_id=?",
                    (ended.isoformat(timespec="seconds"), code, fetched, candidates, applied, note, run_id))
        con.commit()
        write_progress(con, {"mode": mode, "started": started.isoformat(timespec="seconds"),
                             "ended": ended.isoformat(timespec="seconds"), "exit": code, "fetched": fetched,
                             "candidates": candidates, "queued": queue_len, "applied": applied, "note": note})
        con.close()
    log(f"run finished: exit {code} ({note}) in {(datetime.now() - started).total_seconds():.0f} s")
    return code


def print_status() -> int:
    if not DB_PATH.exists():
        print(f"{CONFIG['account_label']}: no runs yet")
        return EXIT_OK
    con = open_db()
    total = con.execute("SELECT COUNT(*) FROM applications WHERE verified=1").fetchone()[0]
    print(f"{CONFIG['account_label']}: {applied_today(con)}/{CONFIG['daily_cap']} applied today, {total} confirmed in "
          f"total; live apply armed in this shell: {os.environ.get(LIVE_ENV) == '1'}")
    for r in con.execute("SELECT started, mode, exit_code, fetched, candidates, applied, note FROM runs "
                         "ORDER BY run_id DESC LIMIT 5"):
        print(f"  run {r[0]} {r[1]}: exit {r[2]}, fetched {r[3]}, candidates {r[4]}, applied {r[5]} ({r[6]})")
    for kind in ("THROTTLED", "LOGIN_REQUIRED"):   # named explicitly: Windows globs ignore case (naukrigulf_*.db)
        m = marker(kind)
        if m.exists():
            print(f"  marker {m.name}: {m.read_text(encoding='utf-8', errors='replace')[:120]}")
    if PROGRESS_PATH.exists():
        p = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        print(f"  progress: confirmed today {p.get('confirmed_today')}, total {p.get('confirmed_total')}, "
              f"last progress {p.get('last_progress_at')}")
    con.close()
    return EXIT_OK


def main() -> int:
    global _live_armed
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="(default) discover and report; never clicks or posts")
    mode.add_argument("--live", action="store_true", help=f"apply for real; refused unless {LIVE_ENV}=1 is also set")
    ap.add_argument("--limit", type=int, help="apply to at most N jobs this run (still bounded by both caps)")
    ap.add_argument("--visible", action="store_true", help="show the browser window instead of headless")
    ap.add_argument("--keywords", help="override the keyword list for this run, separated by ';'")
    ap.add_argument("--status", action="store_true", help="print counts, the last runs and markers, then exit")
    args = ap.parse_args()
    if args.status:
        return print_status()
    if args.live:
        if os.environ.get(LIVE_ENV) != "1":
            log(f"refusing --live: {LIVE_ENV}=1 is not set in this environment; nothing was applied", "ERROR")
            return EXIT_NOT_ARMED
        _live_armed = True

    lock = open(STATE_DIR / "naukrigulf_apply.lock", "a+")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log("another NaukriGulf run is active; exiting")
        return EXIT_OK
    return run(dry_run=not _live_armed, limit=args.limit, visible=args.visible, keywords_override=args.keywords)


if __name__ == "__main__":
    sys.exit(main())
