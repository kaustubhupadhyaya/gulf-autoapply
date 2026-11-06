"""Never apply twice to the same opening, whichever tool found it (lifted out of ats_apply.py on 2026-10-14, plan section 3.1).

Two jobs are the same role when their titles read the same once normalised (lower case, letters and digits only) and their
company names share a distinctive word of three letters or more. Plurals are folded on both sides first ("companies" is read as
"company", "holdings" as "holding"; words of 5+ letters only). Legal forms (LLC, Pvt Ltd, FZCO: STOP_COMPANY_WORDS), words that only
describe a firm (services, solutions, Dubai, Emirates, India ...: GENERIC_COMPANY_WORDS) and placeholders (Confidential, Client,
Stealth Startup: PLACEHOLDER_COMPANY_WORDS) do not count. A name made only of generic words is compared whole, so the same name
written twice is one role while two firms that share only "Services" are not; a name made only of placeholder and legal words matches
nothing. A repost under a new job id, or the same opening reached through another tool, is therefore one role.

Sources, all read-only and best effort (a store that is missing or unreadable adds nothing):
  - every applier database in job-tracker\\state (*.db with an `applications` table): rows with decision 'applied';
  - the LinkedIn UAE pass's job log (FORKED_Auto_job_applier_linkedIn\\logs\\linkedin_uae_jobs.jsonl): live 'applied' rows;
  - state\\manual_applications.csv: applications the user made by hand. He appends one line each: company,title,date.

    python portal_kit\\dedupe.py check [--grep TEXT]           every source, and every role logged under more than one job id
    python portal_kit\\dedupe.py check --company C --title T   would this job be skipped as a duplicate, and of what

`check` only reads. The one thing it writes is the manual list's header line, when that file does not exist yet.
Stdlib only, Python 3.11. Imported as portal_kit.dedupe (ats_apply.py) or loaded by path.
"""
import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

GITHUB = Path(os.environ.get("JOB_TRACKER_HOME") or Path.home() / "GitHub")
JT = Path(os.environ.get("JOB_DASHBOARD_JT") or GITHUB / "job-tracker")
STATE = JT / "state"
LI_UAE_JOBS = Path(os.environ.get("JOB_DASHBOARD_LI") or GITHUB / "FORKED_Auto_job_applier_linkedIn") / "logs" / "linkedin_uae_jobs.jsonl"
MANUAL_CSV = STATE / "manual_applications.csv"
MANUAL_HEADER = ("company", "title", "date")
SHOW = 25                 # check: roles listed per section (narrow the rest with --grep)

def _fold(word: str) -> str:
    """Plural folding, applied to both names before any test: "ies" becomes "y", and a final "s" is dropped, on words of 5+ letters
    ("companies", "holdings", "services"). Titles are not folded."""
    if len(word) >= 5:
        if word.endswith("ies"):
            return word[:-3] + "y"
        if word.endswith("s"):
            return word[:-1]
    return word


def _words(text: str) -> frozenset:
    return frozenset(_fold(w) for w in text.split())


# Legal forms, connectors and articles: dropped from both names before any test. The old list (llc ltd limited fzco fze fz group the
# and inc corp company co), the forms that occur in the real names (pvt, private, llp, wll, sole, proprietorship, pte, pty, gmbh, lp,
# ab, corporation), the standard suffixes that do not occur yet, and the connectors "for", "of" and the articles "a", "an".
STOP_COMPANY_WORDS = _words("""
    llc ltd limited fzc fzco fze fz group the and inc incorporated corp corporation co company pvt private llp lp wll sole
    proprietorship establishment pte pty gmbh plc ag bv sa sarl srl spa ab oy nv kk ltda sdn bhd jsc pjsc psc sae for of a an
""")
# Words that describe a kind of firm, a place, or an adjective of one: they never make two companies one. Built from the real
# company names (2026-10-14): every applications and jobs table in state\*.db, the LinkedIn UAE log (the fullest name per job id)
# and the manual list. Brand and person words stay distinctive (tata, deutsche, amazon, ...). Judgement calls on the same evidence:
# "india" marks a subsidiary (92 names), so "indian" is its adjective; "insurance" is a kind of firm, like "bank".
GENERIC_COMPANY_WORDS = _words("""
    services service solutions solution technologies technology technical tech infotech information info computer digital
    software systems labs data analytics consulting consultancy consultancies consultants counsel studies management business
    staffing recruitment recruiting hiring hrm hr human resources manpower employment talent agency people opportunities
    contract contracting construction engineering trading real estate properties property development developments investment
    investments capital bank financial finance insurance health healthcare medical dental energy gas marine travel tourism
    marketing media network education infra infrastructure integrated works factory managed branch center overseas global
    international holding holdings
    uae dubai abu dhabi emirates gulf middle east mena arabia arabian india indian
""")
# Placeholders name no employer ("Confidential Company": 100 real rows; "Stealth Startup": 3; "Confidential Careers": 5). A placeholder
# word is never distinctive, and a name made only of placeholder and legal words matches nothing. "careers" is added to the reviewed
# list because "Confidential Careers" occurs in the real names. "us" and "based" come from "US Based MNC" (LinkedIn log, a skipped job):
# read as a distinctive "based", that name would group with any other "based" company that has the same title.
PLACEHOLDER_COMPANY_WORDS = _words("""
    our client leading company confidential reputed mnc top stealth startup a an the careers us based
""")


# ------------------------------------------------------------------ the same role (ats_apply's rule, plus the generic words)
def norm_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _placeholder_only(company: str) -> bool:
    """True when the name is made only of placeholder and legal words, with at least one placeholder word ("Confidential Company",
    "US Based MNC"). Such a name names no one: company_tokens gives it no identity, and the LinkedIn merge sets it aside."""
    words = [_fold(w) for w in norm_words(company)]
    return any(w in PLACEHOLDER_COMPANY_WORDS for w in words) and all(
        w in PLACEHOLDER_COMPANY_WORDS or w in STOP_COMPANY_WORDS for w in words)


def company_tokens(company: str) -> list[str]:
    """The words that identify a company, with plurals folded on both sides. Legal forms, connectors and articles
    (STOP_COMPANY_WORDS) are dropped. The distinctive words are the rest of the words of three letters or more that are not
    GENERIC_COMPANY_WORDS or PLACEHOLDER_COMPANY_WORDS. A name made only of placeholder and legal words names no one: []. A name
    whose words are all generic is compared whole (its folded words, legal forms left out), so two firms that merely share
    "Services" or "Dubai" are not one, while the same name written twice still is. A name with no word of three letters or more: []."""
    words = [_fold(w) for w in norm_words(company)]
    base = [w for w in words if len(w) >= 3 and w not in STOP_COMPANY_WORDS]
    if not base:
        return []
    distinctive = [w for w in base if w not in GENERIC_COMPANY_WORDS and w not in PLACEHOLDER_COMPANY_WORDS]
    if distinctive:
        return distinctive
    if _placeholder_only(company):
        return []
    return [" ".join(w for w in words if w not in STOP_COMPANY_WORDS)]


def same_role(company_a: str, title_a: str, company_b: str, title_b: str) -> bool:
    """The same opening under another job id (a repost) or through another tool: the same normalised title and a company word in common."""
    ta, tb = " ".join(norm_words(title_a)), " ".join(norm_words(title_b))
    return bool(ta) and ta == tb and bool(set(company_tokens(company_a)) & set(company_tokens(company_b)))


# ------------------------------------------------------------------ what was applied to, by every tool and by hand
def applier_databases() -> dict:
    """Every database in job-tracker's state folder, named the way ats_apply_config.json names them: naukrigulf_apply.db is
    'naukrigulf', instahyre_1.db 'instahyre1', ats_apply.db 'ats'. One without an `applications` table adds nothing."""
    return {re.sub(r"_apply$", "", p.stem).replace("_", ""): p for p in sorted(STATE.glob("*.db"))}


def ensure_manual_csv(path: Path = MANUAL_CSV) -> bool:
    """Create the manual list with its header line only, when it does not exist. True when it was created now."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8", newline="") as fh:     # "x": never overwrite a list created a moment ago
            csv.writer(fh).writerow(MANUAL_HEADER)
    except FileExistsError:
        return False
    return True


def applied_records(sources: dict | None = None, base: Path = JT, manual: Path | None = MANUAL_CSV,
                    report: list | None = None) -> list[dict]:
    """Every recorded application: {"source", "job_id", "company", "title", "when"}, in this order: the databases, the LinkedIn
    UAE log, the manual list.
    sources: {"sqlite": {name: path}, "linkedin_uae_jobs": path}, the shape of ats_apply_config.json's already_applied_elsewhere
    (an empty dict means none of them); None means every applier database in job-tracker\\state plus the LinkedIn UAE log.
    Relative database paths are taken from `base`. The LinkedIn UAE log gives one record per applied job id, with the company it goes
    by and the longest title its rows carry (_linkedin_by_job). `report`, when given, gets one line per source (what was read)."""
    if sources is None:
        sources = {"sqlite": applier_databases(), "linkedin_uae_jobs": LI_UAE_JOBS}
    note = report.append if report is not None else (lambda _row: None)
    out: list[dict] = []
    for name, rel in (sources.get("sqlite") or {}).items():
        path = Path(rel) if Path(rel).is_absolute() else Path(base) / rel
        if not path.exists():
            note({"source": name, "path": str(path), "rows": None, "note": "missing"})
            continue
        try:
            c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                cols = {r[1] for r in c.execute("PRAGMA table_info(applications)")}
                jid, ts = ("job_id" if "job_id" in cols else "''"), ("ts" if "ts" in cols else "''")
                rows = [{"source": name, "job_id": str(j or ""), "company": co or "", "title": ti or "", "when": str(t or "")[:10]}
                        for j, co, ti, t in c.execute(f"SELECT {jid}, company, title, {ts} FROM applications WHERE decision='applied'")]
            finally:
                c.close()
        except sqlite3.Error as e:
            note({"source": name, "path": str(path), "rows": None, "note": f"unreadable ({str(e)[:60]})"})
            continue
        out += rows
        note({"source": name, "path": str(path), "rows": len(rows)})
    jl = sources.get("linkedin_uae_jobs")
    if jl and Path(jl).exists():
        try:
            rows = _linkedin_rows(Path(jl))
        except OSError as e:
            note({"source": "linkedin", "path": str(jl), "rows": None, "note": f"unreadable ({type(e).__name__})"})
        else:
            # one record per job id, with the company it goes by and the longest title its rows carry (the merge the grouping uses), so a
            # truncated repeat of a name never becomes a record of its own
            per_job = [{"source": "linkedin", "job_id": r["job_id"], "company": r["company"], "title": r["title"], "when": r["when"]}
                       for r in _linkedin_by_job(rows) if r["applied"]]
            # a live application with no job id cannot be merged with another: it stays a record of its own, as before
            no_id = [{"source": "linkedin", "job_id": "", "company": str(r.get("company") or ""), "title": str(r.get("title") or ""),
                      "when": str(r.get("ts") or "")[:10]}
                     for r in rows if not r.get("job_id") and r.get("outcome") == "applied" and r.get("mode") == "live"]
            out += per_job + no_id
            note({"source": "linkedin", "path": str(jl), "rows": len(per_job) + len(no_id)})
    elif jl:
        note({"source": "linkedin", "path": str(jl), "rows": None, "note": "missing"})
    if manual is not None:
        if Path(manual).exists():
            n = 0
            try:
                with open(manual, encoding="utf-8-sig", errors="replace", newline="") as fh:
                    for i, r in enumerate(csv.DictReader(fh), 2):
                        co, ti = str(r.get("company") or "").strip(), str(r.get("title") or "").strip()
                        if co and ti:
                            out.append({"source": "manual", "job_id": f"line {i}", "company": co, "title": ti,
                                        "when": str(r.get("date") or "").strip()})
                            n += 1
                note({"source": "manual", "path": str(manual), "rows": n})
            except (OSError, csv.Error) as e:
                note({"source": "manual", "path": str(manual), "rows": None, "note": f"unreadable ({type(e).__name__})"})
        else:
            note({"source": "manual", "path": str(manual), "rows": None, "note": "missing"})
    return out


def other_applications(sources: dict | None = None, base: Path = JT, manual: Path | None = MANUAL_CSV) -> list[tuple]:
    """(source, company, title) of every application in applied_records(): what duplicate_of() compares a job with."""
    return [(r["source"], r["company"], r["title"]) for r in applied_records(sources, base, manual)]


def _field(job, name: str) -> str:
    v = job.get(name) if isinstance(job, dict) else getattr(job, name, "")
    return str(v or "")


def duplicate_of(job, con: sqlite3.Connection | None, others: list) -> str:
    """'' or where the same opening was already applied to or handled: the caller's own rows under another job id (con: its
    `applications` table, same columns as ats_apply.db; None to skip), or another tool or the manual list (`others`, from
    other_applications()). `job` has company, title and its own id as `key` (ats_apply) or `job_id`: an object or a dict."""
    company, title = _field(job, "company"), _field(job, "title")
    if not (company and title):
        return ""
    if con is not None:
        own = _field(job, "key") or _field(job, "job_id")
        for jid, co, ti, dec in con.execute("SELECT job_id, company, title, decision FROM applications WHERE job_id != ? AND "
                                            "decision IN ('applied', 'submitting', 'unconfirmed', 'challenge')", (own,)):
            if same_role(company, title, co or "", ti or ""):
                return f"already {dec} here under another job id"
    for src, co, ti in others:
        if same_role(company, title, co, ti):
            return f"already applied through {src}"
    return ""


# ------------------------------------------------------------------ check (read-only)
def roles(records: list[dict]) -> list[list[dict]]:
    """The records grouped into roles: one group per opening (same_role, taken as transitive within one normalised title)."""
    by_title: dict[str, list[dict]] = {}
    for r in records:
        t = " ".join(norm_words(r["title"]))
        if t:
            by_title.setdefault(t, []).append(r)
    groups: list[list[dict]] = []
    for recs in by_title.values():
        parent = list(range(len(recs)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        toks = [set(company_tokens(r["company"])) for r in recs]
        for i in range(len(recs)):
            for j in range(i + 1, len(recs)):
                if toks[i] & toks[j]:
                    parent[find(i)] = find(j)
        merged: dict[int, list[dict]] = {}
        for i, r in enumerate(recs):
            merged.setdefault(find(i), []).append(r)
        groups += merged.values()
    return groups


def _linkedin_rows(path: Path) -> list[dict]:
    """Every row of the LinkedIn UAE log that parses as a JSON object. Raises OSError when the file cannot be read."""
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


def _longest(values: list[str]) -> str:
    """The longest value; a tie goes to the later one, the rule the log's rows always followed."""
    best = ""
    for v in values:
        if len(v) >= len(best):
            best = v
    return best


def _fullest_company(names: list[str]) -> str:
    """The company a job id goes by, from its rows' company names (in row order). A name that is a strict prefix of another name of
    the same job is a truncated repeat and does not count. A placeholder-only name ("Confidential Company") is set aside whenever a
    real name remains; among what is left the longest wins, a tie to the later row. With only placeholders left, the longest
    placeholder stands, as before. A name with no word in it counts only when nothing else does."""
    kept = [n for n in names if not any(m != n and len(m) > len(n) and m.startswith(n) for m in names)]
    worded = [n for n in kept if norm_words(n)]
    real = [n for n in worded if not _placeholder_only(n)]
    return _longest(real or worded or kept)


def _linkedin_by_job(rows: list[dict]) -> list[dict]:
    """One record per job id, whatever its outcome. The company is _fullest_company's choice over the rows' names; the title is the
    longest one its rows carry, a tie to the later row (titles show no placeholder flaw on the real log, so they are left alone).
    'applied' marks a live application of the job, dated by its last one. Early rows lost their last letter, so a truncated repeat
    never stands for the job."""
    order: list[str] = []
    names: dict[str, list[str]] = {}
    titles: dict[str, list[str]] = {}
    applied: dict[str, str] = {}
    for r in rows:
        if not r.get("job_id"):
            continue
        jid = str(r["job_id"])
        if jid not in names:
            order.append(jid)
            names[jid], titles[jid] = [], []
        names[jid].append(str(r.get("company") or ""))
        titles[jid].append(str(r.get("title") or ""))
        if r.get("outcome") == "applied" and r.get("mode") == "live":
            applied[jid] = str(r.get("ts") or "")[:10]
    return [{"source": "linkedin", "job_id": jid, "company": _fullest_company(names[jid]), "title": _longest(titles[jid]),
             "when": applied.get(jid, ""), "applied": jid in applied} for jid in order]


def _linkedin_logged(path: Path) -> list[dict]:
    """One record per job id in the LinkedIn UAE log, whatever its outcome; 'applied' marks a live application of it. An unreadable
    log gives []."""
    try:
        return _linkedin_by_job(_linkedin_rows(path))
    except OSError:
        return []


def cmd_check(args) -> int:
    created = ensure_manual_csv()
    if args.company or args.title:
        if not (args.company and args.title):
            print("check: give both --company and --title")
            return 2
        dup = duplicate_of({"company": args.company, "title": args.title}, None, other_applications())
        print(f"duplicate: {dup}" if dup else "not a duplicate: no recorded application of this role")
        return 0
    report: list[dict] = []
    recs = applied_records(report=report)
    print("Sources (read-only):")
    for s in report:
        n = "-" if s["rows"] is None else s["rows"]
        extra = f" ({s['note']})" if s.get("note") else ""
        if s["source"] == "manual" and created:
            extra += " (created now with its header line)"
        print(f"  {s['source']:<11} {n:>5} applied  {s['path']}{extra}")
    grep = (args.grep or "").lower()

    def listing(groups: list[list[dict]], line) -> None:
        picked = [g for g in groups if not grep or any(grep in f"{r['company']} {r['title']}".lower() for r in g)]
        for g in picked[:SHOW]:
            print(line(g))
        if len(picked) > SHOW:
            print(f"  ... and {len(picked) - SHOW} more (narrow the list with --grep TEXT)")

    def ids(items: list[str]) -> str:
        return ", ".join(items[:6]) + (f" and {len(items) - 6} more" if len(items) > 6 else "")
    groups = roles(recs)
    twice = sorted((g for g in groups if len(g) > 1), key=lambda g: -len(g))
    print(f"Applications: {len(recs)} records are {len(groups)} distinct roles; roles applied to more than once: {len(twice)}"
          + (f" (showing those matching {args.grep!r})" if grep else ""))
    listing(twice, lambda g: f"  {g[0]['company'][:40]} / {g[0]['title'][:50]}: {len(g)} records: "
                             + ids([f"{r['source']} {r['job_id']} {r['when']}".strip() for r in g]))
    logged = _linkedin_logged(LI_UAE_JOBS)
    lgroups = roles(logged)
    multi = sorted((g for g in lgroups if len(g) > 1), key=lambda g: (not any(r["applied"] for r in g), -len(g)))
    print(f"LinkedIn UAE log: {len(logged)} job ids are {len(lgroups)} roles; roles logged under more than one job id: {len(multi)}"
          + (f" (showing those matching {args.grep!r})" if grep else ""))
    listing(multi, lambda g: f"  {g[0]['company'][:40]} / {g[0]['title'][:50]}: {len(g)} job ids, "
                             + ("one role: the other job ids are skipped as duplicates: " if any(r["applied"] for r in g)
                                else "one role, not applied to: ")
                             + ids([r["job_id"] + (f" (applied {r['when']})" if r["applied"] else "") for r in g]))
    return 0


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="read-only report (see the module docstring)")
    c.add_argument("--grep", default="", help="show only the roles whose company or title contains this text")
    c.add_argument("--company", default="", help="with --title: is this one job a duplicate, and of what")
    c.add_argument("--title", default="")
    args = ap.parse_args(argv)
    return cmd_check(args)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(_main())

