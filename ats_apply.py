"""ATS applier: applies to UAE data/AI jobs on employer career sites that run on an applicant-tracking system.
Dry run by default; one adapter so far (Workable). Built small on purpose: the digest finds about 0.1-0.2 eligible jobs a week.

    ats_apply.py                    dry run over the digest's matching jobs: fill each form, stop BEFORE Submit
    ats_apply.py --url <job url>    the same for one given job (rehearses the form even if a question is unanswerable)
    ats_apply.py --list             matching jobs from state/gulf_digest.json and what this tool would do with each (no browser)
    ats_apply.py status             counts, last runs, markers
    ats_apply.py verify             confirm 'applied' rows by the confirmation e-mail in the Dubai job-search Gmail (read-only)
    ats_apply.py --live             apply for real; refused unless ATS_APPLY_LIVE=1 is also set (nothing in this repo sets it)

Each run (one browser at a time, Chrome through naukrigulf_apply.ChromeSession, a real headful Chrome parked off-screen unless --headless):
  1. Needs >= min_free_mb of free RAM before a browser starts (else exit 47: the machine is RAM-starved).
  2. Reads the form's own field list (the page's public GET /api/v1/jobs/<code>/form) and answers it: contact fields from the
     user's existing configs (read in-process, never printed), the resume PDF of the job's identity, every other question
     ONLY through portal_kit/facts.py (closed world). A required question the resolver cannot answer exactly means the job
     is skipped and the question text is recorded in state/ats_apply_unanswered.jsonl. Nothing is ever guessed.
  3. Dry run: fills what it can, reads the values back, saves a screenshot in state/ats_evidence/, prints field LABELS only,
     and never touches the Submit button. As a second line of defence the browser itself refuses the apply request in a dry run
     (checked before any form is touched), so even a stray click cannot send an application. Live (armed): submits once;
     'applied' is written only after the application request answered 2xx AND the site's own "Your application has been
     submitted successfully" text appeared after the click; verified=1 only after the confirmation e-mail is found.
  4. Any challenge, rejection (any HTTP status >= 400 on the application request, "verification failed", an error page) or
     e-mail-code step: do nothing more (the browser is closed; only a screenshot remains), write state/ATS_APPLY_CHALLENGE,
     toast, exit 48, never retry. No CAPTCHA solving, no token farming, no evasion.

Exit codes: 0 ok, 44 daily cap reached, 42 needs the user (facts, resume, contact config or mail profile), 47 throttled or
RAM-starved, 48 challenge, 3 --live without ATS_APPLY_LIVE=1, 1 other error.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import msvcrt
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

try:
    from edge_cdp import CDPError
except ImportError:                      # the browser paths report the real problem; --list and status do not need it
    CDPError = RuntimeError

from portal_kit import dedupe            # never apply twice to the same opening (shared with every Gulf tool since 2026-10-14)
from portal_kit.dedupe import company_tokens, duplicate_of, norm_words, same_role

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("ATS_APPLY_CONFIG") or (
    HERE / "ats_apply_config.json" if (HERE / "ats_apply_config.json").is_file()
    else HERE / "ats_apply_config.example.json"))
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
GULF_CONFIG_PATH = Path(os.environ.get("GULF_CONFIG") or (
    HERE / "gulf_config.json" if (HERE / "gulf_config.json").is_file()
    else HERE / "gulf_config.example.json"))
STATE_DIR, LOG_DIR = HERE / "state", HERE / "logs"
STATE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
EVIDENCE_DIR = STATE_DIR / "ats_evidence"
DB_PATH = STATE_DIR / "ats_apply.db"
PROGRESS_PATH = STATE_DIR / "ats_apply_progress.json"
UNANSWERED_PATH = STATE_DIR / "ats_apply_unanswered.jsonl"
DIGEST_PATH = STATE_DIR / "gulf_digest.json"
LOCK_PATH = STATE_DIR / "ats_apply.lock"
LOG_PATH = LOG_DIR / "ats_apply.log"
LIVE_ENV = "ATS_APPLY_LIVE"
ACCOUNT_LABEL = "Employer ATS sites (Data Platform)"

EXIT_OK, EXIT_ERROR, EXIT_NOT_ARMED, EXIT_NEEDS_USER, EXIT_CAP, EXIT_THROTTLED, EXIT_CHALLENGE = 0, 1, 3, 42, 44, 47, 48
# decisions that end a job for good (never tried again); 'skipped' for an unanswerable question is retried once the facts change
FINAL_DECISIONS = {"applied", "challenge", "unconfirmed", "submitting", "failed twice"}   # 'submitting' = clicked, outcome never recorded

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_live_armed = False   # set only by main() when --live and ATS_APPLY_LIVE=1 are both present


class Stop(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


class Challenge(Stop):
    """A bot check, a rejection or an e-mail-code step. The run ends, nothing is retried, and the browser is closed as usual (the form is
    not left open; only the screenshot taken at that moment remains)."""

    def __init__(self, reason: str, status: int | None = None):
        super().__init__(EXIT_CHALLENGE, reason)
        self.status = status


class Skip(Exception):
    """This job cannot be applied to (closed, no form, no answer to a required question). Not an error."""


def log(msg: str, level: str = "INFO") -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def assert_armed() -> None:
    if not (_live_armed and os.environ.get(LIVE_ENV) == "1"):
        raise Stop(EXIT_NOT_ARMED, f"live apply is not armed (--live and {LIVE_ENV}=1 are both required)")


# The same toast the keepalive supervisors show (Show-Toast in instahyre_apply_keepalive.ps1), through hidden PowerShell.
TOAST_PS = r"""
try {
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
  $t = [System.Security.SecurityElement]::Escape($env:ATS_TOAST_TITLE)
  $b = [System.Security.SecurityElement]::Escape($env:ATS_TOAST_BODY)
  $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
  $xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$b</text></binding></visual></toast>")
  $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
} catch { exit 1 }
"""


def toast(title: str, body: str) -> None:
    try:
        env = {**os.environ, "ATS_TOAST_TITLE": title, "ATS_TOAST_BODY": body}
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", TOAST_PS],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode:
            log(f"toast failed (PowerShell exit {r.returncode})", "WARN")
    except Exception as e:
        log(f"toast failed: {type(e).__name__}", "WARN")


def free_ram_mb() -> int:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MemoryStatus()
    m.dwLength = ctypes.sizeof(MemoryStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return int(m.ullAvailPhys // (1024 * 1024))


def check_ram() -> None:
    free, need = free_ram_mb(), int(CONFIG["min_free_mb"])
    if free < need:
        raise Stop(EXIT_THROTTLED, f"the machine is RAM-starved: {free} MB free, {need} MB needed to start a browser; "
                                   "nothing was opened, try again when memory is free")


# ------------------------------------------------------------------ state
def open_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS applications (   -- same columns as naukrigulf_apply.db: the board's earn-back reads
                                                    -- decision='applied' AND verified=1, day = ts[:10]; live outcomes only
            job_id      TEXT PRIMARY KEY,           -- <ats>:<job code>
            title       TEXT,
            company     TEXT,
            location    TEXT,
            url         TEXT,
            decision    TEXT NOT NULL,              -- applied | skipped | challenge | unconfirmed | failed | failed twice
            reason      TEXT,
            http_status INTEGER,                    -- the site's answer to the application request (409 = bot check)
            layer_code  INTEGER,                    -- unused here
            site_flag   INTEGER DEFAULT 0,          -- 1 = the site's own confirmation text was seen
            verified    INTEGER DEFAULT 0,          -- 1 = the confirmation e-mail was found in the job-search Gmail
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
        CREATE TABLE IF NOT EXISTS submits (        -- one row per Submit click, whatever came of it: both caps count clicks, not successes
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      TEXT,
            ts          TEXT NOT NULL
        );
    """)
    return con


def applied_today(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) FROM applications WHERE decision='applied' AND substr(ts,1,10)=?",
                       (datetime.now().strftime("%Y-%m-%d"),)).fetchone()[0]


def clicks_today(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) FROM submits WHERE substr(ts,1,10)=?", (datetime.now().strftime("%Y-%m-%d"),)).fetchone()[0]


def record(con: sqlite3.Connection, job: "Job", decision: str, reason: str, status: int | None = None,
           site_flag: bool = False) -> None:
    # a later write refreshes the job's text fields when it knows more (a --url run learns the title and company only after the page loads)
    con.execute("""INSERT INTO applications (job_id, title, company, location, url, decision, reason, http_status, site_flag, ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_id) DO UPDATE SET decision=excluded.decision, reason=excluded.reason,
                     title=COALESCE(NULLIF(excluded.title, ''), applications.title),
                     company=COALESCE(NULLIF(excluded.company, ''), applications.company),
                     location=COALESCE(NULLIF(excluded.location, ''), applications.location),
                     url=COALESCE(NULLIF(excluded.url, ''), applications.url),
                     http_status=excluded.http_status, site_flag=excluded.site_flag,
                     attempts=applications.attempts + CASE WHEN applications.decision = 'submitting' THEN 0 ELSE 1 END,
                     ts=excluded.ts""",
                (job.key, job.title, job.company, job.location, job.url, decision, reason[:900], status, int(site_flag),
                 datetime.now().isoformat(timespec="seconds")))
    con.commit()


def write_progress(con: sqlite3.Connection, run_info: dict) -> None:
    """Progress means a NEW application confirmed by e-mail (verified=1), never 'the process is alive'."""
    try:
        prev = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except Exception:
        prev = {}
    today = datetime.now().strftime("%Y-%m-%d")
    total = con.execute("SELECT COUNT(*) FROM applications WHERE verified=1").fetchone()[0]
    state = {
        "account": ACCOUNT_LABEL, "day": today, "confirmed_total": total,
        "confirmed_today": con.execute("SELECT COUNT(*) FROM applications WHERE verified=1 AND substr(verified_at,1,10)=?",
                                       (today,)).fetchone()[0],
        "accepted_today": applied_today(con),
        "unconfirmed_total": con.execute("SELECT COUNT(*) FROM applications WHERE decision='applied' AND verified=0").fetchone()[0],
        "daily_cap": int(CONFIG["daily_cap"]), "last_progress_at": prev.get("last_progress_at"), "last_run": run_info,
        "last_live_run": prev.get("last_live_run"), "last_dry_run": prev.get("last_dry_run"), "last_verify": prev.get("last_verify"),
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    if total > int(prev.get("confirmed_total") or 0):
        state["last_progress_at"] = datetime.now().isoformat(timespec="seconds")
    state[{"live": "last_live_run", "verify": "last_verify"}.get(run_info.get("mode"), "last_dry_run")] = run_info
    PROGRESS_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def marker(kind: str) -> Path:
    return STATE_DIR / f"ATS_APPLY_{kind}"


def set_marker(kind: str, until: datetime, what: str, job: str = "") -> None:
    marker(kind).write_text(json.dumps({"until": until.isoformat(timespec="seconds"),
                                        "since": datetime.now().isoformat(timespec="seconds"), "what": what, "job": job}),
                            encoding="utf-8")


def check_markers() -> None:
    for kind, code, label in (("CHALLENGE", EXIT_CHALLENGE, "a bot check or rejection"), ("THROTTLED", EXIT_THROTTLED, "a throttle")):
        m = marker(kind)
        if not m.exists():
            continue
        try:
            until = datetime.fromisoformat(json.loads(m.read_text(encoding="utf-8"))["until"])
        except Exception:
            until = datetime.fromtimestamp(m.stat().st_mtime) + timedelta(hours=CONFIG["challenge_backoff_hours"])
        if datetime.now() < until:
            raise Stop(code, f"{label} was hit earlier; backing off until {until:%Y-%m-%d %H:%M} (marker {m.name}); not retrying")
        m.unlink(missing_ok=True)


def ledger_unanswered(job: "Job", rows: list[dict]) -> None:
    """One line per question the resolver could not answer, once per facts revision (a re-run adds nothing new)."""
    seen = set()
    try:
        for line in UNANSWERED_PATH.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            seen.add((d.get("company"), d.get("title"), d.get("question"), d.get("facts_rev")))
    except (OSError, ValueError):
        pass
    with open(UNANSWERED_PATH, "a", encoding="utf-8") as fh:
        for r in rows:
            if (job.company, job.title, r.get("question"), r.get("facts_rev")) in seen:
                continue
            fh.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "ats": job.ats, "company": job.company,
                                 "title": job.title, **r}, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ the applicant (contact fields are read in-process, never printed)
@dataclass
class Applicant:
    identity: str
    first_name: str
    last_name: str
    email: str
    phone: str                          # digits only
    address: str
    resume: Path | None
    facts: object = None               # portal_kit.facts.Facts(live=True): what a live run may answer
    facts_loose: object = None          # Facts(live=False): tells a 'needs a proposed fact' refusal from a real one
    ctx: dict = field(default_factory=dict)

    def e164(self) -> str:
        d = self.phone
        return "+" + d if len(d) > 10 else "+91" + d


def _personals() -> dict:
    """Plain assignments of the LinkedIn bot's config/personals.py, read with ast (nothing is imported or executed)."""
    import ast
    out = {}
    try:
        for node in ast.parse(Path(CONFIG["personals_py"]).read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                try:
                    out[node.targets[0].id] = ast.literal_eval(node.value)
                except Exception:
                    pass
    except OSError:
        pass
    return out


def load_applicant(identity: str) -> Applicant:
    spec = (CONFIG["identities"] or {}).get(identity)
    if not spec:
        raise Stop(EXIT_NEEDS_USER, f"unknown identity {identity!r}")
    try:
        cfg = json.loads(Path(spec["contact_config"]).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise Stop(EXIT_NEEDS_USER, f"cannot read the {identity} contact config ({type(e).__name__}); it holds the name, e-mail and phone")
    # This config file keeps a plaintext password in its credentials block. json.loads necessarily reads the whole file, so the password
    # sits in memory for these few lines; it is dropped immediately and is never used, printed, logged or copied. Only the e-mail is wanted.
    (cfg.get("credentials") or {}).pop("password", None)
    info, per = cfg.get("personal_info") or {}, _personals()
    first = str(info.get("firstname") or per.get("first_name") or "").strip()
    last = str(info.get("lastname") or per.get("last_name") or "").strip()
    email = str((cfg.get("credentials") or {}).get("email") or "").strip()      # the e-mail only; the password is never read
    phone = re.sub(r"\D", "", str(info.get("phone") or per.get("phone_number") or ""))
    if not (first and last and re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]+", email) and len(phone) >= 10):
        raise Stop(EXIT_NEEDS_USER, f"the {identity} contact config lacks a name, a valid e-mail or a phone number")
    address = ", ".join(str(per.get(k)).strip() for k in ("current_city", "state", "country") if str(per.get(k) or "").strip())
    resume = None
    rs = spec.get("resume") or {}
    if "python_constant" in rs:
        pc = rs["python_constant"]
        try:
            m = re.search(rf'^{re.escape(pc["name"])}\s*=\s*r?["\']([^"\']+)["\']', Path(pc["file"]).read_text(encoding="utf-8"), re.M)
            resume = Path(m.group(1)) if m else None
        except OSError:
            resume = None
    elif "json_key" in rs:
        node = cfg
        for k in rs["json_key"]:
            node = node.get(k) if isinstance(node, dict) else None
        resume = Path(node) if node else None
    if resume is None or not resume.is_file() or resume.suffix.lower() != ".pdf":
        raise Stop(EXIT_NEEDS_USER, f"the {identity} resume PDF named in the existing configs is missing or not a PDF")
    return Applicant(identity, first, last, email, phone, address, resume)


def contact_names(identity: str) -> tuple:
    """First and last name from the identity's contact config, only to blank them out of mail subjects before printing."""
    try:
        info = json.loads(Path(CONFIG["identities"][identity]["contact_config"]).read_text(encoding="utf-8-sig")).get("personal_info") or {}
    except (OSError, ValueError, KeyError):
        return ()
    return tuple(str(info.get(k)).strip() for k in ("firstname", "lastname") if str(info.get(k) or "").strip())


def load_facts():
    try:
        from portal_kit.facts import Facts
        return Facts(live=True), Facts(live=False)
    except Exception as e:
        raise Stop(EXIT_NEEDS_USER, f"the facts file or resolver cannot be loaded ({type(e).__name__}); nothing may be answered without it")


# ------------------------------------------------------------------ jobs
@dataclass
class Job:
    key: str                  # <ats>:<code>
    ats: str                  # adapter key, "" when there is no adapter
    title: str
    company: str
    location: str
    url: str
    identity: str = "data"
    account: str = ""
    code: str = ""
    skip: str = ""            # why this job cannot be attempted at all
    new: bool = False


# ATS types seen in the digest that have no adapter, with the reason shown by --list and recorded as the skip reason.
UNSUPPORTED = {
    "lever": "Lever: hCaptcha at submit, out of scope",
    "greenhouse": "Greenhouse: invisible reCAPTCHA Enterprise plus an emailed 8-character code, no adapter yet",
    "ashby": "Ashby: reCAPTCHA, no adapter yet",
    "michaelpage": "agency listing: the agency applies for you",
}
URL_TYPES = (("lever", re.compile(r"(^|[./])lever\.co/")), ("greenhouse", re.compile(r"greenhouse\.io/|[?&]gh_jid=")),
             ("ashby", re.compile(r"ashbyhq\.com/")))


def digest_sources() -> dict:
    try:
        return {s["key"]: s for s in json.loads(GULF_CONFIG_PATH.read_text(encoding="utf-8"))["digest"]["sources"]}
    except (OSError, ValueError, KeyError):
        return {}


def classify(item: dict, sources: dict) -> Job:
    url = item.get("url") or ""
    m = re.search(r"\(([a-z0-9-]+)\)", item.get("reason") or "")
    src = sources.get(m.group(1)) if m else None
    job = Job(key="", ats="", title=item.get("title", ""), company=item.get("company", ""), location=item.get("location", ""),
              url=url, identity=item.get("identity") or "data", new=bool(item.get("new")))
    for ad in ADAPTERS:
        if ad.detect(url):
            job.ats = ad.key
            job.account, job.code = ad.parse(url)
            if not job.account and src and src.get("type") == ad.key:
                job.account = src.get("account", "")
            job.key = f"{ad.key}:{job.code}"
            return job
    stype = next((t for t, rx in URL_TYPES if rx.search(url)), (src or {}).get("type", ""))
    job.key = f"{stype or 'unknown'}:{url or job.title}"
    job.skip = UNSUPPORTED.get(stype, f"no adapter for this site ({stype or 'unknown ATS'})")
    return job


# The digest also lists Saudi Arabia and Qatar jobs since 2026-10-19, but this applier answers visa and work-authorisation questions
# with UAE facts only (ctx region "UAE"), so any job not located in the UAE is listed and skipped, never applied to.
UAE_WORDS = ("uae", "united arab emirates", "dubai", "abu dhabi", "sharjah", "ajman", "ras al khaimah", "fujairah", "al ain",
             "umm al quwain")


def is_uae(location: str) -> bool:
    loc = (location or "").lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", loc) for w in UAE_WORDS)


def digest_jobs() -> list[Job]:
    try:
        items = json.loads(DIGEST_PATH.read_text(encoding="utf-8")).get("items", [])
    except (OSError, ValueError):
        return []
    src = digest_sources()
    jobs = []
    for i in items:
        if i.get("manual") or not i.get("url"):
            continue
        job = classify(i, src)
        if not job.skip and not is_uae(i.get("location", "")):
            job.skip = (f"not a UAE job ({(i.get('location') or 'no location')[:40]}): this applier answers visa questions with UAE "
                        "facts only")
        jobs.append(job)
    return jobs


def job_from_url(url: str, identity: str) -> Job:
    for ad in ADAPTERS:
        if ad.detect(url):
            acct, code = ad.parse(url)
            return Job(key=f"{ad.key}:{code}", ats=ad.key, title="", company="", location="", url=url, identity=identity,
                       account=acct, code=code)
    stype = next((t for t, rx in URL_TYPES if rx.search(url)), "")
    return Job(key=f"{stype or 'unknown'}:{url}", ats="", title="", company="", location="", url=url, identity=identity,
               skip=UNSUPPORTED.get(stype, "no adapter for this site"))


def db_status(con: sqlite3.Connection | None) -> dict:
    if con is None:
        return {}
    return {r[0]: (r[1], r[2] or "", r[3]) for r in con.execute("SELECT job_id, decision, reason, attempts FROM applications")}


def remember(con: sqlite3.Connection, known: dict, job: "Job", decision: str, reason: str, status: int | None = None,
             site_flag: bool = False) -> None:
    """record() plus the in-memory view of the database, so a later job in the same run sees what this one just did."""
    record(con, job, decision, reason, status, site_flag)
    known[job.key] = (decision, reason, con.execute("SELECT attempts FROM applications WHERE job_id=?", (job.key,)).fetchone()[0])


# ------------------------------------------------------------------ never apply twice to the same opening
# norm_words, company_tokens, same_role and duplicate_of come from portal_kit/dedupe.py (imported at the top); company_tokens there
# has the generic-word step (2026-10-14).
def other_applications() -> list[tuple]:
    """(source, company, title) of what the other tools applied to: the stores named in ats_apply_config.json (already_applied_elsewhere:
    the NaukriGulf and Instahyre databases and the LinkedIn UAE job log), then the user's own list, state/manual_applications.csv.
    Read-only and best effort; a store that is missing or unreadable adds nothing. Relative store paths are read from job-tracker
    (dedupe.JT), not from this checkout, so a worktree and the live copy see the same stores."""
    return dedupe.other_applications(CONFIG.get("already_applied_elsewhere") or {}, dedupe.JT)


def cmd_list() -> int:
    if not DIGEST_PATH.exists():
        print("no digest yet (state/gulf_digest.json); run gulf_digest.py first")
        return EXIT_ERROR
    jobs = digest_jobs()
    d = json.loads(DIGEST_PATH.read_text(encoding="utf-8"))
    con = open_db() if DB_PATH.exists() else None
    known = db_status(con)
    others = other_applications()
    print(f"digest {d.get('updated')}: {len(jobs)} employer-board job(s) match the digest rules "
          f"({sum(1 for j in jobs if not j.skip)} on a site this tool can apply to)")
    for j in jobs:
        todo = f"WOULD TRY ({j.ats})" if not j.skip else f"skip: {j.skip}"
        dup = duplicate_of(j, con, others) if con else next((f"already applied through {s}" for s, c, t in others if same_role(j.company, j.title, c, t)), "")
        if j.key in known:
            dec, why, n = known[j.key]
            todo = f"{dec}" + (f": {why[:90]}" if why else "")
        elif dup:
            todo = f"skip: duplicate, {dup}"
        spec = CONFIG["identities"].get(j.identity) or {}
        ident = spec.get("label", j.identity)
        if not j.skip and j.key not in known and not dup and not spec.get("mailbox_verifiable"):
            todo = f"dry run only: {ident} confirmation mail is not readable, a live run skips it"
        print(f"  {'NEW ' if j.new else '    '}{j.company[:22]:22} {j.title[:46]:46} {ident[:22]:22} {todo}")
    if con:
        con.close()
    return EXIT_OK


# ------------------------------------------------------------------ page helpers (CDP through ChromeSession)
def mouse_click(s, x: float, y: float) -> None:
    s.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    s.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    s.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})


class Page:
    """Small helpers over a ChromeSession: evaluate, wait, real mouse clicks, typed text, full-page screenshots."""

    def __init__(self, session):
        self.s = session

    def ev(self, fn: str, *args, await_promise: bool = False, timeout: float = 60):
        expr = "(" + fn + ")(" + ",".join(json.dumps(a) for a in args) + ")"
        return self.s.eval(expr, await_promise=await_promise, timeout=timeout)

    def go(self, url: str, timeout: float = 90) -> str:
        return self.s.navigate(url, timeout=timeout)

    def href(self) -> str:
        return str(self.s.eval("location.href"))

    def text(self, limit: int = 8000) -> str:
        return str(self.ev(r"(n) => (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').slice(0, n)", limit)).lower()

    def wait(self, cond_js: str, timeout: float = 30, every: float = 0.5) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.s.eval(cond_js):
                    return True
            except CDPError:
                pass
            time.sleep(every)
        return False

    def center(self, sel: str) -> dict | None:
        """Where to click an element: scrolled into view instantly (never a smooth scroll that would leave the rect stale), then measured."""
        return self.ev(r"""(sel) => { const e = document.querySelector(sel); if (!e) return null;
            e.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'}); const r = e.getBoundingClientRect();
            return (r.width || r.height) ? {x: r.left + r.width / 2, y: r.top + r.height / 2} : null; }""", sel)

    def click_xy(self, x: float, y: float) -> bool:
        """A real mouse click, refused when the point lies on the Submit button: no step before the final live click may press it."""
        if not self.ev("(x, y) => { const e = document.elementFromPoint(x, y); return !!e && !e.closest('button[data-ui=\"apply-button\"]'); }", x, y):
            return False
        mouse_click(self.s, x, y)
        return True

    def click(self, sel: str) -> bool:
        c = self.center(sel)
        return bool(c) and self.click_xy(c["x"], c["y"])

    def type_into(self, sel: str, text: str) -> bool:
        ok = self.ev(r"""(sel) => { const e = document.querySelector(sel); if (!e) return false;
            e.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'}); e.focus(); if (e.select) e.select();
            return document.activeElement === e; }""", sel)
        if not ok:
            return False
        self.s.call("Input.insertText", {"text": text})
        self.ev("(sel) => { const e = document.querySelector(sel); if (e) e.blur(); }", sel)
        return True

    def value(self, sel: str) -> str:
        return str(self.ev("(sel) => { const e = document.querySelector(sel); return e ? String(e.value || '') : null; }", sel) or "")

    def shot(self, name: str) -> Path:
        EVIDENCE_DIR.mkdir(exist_ok=True)
        m = self.s.call("Page.getLayoutMetrics")
        size = m.get("cssContentSize") or m.get("contentSize") or {"width": 1280, "height": 900}
        clip = {"x": 0, "y": 0, "width": float(size["width"]), "height": min(float(size["height"]), 14000.0), "scale": 1}
        data = self.s.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True, "clip": clip})["data"]
        path = EVIDENCE_DIR / f"{name}_{datetime.now():%Y%m%d_%H%M%S}.png"
        path.write_bytes(base64.b64decode(data))
        return path

    def challenge_marks(self) -> list[str]:
        return list(self.s.eval(CHALLENGE_JS) or [])


CHALLENGE_JS = r"""(() => {
  const body = (document.body ? document.body.innerText : '').toLowerCase();
  const head = (document.title || '').toLowerCase() + ' ' + body.slice(0, 800);
  const marks = [];
  if (/just a moment|attention required|verify you are human|checking your browser|performing security verification|are you a robot|access denied/.test(head)) marks.push('interstitial page');
  const big = Array.from(document.querySelectorAll('iframe')).filter(f => /challenges\.cloudflare\.com|hcaptcha|recaptcha|turnstile/i.test(f.src || '') && f.offsetWidth >= 100 && f.offsetHeight >= 40);
  if (big.length) marks.push('visible captcha frame');
  if (/couldn.t process your request|from a different browser/.test(body)) marks.push("the site's own bot-check error");
  if (/verification failed|security check failed|could not verify you/.test(body)) marks.push('verification failed');
  if (/(enter|type) the (\d+|eight|8)[- ]?(character|digit)?[- ]?(security )?code|verification code|code we (emailed|sent)/.test(body)) marks.push('e-mailed code step');
  // a reset or refused connection ends on Chrome's own error page: that is how a site's edge turns an automated browser away
  if (location.protocol === 'chrome-error:' || /err_connection_(reset|closed|refused)|err_http2_protocol_error|err_empty_response|err_ssl_protocol_error|this site can.t be reached/.test(body)) marks.push('connection reset or browser error page');
  return marks;
})()"""


def guard_challenge(page: Page, what: str) -> None:
    marks = page.challenge_marks()
    if marks:
        raise Challenge(f"{what}: {', '.join(marks)}")


# ------------------------------------------------------------------ adapters
@dataclass
class Item:
    fid: str
    label: str
    wtype: str
    required: bool
    options: list = field(default_factory=list)
    opt_names: dict = field(default_factory=dict)
    max_len: int | None = None
    answer: str | None = None
    why: str = ""             # why there is no answer
    intent: str = ""
    status: str = ""          # ready | filled | blank | blocked | ignored | problem


@dataclass
class FillResult:
    items: list = field(default_factory=list)
    blocked: list = field(default_factory=list)      # required, no answer
    blank: list = field(default_factory=list)        # optional, no answer
    problems: list = field(default_factory=list)     # something on the page did not take what was typed
    screenshot: Path | None = None
    touched: bool = False
    submit_present: bool = False
    sections: int = 0


@dataclass
class Outcome:
    kind: str                 # confirmed | challenge | unconfirmed | no_request (the form's own validation stopped it) | no_click
    status: int | None = None
    note: str = ""


def canned_reason(exc) -> str:
    """Why the resolver refused a question, in words that carry no values. Its own reasons can quote option lists and numbers taken from
    the user's facts ("no choice covers 15 day among [...]"), and this text reaches the screen, the ledger and the reports."""
    r = getattr(exc, "reason", "") or ""
    if r.startswith("no intent maps"):
        phrase = "no intent maps this question"
    elif r.startswith("compound question"):
        phrase = "compound question (two questions in one)"
    elif r.startswith("a rule covers this topic"):
        phrase = "a rule covers this topic but not this wording or form shape"
        rule = re.search(r":\s*([a-z_.]+)\s*$", r)                  # the rule's own name, never a value
        if rule and not getattr(exc, "intent", ""):
            return f"{phrase} [{rule.group(1)}]"
    else:
        phrase = "the resolver refused it"
    return phrase + (f" [{exc.intent}]" if getattr(exc, "intent", "") else "")


class Adapter:
    """One per ATS. Greenhouse and Ashby adapters would implement the same four calls; Lever is skipped (UNSUPPORTED)."""
    key = ""

    def detect(self, url: str) -> bool:                      # is this job on this ATS?
        raise NotImplementedError

    def parse(self, url: str) -> tuple[str, str]:            # (account, job code)
        raise NotImplementedError

    def open(self, page: Page, job: Job) -> None:            # load the application form (raises Skip / Challenge / Stop)
        raise NotImplementedError

    def fill(self, page: Page, profile: Applicant, fill_blocked: bool = False) -> FillResult:
        raise NotImplementedError

    def submit(self, page: Page, on_click=None) -> Outcome:  # one click, live only; on_click runs right before it
        raise NotImplementedError

    def confirmed(self, page: Page) -> bool:                 # the site's own confirmation text is on the page
        raise NotImplementedError


class Workable(Adapter):
    """apply.workable.com/<account>/j/<code>/apply/. Cloudflare Turnstile guards the submit (a rejection is HTTP 409)."""
    key = "workable"
    URL = re.compile(r"^https?://apply\.workable\.com/(?:(?P<account>[A-Za-z0-9_-]+)/)?j/(?P<code>[A-Za-z0-9]{6,})(?:/apply)?/?(?:[?#].*)?$")
    SUBMIT = 'button[data-ui="apply-button"]'
    CORE = {"firstname": "first_name", "lastname": "last_name", "email": "email", "phone": "phone", "address": "address"}
    KIND = {"text": "text", "email": "text", "paragraph": "textarea", "boolean": "radio", "dropdown": "select"}
    BOOL_VALUE = {"Yes": "true", "No": "false"}              # a boolean field's two radios; any other answer is an error, never a silent "No"
    NAME_LABELS = {"first name": "first", "given name": "first", "last name": "last", "family name": "last", "surname": "last",
                   "full name": "full", "legal name": "full", "name": "full"}
    SUCCESS = re.compile(r"application has been submitted successfully")
    API = re.compile(r"/api/v1/jobs/[^/]+/apply(?:[/?]|$)")

    def detect(self, url: str) -> bool:
        return bool(self.URL.match(url or ""))

    def parse(self, url: str) -> tuple[str, str]:
        m = self.URL.match(url)
        return (m.group("account") or "", m.group("code").upper()) if m else ("", "")

    # -- page
    def open(self, page: Page, job: Job) -> None:
        account = job.account
        if not account:                                    # a shortlink: the site redirects it to /<account>/j/<code>
            page.go(f"https://apply.workable.com/j/{job.code}")
            time.sleep(3)
            guard_challenge(page, "on loading the job link")
            m = re.search(r"apply\.workable\.com/([^/]+)/j/", page.href())
            if not m:
                raise Skip("the job link does not lead to a Workable job page (closed or moved)")
            account = job.account = m.group(1)
        page.go(f"https://apply.workable.com/{account}/j/{job.code}/apply/")
        guard_challenge(page, "on loading the application page")          # also catches Chrome's connection-reset error page
        if not page.wait("!!document.querySelector('input[name=\"firstname\"]')", 40):
            guard_challenge(page, "on loading the application page")
            t = page.text(2000)
            if "no longer available" in t or "page not found" in t:
                raise Skip("the job is no longer available")
            raise Skip("the application form did not appear within 40 s")
        title = str(page.s.eval("document.title") or "")
        m = re.match(r"^(.*?) - (.*?) - Application$", title)
        if m:
            job.title = job.title or m.group(1)
            job.company = job.company or m.group(2)

    def read_spec(self, page: Page, code: str) -> list:
        """The form's own field list: the same same-origin GET the page makes itself."""
        try:
            res = page.ev(r"""async (path) => { const r = await fetch(path, {credentials: 'same-origin', headers: {'Accept': 'application/json'}});
                const text = await r.text(); let data = null; try { data = JSON.parse(text); } catch (e) {}
                return {status: r.status, data: data, text: data === null ? text.slice(0, 160) : null}; }""",
                          f"/api/v1/jobs/{code}/form", await_promise=True)
        except CDPError as e:
            if "Failed to fetch" in str(e):                   # the page loaded, then its own request was cut: how an edge turns a bot away
                raise Challenge("the form definition request was cut off in the page (connection reset or refused)")
            raise
        if res["status"] in (401, 403):                       # the site refusing the browser is a challenge, not a throttle
            raise Challenge(f"the form definition was refused (HTTP {res['status']})", res["status"])
        if res["status"] == 429:
            until = datetime.now() + timedelta(minutes=int(CONFIG["throttle_backoff_minutes"]))
            set_marker("THROTTLED", until, "the form definition answered HTTP 429", code)
            raise Stop(EXIT_THROTTLED, f"Workable answered HTTP 429 to the form definition; pausing until {until:%H:%M}")
        if res["status"] != 200 or not isinstance(res.get("data"), list):
            raise Skip(f"the form definition is not available (HTTP {res['status']})")
        return res["data"]

    # -- answers
    def plan(self, spec: list, profile: Applicant) -> list[Item]:
        items = []
        for sec in spec:
            for f in sec.get("fields", []):
                opts = f.get("options") or []
                it = Item(fid=f["id"], label=(f.get("label") or f["id"]).strip(), wtype=f.get("type", ""), required=bool(f.get("required")),
                          options=[o.get("value", "") for o in opts], opt_names={o.get("value", ""): o.get("name", "") for o in opts},
                          max_len=f.get("maxLength"))
                self.answer(it, profile)
                items.append(it)
        return items

    def answer(self, it: Item, profile: Applicant) -> None:
        if it.fid == "resume":
            it.answer, it.intent = "resume", "resume"
            return
        if it.wtype == "group":                              # education / experience blocks
            it.why = "a repeating section this tool does not fill"
            return
        if it.fid in self.CORE:
            val = str(getattr(profile, self.CORE[it.fid]) if it.fid != "phone" else profile.e164())
            if val:
                it.answer, it.intent = val, f"contact.{it.fid}"
            else:
                it.why = "no value for this contact field in the user's configs"
            return
        kind = self.KIND.get(it.wtype)
        if kind is None:
            it.why = f"a '{it.wtype}' field this tool does not fill"
            return
        options = ["Yes", "No"] if it.wtype == "boolean" else it.options
        try:
            from portal_kit.facts import Unanswerable
            try:
                a = profile.facts.resolve(it.label, kind, options, numeric=False, required=it.required, ctx=profile.ctx)
            except Unanswerable as e:
                it.intent = e.intent
                try:
                    profile.facts_loose.resolve(it.label, kind, options, numeric=False, required=it.required, ctx=profile.ctx)
                    it.why = "answerable only with a proposed fact the user has not confirmed" + (f" ({', '.join(e.facts)})" if e.facts else "")
                except Unanswerable:
                    it.why = canned_reason(e)
                return
        except ImportError:
            it.why = "the resolver is not importable"
            return
        it.intent = a.intent
        if a.keep:                                           # a contact intent: this tool's own value for it
            if a.intent == "contact.email":
                it.answer = profile.email
            elif a.intent == "contact.phone":
                it.answer = profile.e164()
            elif a.intent == "contact.name":                 # only an exact name label gets a name; "middle name" and the like get none
                which = self.NAME_LABELS.get(" ".join(norm_words(re.sub(r"\((required|optional)\)", "", it.label, flags=re.I))))
                it.answer = {"first": profile.first_name, "last": profile.last_name,
                             "full": f"{profile.first_name} {profile.last_name}"}.get(which)
            if it.answer is None:
                it.why = f"a contact field ({a.intent}) this tool has no value for"
                return
        elif a.check is not None:
            it.why = "a tick box this tool does not handle"
        else:
            it.answer = a.value
        # the shape of the answer must fit the field: anything else is no answer at all, never a coerced one
        if it.answer is not None and not str(it.answer).strip():
            it.answer, it.why = None, "the resolver's answer is empty"
        if it.answer is not None and it.wtype == "boolean" and it.answer not in self.BOOL_VALUE:
            it.answer, it.why = None, "the resolver's answer to a Yes/No question is neither Yes nor No"
        if it.answer is not None and it.max_len and len(it.answer) > int(it.max_len):
            it.answer, it.why = None, "the answer is longer than the field allows (never truncated)"
        if it.answer is not None and it.wtype == "dropdown" and it.answer not in it.opt_names:
            it.answer, it.why = None, "the resolver's option is not one of the form's options"

    # -- fill
    def fill(self, page: Page, profile: Applicant, fill_blocked: bool = False) -> FillResult:
        res = FillResult()
        spec = self.read_spec(page, profile.ctx["code"])
        res.sections = len(spec)
        res.items = items = self.plan(spec, profile)
        for it in items:
            if it.answer is not None:
                it.status = "ready"
            elif it.required:
                it.status = "blocked"
            else:
                it.status = "ignored" if it.wtype == "group" else "blank"
        res.blocked = [i for i in items if i.required and i.answer is None]
        res.blank = [i for i in items if not i.required and i.answer is None and i.wtype != "group"]
        if res.blocked and not fill_blocked:
            return res                                       # a job that will be skipped gets no filled form (and, in a dry run, no screenshot of one)
        page.click('button[data-ui="cookie-consent-decline"]')   # the banner's own "Decline all"
        time.sleep(0.8)
        res.touched = True
        # the resume first: if the site reads it and pre-fills fields, the typed answers below are the last word
        for it in sorted(items, key=lambda i: i.fid != "resume"):
            if it.answer is None:
                continue
            try:
                ok = self.set_field(page, it, profile)
            except (CDPError, KeyError):                     # no exception text: it could quote the page
                ok = False
            if not ok:
                it.status = "problem"
                res.problems.append(f"{it.label}: could not be set on the page")
            else:
                it.status = "filled"
        time.sleep(0.5)
        for it in items:                                     # read every answer back from the page
            if it.status != "filled":
                continue
            try:
                kept = self.read_back(page, it, profile)
            except (CDPError, KeyError):
                kept = False
            if not kept:
                it.status = "problem"
                res.problems.append(f"{it.label}: the page did not keep the value")
        res.submit_present = bool(page.center(self.SUBMIT))
        res.screenshot = page.shot(f"workable_{profile.ctx['code']}")
        return res

    def set_field(self, page: Page, it: Item, profile: Applicant) -> bool:
        s = page.s
        if it.fid == "resume":
            r = s.call("Runtime.evaluate", {"expression": "document.querySelector('input[type=file][data-ui=\"resume\"]') || "
                                                          "document.querySelector('input[type=file]')", "returnByValue": False})
            oid = (r.get("result") or {}).get("objectId")
            if not oid:
                return False
            s.call("DOM.setFileInputFiles", {"objectId": oid, "files": [str(profile.resume)]})
            # Measured on the real Bayut form (2026-10-02, network_summary over all 76 requests of a dry run): choosing the file sends nothing.
            # The only request with a body was Cloudflare's own text/plain beacon; there was no multipart request. The browser just holds the
            # file (the field then offers "Replace file") and reads it when Submit is pressed, so a dry run does not upload the resume.
            return page.wait(self.resume_attached_js(profile.resume.name), 20)
        if it.wtype == "boolean":
            want = self.BOOL_VALUE[it.answer]                # KeyError for anything but Yes/No: an error, never a silent "No"
            c = page.ev(r"""(name, v) => { const e = document.querySelector(`input[type=radio][name="${name}"][value="${v}"]`); if (!e) return null;
                const t = e.closest('label') || document.querySelector(`label[for="${e.id}"]`) || e;
                t.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                const r = t.getBoundingClientRect(); return (r.width || r.height) ? {x: r.left + r.width / 2, y: r.top + r.height / 2} : {js: true}; }""",
                        it.fid, want)
            if not c:
                return False
            if c.get("js"):
                page.ev("(name, v) => document.querySelector(`input[type=radio][name=\"${name}\"][value=\"${v}\"]`).click()", it.fid, want)
            elif not page.click_xy(c["x"], c["y"]):
                return False
            time.sleep(0.3)
            return True
        if it.wtype == "dropdown":
            if not page.click(f'input[id="input_{it.fid}_input"]'):
                return False
            time.sleep(0.6)
            pos = page.ev(r"""(txt) => { const n = t => (t || '').replace(/\s+/g, ' ').trim().toLowerCase();
                const els = Array.from(document.querySelectorAll('[role=option]')).filter(e => e.offsetWidth || e.offsetHeight);
                const hit = els.find(e => n(e.innerText) === n(txt)); if (!hit) return null;
                hit.scrollIntoView({block: 'nearest', inline: 'nearest', behavior: 'instant'});
                const r = hit.getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2}; }""", it.answer)
            if not pos or not page.click_xy(pos["x"], pos["y"]):
                return False
            time.sleep(0.4)
            return True
        sel = f'textarea[name="{it.fid}"]' if it.wtype == "paragraph" else f'input[name="{it.fid}"]'
        return page.type_into(sel, str(it.answer))

    @staticmethod
    def resume_attached_js(name: str) -> str:
        return ("(() => { const e = document.querySelector('input[type=file][data-ui=\"resume\"]') || document.querySelector('input[type=file]');"
                " return !!e && e.files.length === 1 && e.files[0].name === NAME && /replace file/i.test(document.body.innerText); })()"
                ).replace("NAME", json.dumps(name))

    def read_back(self, page: Page, it: Item, profile: Applicant) -> bool:
        if it.fid == "resume":
            return bool(page.s.eval(self.resume_attached_js(profile.resume.name)))
        if it.wtype == "boolean":
            got = page.ev("(name) => { const e = document.querySelector(`input[type=radio][name=\"${name}\"]:checked`); return e ? e.value : ''; }", it.fid)
            return got == self.BOOL_VALUE[it.answer]
        if it.wtype == "dropdown":
            return page.value(f'input[name="{it.fid}"]') == it.opt_names.get(str(it.answer))
        got = page.value(f'textarea[name="{it.fid}"]' if it.wtype == "paragraph" else f'input[name="{it.fid}"]')
        if it.fid == "phone":
            want, have = re.sub(r"\D", "", profile.e164()), re.sub(r"\D", "", got)
            # the number must be the user's AND the widget's selected country must be the number's own country
            dial = page.ev(r"""() => { const i = document.querySelector('input[name="phone"]'); const box = i && (i.closest('.iti') || i.parentElement);
                const d = box && box.querySelector('[class*="selected-dial-code"]'); return d ? d.textContent.trim() : null; }""")
            return len(have) >= 10 and want.endswith(have[-10:]) and (dial is None or dial.lstrip("+") == want[:len(want) - 10])
        return " ".join(got.split()) == " ".join(str(it.answer).split())

    # -- live only
    SUBMIT_TARGET_JS = r"""() => {
      const b = document.querySelector('button[data-ui="apply-button"]');
      if (!b || b.disabled) return null;
      b.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
      const r = b.getBoundingClientRect();
      if (!(r.width && r.height)) return null;
      const x = r.left + r.width / 2, y = r.top + r.height / 2;
      const e = document.elementFromPoint(x, y);
      return {x: x, y: y, ok: !!e && e.tagName !== 'IFRAME' && e.closest('button') === b};   // ok: the point is the Submit button itself, not a frame or an overlay
    }"""

    def submit(self, page: Page, on_click=None) -> Outcome:
        """One real click on the site's own Submit button, then watch what the site answers. Never clicks twice.
        Before the click: no challenge on the page, the confirmation text NOT already showing, and the click point checked three times
        (the last two straight before the press) to be the Submit button itself, not an iframe or an overlay."""
        assert_armed()
        s = page.s
        guard_challenge(page, "before Submit")
        if self.confirmed(page):                             # the baseline: the text after the click only counts if it was not there before
            raise Skip("the page already shows the submitted-successfully text before any click; nothing was clicked")
        t = page.ev(self.SUBMIT_TARGET_JS)
        if not (t and t["ok"]):
            raise Stop(EXIT_ERROR, "the Submit application button is missing, disabled or covered; refusing to click")
        s.events.clear()
        s.keep_events = True
        s.call("Network.enable", {"maxTotalBufferSize": 10_000_000, "maxResourceBufferSize": 2_000_000})
        clicked = False
        try:
            if on_click:
                on_click()                                   # records "submitting" and counts the click, BEFORE any press: a crash never repeats it
            t = page.ev(self.SUBMIT_TARGET_JS)               # the last looks: nothing slow (Network.enable, the database) comes after these
            if not (t and t["ok"]):
                return Outcome("no_click", None, "the Submit button was covered or had moved just before the click; nothing was clicked")
            s.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": t["x"], "y": t["y"]})
            t2 = page.ev(self.SUBMIT_TARGET_JS)
            if not (t2 and t2["ok"]) or abs(t2["x"] - t["x"]) > 1 or abs(t2["y"] - t["y"]) > 1:
                return Outcome("no_click", None, "the Submit button moved or was covered while the pointer moved to it; nothing was clicked")
            clicked = True
            s.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": t2["x"], "y": t2["y"], "button": "left", "clickCount": 1})
            s.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": t2["x"], "y": t2["y"], "button": "left", "clickCount": 1})
            return self.watch(page)
        except Exception as e:
            if not clicked:
                raise
            return Outcome("unconfirmed", None, f"{type(e).__name__} after the Submit click: check this application by hand")
        finally:
            s.keep_events = False
            try:
                s.call("Network.disable")
            except Exception:
                pass

    def watch(self, page: Page) -> Outcome:
        """After the click: follow the application request and the page for up to 90 s. A failing browser connection is 'not yet', never a verdict."""
        s = page.s
        req_id = status = None
        lost = False
        end = time.time() + 90
        while time.time() < end:
            try:
                s.pump(1.0)
                for ev in list(s.events):
                    p = ev.get("params") or {}
                    if ev.get("method") == "Network.requestWillBeSent" and (p.get("request") or {}).get("method") == "POST" \
                            and self.API.search((p.get("request") or {}).get("url", "")):
                        req_id = p.get("requestId")
                    elif ev.get("method") == "Network.responseReceived" and req_id and p.get("requestId") == req_id:
                        status = (p.get("response") or {}).get("status")
                s.events.clear()
                if status is not None and status >= 400:     # 409 is the Turnstile rejection; every other refusal is treated the same: stop, never retry
                    return Outcome("challenge", status, f"HTTP {status} from the application request")
                if status is not None and 200 <= status < 300 and self.confirmed(page):
                    return Outcome("confirmed", status, "the application request answered 2xx and the site's own confirmation text appeared after the click")
                marks = page.challenge_marks()
                if marks:
                    return Outcome("challenge", status, ", ".join(marks))
            except Exception:                                # a navigation, a closed websocket: try again until the 90 s are up
                lost = True
                time.sleep(1.0)
        try:
            shown_text = self.confirmed(page)
        except Exception:
            shown_text = False
        if status is None:
            if lost:
                return Outcome("unconfirmed", None, "the connection to the browser was lost after the click and no outcome was seen; check this application by hand")
            if shown_text:
                return Outcome("unconfirmed", None, "the confirmation text is on the page but no application request was seen; check this application by hand")
            try:
                shown = page.ev("() => Array.from(document.querySelectorAll('[aria-invalid=\"true\"], [role=alert]'))"
                                ".filter(e => e.offsetWidth || e.offsetHeight).length")
            except Exception:
                shown = 0
            if shown:
                return Outcome("no_request", None, f"the form's own validation stopped the submit ({shown} message(s) shown); nothing was sent")
            # nothing left the page and the form shows no complaint: a bot check that never surfaced. Stop, do not retry.
            return Outcome("challenge", None, "no application request left the page within 90 s and no validation message is shown "
                                              "(treated as a silent bot check)")
        if 200 <= status < 300:
            return Outcome("unconfirmed", status, f"HTTP {status} but the site's confirmation text did not appear within 90 s; check this application by hand")
        return Outcome("unconfirmed", status, f"HTTP {status} from the application request and no confirmation; check this application by hand")

    def confirmed(self, page: Page) -> bool:
        return bool(self.SUCCESS.search(page.text(4000)))


ADAPTERS: list[Adapter] = [Workable()]


# ------------------------------------------------------------------ browser
def automation_chromes(profile_dir: str) -> list[int]:
    """Process ids of the AUTOMATION Chrome on exactly this profile directory: a browser process (no --type=) that carries
    --remote-debugging-port and whose whole --user-data-dir value is this path (so ats-mail never matches ats-mail2). A Chrome the user
    opened by hand, such as the visible sign-in window of `ats_apply.cmd setup`, has no debugging port and is never matched."""
    ps = "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, timeout=60,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.decode("utf-8", "replace").strip()
        rows = json.loads(out) if out else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    if isinstance(rows, dict):
        rows = [rows]
    want = re.compile(r'--user-data-dir=("?)' + re.escape(str(Path(profile_dir))) + r'\1(?=\s|$)', re.I)
    return [int(r["ProcessId"]) for r in rows
            if r.get("CommandLine") and want.search(r["CommandLine"]) and "--remote-debugging-port" in r["CommandLine"]
            and "--type=" not in r["CommandLine"]]


def kill_automation_chrome(profile_dir: str) -> list[int]:
    pids = automation_chromes(profile_dir)
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if pids:
        time.sleep(2)
    return pids


def open_browser(headless: bool, profile_dir: str | None = None):
    """A real (headful) Chrome by default, its window parked off-screen so nothing pops up. Headless is only on request (--headless):
    ChromeSession then removes "Headless" from the user agent and sends client-hint brands made to match the user's normal Chrome, which is
    fingerprint adjustment this tool does not rely on (the approved plan: no fingerprint spoofing). A challenge is exit 48, never a workaround."""
    check_ram()
    try:
        from naukrigulf_apply import ChromeSession
    except Exception as e:
        raise Stop(EXIT_ERROR, f"cannot import ChromeSession from naukrigulf_apply.py ({type(e).__name__}: {str(e)[:100]}); "
                               "it is another session's file and must be present")
    profile = profile_dir or CONFIG["apply_profile_dir"]

    def start():
        s = ChromeSession(profile, headless=headless, exe=CONFIG["chrome_exe"])
        if not headless:
            s.extra_args = s.extra_args + ("--window-position=-32000,-32000",)    # where Windows parks a minimised window; the page stays "visible"
        try:
            s.start()
            if not headless:
                # EdgeSession.start() always sends Network.setUserAgentOverride with a bare user-agent string, even to a headful Chrome. That
                # leaves navigator.userAgentData empty and the sec-ch-ua headers off, which no real Chrome does. An empty string removes the
                # override again, before any page is loaded (the identity is logged on the first page: see log_identity).
                s.call("Network.setUserAgentOverride", {"userAgent": ""})
            s.is_headless = headless
            return s
        except BaseException:
            s.close()                           # never leave a half-started Chrome behind
            raise
    try:
        return start()
    except CDPError as e:
        killed = kill_automation_chrome(profile)
        log(f"Chrome start failed ({str(e)[:100]}); closed {len(killed)} stale automation Chrome(s) on exactly this profile, retrying once", "WARN")
        try:
            return start()
        except CDPError:
            raise Stop(EXIT_NEEDS_USER, "Chrome would not start on this profile: it is probably open in a window that is not an automation "
                                        "browser (for example the sign-in window of `ats_apply.cmd setup`); close that window and run again")


APPLY_BLOCK_URLS = ["*/api/v1/jobs/*/apply*"]
APPLY_PROBE_JS = ("(async () => { try { await fetch('https://apply.workable.com/api/v1/jobs/BLOCKPROBE/apply', "
                  "{method: 'GET', mode: 'no-cors', cache: 'no-store'}); return 'sent'; } catch (e) { return 'blocked'; } })()")


def was_refused(ev: dict) -> bool:
    """A DevTools event for a request the browser itself refused (Network.setBlockedURLs): Chrome 154 reports blockedReason 'inspector'."""
    p = ev.get("params") or {}
    return ev.get("method") == "Network.loadingFailed" and (p.get("blockedReason") == "inspector" or "BLOCKED_BY_CLIENT" in str(p.get("errorText", "")))


def block_apply_requests(s) -> None:
    """Dry runs only: the browser itself refuses every request to the application endpoint, so even a stray click on Submit cannot send an
    application. Proved before any form is touched, with a GET to a made-up job code (a harmless request that must never leave the machine);
    if the proof fails, the dry run stops."""
    s.call("Network.enable", {"maxTotalBufferSize": 10_000_000, "maxResourceBufferSize": 2_000_000})
    s.call("Network.setBlockedURLs", {"urls": APPLY_BLOCK_URLS})
    s.events.clear()
    s.keep_events = True                       # kept for the whole dry run: network_summary() reports what the form fill sent
    verdict = s.eval(APPLY_PROBE_JS, await_promise=True)
    s.pump(1.0)
    refused = any(was_refused(ev) for ev in s.events)
    s.events.clear()
    if verdict != "blocked" or not refused:
        raise Stop(EXIT_ERROR, "the block on the application request could not be proved; a dry run touches no form without it")
    log("dry run: the browser refuses the application request (proved with a probe that never left the machine)")


def log_identity(page: Page) -> None:
    """Labels only: what this browser tells a site about itself."""
    d = page.ev("() => ({headless: /headless/i.test(navigator.userAgent), brands: navigator.userAgentData ? navigator.userAgentData.brands.length : 0})")
    mode = ("headless: ChromeSession adjusts the user agent and client hints" if getattr(page.s, "is_headless", False)
            else "headful, window off-screen, user-agent override cleared")
    log(f"browser identity ({mode}): user agent free of 'Headless': {'yes' if not d['headless'] else 'NO'}; "
        f"navigator.userAgentData brands: {d['brands']}")


def network_summary(s) -> dict:
    """What the page sent since the last summary, without any content: how many requests, which ones carry a body (POST/PUT/PATCH or any
    request with post data) by method, host+path and content type, and how many application requests the dry-run block refused."""
    s.pump(1.5)
    reqs: dict = {}
    refused = 0
    for ev in s.events:
        p = ev.get("params") or {}
        if ev.get("method") == "Network.requestWillBeSent":
            rq = p.get("request") or {}
            u = urlparse(rq.get("url", ""))
            ctype = next((v for k, v in (rq.get("headers") or {}).items() if k.lower() == "content-type"), "")
            reqs[p.get("requestId")] = {"method": rq.get("method", ""), "where": u.netloc + u.path[:80], "body": bool(rq.get("hasPostData")),
                                        "type": str(ctype).split(";")[0]}
        elif was_refused(ev):
            refused += 1
    s.events.clear()
    bodies = [f"{r['method']} {r['where']} {r['type']}".strip() for r in reqs.values() if r["method"] not in ("GET", "HEAD", "OPTIONS") or r["body"]]
    return {"requests": len(reqs), "with_body": bodies, "multipart": sum(1 for r in reqs.values() if r["type"].startswith("multipart/")),
            "refused_apply_requests": refused}


# ------------------------------------------------------------------ the run
@dataclass
class Result:
    decision: str = ""
    reason: str = ""
    status: int | None = None
    site_flag: bool = False
    fill: FillResult | None = None
    network: dict | None = None


def attempt(page: Page, ad: Adapter, job: Job, ap: Applicant, dry: bool, fill_blocked: bool, on_click=None, con=None, others=None) -> Result:
    ap.ctx = {"region": "UAE", "title": job.title, "company": job.company, "code": job.code}
    try:
        ad.open(page, job)
        ap.ctx.update(title=job.title, company=job.company)
        if not getattr(page.s, "identity_logged", False):
            log_identity(page)
            page.s.identity_logged = True
        dup = duplicate_of(job, con, others or []) if con is not None else ""      # a --url job learns its title and company only now
        if dup:
            return Result("skipped", f"duplicate: {dup}")
        fr = ad.fill(page, ap, fill_blocked=fill_blocked)
    except Skip as e:
        return Result("skipped", str(e))
    res = Result(fill=fr)
    if fr.blocked:
        res.decision = "skipped"
        res.reason = f"unanswerable [{ap.facts.rev}]: " + " | ".join(b.label for b in fr.blocked)[:700]
        return res
    if fr.problems:
        res.decision, res.reason = "failed", "form not filled as intended: " + "; ".join(fr.problems)[:700]
        return res
    guard_challenge(page, "after filling the form")
    if dry:
        res.decision, res.reason = "dry-run", "filled and stopped before Submit"
        return res
    clicked: list = []

    def mark_click():
        if on_click:
            on_click()
        clicked.append(True)
    try:
        out = ad.submit(page, on_click=mark_click)
    except Skip as e:                                        # refused before any click
        res.decision, res.reason = "failed", str(e)
        return res
    except Stop:
        raise
    except Exception as e:
        if not clicked:
            raise
        out = Outcome("unconfirmed", None, f"{type(e).__name__} after the Submit click: check this application by hand")
    res.status = out.status
    if out.kind == "confirmed":
        res.decision, res.reason, res.site_flag = "applied", out.note, True
    elif out.kind == "challenge":
        raise Challenge(out.note, out.status)
    elif out.kind in ("no_request", "no_click"):
        res.decision, res.reason = "failed", out.note
    else:
        res.decision, res.reason = "unconfirmed", out.note
    return res


def print_fill(job: Job, fr: FillResult | None, network: dict | None = None) -> None:
    """Field LABELS only, never a value."""
    if fr:
        print(f"  form: {fr.sections} sections, {len(fr.items)} fields, {sum(1 for i in fr.items if i.required)} required")
        for it in fr.items:
            print(f"    {it.status or ('open' if it.answer else 'n/a'):8} {'*' if it.required else ' '} {it.label[:100]}"
                  + (f"   <- {it.why[:110]}" if it.why and it.status in ("blocked", "blank", "problem") else ""))
        if fr.touched:
            print(f"  Submit button: {'present' if fr.submit_present else 'NOT found'} (not clicked)")
        if fr.screenshot:
            print(f"  screenshot: {fr.screenshot}")
    if network:
        print(f"  network during this job: {network['requests']} requests; "
              f"{'with a body: ' + '; '.join(network['with_body']) if network['with_body'] else 'none carried a body (no POST/PUT/PATCH)'}; "
              f"multipart uploads: {network['multipart']}; application requests refused by the dry-run block: {network['refused_apply_requests']}")


def write_dry_report(rows: list[dict]) -> Path:
    path = STATE_DIR / f"ats_apply_dryrun_{datetime.now():%Y%m%d}.json"
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        old = []
    path.write_text(json.dumps(old + rows, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def toast_check(job: Job) -> None:
    toast("ATS apply: check this application", f"{job.company or job.account}: the run stopped after Submit was clicked and before the outcome "
                                              f"was recorded. Check by hand: {job.url[:80]}")


def run(args) -> int:
    dry = not _live_armed
    mode = "dry-run" if dry else "live"
    con = open_db()
    started = datetime.now()
    run_id = con.execute("INSERT INTO runs (started, mode) VALUES (?, ?)", (started.isoformat(timespec="seconds"), mode)).lastrowid
    con.commit()
    code, note = EXIT_OK, ""
    fetched = candidates = applied = clicks = queued = 0
    browser = None
    report: list[dict] = []
    unresolved: Job | None = None                  # the job whose Submit was clicked and whose outcome is not recorded yet
    try:
        check_markers()
        cap = int(CONFIG["daily_cap"])
        done = max(applied_today(con), clicks_today(con))          # every Submit click counts against both caps, whatever came of it
        budget = min(cap - done, int(CONFIG["per_run_cap"]))
        if args.limit is not None:
            budget = min(budget, args.limit)
        if not dry and budget <= 0:
            raise Stop(EXIT_CAP, f"daily cap reached ({done}/{cap} Submit clicks today)")
        known = db_status(con)
        others = other_applications()
        jobs = [job_from_url(args.url, args.identity)] if args.url else digest_jobs()
        fetched = len(jobs)
        todo: list[Job] = []
        seen: set = set()
        for j in jobs:
            dec = known.get(j.key, ("", "", 0))
            dup = "" if j.skip else duplicate_of(j, con, others)
            if j.key in seen:
                log(f"{j.company} / {j.title}: listed twice; once is enough")
            elif j.skip:
                log(f"{j.company or j.key[:40]} / {j.title or '-'}: skipped - {j.skip}")
                if not dry and j.key not in known:
                    remember(con, known, j, "skipped", j.skip)
            elif dec[0] in FINAL_DECISIONS and not (dry and args.url):
                log(f"{j.company} / {j.title}: already handled ({dec[0]})")
            elif dec[0] == "failed" and dec[2] >= 2 and not (dry and args.url):
                log(f"{j.company} / {j.title}: failed twice; not tried again")
            elif dup:
                log(f"{j.company} / {j.title}: skipped - duplicate, {dup}")
                if not dry:
                    remember(con, known, j, "skipped", f"duplicate: {dup}")
            else:
                todo.append(j)
            seen.add(j.key)
        candidates = len(todo)
        log(f"starting {mode.upper()}: {fetched} job(s) considered, {candidates} on a supported site "
            f"({done}/{cap} Submit clicks today, budget {max(budget, 0)})")
        if args.limit is not None:
            todo = todo[:args.limit]
        if todo:
            facts, facts_loose = load_facts()
            applicants: dict[str, Applicant] = {}
        for n, job in enumerate(todo):
            if not dry and clicks >= budget:
                log("per-run budget used (every Submit click counts); stopping")
                break
            ident = (CONFIG["identities"].get(job.identity) or {})
            if not ident:
                log(f"{job.company} / {job.title}: unknown identity {job.identity!r}; skipped", "WARN")
                continue
            if not dry and not ident.get("mailbox_verifiable"):
                reason = (f"{ident.get('label', job.identity)} identity: its confirmation e-mail goes to a mailbox this tool cannot read, "
                          "so the application could never be verified; not applying")
                log(f"{job.company} / {job.title}: skipped - {reason}")
                remember(con, known, job, "skipped", reason)
                continue
            prev = known.get(job.key)
            if not dry and prev and prev[0] == "skipped" and prev[1].startswith(f"unanswerable [{facts.rev}]"):
                log(f"{job.company} / {job.title}: skipped before under the same facts; not opened again")
                continue
            if job.identity not in applicants:
                ap = applicants[job.identity] = load_applicant(job.identity)
                ap.facts, ap.facts_loose = facts, facts_loose
            ap = applicants[job.identity]
            if browser is None:
                browser = open_browser(args.headless)
                log("browser started (one at a time; always closed at the end)")
                if dry:
                    block_apply_requests(browser)
            page = Page(browser)
            if n and not dry:
                time.sleep(int(CONFIG["gap_seconds"]))
            ad = next(a for a in ADAPTERS if a.key == job.ats)
            log(f"{'DRY RUN' if dry else 'LIVE'}: {job.company or job.account} / {job.title or job.code} [{job.key}] as {ident.get('label')}")
            prior = known.get(job.key, ("", "", 0))[0]

            def on_click(job=job):                  # called right before the click sequence: one commit for the click count and the 'submitting' row
                nonlocal clicks, unresolved
                con.execute("INSERT INTO submits (job_id, ts) VALUES (?, ?)", (job.key, datetime.now().isoformat(timespec="seconds")))
                remember(con, known, job, "submitting", "Submit is about to be clicked; the outcome is not known yet")
                clicks += 1
                unresolved = job
            try:
                res = attempt(page, ad, job, ap, dry, fill_blocked=bool(args.url) and dry,       # only a dry run rehearses a blocked form
                              on_click=on_click, con=con, others=others)
            except Challenge as c:
                shot = None
                try:
                    shot = page.shot(f"challenge_{job.code}")
                except Exception:
                    pass
                remember(con, known, job, "challenge", f"{c.reason} (never retried)", c.status)     # in a dry run too: never probed again
                until = datetime.now() + timedelta(hours=int(CONFIG["challenge_backoff_hours"]))
                set_marker("CHALLENGE", until, c.reason, job.key)
                toast("ATS apply: bot check, stopped", f"{job.company or job.account}: {c.reason[:90]}. Nothing was retried. Apply by hand: {job.url[:80]}")
                report.append({"job": job.key, "title": job.title, "company": job.company, "decision": "challenge", "reason": c.reason,
                               "screenshot": str(shot) if shot else None})
                unresolved = None
                raise
            if dry:
                res.network = network_summary(browser)
            fr = res.fill
            print_fill(job, fr, res.network)
            if fr and fr.blocked:
                ledger_unanswered(job, [{"question": b.label, "kind": b.wtype, "reason": b.why, "intent": b.intent,
                                         "facts_rev": ap.facts.rev, "required": True} for b in fr.blocked])
            report.append({"job": job.key, "title": job.title, "company": job.company, "identity": job.identity,
                           "decision": res.decision, "reason": res.reason, "touched": bool(fr and fr.touched),
                           "fields": [{"label": i.label, "required": i.required, "status": i.status, "why": i.why[:120]}
                                      for i in (fr.items if fr else [])],
                           "network": res.network, "screenshot": str(fr.screenshot) if fr and fr.screenshot else None})
            log(f"{job.company} / {job.title}: {res.decision} - {res.reason[:200]}",
                "WARN" if res.decision in ("failed", "unconfirmed") else "INFO")
            if not dry:
                if res.decision == "failed" and prior == "failed":
                    res.decision = "failed twice"
                remember(con, known, job, res.decision, res.reason, res.status, res.site_flag)
                unresolved = None
                if res.decision == "applied":
                    applied += 1
                elif res.decision == "unconfirmed":
                    toast("ATS apply: check this application", f"{job.company}: submitted but not confirmed. Stopped. Check by hand: {job.url[:80]}")
                    raise Stop(EXIT_ERROR, f"{job.key}: submitted but not confirmed; stopping so nothing is submitted twice")
        queued = len(todo)
        note = (f"dry run: {candidates} candidate(s); {sum(1 for r in report if r.get('touched'))} form(s) filled up to the Submit button "
                f"(not clicked); {sum(1 for r in report if r['decision'] == 'skipped')} would be skipped"
                if dry else f"applied {applied} of {queued} queued ({clicks} Submit click(s))")
        if not dry and max(applied_today(con), clicks_today(con)) >= cap:
            code, note = EXIT_CAP, f"daily cap reached ({cap}); {note}"
    except Stop as e:
        code, note = e.code, e.reason
        log(e.reason, "INFO" if e.code in (EXIT_CAP,) else "ERROR")
        if unresolved is not None:
            toast_check(unresolved)
    except Exception as e:
        code, note = EXIT_ERROR, f"{type(e).__name__}: {str(e)[:300]}"
        log(f"run crashed: {note}", "ERROR")
        if unresolved is not None:
            toast_check(unresolved)
    finally:
        if browser is not None:
            browser.close()
            log("browser closed")
        ended = datetime.now()
        con.execute("UPDATE runs SET ended=?, exit_code=?, fetched=?, candidates=?, applied=?, note=? WHERE run_id=?",
                    (ended.isoformat(timespec="seconds"), code, fetched, candidates, applied, note[:500], run_id))
        con.commit()
        write_progress(con, {"mode": mode, "started": started.isoformat(timespec="seconds"), "ended": ended.isoformat(timespec="seconds"),
                             "exit": code, "fetched": fetched, "candidates": candidates, "queued": queued, "applied": applied, "note": note[:300]})
        con.close()
        if report:
            log(f"dry-run report (labels only): {write_dry_report(report)}" if dry else "run report saved in the database")
    log(f"run finished: exit {code} ({note}) in {(datetime.now() - started).total_seconds():.0f} s")
    return code


# ------------------------------------------------------------------ verify: the confirmation e-mail in the Dubai job-search Gmail
GMAIL_ROWS_JS = r"""(() => {
  const rows = Array.from(document.querySelectorAll('div[role=main] tr.zA')).slice(0, 40);
  return rows.map(r => {
    const em = r.querySelector('span[email]'), subj = r.querySelector('span.bog'), snip = r.querySelector('span.y2'), d = r.querySelector('td.xW span[title]');
    return {from: em ? em.getAttribute('email') : '', name: em ? em.textContent : '', subject: subj ? subj.textContent : '',
            snippet: snip ? snip.textContent.slice(0, 160) : '', date: d ? d.getAttribute('title') : ''};
  });
})()"""
SEARCH_BOX_JS = "() => { const q = document.querySelector('input[name=\"q\"]'); return q ? q.value : null; }"
MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def parse_mail_date(text: str) -> datetime | None:
    """Gmail's row tooltip: 'Sun, 20 Sept 2026, 05:12' (en-IN, 24 h or with AM/PM) or 'Sun, Sep 20, 2026, 5:12 AM' (en-US)."""
    t = (text or "").replace(" ", " ")
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4}),?\s+(\d{1,2}):(\d{2})\s*([AP]M)?", t, re.I)
    if m:
        d, mon, y, hh, mm, ampm = m.groups()
    else:
        m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4}),?\s+(\d{1,2}):(\d{2})\s*([AP]M)?", t, re.I)
        if not m:
            return None
        mon, d, y, hh, mm, ampm = m.groups()
    if mon[:3].lower() not in MONTHS:
        return None
    if ampm:
        hh = str(int(hh) % 12 + (12 if ampm.upper() == "PM" else 0))
    return datetime(int(y), MONTHS[mon[:3].lower()], int(d), int(hh), int(mm))


def ensure_mail_profile() -> str:
    dst = Path(CONFIG["mail_profile_dir"])
    if (dst / "Local State").exists():
        return str(dst)
    src = Path(CONFIG["mail_profile_source"])
    if not (src / "Local State").exists():
        raise Stop(EXIT_NEEDS_USER, "the signed-in Google profile for the Dubai job-search Gmail is missing; nothing can be verified")
    log("copying the signed-in job-search Gmail profile once (caches left out)")
    skip = shutil.ignore_patterns("Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache", "DawnCache", "DawnGraphiteCache",
                                  "DawnWebGPUCache", "Crashpad", "CacheStorage", "ScriptCache", "Singleton*", "lockfile",
                                  "DevToolsActivePort", "BrowserMetrics*", "*.log", "optimization_guide*", "component_crx_cache")
    try:
        shutil.copytree(src, dst, ignore=skip, dirs_exist_ok=True, ignore_dangling_symlinks=True)
    except (OSError, shutil.Error) as e:
        raise Stop(EXIT_ERROR, f"could not copy the Gmail profile ({type(e).__name__}); is Chrome open on it?")
    return str(dst)


def mail_rows(page: Page, query: str) -> list[dict]:
    """The rows of one Gmail search. Every search is a full page load: the #search address is a same-document change after the first
    one, and a wait that only looked for rows could pass on the PREVIOUS search's rows. When the search box can be read it must show
    this search too."""
    page.go("about:blank")
    page.go("https://mail.google.com/mail/u/0/#search/" + quote(query), timeout=90)
    want = set(re.findall(r"[a-z0-9_:.]+", query.lower()))
    end = time.time() + 45
    while time.time() < end:                    # Gmail shows rows, "no messages matched", or hands over to Google's sign-in
        if "accounts.google.com" in page.href():
            set_marker("LOGIN_REQUIRED", datetime.now() + timedelta(days=3650), "the job-search Gmail session in the mail profile is signed out")
            raise Stop(EXIT_NEEDS_USER, "the job-search Gmail is signed out in the mail profile; sign in once by hand through Google in "
                                        "that profile (nothing here ever types a password); verification waits until then")
        try:
            if page.s.eval("!!document.querySelector('div[role=main] tr.zA') || /no messages matched/i.test(document.body.innerText)"):
                box = page.ev(SEARCH_BOX_JS)
                if box is None or want <= set(re.findall(r"[a-z0-9_:.]+", str(box).lower())):
                    break
        except CDPError:
            pass
        time.sleep(1.0)
    else:
        log("Gmail did not show this search within 45 s; treated as no result", "WARN")
        return []
    marker("LOGIN_REQUIRED").unlink(missing_ok=True)
    time.sleep(1.0)
    return list(page.s.eval(GMAIL_ROWS_JS) or [])


def mail_is_confirmation(row: dict, applied_at: datetime, sender_words: list, tokens: list, max_hours: int) -> bool:
    """A mail from the ATS (sender address) dated between the Submit click (10 minutes of slack for clock skew) and max_hours after it,
    whose subject or snippet names the company and talks about an application ("appl": application, applying, applied)."""
    when = parse_mail_date(row.get("date", ""))
    if not when or not (applied_at - timedelta(minutes=10) <= when <= applied_at + timedelta(hours=max_hours)):
        return False
    if not any(w in row.get("from", "").lower() for w in sender_words):
        return False
    text = f"{row.get('subject', '')} {row.get('snippet', '')}".lower()
    return bool(tokens) and any(t in text for t in tokens) and "appl" in text


def cmd_verify(args) -> int:
    lock = open(LOCK_PATH, "a+")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log("another ATS run is active; exiting")
        return EXIT_OK
    con = open_db()
    # 'applied' rows become verified; 'submitting' and 'unconfirmed' rows (a click whose outcome was never seen) are only REPORTED when their mail exists
    rows_to_check = con.execute("SELECT job_id, title, company, ts, decision FROM applications WHERE (decision='applied' AND verified=0) "
                                "OR decision IN ('submitting', 'unconfirmed')").fetchall()
    if not rows_to_check and not args.show:
        print("nothing to verify: no 'applied' row is waiting for its confirmation e-mail")
        con.close()
        return EXIT_OK
    browser, code, note, found = None, EXIT_OK, "", 0
    words = CONFIG["confirmation_mail"]["sender_domain_words"]
    days = args.days or int(CONFIG["confirmation_mail"]["lookback_days"])
    max_hours = int(CONFIG["confirmation_mail"].get("max_delay_hours", 72))
    try:
        browser = open_browser(args.headless, ensure_mail_profile())
        page = Page(browser)
        if args.show:                           # what the matcher has to work with: sender domain, date and subject, no names
            rows = mail_rows(page, f"in:anywhere newer_than:{days}d (workable OR from:workablemail.com OR from:workable.com)")
            log(f"Gmail (read-only): {len(rows)} recent Workable-looking mail(s)")
            names = contact_names("data")
            for r in rows:
                subj = r["subject"]
                for n in names:
                    subj = re.sub(re.escape(n), "<name>", subj, flags=re.I)
                print(f"  {r['date'][:24]:24} {r['from'].split('@')[-1][:30]:30} {subj[:90]}")
        for job_id, title, company, ts, decision in rows_to_check:
            applied_at = datetime.fromisoformat(ts)
            toks = company_tokens(company) or company_tokens(title)
            # the search asks for the company's words anywhere in the mail; the code then wants a Workable sender, the right dates and the right words
            rows = mail_rows(page, f"in:anywhere newer_than:{days}d " + " ".join(f'"{t}"' for t in toks)) if toks else []
            hit = next((r for r in rows if mail_is_confirmation(r, applied_at, words, toks, max_hours)), None)
            if decision == "applied":
                if hit:
                    con.execute("UPDATE applications SET verified=1, verified_at=? WHERE job_id=?",
                                (datetime.now().isoformat(timespec="seconds"), job_id))
                    found += 1
                    log(f"VERIFIED {company} / {title}: the confirmation e-mail is in the job-search Gmail")
                else:
                    log(f"not yet: no confirmation e-mail for {company} / {title} (applied {ts[:16]})", "WARN")
            elif hit:
                msg = (f"a confirmation mail exists for {company} / {title}, whose row says '{decision}': the application probably went "
                       "through. The row is NOT changed; check the job by hand.")
                print(msg)
                log(msg, "WARN")
            else:
                log(f"no confirmation mail for {company} / {title} (row says '{decision}', clicked {ts[:16]}); check this job by hand", "WARN")
        con.commit()
        note = f"verified {found} of {sum(1 for r in rows_to_check if r[4] == 'applied')}"
    except Stop as e:
        code, note = e.code, e.reason
        log(e.reason, "ERROR")
    except Exception as e:
        code, note = EXIT_ERROR, f"{type(e).__name__}: {str(e)[:200]}"
        log(f"verify crashed: {note}", "ERROR")
    finally:
        if browser is not None:
            browser.close()
        write_progress(con, {"mode": "verify", "ended": datetime.now().isoformat(timespec="seconds"), "exit": code, "note": note})
        con.close()
    return code


# ------------------------------------------------------------------ status
def cmd_status() -> int:
    if not DB_PATH.exists():
        print(f"{ACCOUNT_LABEL}: no runs yet")
        return EXIT_OK
    con = open_db()
    total = con.execute("SELECT COUNT(*) FROM applications WHERE verified=1").fetchone()[0]
    print(f"{ACCOUNT_LABEL}: {applied_today(con)} applied and {clicks_today(con)} Submit click(s) today (daily cap {CONFIG['daily_cap']} counts clicks), "
          f"{total} confirmed in total; live apply armed in this shell: {os.environ.get(LIVE_ENV) == '1'}; "
          f"free RAM {free_ram_mb()} MB (needs {CONFIG['min_free_mb']})")
    for r in con.execute("SELECT started, mode, exit_code, fetched, candidates, applied, note FROM runs ORDER BY run_id DESC LIMIT 5"):
        print(f"  run {r[0]} {r[1]}: exit {r[2]}, fetched {r[3]}, candidates {r[4]}, applied {r[5]} ({(r[6] or '')[:100]})")
    for kind in ("CHALLENGE", "THROTTLED", "LOGIN_REQUIRED"):
        m = marker(kind)
        if m.exists():
            print(f"  marker {m.name}: {m.read_text(encoding='utf-8', errors='replace')[:200]}")
    for dec, n in con.execute("SELECT decision, COUNT(*) FROM applications GROUP BY decision"):
        print(f"  {dec}: {n}")
    con.close()
    return EXIT_OK


def main() -> int:
    global _live_armed
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", nargs="?", default="run", choices=["run", "status", "verify"],
                    help="run (default): dry run unless --live; status; verify (confirmation e-mails, read-only)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="(default) fill the form, stop before Submit")
    mode.add_argument("--live", action="store_true", help=f"apply for real; refused unless {LIVE_ENV}=1 is also set")
    ap.add_argument("--url", help="one given job (a Workable apply or job link)")
    ap.add_argument("--list", action="store_true", help="print the matching jobs from the digest and what would happen; no browser")
    ap.add_argument("--limit", type=int, help="at most N jobs this run (still bounded by both caps)")
    ap.add_argument("--identity", default="data", choices=sorted(CONFIG["identities"]), help="identity for --url (digest jobs carry their own)")
    ap.add_argument("--headless", action="store_true", help="hidden browser (ChromeSession then adjusts the user agent and client hints: not the default)")
    ap.add_argument("--status", action="store_true", help="same as the status command")
    ap.add_argument("--show", action="store_true", help="with verify: list the recent Workable mail it looks at (subjects, no names)")
    ap.add_argument("--days", type=int, help="with verify: how many days of mail to look through (default from the config)")
    args = ap.parse_args()
    if args.list:
        return cmd_list()
    if args.status or args.command == "status":
        return cmd_status()
    if args.live:
        if os.environ.get(LIVE_ENV) != "1":
            log(f"refusing --live: {LIVE_ENV}=1 is not set in this environment; nothing was applied", "ERROR")
            return EXIT_NOT_ARMED
        _live_armed = True
    if args.command == "verify":
        return cmd_verify(args)
    lock = open(LOCK_PATH, "a+")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log("another ATS run is active; exiting")
        return EXIT_OK
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
