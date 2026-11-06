"""Closed-world answers for application forms (the Gulf portals and the LinkedIn UAE pass).

A question is answered only when exactly one intent maps it to known facts AND the form offers an option that says
exactly that. Anything else raises Unanswerable: the job is skipped, the question is listed verbatim for the user,
and nothing is guessed. Compound questions ("based in Dubai or willing to relocate?") are skipped for the same reason.

Facts live outside every repo (~/.secrets/gulf_facts.json or $GULF_FACTS). Status per fact:
  known / decided   usable everywhere        proposed   usable in inventory runs only, flagged, never in a live run
  unknown           never usable (the question is skipped and listed)
Never answered here: ID numbers, dates of birth. Contact fields are returned as keep=True (the portal's own
pre-filled profile is used; a caller may fall back to its own profile config).

    python portal_kit/facts.py list                       the facts (sensitive values hidden)
    python portal_kit/facts.py ask "label" [kind] [opt|opt]   what the resolver would do with a question
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

FACTS_PATH = Path(os.environ.get("GULF_FACTS") or Path.home() / ".secrets" / "gulf_facts.json")
ALL_KINDS = ("text", "select", "radio", "textarea", "checkbox")


class Unanswerable(Exception):
    """No known fact answers this question exactly. The caller skips the job and lists the question."""

    def __init__(self, reason: str, *, intent: str = "", facts: tuple = ()):
        super().__init__(reason)
        self.reason, self.intent, self.facts = reason, intent, tuple(facts)


@dataclass
class Answer:
    value: str = ""             # text to type, or the exact option text to choose
    intent: str = ""
    facts: tuple = ()
    proposed: bool = False      # used a fact that is only proposed (inventory runs only)
    keep: bool = False          # contact field: leave what the portal pre-filled
    check: bool | None = None   # checkbox: True = tick it
    typeahead: bool = False     # text box with suggestions: pick the first one


@dataclass
class Q:
    n: str                      # normalised label
    raw: str
    kind: str                   # text | select | radio | textarea | checkbox
    options: list = field(default_factory=list)
    numeric: bool = False       # the form wants digits only
    required: bool = True
    ctx: dict = field(default_factory=dict)   # {"region": "UAE", "title": ..., "company": ...}


def norm(text: str) -> str:
    t = (text or "").replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    t = t.replace("\xa0", " ").replace("\ufffd", "'")      # the history CSV lost the apostrophe in "what's"
    t = re.sub(r"\s+", " ", t.lower()).strip()
    t = re.sub(r"\s*\((required|optional)\)\s*$", "", t)
    return t.strip(" ?:*")


def _fmt(x) -> str:
    if isinstance(x, float):
        return str(int(x)) if x == int(x) else f"{x:.2f}".rstrip("0").rstrip(".")
    return str(x)


# ------------------------------------------------------------------ facts
class Facts:
    def __init__(self, path: str | Path | None = None, live: bool = False, today: date | None = None):
        self.path = Path(path or FACTS_PATH)
        self.live = live
        self.today = today or date.today()
        data = self.path.read_bytes()
        # the revision covers the facts AND this resolver, so a rule fix invalidates what an older resolver decided
        self.rev = hashlib.sha1(data + Path(__file__).read_bytes()).hexdigest()[:10]
        raw = json.loads(data.decode("utf-8-sig"))
        self.meta, self.raw = raw.get("_meta", {}), raw["facts"]
        self._used_proposed: set = set()

    # -- raw access
    def status(self, key: str) -> str:
        return (self.raw.get(key) or {}).get("status", "missing")

    def get(self, key: str):
        """The usable value of a fact, or Unanswerable (unknown, expired, only proposed in a live run, or missing)."""
        f = self.raw.get(key)
        if f is None:
            raise Unanswerable(f"fact {key} is not in the facts file", facts=(key,))
        st = f.get("status", "known")
        if st == "proposed":
            if self.live:
                raise Unanswerable(f"fact {key} is only proposed (not confirmed by the user)", facts=(key,))
        elif st not in ("known", "decided"):
            raise Unanswerable(f"fact {key} is {st}", facts=(key,))
        if f.get("expires") and date.fromisoformat(f["expires"]) < self.today:
            raise Unanswerable(f"fact {key} expired on {f['expires']}", facts=(key,))
        val = self._value(key, f)
        if val is None:
            raise Unanswerable(f"fact {key} has no value", facts=(key,))
        if st == "proposed":
            self._used_proposed.add(key)
        return val

    def _value(self, key: str, f: dict):
        d = f.get("derive")
        if not d:
            return f.get("value")
        src = self.get(f["from"])
        fxm = self.meta.get("fx") or {}
        fx = float(fxm.get("inr_per_aed") or 0)
        if d in ("inr_to_aed", "aed_to_inr_year"):
            try:
                age = (self.today - date.fromisoformat(str(fxm.get("date"))[:10])).days
            except ValueError:
                age = 999
            if age > 14:                    # an old exchange rate would put a wrong number on a form
                raise Unanswerable(f"the INR/AED rate is {age} days old; refresh _meta.fx in the facts file", facts=(key,))
        if d in ("months_since", "years_floor", "years_1dp"):
            y, m = (int(x) for x in str(src).split("-")[:2])
            months = (self.today.year - y) * 12 + self.today.month - m
            return {"months_since": months, "years_floor": months // 12, "years_1dp": round(months / 12, 1)}[d]
        if d == "div":
            return float(src) / float(f["by"])
        if d == "mul":
            return float(src) * float(f["by"])
        if d == "inr_to_aed" and fx:
            return float(src) / fx
        if d == "aed_to_inr_year" and fx:
            return float(src) * 12 * fx
        raise Unanswerable(f"fact {key} has an unknown derivation {d}", facts=(key,))

    def skill_years(self, skill: str):
        """Years for a technology: the resume's total, a stated number, or Unanswerable (not on the resume / duration unknown)."""
        table = self.get("skill_years")
        cands = _skill_candidates(skill)
        for c in cands:
            if c in table:
                v = table[c]
                if v is None:
                    raise Unanswerable(f"'{skill}' is on the resume but its years are not stated", facts=("skill_years",))
                return self.get("experience_years_int") if v == "total" else v
        raise Unanswerable(f"'{skill}' is not on the resume", facts=("skill_years",))

    # -- the resolver
    def resolve(self, label: str, kind: str, options: list | None = None, numeric: bool = False,
                required: bool = True, ctx: dict | None = None) -> Answer:
        self._used_proposed = set()
        q = Q(norm(label), label, kind, [o for o in (options or []) if norm(o) not in ("select an option", "")],
              numeric, required, ctx or {})
        hits = [(r, m) for r in RULES if kind in r.kinds and (m := r.rx.search(q.n)) and not (r.neg and r.neg.search(q.n))]
        if not hits:
            # A rule that knows this TOPIC but whose guard or form kinds exclude this wording ("at least 5 years of ...", a salary threshold,
            # "gender" as a text box) is a refusal nothing may rescue. Only a topic no rule recognises at all is "no intent maps this
            # question", the single refusal the AI answerer is allowed to take over.
            known = sorted({r.name for r in RULES if r.rx.search(q.n)})
            if known:
                raise Unanswerable("a rule covers this topic but not this wording or form shape: " + ", ".join(known[:3]), intent=known[0])
            raise Unanswerable("no intent maps this question")
        blocks = [h for h in hits if h[0].priority >= 90]
        if not blocks and COMPOUND.search(q.n) and not re.search(r"\bfresher\b", q.n):   # "fresher or under 1 year" is one condition
            # "on notice or can you join in 30 days?", "authorised to work? if not, will you need sponsorship?": two questions in one
            raise Unanswerable("compound question (two questions in one)")
        if blocks:
            hits = blocks[:1]
        else:
            fams = sorted({r.family for r, _ in hits})
            if len(fams) > 1:
                raise Unanswerable("compound question (" + " + ".join(fams) + ")")
        top = max(r.priority for r, _ in hits)
        answers = [r.fn(self, q, m) for r, m in hits if r.priority == top]
        if len({(a.value, a.keep, a.check) for a in answers}) > 1:
            raise Unanswerable("conflicting intents: " + ", ".join(sorted({a.intent for a in answers})))
        a = answers[0]
        a.proposed = bool(self._used_proposed)
        return a

    def ledger_row(self, q_label: str, kind: str, exc: Unanswerable) -> dict:
        return {"question": q_label, "kind": kind, "reason": exc.reason, "intent": exc.intent, "facts": list(exc.facts),
                "facts_rev": self.rev}


# ------------------------------------------------------------------ rules
@dataclass
class Rule:
    name: str
    family: str
    rx: re.Pattern
    kinds: tuple
    neg: re.Pattern | None
    priority: int
    fn: object


RULES: list[Rule] = []


def rule(name: str, rx: str, family: str | None = None, kinds=ALL_KINDS, neg: str | None = None, priority: int = 50):
    def deco(fn):
        RULES.append(Rule(name, family or name.split(".")[0], re.compile(rx), tuple(kinds),
                          re.compile(neg) if neg else None, priority, fn))
        return fn
    return deco


DECLINE_RX = r"decline|prefer not|do not wish|don't wish|not wish|rather not|choose not|not to (say|disclose|answer)"
COMPOUND = re.compile(r"\b(if not|if yes|if so|if no|otherwise|and if)\b|[,;]? or (will|do|are|is|can|could|would|have|need|planning|going|"
                      r"looking|willing|able|available|serving|currently)\b|\?\s*\S+.*\?")
GENERIC_EXPERIENCE = {"work", "working", "professional", "total", "overall", "relevant", "industry", "it", "information technology",
                      "software development", "software engineering", "software", "engineering", "development", "programming",
                      "technical", "hands-on", "hands on", "practical", "full-time", "full time", "data"}
UAE_WORDS = r"(uae|u\.a\.e|united arab emirates|emirates|emirati|dubai|abu dhabi|sharjah|ajman)"


def _opts(q: Q) -> list:
    return [o for o in q.options if o.strip()]


def pick_yes_no(q: Q, yes: bool) -> str:
    if q.kind not in ("select", "radio"):
        return "Yes" if yes else "No"
    want = "yes" if yes else "no"
    opts = _opts(q)
    exact = [o for o in opts if norm(o) == want]
    if len(exact) == 1:
        return exact[0]
    starts = [o for o in opts if re.match(rf"^{want}\b", norm(o))]
    if len(starts) == 1:
        return starts[0]
    raise Unanswerable(f"no single option for '{want}' among {opts[:6]}")


def pick_by(q: Q, rx: str, what: str) -> str:
    opts = _opts(q)
    hits = [o for o in opts if re.search(rx, norm(o))]
    if len(hits) == 1:
        return hits[0]
    raise Unanswerable(f"{'no' if not hits else 'several'} option(s) for {what} among {opts[:6]}")


CLOSED_START = r"^(do|does|did|are|is|was|were|will|would|can|could|have|has|should|am|may)\b"


_UNIT = {"year": "year", "years": "year", "yr": "year", "yrs": "year", "month": "month", "months": "month", "mo": "month", "mos": "month",
         "day": "day", "days": "day", "week": "week", "weeks": "week"}
_NUM = r"(\d+(?:\.\d+)?)"
_INF = float("inf")


def _opt_interval(text: str):
    """(low, high, low_inclusive, high_inclusive, unit) a choice like '3 years', '3-5 years', '5+ years', 'Less than 15 days'
    stands for; None if it names no number. 'Less than 15' excludes 15 and 'More than 3' excludes 3."""
    t = norm(text)
    um = re.search(r"\b(years?|yrs?|months?|mos?|days?|weeks?)\b", t)
    unit = _UNIT[um.group(1)] if um else None
    if re.search(r"\b(none|no experience|fresher|zero)\b", t) and not re.search(r"\d", t):
        return 0.0, 0.0, True, True, unit
    m = re.search(rf"{_NUM}\s*(?:-|–|to)\s*{_NUM}", t)
    if m:
        return float(m.group(1)), float(m.group(2)), True, True, unit
    m = re.search(rf"(?:less than|under|below|fewer than|<)\s*{_NUM}", t)
    if m:
        return 0.0, float(m.group(1)), True, False, unit
    m = re.search(rf"(?:up to|upto|within|max(?:imum)?(?: of)?|at most)\s*{_NUM}|{_NUM}\s*(?:or less|or fewer|and below)", t)
    if m:
        return 0.0, float(m.group(1) or m.group(2)), True, True, unit
    m = re.search(rf"(?:more than|over|above|greater than|>)\s*{_NUM}", t)
    if m:
        return float(m.group(1)), _INF, False, True, unit
    m = re.search(rf"{_NUM}\s*\+|{_NUM}\s*(?:or more|and above|and over|or above|plus)|(?:at least|minimum(?: of)?)\s*{_NUM}", t)
    if m:
        return float(next(g for g in m.groups() if g)), _INF, True, True, unit
    m = re.match(rf"^{_NUM}\b", t)
    if m:
        return float(m.group(1)), float(m.group(1)), True, True, unit
    return None


def pick_number(q: Q, value: float, unit: str | None = None) -> str:
    """The one choice that covers a number (years, months, days). Zero or several equally narrow choices: Unanswerable."""
    opts = _opts(q)
    hits = []
    for o in opts:
        iv = _opt_interval(o)
        if iv is None:
            continue
        lo, hi, lo_in, hi_in, u = iv
        if unit and u and u != unit:
            continue
        if (value > lo or (lo_in and value == lo)) and (value < hi or (hi_in and value == hi)):
            hits.append((hi - lo, o))
    if not hits:
        raise Unanswerable(f"no choice covers {value:g} {unit or ''} among {opts[:8]}".replace("  ", " "))
    hits.sort(key=lambda h: h[0])
    if len(hits) > 1 and hits[0][0] == hits[1][0]:
        raise Unanswerable(f"{value:g} {unit or ''} fits several choices equally: {[h[1] for h in hits[:3]]}".replace("  ", " "))
    return hits[0][1]


def _yn(q: Q, yes: bool, intent: str, *keys: str) -> Answer:
    if q.kind in ("text", "textarea") and not re.match(CLOSED_START, q.n):
        # "please state the type of visa..." and "rate yourself out of 10" are not Yes/No questions
        raise Unanswerable("open text question; a Yes/No answer is not what it asks", intent=intent, facts=keys)
    return Answer(value=pick_yes_no(q, yes), intent=intent, facts=keys, check=yes if q.kind == "checkbox" else None)


def _text_only(q: Q, intent: str) -> None:
    if q.kind not in ("text", "textarea"):
        raise Unanswerable("this kind of question has no matching intent here", intent=intent)


# ---- contact fields: the portal's own pre-filled profile is used
@rule("contact.phone_country_code", r"^phone country code$|\bcountry code\b", kinds=("select", "text"), priority=80)
def _r_cc(f, q, m):
    return Answer(keep=True, intent="contact.phone_country_code")


@rule("contact.email", r"^e-?mail( address)?$", kinds=("select", "text"), priority=80)
def _r_email(f, q, m):
    return Answer(keep=True, intent="contact.email")


@rule("contact.phone", r"^((mobile|cell) )?phone( number)?$|^mobile( phone)?( number)?$|^contact (number|no)$", kinds=("text",), priority=80)
def _r_phone(f, q, m):
    return Answer(keep=True, intent="contact.phone")


@rule("contact.name", r"^(first|last|middle|full|legal|given|family|sur)\s?name$|^name$", kinds=("text",), priority=80)
def _r_name(f, q, m):
    return Answer(keep=True, intent="contact.name")


# U4 (2026-10-17): also "Please enter your LinkedIn profile URL" (seen on a real Easy Apply form, 2026-10-08 and 10-11: it had no rule, so the job was
# blocked), "Your LinkedIn profile", "LinkedIn profile link", "Link to your LinkedIn profile". The value is the form's own pre-fill or the bot's configured profile URL.
@rule("contact.linkedin", r"^(((please )?(enter|provide|share|paste|add) )?((your|the) )?linkedin( profile)?( url| link)?|link to your linkedin( profile)?)$",
      kinds=("text",), priority=80)
def _r_linkedin(f, q, m):
    return Answer(keep=True, intent="contact.linkedin")


# ---- never answered
@rule("id.block", r"(emirates id|\beid\b|national id|aadhaa?r|\bpan (card|number)|passport (number|no)|visa (number|no)|"
                  r"labou?r card|\bid number|social security|date of birth|\bdob\b)", priority=95)
def _r_ids(f, q, m):
    raise Unanswerable("ID numbers and dates of birth are never typed by a bot", intent="id.block")


@rule("demographic.decline", r"\b(gender|sex|race|ethnic\w*|religio\w*|veteran|disabilit\w*|marital|sexual orientation|pronouns?|caste)\b",
      kinds=("select", "radio"),
      neg=r"\b(colleagues?|co-?workers?|team|work(ing)? (with|alongside)|diverse|diversity|any (race|religion|gender)|regardless)\b", priority=70)
def _r_demo(f, q, m):
    return Answer(value=pick_by(q, DECLINE_RX, "a decline option"), intent="demographic.decline")


# ---- nationality, location, residence
@rule("nationality.uae_national", rf"^(are you|do you (hold|have)|is your)\b[^?]{{0,25}}\b{UAE_WORDS}\b[^?]{{0,15}}\b(national|citizen\w*|nationality|passport)\b",
      neg=r"\b(work(ing)? (with|alongside)|colleagues?|team|manager|report)\b", priority=70)
def _r_uae_nat(f, q, m):
    nat = str(f.get("nationality")).lower()
    return _yn(q, bool(re.search(r"emirati|uae|gcc", nat)), "nationality.uae_national", "nationality")


@rule("nationality.is", r"\bnationality\b|\bcitizenship\b|country of citizenship|which country.*citizen", kinds=("text", "select"),
      neg=rf"{UAE_WORDS}|\bus\b|united states", priority=60)
def _r_nat(f, q, m):
    nat = str(f.get("nationality"))
    if q.kind == "select":
        return Answer(value=pick_by(q, r"^(indian|india)$", "Indian"), intent="nationality.is", facts=("nationality",))
    return Answer(value=nat, intent="nationality.is", facts=("nationality",))


@rule("location.current", r"^(current |present )?(location|city|city/town|town)( \(city\))?$|(current(ly)?|present(ly)?) (location|city|base[ds]?|country of residence|place)|"
                          r"where (are you|do you) (currently |now )?(based|located|live|living|reside)|country of residence|^location$",
      kinds=("text", "select"), neg=r"\bjob\b|work location|office|commut|relocat|prefer", priority=55)
def _r_loc(f, q, m):
    city, country = str(f.get("current_city")), str(f.get("current_country"))
    if q.kind == "select":
        return Answer(value=pick_by(q, rf"^{country.lower()}$", country), intent="location.current", facts=("current_country",))
    only_country = "country" in q.n and "city" not in q.n
    only_city = "city" in q.n and "country" not in q.n
    val = country if only_country else city if only_city else f"{city}, {country}"
    return Answer(value=val, intent="location.current", facts=("current_city", "current_country"), typeahead=not only_country)


@rule("residence.in_uae", rf"^(are you|do you|where are you)\b[^?]{{0,30}}\b(living|live|residing|reside|based|located|resident)\b[^?]{{0,25}}\b(in|within|inside|of)\b[^?]{{0,15}}\b{UAE_WORDS}\b",
      neg=r"\b(company|employer|role|position|job|office|team|family|spouse|wife|husband|relatives?|friends?|client|customer|colleagues?)\b", priority=65)
def _r_res(f, q, m):
    return _yn(q, bool(f.get("in_uae")), "residence.in_uae", "in_uae")


# ---- visa, sponsorship, work authorisation
VISA_DENY = re.compile(r"\b(not|cannot|can't|unable|without|no need|already|sponsored by|employer that|company that|visa sponsor|have a sponsor|"
                       r"do you know|if yes|if not|if so|please (state|specify|describe|explain|provide)|which|what (type|kind)|describe|explain|"
                       r"h-?1b|b-?1|b-?2|l-?1|schengen|green card|permanent resident|current (employer|company)|"
                       r"dependents?|dependants?|spouse|wife|husband|family|children|child|kids?|parents?|partner|renew\w*|transfer\w*|cancel\w*|"
                       r"extension|extend|arrange|process|assist\w*)\b")
OTHER_COUNTRY = re.compile(r"\b(saudi|ksa|qatar|kuwait|oman|bahrain|gcc|usa|u\.s\.a?\.?|united states|uk|united kingdom|canada|australia|europe|eu|"
                           r"singapore|germany|japan|china|france|riyadh|jeddah|dammam|doha|muscat|manama|cairo|amman)\b|"
                           r"\bus (citizen|work|visa|based|national|resident|authori[sz]ation|employer|company|time|tax)\b")


@rule("visa.family", r"\b(visa|sponsor\w*|work permit|residence permit|residency|labou?r card|emirates id|work authori[sz]ation|authori[sz]ed to work|"
                     r"eligible to work|right to work|legally (able|allowed|authori[sz]ed|permitted)|permission to work)\b", priority=60)
def _r_visa(f, q, m):
    """Visa wording is where a wrong Yes costs most, so only three exact question shapes are answered; everything else is skipped."""
    n = q.n
    if VISA_DENY.search(n):
        raise Unanswerable("visa wording this resolver does not answer (negation, employer, another visa, or a follow-up)", intent="visa.family")
    uae = re.search(rf"\b{UAE_WORDS}\b", n)
    india = re.search(r"\bindia\b", n)
    region_uae = q.ctx.get("region") == "UAE"
    if OTHER_COUNTRY.search(n) and not uae:
        raise Unanswerable("work-authorisation question about a country the facts do not cover", intent="visa.family")
    # 1. "Will you (now or in the future) require sponsorship ...?" / "Do you require visa sponsorship ...?"
    if re.match(r"^(will|do|would)\b[^?]{0,70}\b(require|need)\b[^?]{0,60}\bsponsorship\b[^?]{0,80}$", n):
        if india and not uae:
            raise Unanswerable("sponsorship in India: the facts hold the UAE work-visa need only", intent="visa.sponsorship")
        return _yn(q, bool(f.get("needs_visa_sponsorship")), "visa.sponsorship", "needs_visa_sponsorship")
    # 2. "Are you legally authorised to work in <the UAE | India | the country you apply in>?" / "Do you have the right to work ...?"
    if re.match(r"^((are|do) you\b[^?]{0,25}\b(authori[sz]ed|eligible|permitted|allowed)\b[^?]{0,15}\bto work\b|"
                r"do you have (the |a )?(legal )?right to work\b)[^?]{0,80}$", n):
        if uae or (region_uae and not india):
            return _yn(q, bool(f.get("work_authorization_uae")), "visa.authorization_uae", "work_authorization_uae")
        if india:
            return _yn(q, str(f.get("nationality")).lower() == "indian", "visa.authorization_india", "nationality")
        raise Unanswerable("work-authorisation question without a country", intent="visa.family")
    # 3. "Do you (currently) have/hold a (valid) (UAE) (work|residence|employment) visa|permit?"
    hv = re.match(r"^do you (currently )?(have|hold)\b ?(a |an )?(valid |current |active )?(uae |dubai |emirates )?"
                  r"(?P<type>employment |work |residence |residency |resident |visit |tourist |golden )?"
                  r"(?P<noun>visa|residence permit|residency permit|work permit|labou?r card)\b"
                  r"( (for|in) (the )?(uae|united arab emirates|dubai|abu dhabi))?$", n)
    if hv:
        # an explicit UAE mention, or in a UAE job a work/employment/residence visa or permit; a bare "a visa" could be any visa
        work_type = bool(re.search(r"employment|work|residen", (hv.group("type") or "") + hv.group("noun")))
        if uae or (region_uae and work_type):
            f.get("uae_visa_status")      # the fact must be usable; the answer is "No UAE visa yet"
            return _yn(q, False, "visa.held_uae", "uae_visa_status")
        raise Unanswerable("visa question without a country or a work-visa type", intent="visa.family")
    raise Unanswerable("visa question not one of the known shapes", intent="visa.family")


# ---- relocation, commuting, work mode, start date, notice
@rule("relocation.willing", r"\b(willing|open|able|ready|prepared|happy|comfortable|interested|keen|agree)\b[^?]{0,30}\bre-?locat\w*|"
                            r"^(will|would|can|could) you\b[^?]{0,20}\bre-?locat\w*",
      neg=r"\b(ever|before|previously|in the past|cost|expense|assist\w*|package|allowance|support|reimburs\w*|budget|need relocation|"
          r"require relocation|within|immediately|asap|right away|by (next|the end|end of|\w+ \d)|for \d+ (months?|years?)|"
          r"\d+ (days?|weeks?|months?)|next (week|month)|short notice)\b", priority=60)
def _r_reloc(f, q, m):
    if OTHER_COUNTRY.search(q.n) and not re.search(rf"\b{UAE_WORDS}\b", q.n):
        raise Unanswerable("relocation to a country outside the UAE search is not decided", intent="relocation.willing", facts=("relocate_any_city",))
    return _yn(q, bool(f.get("relocate_any_city")), "relocation.willing", "relocate_any_city")


@rule("commute.ok", r"\b(comfortable|willing|able|ok|okay|fine)\b.*\b(commut\w*|travel(l?ing)? to (the|this) (office|job|location)|coming (in)?to the office)\b",
      neg=r"\b(\d+|one|two|three|four)\s*(hours?|hrs?|minutes?|mins?|km|kilomet\w*|miles?)\b|\b(long|longer|more than|over|up to|per day|a day|daily)\b",
      priority=60)
def _r_commute(f, q, m):
    return _yn(q, bool(f.get("relocate_any_city")), "commute.ok", "relocate_any_city")


@rule("work_mode.ok", r"\b(comfortable|willing|open|ok|okay|fine|able|prefer|available)\b.*\b(on-?\s?site|hybrid|remote|work from home|wfh|office)\b|"
                      r"\b(on-?\s?site|hybrid|remote)\b (setting|role|mode|work|arrangement|position)",
      neg=r"\b(experience|years?|worked|have you (ever )?(worked|managed|led)|managing|leading|supporting|manage)\b", priority=55)
def _r_mode(f, q, m):
    pref = f.get("work_mode_preference")        # {"onsite": bool, "hybrid": bool, "remote": bool}
    which = "hybrid" if "hybrid" in q.n else "remote" if re.search(r"remote|work from home|wfh", q.n) else "onsite"
    if not isinstance(pref, dict) or which not in pref:
        raise Unanswerable(f"work_mode_preference has no entry for {which}", intent="work_mode.ok", facts=("work_mode_preference",))
    if re.search(r"\bprefer", q.n):
        raise Unanswerable("a preference question needs the user's own wording", intent="work_mode.ok")
    return _yn(q, bool(pref[which]), "work_mode.ok", "work_mode_preference")


@rule("start.immediately", r"\b(start|join|joining|begin)\b.*\b(immediate|immediately|right away|asap|at once)\b|\bimmediate joiner\b|\bjoin immediately\b", neg=r"\bor\b.*\bnotice\b|serving", priority=60)
def _r_start(f, q, m):
    return _yn(q, bool(f.get("join_immediately")), "start.immediately", "join_immediately")


@rule("join.within", r"\b(join|joining|start)\b.*\b(within|in|by|inside)\s+(\d{1,3})\s*days?\b", neg=r"\d\s*/\s*\d|\bor\b|immediate", priority=58)
def _r_within(f, q, m):
    asked, days = int(m.group(3)), int(f.get("notice_period_days"))
    if asked < days:
        # the decided "join immediately" and the 15-day notice disagree for a shorter window: the user decides, not the bot
        raise Unanswerable(f"asked about {asked} days, shorter than the {days}-day notice", intent="join.within", facts=("notice_period_days", "join_immediately"))
    return _yn(q, True, "join.within", "notice_period_days")


@rule("serving.notice", r"\bserving\b.*\bnotice\b|\b(currently|presently) on notice\b|\bare you on notice\b|notice period.*\bserving\b", priority=65)
def _r_serving(f, q, m):
    return _yn(q, bool(f.get("serving_notice")), "serving.notice", "serving_notice")


@rule("notice.period", r"\bnotice( period| tenure)?\b|\bhow soon (can|could|would|will) you (be able to )?(join|start)\b|\bhow soon you can join\b|\bwhen can you (join|start)\b|"
                       r"\bin how many days you can join\b|\blead time to join\b|\bearliest (start|joining)\b", kinds=("text", "select", "radio"),
      neg=r"\bserving\b|\bimmediate joiner\b|within \d+|\bor\b.*\b(30|45|60|90)\b", priority=55)
def _r_notice(f, q, m):
    days = int(f.get("notice_period_days"))
    n = q.n
    if q.kind in ("select", "radio"):
        return Answer(value=pick_number(q, float(days), "day"), intent="notice.period", facts=("notice_period_days",))
    if "month" in n and "day" not in n:
        raise Unanswerable("notice period asked in months; 15 days is not a whole month", intent="notice.period", facts=("notice_period_days",))
    if "week" in n and "day" not in n:
        return Answer(value=_fmt(days / 7 if days % 7 == 0 else round(days / 7, 1)), intent="notice.period", facts=("notice_period_days",))
    if q.numeric or re.search(r"\bdays?\b|in days|number of days", n):
        return Answer(value=_fmt(days), intent="notice.period", facts=("notice_period_days",))
    return Answer(value=str(f.get("notice_period_text")), intent="notice.period", facts=("notice_period_text",))


# ---- salary
def _currency(n: str):
    """The currency a salary question names, or None. 'CTC' alone names none: UAE recruiters use it too."""
    aed = re.search(r"\b(aed|dirhams?|dhs?)\b", n)
    inr = re.search(r"\b(inr|rs\.?|rupees?|lpa|lakhs?|lacs?)\b|₹", n)
    usd = re.search(r"\busd\b|\$|us dollars?", n)
    found = [c for c, hit in (("AED", aed), ("INR", inr), ("USD", usd)) if hit]
    if len(found) > 1:
        raise Unanswerable("salary question names more than one currency", intent="salary")
    return found[0] if found else None


@rule("salary.amount", r"\b(salary|ctc|ectc|e-ctc|compensation|remuneration|package|pay|wage|stipend|cost to (the )?company)\b",
      neg=r"\b(range|acceptable|okay|ok with|comfortable|agree|negotiable|bonus|slab|counter|negotiat\w*|equity|stock|relocation)\b|"
          r"\b(other|another|any|competing|existing|alternate|alternative)\s+offers?\b|\boffers? (in hand|letter|from)\b|"
          r"\b(basic|net|gross|in[- ]hand|take[- ]home|allowances?|housing|transport(ation)?|variable|base (salary|pay))\b", priority=50)
def _r_salary(f, q, m):
    n = q.n
    if q.kind in ("select", "radio"):
        # a Yes/No or a choice ("Is your current salary above AED 20,000?") is not answered with a figure: no option holds it, and a caller
        # that maps "anything but Yes" to No would answer a question that has no fact behind it
        raise Unanswerable("a salary asked as a choice is not answered", intent="salary")
    current = re.search(r"\b(current|present|existing|last drawn|last salary|drawing|most recent)\b", n)
    expected = re.search(r"\b(expect\w*|desired|asking|offer\w*|preferred|target|looking for|requirement|ectc|e-ctc)\b", n)
    if bool(current) == bool(expected):
        raise Unanswerable("salary question is not clearly current or expected", intent="salary")
    cur = _currency(n)
    per = ("month" if re.search(r"\b(month|monthly|per month|pm|p\.m)\b", n)
           else "year" if re.search(r"\b(year|yearly|annual|annum|per annum|lpa|pa|p\.a)\b", n) else None)
    lakh = bool(re.search(r"\b(lpa|lakhs?|lacs?)\b", n))
    assumed = False                 # the unit was not in the question: the proposed default (AED per month) fills the gap
    if cur is None:
        d_cur, d_per = str(f.get("salary_default_unit")).split("/")
        cur, assumed = d_cur, True
        per = per or d_per
    elif per is None:
        if cur == "AED":
            per, assumed = str(f.get("salary_default_unit")).split("/")[1], True
        elif cur == "INR":
            per = "year"            # CTC and LPA are annual by convention
    if cur == "USD":
        raise Unanswerable("salary asked in USD; the facts hold AED and INR", intent="salary")
    if cur == "AED":
        base = "current_salary_aed_month" if current else "expected_salary_aed_month"
        v = float(f.get(base)) * (12 if per == "year" else 1)
    else:                           # INR; the facts hold it per year
        base = "current_salary_inr_year" if current else "expected_salary_inr_year_equiv"
        v = float(f.get(base)) / (12 if per == "month" else 1)
        if lakh:
            v /= 100000
    digits = _fmt(round(v, 2)) if lakh else _fmt(round(v))
    if q.numeric or not assumed:
        return Answer(value=digits, intent="salary.amount", facts=(base,))
    return Answer(value=f"{cur} {int(round(v)):,} per {per}", intent="salary.amount", facts=(base,))


# ---- experience
def _skill_candidates(skill: str) -> list:
    s = norm(skill)
    out = []
    for p in re.findall(r"\(([^)]+)\)", s):
        out.append(p.strip())
    base = re.sub(r"\([^)]*\)", "", s).strip(" ,.;")
    base = re.sub(r"\b(programming language|language|framework|development|technology|technologies|tools?)\b$", "", base).strip()
    out.insert(0, base)
    out = [ALIASES.get(c, c) for c in out]
    return [c for c in out if c]


ALIASES = {"amazon web services": "aws", "apache spark": "spark", "apache airflow": "airflow", "postgres": "postgresql",
           "data warehouse": "data warehousing", "datawarehouse": "data warehousing", "data warehouse development": "data warehousing",
           "extract transform load": "etl", "structured query language": "sql", "apache kafka": "kafka", "data pipeline": "data pipelines",
           "py spark": "pyspark", "relational database": "relational databases", "database": "databases", "google cloud platform": "gcp",
           "microsoft azure": "azure", "k8s": "kubernetes", "apache hadoop": "hadoop", "ci cd": "ci/cd"}


EXP_FILLER = set("how many much years year yrs yr months month of work working experience experiences do does you your have has currently the what "
                 "is are there total overall professional relevant industry it information technology software development engineering "
                 "hands-on hands on practical in a an please enter provide state mention number numbers digits numeric only whole integer "
                 "decimal allowed after graduation graduating full-time full time can tell us about gained accumulated since so far".split())
EXP_FORMAT_HINT = re.compile(r"\([^)]*\b(only|whole|numbers?|digits|numeric|integer|decimals?|e\.?g\.?|example|years?|yrs?|months?)\b[^)]*\)")


_EXPW = r"(?:experience|experiance|expereince|experince)"          # the form-writers' usual misspellings are read as the word
@rule("experience.years", rf"\b(years?|yrs?|months?)\b.*\b{_EXPW}\b|\b{_EXPW}\b.*\b(years?|yrs?|months?)\b|\b(total|overall) (work |professional |it )?{_EXPW}\b|"
                          rf"\b{_EXPW} in years\b",
      neg=r"\b(minimum|at least|do you have \d|\d\+? (years?|yrs?)|are you|will you|have you worked|additional|extra|beyond|other than|apart from|"
          r"rate|rated|rating|on a scale|scale of|out of \d+|score|describe|explain|summari[sz]e|tell us|how would you)\b", priority=50)
def _r_exp(f, q, m):
    n = re.sub(r"\b(?:experiance|expereince|experince)\b", "experience", q.n)
    months = bool(re.search(r"\bmonths?\b", n)) and not re.search(r"\byears?\b|\byrs?\b", n)
    skill = None
    a = re.search(r"experience(?: do you (?:currently )?have| have you| you have)? (?:with|in|using|on|of|working with|working on)\s+(?P<s>.+)$", n)
    b = re.search(r"\b(?:years?|yrs?|months?) of (?P<s>[^?]+?) experience\b", n)
    if a:
        skill = a.group("s")
    elif b and norm(b.group("s")) not in GENERIC_EXPERIENCE:
        skill = b.group("s")
    if skill is None:
        # no "with X" shape: whatever words are left once the generic ones are removed name a technology or a role, never the total.
        # A parenthesised format hint ("(only whole numbers allowed)", "(in years)") is not a technology.
        rest = [w for w in re.findall(r"[a-z0-9+#./&-]+", EXP_FORMAT_HINT.sub(" ", n)) if w not in EXP_FILLER]
        if rest:
            skill = " ".join(rest)
    if skill is None or norm(skill) in GENERIC_EXPERIENCE:
        key = "experience_months" if months else "experience_years_int"
        val = f.get(key)
        if q.kind in ("select", "radio"):        # a choice like "3 years" or "2-4 years", not a number to type
            return Answer(value=pick_number(q, float(val), "month" if months else "year"), intent="experience.total", facts=(key,))
        return Answer(value=_fmt(val), intent="experience.total", facts=(key,))
    if months:
        raise Unanswerable("months of experience asked for a single technology", intent="experience.skill")
    skill = re.sub(r"\s+(do you (currently )?have|you have)$", "", skill).strip(" ?.")
    yrs = f.skill_years(skill)
    if q.kind in ("select", "radio"):
        return Answer(value=pick_number(q, float(yrs), "year"), intent="experience.skill", facts=("skill_years", "experience_years_int"))
    return Answer(value=_fmt(yrs), intent="experience.skill", facts=("skill_years", "experience_years_int"))


@rule("fresher.no", r"\bfresher\b|\b(no more than|less than|under|at most)\s+(1|one)\s+year\b[^?]{0,40}\bexperience\b",
      neg=r"\bexperience\s+(with|in|using|on)\b", priority=60)
def _r_fresher(f, q, m):
    return _yn(q, int(f.get("experience_years_int")) < 1, "fresher.no", "experience_years_int")


# ---- education
@rule("education.level", r"\b(bachelor'?s?|master'?s?|doctorate|doctoral|ph\.?\s?d|high school|secondary|associate'?s?|diploma|post[- ]?graduate|undergraduate|graduate|mba|b\.?\s?tech|m\.?\s?tech|b\.?e\b)\b.*\b(degree|education|qualification|completed|hold|level)\b|"
                       r"\b(level of education|education level|highest (level of )?(education|qualification|degree)|educational? qualification)\b", family="education",
      neg=r"\bgpa\b|\bcgpa\b|\bscore\b|percent|\btier\b|\biit\b|\bnit\b|\biim\b|\bivy\b|top[- ]?(tier|\d+)|do not apply|reputed|premier|accredited|"
          r"(from|at) (an?|the) ", priority=55)
def _r_edu(f, q, m):
    n = q.n
    asked = ("bachelor" if re.search(r"bachelor|undergraduate|b\.?\s?tech|\bb\.?e\b", n) else
             "master" if re.search(r"master|m\.?\s?tech|mba|post[- ]?graduate", n) else
             "doctorate" if re.search(r"doctor|ph\.?\s?d", n) else
             "school" if re.search(r"high school|secondary", n) else None)
    if asked is None:
        if q.kind in ("select", "radio") and EDU_HIGHEST_LABEL.fullmatch(n):
            # exactly "What is your highest level of education?" (and its plain variants), never "...you are pursuing / plan to pursue /
            # required for this role / of your spouse": answered with the one option that names the recorded highest level
            highest = str(f.get("education_highest")).lower()
            if highest not in EDU_WORD:
                raise Unanswerable("education_highest is not a level this resolver knows", intent="education.level", facts=("education_highest",))
            return Answer(value=_edu_option(q, highest), intent="education.highest", facts=("education_highest",))
        raise Unanswerable("education level not understood (associate, diploma, or an open question)", intent="education.level")
    if asked in ("bachelor", "school"):
        # a bachelor's degree is a recorded fact (the resume); a school-leaving certificate is implied by it
        if "bachelor" not in str(f.get("education_degree")).lower():
            raise Unanswerable("education_degree does not record a bachelor's degree", intent="education.level", facts=("education_degree",))
        if asked == "bachelor":
            fld = re.search(r"bachelor'?s?( degree)?\s+(in|of)\s+(?P<f>[^?]+)$", n)
            if fld:                               # the field the question names must be one the degree (Information Science) covers
                field = fld.group("f")
                if EDU_FIELD_BAD.search(field) or not EDU_FIELD_OK.search(field):
                    raise Unanswerable("bachelor's degree in a field the facts do not state", intent="education.level", facts=("education_degree",))
        return _yn(q, True, "education.level", "education_degree")
    # master's / doctorate: answered from the recorded highest level, which stays 'proposed' until the user confirms it
    levels = {"school": 0, "bachelor": 1, "master": 2, "doctorate": 3}
    highest = str(f.get("education_highest")).lower()
    if highest not in levels:
        raise Unanswerable("education_highest is not a level this resolver knows", intent="education.level", facts=("education_highest",))
    return _yn(q, levels[highest] >= levels[asked], "education.level", "education_highest")


_EDU_DONE = r"(?: (?:that )?(?:you )?(?:have )?(?:completed|attained|achieved|obtained|earned))?"
EDU_HIGHEST_LABEL = re.compile(      # matched against the whole normalised label: any extra word (pursuing, required, spouse, "and in which field") refuses
    r"(?:please )?(?:(?:select|choose|enter|state) )?(?:what(?:'s| is| was) )?(?:your|the) highest (?:level of )?(?:education|qualification|degree)" + _EDU_DONE
    + r"|(?:what(?:'s| is| was) )?(?:your|the) level of education" + _EDU_DONE
    + r"|(?:highest (?:level of )?(?:education|qualification|degree)|(?:education|qualification) level|level of education)(?: (?:completed|attained|achieved|obtained))?")
EDU_WORD = {"school": r"high school|secondary", "bachelor": r"bachelor", "master": r"master|post[- ]?graduate degree", "doctorate": r"doctor"}
EDU_ABBR = {"school": None, "bachelor": r"^\s*B\.?\s?(?:E|Tech)\b", "master": r"^\s*(?:M\.?\s?Tech|MBA)\b", "doctorate": r"^\s*Ph\.?\s?D\b"}
EDU_NEGATED = re.compile(r"\b(less than|below|under|some|in progress|pursuing|incomplete|not (?:yet )?completed|ongoing|currently|dropped?|non[- ]degree|no formal|"
                         r"final year|pre-?final|expected|appearing|enrolled|awaiting|candidate|yet to|to be completed|undergoing|studying)\b")


def _edu_option(q: Q, level: str) -> str:
    """The one option that names exactly this education level and is not its negation ("Less than a bachelor's degree", "in progress").
    The abbreviations (B.E., B.Tech, M.Tech, MBA, PhD) are matched on the RAW option, case-sensitively and only at its start, so the English
    word "be" in "Other (to be specified)" can never read as B.E.; "Undergraduate" and "Graduate" name no level (they mean different
    things in India and the US) and never match."""
    hits = []
    for o in _opts(q):
        if EDU_NEGATED.search(norm(o)):
            continue
        if re.search(EDU_WORD[level], norm(o)) or (EDU_ABBR[level] and re.search(EDU_ABBR[level], o)):
            hits.append(o)
    if len(hits) == 1:
        return hits[0]
    raise Unanswerable(f"{'no' if not hits else 'several'} option(s) name the highest education '{level}' among {_opts(q)[:6]}",
                       intent="education.highest", facts=("education_highest",))


EDU_FIELD_OK = re.compile(r"\b(computer (science|engineering)|information (technology|science)|software engineering|software|it|cs|ise|"
                          r"related field|relevant field|technical field|stem)\b")
EDU_FIELD_BAD = re.compile(r"\b(applications?|business|management|commerce|arts|finance|accounting|economics|marketing)\b")


# ---- languages
@rule("language.level", r"\b(english|hindi|arabic|urdu|french)\b", kinds=("text", "select", "radio"), priority=55)
def _r_lang(f, q, m):
    lang = m.group(1)
    key = {"english": "language_english", "hindi": "language_hindi", "arabic": "language_arabic"}.get(lang)
    if key is None:
        raise Unanswerable(f"language {lang} is not in the facts", intent="language.level")
    level = str(f.get(key)).lower()
    native = bool(re.search(r"native|bilingual", level))
    if re.search(r"out of \d+|on a scale|scale of|rate (yourself|your)|rating", q.n):
        raise Unanswerable("a self-rating needs the user's own number", intent="language.level", facts=(key,))
    if re.search(r"proficien|level|fluen\w+ level|how well", q.n) and q.kind in ("select", "radio"):
        if not native:
            raise Unanswerable(f"{key} level has no mapped option", intent="language.level", facts=(key,))
        return Answer(value=pick_by(q, r"native|bilingual", "native or bilingual"), intent="language.level", facts=(key,))
    if re.search(r"\b(speak|fluent|fluency|communicat\w*|proficient|read|write|know|understand)\b", q.n):
        return _yn(q, bool(re.search(r"native|bilingual|fluent|professional", level)), "language.yes_no", key)
    raise Unanswerable("language question not understood", intent="language.level")


# ---- screening, documents, employers, source
@rule("screening.consent", r"\b(background (check|verification)|reference check|drug test|pre-?employment (screening|check))\b", neg=r"criminal|convict", priority=55)
def _r_screen(f, q, m):
    return _yn(q, bool(f.get("willing_screening")), "screening.consent", "willing_screening")


@rule("passport.valid", r"\b(valid|current|active)\b.*\bpassport\b|\bpassport\b.*\b(valid|validity)\b|^do you (have|hold) a passport", priority=60)
def _r_passport(f, q, m):
    return _yn(q, bool(f.get("passport_valid")), "passport.valid", "passport_valid")


@rule("employer.current", r"^(what is |what's |please (enter|provide|state|mention) )?(the name of )?(your )?(current|present|latest|most recent) "
                          r"(employer|company|organi[sz]ation)( name)?\s*(\(.*)?$|^who is your (current|latest) employer$|^name of (your )?(current|latest) "
                          r"(employer|company)$", kinds=("text",),
      neg=r"\bcost\b|\bctc\b|salary|\blpa\b|notice|\bpay\b|\bwhy\b|leaving|reason|manager|supervisor|contact|permission|approach|reference", priority=55)
def _r_emp(f, q, m):
    return Answer(value=str(f.get("current_employer")), intent="employer.current", facts=("current_employer",))


@rule("employer.title", r"^(what is |what's |please (enter|provide|state|mention) )?(the )?(your )?(current|present|latest|most recent) "
                        r"(job title|designation|position|role|title)\s*(\(.*)?$|^(what is |what's )?your (job title|designation|title)$", kinds=("text",),
      neg=r"\b(responsibilit\w*|duties|describe|reporting|report|tell|team|size)\b", priority=55)
def _r_title(f, q, m):
    return Answer(value=str(f.get("current_title")), intent="employer.title", facts=("current_title",))


@rule("source.hear", r"how did you (hear|find|come across|learn)|where did you (hear|find|see)|source of (application|referral)", kinds=("text", "select", "radio"), priority=55)
def _r_hear(f, q, m):
    # "LinkedIn" is true only when the caller says the job was found on LinkedIn (the LinkedIn UAE pass passes ctx channel="linkedin");
    # anywhere else (an employer career site) how the job was found is not a fact this resolver has
    if (q.ctx or {}).get("channel") != "linkedin":
        raise Unanswerable("how the job was found is only known inside the LinkedIn pass", intent="source.hear")
    if q.kind == "text":
        return Answer(value="LinkedIn", intent="source.hear")
    return Answer(value=pick_by(q, r"^linkedin( job (posting|board))?$", "LinkedIn"), intent="source.hear")


@rule("consent.check", r"\b(agree|accept|acknowledge|certify|confirm|declare|consent|authori[sz]e|terms|privacy)\b", kinds=("checkbox",),
      neg=r"marketing|newsletter|promotion|sms|whatsapp|email me|receive|subscribe|contact me about other|driving|licen[cs]e|years?\b|experience|"
          r"offer|salary|\baed\b|currently (work|employ)|\bhold\b|possess|certified|degree|visa|resident|citizen|nationality|"
          r"employment|contract|\brole\b|position|\bweeks?\b|probation|qualif\w*|requirements?|\bmeet\b|over \d+|\bage\b|\bold\b", priority=55)
def _r_consent(f, q, m):
    # only boilerplate is ticked: terms and conditions, privacy, data processing, "the information is true". A box that states a
    # fact or accepts a commitment ("I hold a UAE driving licence", "I accept the terms of the contract", "I am over 18") has no
    # fact behind it.
    if not re.search(r"\b(terms and conditions|terms of (use|service)|privacy (policy|notice|statement)|data (processing|protection)|"
                     r"cookie (policy|notice)|processing of (my )?(personal )?(data|information)|"
                     r"(information|details|answers|statements?) (provided |given )?(is|are) (true|accurate|correct|complete)|"
                     r"(true|accurate|correct) (and complete|to the best of my knowledge)|"
                     r"read and (understood|agree)( to)? (the )?(terms|privacy|policy|notice|statement))\b", q.n):
        raise Unanswerable("a checkbox that states a fact or a commitment, not boilerplate consent", intent="consent.check")
    return Answer(value="checked", intent="consent.check", check=True)


# ------------------------------------------------------------------ CLI
def _cli(argv: list) -> int:
    f = Facts(live=False)
    if argv[:1] == ["list"]:
        for k, v in f.raw.items():
            shown = "<sensitive>" if v.get("sensitive") else ("<map>" if isinstance(v.get("value"), dict) else str(v.get("value", "(derived)"))[:50])
            print(f"{k:34} {v.get('status', 'known'):9} {shown}")
        return 0
    if argv[:1] == ["ask"] and len(argv) >= 2:
        kind = argv[2] if len(argv) > 2 else "text"
        opts = argv[3].split("|") if len(argv) > 3 else []
        for live in (False, True):
            f.live = live
            try:
                a = f.resolve(argv[1], kind, opts, ctx={"region": "UAE"})
                print(f"{'live     ' if live else 'inventory'}: {a.intent} -> {'<sensitive>' if a.intent.startswith('salary') else a.value!r} keep={a.keep} check={a.check} proposed={a.proposed}")
            except Unanswerable as e:
                print(f"{'live     ' if live else 'inventory'}: UNANSWERABLE: {e.reason}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
