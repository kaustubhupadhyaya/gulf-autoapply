"""One gentle look at whether NaukriGulf lets the applier's browser work again: the applier's own open_session (one dashboard
load, then one or two GETs), in a real headful Chrome parked off-screen (not the headless mode that adjusts the user agent). Nothing
is cleared, nothing is edited, no cookie is touched: if the site still cuts the
connection, the park is extended (12 h, then 24 h, then 48 h) and nothing else is tried.

    naukrigulf_gentle_check.py            run the check (refuses while a park marker is still in force)

Result: state/naukrigulf_gentle_check.json. Exit 0 the browser works (park markers removed; a dry run is the next step, and
live applying still needs the user), 47 still blocked (parked longer), 42 signed out, 3 parked and not due yet, 1 other error.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import naukrigulf_apply as NG                      # import only: loads config; nothing runs
from edge_cdp import CDPError

RESULT = NG.STATE_DIR / "naukrigulf_gentle_check.json"
LADDER_HOURS = (12, 24, 48)


class OffScreenChrome(NG.ChromeSession):
    """A real headful Chrome with its window parked off-screen: the check is taken with a genuine browser, not the headless mode in which
    ChromeSession rewrites the user agent and client hints (the approved plan: no fingerprint spoofing)."""

    def __init__(self, profile_dir, headless, exe):
        super().__init__(profile_dir, headless, exe)
        if not headless:
            self.extra_args = self.extra_args + ("--window-position=-32000,-32000",)


def read_result() -> dict:
    try:
        return json.loads(RESULT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def park(hours: int, why: str) -> None:
    until = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
    NG.marker("THROTTLED").write_text(json.dumps({"until": until, "what": why, "status": None}), encoding="utf-8")
    NG.marker("CHALLENGE").write_text(json.dumps({"until": until, "since": datetime.now().isoformat(timespec="seconds"), "what": why}),
                                      encoding="utf-8")


def write(**kw) -> None:
    prev = read_result()
    RESULT.write_text(json.dumps({**prev, **kw, "at": datetime.now().isoformat(timespec="seconds")}, indent=1), encoding="utf-8")


def main() -> int:
    for kind in ("THROTTLED", "CHALLENGE"):
        m = NG.marker(kind)
        if m.exists():
            try:
                until = datetime.fromisoformat(json.loads(m.read_text(encoding="utf-8"))["until"])
            except Exception:
                until = datetime.now() + timedelta(minutes=30)
            if datetime.now() < until:
                NG.log(f"gentle check: parked until {until:%d %b %H:%M} ({m.name}); not due yet")
                return 3
    blocks = int(read_result().get("blocks", 0))
    NG.ChromeSession = OffScreenChrome          # open_session builds its browser from this module-level name
    try:
        s, me = NG.open_session(True)
    except NG.Stop as e:
        NG.log(f"gentle check: {e.reason}", "WARN")
        write(ok=False, verdict="needs the user", reason=e.reason[:200])
        return e.code
    except CDPError as e:
        hours = LADDER_HOURS[min(blocks, len(LADDER_HOURS) - 1)]
        park(hours, f"gentle check {datetime.now():%Y-%m-%d %H:%M}: the site still cuts the automated browser's connection ({str(e)[:80]})")
        write(ok=False, verdict="still blocked", blocks=blocks + 1, reason=str(e)[:200], parked_hours=hours)
        NG.log(f"gentle check: still blocked ({str(e)[:80]}); parked {hours} h more", "WARN")
        return NG.EXIT_THROTTLED
    except Exception as e:
        NG.log(f"gentle check: {type(e).__name__}: {str(e)[:160]}", "ERROR")
        write(ok=False, verdict="error", reason=f"{type(e).__name__}: {str(e)[:160]}")
        return 1
    try:
        s.close()
    except Exception:
        pass
    for kind in ("THROTTLED", "CHALLENGE"):
        NG.marker(kind).unlink(missing_ok=True)
    write(ok=True, verdict="the browser works again", blocks=0, apply_ready=me.get("apply_ready"))
    NG.log("gentle check: the site answers the automated browser again; park markers removed. Next: a dry run with a small search list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
