# gulf-autoapply

Gulf job tooling: two appliers, a gentle re-check, and a read-only job digest. Windows, Python 3.11 or newer.

Every applier runs as a **dry run by default**. Live applying requires both `--live` and an arming environment variable, and nothing in this repository sets either.

## Tools

| File | Purpose |
|---|---|
| `naukrigulf_apply.py` | NaukriGulf Easy Apply: finds Gulf data jobs that pass the filters, dry-run by default; `--live` presses the site's own Easy Apply button |
| `naukrigulf_gentle_check.py` | one gentle browser check after a block: extends the park or clears the markers |
| `ats_apply.py` | employer ATS forms (Workable adapter): fills a form in a dry run, stops before Submit; `--live` submits |
| `gulf_digest.py` | fetches public agency and employer-board listings and writes a shortlist no bot applies to |
| `edge_cdp.py` | minimal Edge/Chrome DevTools Protocol driver used by the appliers |

Exit codes follow one contract: `0` finished, `44` daily cap, `42` needs the user, `47` throttled or machine-starved, `48` challenge, `3` live not armed, `1` error.

## Run window

A new browser starts only between **04:00 and 10:00 IST** (this PC's clock). `edge_cdp.EdgeSession.start` refuses outside that window with `WindowClosed`. The window comes from `GULF_APPLY_WINDOW="HH:MM-HH:MM"`, or from `appliers_window.txt` (`start=` / `end=`) beside `edge_cdp.py` or beside its parent folder; with neither it stays 04:00-10:00.

## Setup

```bash
pip install -r requirements.txt
cp naukrigulf_config.example.json naukrigulf_config.json
cp ats_apply_config.example.json ats_apply_config.json
cp gulf_config.example.json gulf_config.json
```

Then fill the paths and account fields in your local copies. The three real config files, `state/`, `logs/`, browser profiles, databases, resumes and screenshots are git-ignored.

Each tool also accepts a config path through an environment variable: `NAUKRIGULF_CONFIG`, `ATS_APPLY_CONFIG` or `GULF_CONFIG`. With no local config the example file is read, so `--help` and `--list` work on a fresh clone.

### Dry runs

```bash
python naukrigulf_apply.py --dry-run
python ats_apply.py --list
python gulf_digest.py
```

`naukrigulf_apply.py` writes `state/naukrigulf_dryrun_<date>.json` and `.md`; it never clicks Apply in a dry run.

### Live runs

Live applying is refused unless the matching variable is set in the same process:

```powershell
$env:NAUKRIGULF_APPLY_LIVE = "1"; python naukrigulf_apply.py --live
$env:ATS_APPLY_LIVE = "1"; python ats_apply.py --live
```

`ats_apply.py` uses `portal_kit/facts.py` as a closed world for screening questions. A required question the resolver cannot answer exactly is skipped and recorded, never guessed. The facts file lives outside the repository (`~/.secrets/gulf_facts.json` or `$GULF_FACTS`).

## Layout

```text
portal_kit/
  dedupe.py    never apply twice to the same opening
  facts.py     screening answers from a facts file outside the repo
state/         databases, progress and markers (git-ignored)
logs/          run logs (git-ignored)
```

## Tests

Continuous integration runs `python -m py_compile` on every Python file. There are no unit tests; each tool is verified through real dry runs.

## Licence

MIT. See `LICENSE`.
