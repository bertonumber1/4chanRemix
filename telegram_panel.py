"""telegram_panel.py — control surface for the Telegram tools.

Two jobs sit either side of the library and until now had no shared face:

    INBOUND   tg-scraper      pulls lossless out of channels
    OUTBOUND  TG-UPLOAD       posts the library into our own group

Both are driven from a terminal on whichever box they happen to live on. This
module puts them behind the web UI so they can be watched and switched from a
phone, and is written so a machine that has only one of them (or neither) still
loads the tab and says plainly what is missing rather than erroring.

Nothing here talks to Telegram directly. It shells out to the same scripts you
would run by hand, so there is exactly one implementation of the actual work.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

# ─── where the tools live ─────────────────────────────────────────────────────
# Defaults per platform; every one can be overridden in config.toml [telegram].
# The two tools live on different boxes: the uploader runs on Windows (next to
# the drive it posts from), the scraper on Linux (next to the rest of
# music-tools). Rather than leave each machine's panel controlling only half,
# `scraper_ssh` lets the Windows UI run the scraper over ssh on the Linux box,
# so either panel is a full control surface.
_WIN_DEFAULTS = {
    "upload_dir":  r"D:\TG-UPLOAD",
    "scraper":     "/home/media/music-tools/telegram-post/tg-scraper",
    "scraper_ssh": "server",
    "drip_task":   "TG Upload Drip",
}
_NIX_DEFAULTS = {
    "upload_dir":  "",                       # the uploader is Windows-side
    "scraper":     "/home/media/music-tools/telegram-post/tg-scraper",
    "scraper_ssh": "",                       # it is already local here
    "drip_task":   "",
}

# A scraper run can be long; keep a bounded tail rather than growing forever.
_MAX_LINES = 4000


def defaults() -> dict:
    return dict(_WIN_DEFAULTS if os.name == "nt" else _NIX_DEFAULTS)


def settings(cfg: dict) -> dict:
    """[telegram] from config.toml, with per-platform defaults filled in."""
    out = defaults()
    tg = (cfg or {}).get("telegram") or {}
    for k in list(out):
        v = tg.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    out["python"] = (tg.get("python") or "").strip() or sys.executable
    return out


# ─── one job slot, with a tail of output ──────────────────────────────────────
class Job:
    """A single running scraper command.

    One at a time on purpose: two concurrent Telethon clients on the same
    .session file fight over the SQLite lock and whichever loses dies, which is
    exactly the failure the scraper's own docstring warns about.
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lines: deque[str] = deque(maxlen=_MAX_LINES)
        self.seq = 0                 # monotonic line counter for polling
        self.label = ""
        self.started = 0.0
        self.rc: int | None = None
        self._lock = threading.Lock()

    # -- state -----------------------------------------------------------
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def snapshot(self, since: int = 0) -> dict:
        # If the process is gone but the pump thread has not recorded rc yet,
        # report the real exit code rather than a misleading "still starting".
        rc = self.rc
        if rc is None and self.proc is not None:
            rc = self.proc.poll()
        with self._lock:
            first = self.seq - len(self.lines)
            start = max(0, since - first)
            return {
                "running": self.running(),
                "label": self.label,
                "rc": rc,
                "seq": self.seq,
                "elapsed": int(time.time() - self.started) if self.started else 0,
                "lines": list(self.lines)[start:],
            }

    def _add(self, line: str) -> None:
        with self._lock:
            self.lines.append(line.rstrip("\n"))
            self.seq += 1

    # -- control ---------------------------------------------------------
    def start(self, argv: list[str], cwd: str | None, label: str) -> tuple[bool, str]:
        if self.running():
            return False, "a job is already running"
        with self._lock:
            self.lines.clear()
            self.seq = 0
            self.rc = None
            self.label = label
            self.started = time.time()

        env = dict(os.environ)
        # Without this a piped child buffers everything and the UI shows an
        # empty box until the process ends -- the exact way the first upload
        # speed test lost all of its output.
        env["PYTHONUNBUFFERED"] = "1"
        try:
            self.proc = subprocess.Popen(
                argv, cwd=cwd or None, env=env,
                stdin=subprocess.DEVNULL,       # a service has no usable stdin
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except Exception as exc:
            self._add("could not start: %s" % exc)
            self.rc = -1
            return False, str(exc)

        self._add("$ " + " ".join(argv))
        threading.Thread(target=self._pump, daemon=True).start()
        return True, "started"

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self._add(line)
        self.proc.wait()
        self.rc = self.proc.returncode
        self._add("--- finished, exit %s ---" % self.rc)

    def stop(self) -> tuple[bool, str]:
        if not self.running():
            return False, "nothing running"
        try:
            self.proc.terminate()       # type: ignore[union-attr]
            return True, "stopping"
        except Exception as exc:
            return False, str(exc)


JOB = Job()


# ─── inbound: tg-scraper ──────────────────────────────────────────────────────
def scraper_argv(st: dict, mode: str, channel: str, opts: dict) -> list[str]:
    argv = [st["scraper"], mode] if st["scraper_ssh"] else [st["python"], st["scraper"], mode]
    if mode in ("scan", "download"):
        argv.append(channel)
    if mode == "list" and channel:
        argv.append(channel)
    if mode == "download":
        if opts.get("dry_run"):
            argv.append("--dry-run")
        if opts.get("oldest"):
            argv.append("--oldest")
        if opts.get("archives"):
            argv.append("--archives")
        if opts.get("limit"):
            argv += ["--limit", str(int(opts["limit"]))]
        if opts.get("max_gb"):
            argv += ["--max-gb", str(float(opts["max_gb"]))]
    if st["scraper_ssh"]:
        # -n (stdin from /dev/null) and BatchMode: the UI runs as a service with
        # no usable stdin, and `ssh -tt` there dies before it produces a single
        # line of output. BatchMode also turns a missing key into an immediate
        # error instead of a password prompt nobody can answer.
        argv = ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                st["scraper_ssh"]] + argv
    return argv


def run_scraper(cfg: dict, mode: str, channel: str, opts: dict) -> tuple[bool, str]:
    st = settings(cfg)
    if mode not in ("list", "scan", "download"):
        return False, "unknown mode %r" % mode
    if mode in ("scan", "download") and not channel.strip():
        return False, "give a channel id, @username, or part of its name"
    scraper = st["scraper"]
    if st["scraper_ssh"]:
        if not shutil.which("ssh"):
            return False, "ssh not found — needed to reach %s" % st["scraper_ssh"]
        cwd = None                       # the remote side has its own cwd
    else:
        if not scraper or not Path(scraper).exists():
            return False, "tg-scraper not found at %r - set telegram.scraper in config" % scraper
        cwd = str(Path(scraper).parent)
    argv = scraper_argv(st, mode, channel.strip(), opts)
    label = "%s %s" % (mode, channel.strip() or "")
    return JOB.start(argv, cwd, label.strip())


# ─── outbound: the TG-UPLOAD drip ─────────────────────────────────────────────
def _upload_paths(st: dict) -> dict:
    d = Path(st["upload_dir"]) if st["upload_dir"] else None
    return {
        "dir":   d,
        "stop":  (d / "STOP") if d else None,
        "state": (d / "uploader_state.json") if d else None,
        "log":   (d / "uploader.log") if d else None,
    }


def _proc_running(needle: str) -> bool:
    """Is a python process running <needle>? Cheap and platform-specific."""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["wmic", "process", "where", "name like '%python%'", "get", "commandline"],
                capture_output=True, text=True, timeout=15).stdout
        else:
            out = subprocess.run(["ps", "-eo", "args"],
                                 capture_output=True, text=True, timeout=15).stdout
        return needle.lower() in out.lower()
    except Exception:
        return False


def _task_state(name: str) -> str:
    if not name or os.name != "nt":
        return "n/a"
    try:
        out = subprocess.run(["schtasks", "/query", "/tn", name, "/fo", "list"],
                             capture_output=True, text=True, timeout=15).stdout
        for line in out.splitlines():
            if line.lower().startswith("status:"):
                return line.split(":", 1)[1].strip()
        return "unknown"
    except Exception:
        return "unknown"


def uploader_status(cfg: dict) -> dict:
    st = settings(cfg)
    p = _upload_paths(st)
    out = {
        "configured": bool(p["dir"] and p["dir"].exists()),
        "dir": str(p["dir"]) if p["dir"] else "",
        "running": False,
        "stopped_by_latch": False,
        "task": _task_state(st["drip_task"]),
        "task_name": st["drip_task"],
        "posted": 0, "files": 0, "failed": 0, "total": 0,
        "last": "", "last_error": "", "log_tail": [],
    }
    if not out["configured"]:
        return out

    out["running"] = _proc_running("uploader.py")
    out["stopped_by_latch"] = bool(p["stop"] and p["stop"].exists())

    try:
        if p["state"] and p["state"].exists():
            d = json.loads(p["state"].read_text(encoding="utf-8"))
            out["posted"] = int(d.get("posted") or 0)
            out["files"] = int(d.get("files") or 0)
            out["failed"] = len(d.get("failed") or {})
            done = d.get("done") or {}
            if done:
                out["last"] = Path(list(done)[-1]).name
    except Exception as exc:
        out["last_error"] = "state unreadable: %s" % exc

    try:
        if p["log"] and p["log"].exists():
            tail = p["log"].read_text(encoding="utf-8", errors="replace").splitlines()[-14:]
            out["log_tail"] = tail
            for line in reversed(tail):
                # the run header carries the real denominator
                if "to post:" in line:
                    try:
                        out["total"] = int(line.split("to post:")[1].strip().split()[0])
                    except Exception:
                        pass
                    break
                if line.strip().startswith("FAIL") and not out["last_error"]:
                    out["last_error"] = line.strip()[:200]
    except Exception:
        pass
    return out


def uploader_set(cfg: dict, run: bool) -> tuple[bool, str]:
    """The one switch.

    OFF writes the STOP latch and disables the drip task. It does NOT kill the
    process: state is saved only after every file in a release lands, so a kill
    mid-release re-posts those tracks on resume. The running job notices the
    latch at its next release boundary and exits cleanly by itself.

    ON removes the latch and re-enables the task.
    """
    st = settings(cfg)
    p = _upload_paths(st)
    if not (p["dir"] and p["dir"].exists()):
        return False, "TG-UPLOAD not found at %r" % st["upload_dir"]

    task = st["drip_task"]
    try:
        if run:
            if p["stop"] and p["stop"].exists():
                p["stop"].unlink()
            if task and os.name == "nt":
                subprocess.run(["schtasks", "/change", "/tn", task, "/enable"],
                               capture_output=True, text=True, timeout=20)
                subprocess.run(["schtasks", "/run", "/tn", task],
                               capture_output=True, text=True, timeout=20)
            return True, "uploader enabled"
        else:
            p["stop"].write_text(                      # type: ignore[union-attr]
                "paused from the web UI %s\n" % time.strftime("%Y-%m-%d %H:%M"),
                encoding="utf-8")
            if task and os.name == "nt":
                subprocess.run(["schtasks", "/change", "/tn", task, "/disable"],
                               capture_output=True, text=True, timeout=20)
            return True, "stop latch set — it will finish the current release, then exit"
    except Exception as exc:
        return False, str(exc)


def uploader_retry_failed(cfg: dict) -> tuple[bool, str]:
    """Clear the `failed` map so the next run re-attempts those releases.

    They are only in `failed` (never in `done`), so emptying it is enough to
    put them back in the queue; nothing gets double-posted.
    """
    st = settings(cfg)
    p = _upload_paths(st)
    if not (p["state"] and p["state"].exists()):
        return False, "no uploader_state.json"
    if _proc_running("uploader.py"):
        return False, "stop the uploader first — it rewrites this file"
    try:
        d = json.loads(p["state"].read_text(encoding="utf-8"))
        n = len(d.get("failed") or {})
        if not n:
            return True, "nothing was failed"
        d["failed"] = {}
        tmp = str(p["state"]) + ".tmp"
        Path(tmp).write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, p["state"])
        return True, "re-queued %d failed release(s)" % n
    except Exception as exc:
        return False, str(exc)


def status(cfg: dict, since: int = 0) -> dict:
    st = settings(cfg)
    return {
        "platform": "windows" if os.name == "nt" else ("macos" if sys.platform == "darwin" else "linux"),
        "paths": {
            "scraper": (st["scraper_ssh"] + ":" + st["scraper"]) if st["scraper_ssh"] else st["scraper"],
            "scraper_ok": bool(shutil.which("ssh")) if st["scraper_ssh"]
                          else bool(st["scraper"] and Path(st["scraper"]).exists()),
            "upload_dir": st["upload_dir"],
            "python": st["python"],
            "drip_task": st["drip_task"],
        },
        "job": JOB.snapshot(since),
        "uploader": uploader_status(cfg),
    }
