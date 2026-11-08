"""Minimal driverless Edge session over the Chrome DevTools Protocol.

Launches msedge.exe on a persistent profile with a DevTools port and drives the page over a
websocket. There is no WebDriver: on 2026-09-30 an Edge auto-update (to 154) broke every
Selenium-based bot here with a driver/browser version mismatch. A bare DevTools connection has
no driver to mismatch.

Used by instahyre_apply.py (Edge) and naukrigulf_apply.py (Chrome, via the optional `exe`
argument: a Chrome profile can only be opened by chrome.exe). Pages run in the profile's saved,
logged-in session; nothing here types credentials.
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, time as dtime
from pathlib import Path

import websocket

EDGE_EXE = next((p for p in (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
) if os.path.exists(p)), "msedge.exe")

# Unfocused automation windows throttle timers to ~1/min, which hangs awaited page JS (seen
# 2026-09-28 on these same profiles); sync/extensions would pull the user's Microsoft account in.
BASE_FLAGS = (
    "--no-first-run", "--no-default-browser-check", "--disable-sync", "--disable-extensions",
    "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion",
    "--window-size=1600,1000",
)


class CDPError(RuntimeError):
    pass


class WindowClosed(RuntimeError):
    """A new Gulf browser was asked to start outside the all-applier daily window."""


# The all-applier daily window: a new Gulf browser starts only inside it, 04:00-10:00 IST on this PC's clock.
# GULF_APPLY_WINDOW="HH:MM-HH:MM" overrides it; otherwise job-tracker's appliers_window.txt (start=/end=) is read.
WINDOW_DEFAULT = ("04:00", "10:00")


def _hhmm(text: str) -> bool:
    try:
        h, m = text.split(":")
        return 0 <= int(h) < 24 and 0 <= int(m) < 60
    except (AttributeError, ValueError):
        return False


def applier_window() -> tuple[str, str]:
    """(start, end) of the window as "HH:MM". In order: GULF_APPLY_WINDOW, an appliers_window.txt beside this
    file or beside its parent (job-tracker's start=/end= file), else 04:00-10:00. A malformed value falls
    through to the next source."""
    env = os.environ.get("GULF_APPLY_WINDOW", "").replace("start=", "").replace("end=", "")
    start, sep, end = env.partition("-")
    if sep and _hhmm(start.strip()) and _hhmm(end.strip()) and start.strip() != end.strip():
        return start.strip(), end.strip()
    for base in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent):
        try:
            lines = (base / "appliers_window.txt").read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        found = {}
        for line in lines:
            key, _, value = line.strip().partition("=")
            if key in ("start", "end"):
                found[key] = value.strip()
        if _hhmm(found.get("start", "")) and _hhmm(found.get("end", "")) and found["start"] != found["end"]:
            return found["start"], found["end"]
    return WINDOW_DEFAULT


def in_applier_window(now: datetime | None = None) -> bool:
    """True when a new Gulf browser may start now (start <= now < end, this PC's clock = IST)."""
    start, end = (dtime.fromisoformat(t) for t in applier_window())
    return start <= (now or datetime.now()).time() < end


def free_ram_mb() -> int | None:
    """Available physical memory in MB (Task Manager's 'Available'), or None if unreadable."""
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = _MemStatus()
        st.dwLength = ctypes.sizeof(_MemStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        return int(st.ullAvailPhys // (1024 * 1024))
    except Exception:
        return None


def wait_for_ram(min_mb: int = 1024, max_min: float = 10, poll_sec: float = 30) -> str | None:
    """Bounded wait for free memory before starting a browser (same rule as the LinkedIn supervisor).
    2026-10-08: Edge starts failed while 0.5 GB was free and 5+ browser bots ran at once. Returns a
    line for the caller's log when it had to wait, else None. Never an open loop."""
    free = free_ram_mb()
    if free is None or free >= min_mb:
        return None
    start = time.time()
    deadline = start + max_min * 60
    while time.time() < deadline:
        time.sleep(poll_sec)
        free = free_ram_mb()
        if free is None or free >= min_mb:
            return f"waited {time.time() - start:.0f}s for free RAM (now {free} MB)"
    return f"free RAM still {free} MB after {max_min} min; starting anyway (bounded wait)"


class EdgeSession:
    def __init__(self, profile_dir: str | Path, headless: bool = True, extra_args=(), exe: str | Path | None = None):
        self.profile = Path(profile_dir)
        self.headless = headless
        self.extra_args = tuple(extra_args)
        self.exe = str(exe or EDGE_EXE)
        self.proc: subprocess.Popen | None = None
        self.ws = None
        self.port = None
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------ lifecycle
    def start(self, timeout: float = 90) -> "EdgeSession":
        if not in_applier_window():
            start, end = applier_window()
            raise WindowClosed(f"no new Gulf browser outside {start}-{end} IST (now {datetime.now():%H:%M})")
        self.profile.mkdir(parents=True, exist_ok=True)
        port_file = self.profile / "DevToolsActivePort"
        try:
            port_file.unlink()
        except OSError:
            pass
        args = [self.exe, f"--user-data-dir={self.profile}", "--remote-debugging-port=0",
                *BASE_FLAGS, *self.extra_args]
        if self.headless:
            args.append("--headless=new")
        args.append("about:blank")
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    self.port = int(port_file.read_text(encoding="ascii").splitlines()[0])
                    break
                except (OSError, ValueError, IndexError):
                    if self.proc.poll() is not None:
                        raise CDPError(f"Edge exited with code {self.proc.returncode} before opening DevTools "
                                       f"(profile in use by another Edge?)")
                    time.sleep(0.5)
            if not self.port:
                raise CDPError(f"Edge did not open a DevTools port within {timeout:.0f}s")
            page = None
            while time.time() < deadline and page is None:
                try:
                    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=10))
                    page = next((t for t in targets if t.get("type") == "page"), None)
                except OSError:
                    pass
                if page is None:
                    time.sleep(0.5)
            if page is None:
                raise CDPError("no page target in the new Edge")
        except CDPError:
            # 2026-10-08: a failed start used to leave this Edge running. On 10-07 it held the profile
            # for 24 h, and every later start exited with code 21 ("profile in use").
            self._kill_own()
            raise
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
        self.call("Page.enable")
        self.call("Runtime.enable")
        # Headless Edge advertises "HeadlessEdg"; present the normal user agent (Cloudflare fronts
        # instahyre.com and challenges non-browser clients).
        ua = str(self.eval("navigator.userAgent")).replace("Headless", "")
        self.call("Network.setUserAgentOverride", {"userAgent": ua})
        return self

    def close(self) -> None:
        try:
            if self.ws is not None and self.port:
                ver = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=5))
                b = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=10, suppress_origin=True)
                b.send(json.dumps({"id": 1, "method": "Browser.close"}))
                b.close()
        except Exception:
            pass
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _kill_own(self) -> None:
        """Kill the browser this session launched (its whole process tree), if still running."""
        if self.proc is not None and self.proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ protocol
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
        raise CDPError(f"{method}: no reply within {timeout:.0f}s")

    def eval(self, expression: str, await_promise: bool = False, timeout: float = 60):
        res = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                             "awaitPromise": await_promise}, timeout=timeout)
        if res.get("exceptionDetails"):
            detail = res["exceptionDetails"]
            text = (detail.get("exception") or {}).get("description") or detail.get("text")
            raise CDPError(f"page JS error: {str(text)[:300]}")
        return (res.get("result") or {}).get("value")

    def navigate(self, url: str, timeout: float = 60) -> str:
        res = self.call("Page.navigate", {"url": url})
        if res.get("errorText"):
            raise CDPError(f"{url}: navigation failed ({res['errorText']})")
        deadline = time.time() + timeout
        time.sleep(1.0)
        last = ""
        while time.time() < deadline:
            try:
                last = str(self.eval("location.href"))
                # 2026-10-08: about:blank already reports readyState "complete" before the navigation
                # commits, so this used to return at once and callers read "blank" as signed out.
                if last.startswith("http") and self.eval("document.readyState") in ("interactive", "complete"):
                    return last
            except CDPError:
                pass
            time.sleep(0.5)
        raise CDPError(f"{url} did not load within {timeout:.0f}s (still on {last or 'nothing'})")

    def fetch_json(self, path: str, method: str = "GET", body: dict | None = None, timeout: float = 60) -> dict:
        """Same-origin fetch from inside the logged-in page. POSTs carry Django's CSRF header the
        same way Instahyre's own AngularJS app does (csrftoken cookie -> X-CSRFToken)."""
        js = f"""(async () => {{
            const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
            const opts = {{method: {json.dumps(method)}, credentials: 'same-origin',
                          headers: {{'Accept': 'application/json, text/plain, */*',
                                     'X-Requested-With': 'XMLHttpRequest'}}}};
            if ({json.dumps(method)} !== 'GET') {{
                opts.headers['Content-Type'] = 'application/json;charset=UTF-8';
                if (m) opts.headers['X-CSRFToken'] = decodeURIComponent(m[1]);
                opts.body = {json.dumps(json.dumps(body or {}))};
            }}
            const r = await fetch({json.dumps(path)}, opts);
            const text = await r.text();
            let data = null; try {{ data = JSON.parse(text); }} catch (e) {{}}
            return JSON.stringify({{status: r.status, retryAfter: r.headers.get('retry-after'),
                                    data: data, text: data === null ? text.slice(0, 300) : null}});
        }})()"""
        return json.loads(self.eval(js, await_promise=True, timeout=timeout))
