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
    # RETIRED 2026-09-10 -- kept only so an old config still resolves. The live
    # uploader is the Linux one; running this copy would re-post its releases.
    "upload_dir":  r"D:\TG-UPLOAD",
    "scraper":     "/home/media/music-tools/telegram-post/tg-scraper",
    "scraper_ssh": "server",
    "drip_task":   "TG Upload Drip",
}
_NIX_DEFAULTS = {
    # 2026-09-10: the uploader MOVED here from the Windows box -- same account,
    # same files (over CIFS), 7x the speed (1.45 MB/s vs 0.21). The Windows
    # copy is retired; its state file still holds D:\ keys and must not run.
    "upload_dir":  "/home/media/music-tools/tg-upload",
    "scraper":     "/home/media/music-tools/telegram-post/tg-scraper",
    "scraper_ssh": "",                       # it is already local here
    # Windows drips the uploader from a scheduled task; here it is a systemd
    # USER unit. It must NOT be a plain child of this web UI: a process the
    # panel spawns lives in the web UI's cgroup, so restarting music-organiser
    # killed the upload mid-run (it did, on 2026-09-10). Its own unit also
    # gives it Restart=always and RequiresMountsFor= on the library mount.
    "drip_task":   "",
    "drip_script": "run.sh",
    "drip_unit":   "tg-upload.service",
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
                # Channel names are full of emoji and accents. text=True alone
                # decodes with the LOCALE codec, which on Windows is cp1252 and
                # raises UnicodeDecodeError inside the reader thread -- silently,
                # because it is a daemon thread, so the box just stayed empty.
                text=True, encoding="utf-8", errors="replace", bufsize=1,
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
        """Drain the child's output. Never let this thread die quietly -- an
        exception in here used to leave the UI showing an empty log for a job
        that had actually run fine."""
        assert self.proc and self.proc.stdout
        try:
            for line in self.proc.stdout:
                self._add(line)
        except Exception as exc:
            self._add("[output reader failed: %r]" % (exc,))
        try:
            self.proc.wait()
            self.rc = self.proc.returncode
        except Exception:
            self.rc = -1
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
        "review": (d / "needs_review.txt") if d else None,
        "drip":  (d / (st.get("drip_script") or "run.sh")) if d else None,
    }


def _lock_held(path) -> bool:
    """Is someone holding this flock? Exact, unlike grepping `ps`.

    The panel used to look for the uploader's ABSOLUTE path in `ps`, but a loop
    started from its own directory shows up as "bash ./run.sh" -- so the check
    said "nothing running" and started a second loop posting the same releases.
    """
    if os.name == "nt":
        return False
    try:
        import fcntl
        with open(path, "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    except Exception:
        return False


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
        "held": 0, "held_rows": [],
        "last": "", "last_error": "", "log_tail": [],
    }
    if not out["configured"]:
        return out

    if os.name == "nt":
        out["running"] = _proc_running("uploader.py")
        out["drip_up"] = False
    else:
        out["running"] = _lock_held(p["dir"] / ".uploader.lock")
        out["drip_up"] = _lock_held(p["dir"] / ".run.lock")
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

    # Folders the release rules held back: more tracks than one release can
    # honestly have, i.e. a container or a whole series dumped flat. They are
    # NOT posted and NOT marked done, so this list is work waiting on a human.
    try:
        if p["review"] and p["review"].exists():
            rows = [ln.rstrip() for ln in
                    p["review"].read_text(encoding="utf-8", errors="replace").splitlines()
                    if ln.strip() and not ln.startswith("#")]
            out["held"] = len(rows)
            out["held_rows"] = rows[:40]
    except Exception:
        pass

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
            # Linux: no scheduled task -- a systemd user unit runs the loop.
            # Removing the latch is enough if it is already up (it re-checks
            # every 30s).
            if _lock_held(p["dir"] / ".run.lock"):
                return True, "uploader enabled — the drip loop picks it up within 30s"
            unit = st.get("drip_unit") or ""
            if unit:
                r = subprocess.run(["systemctl", "--user", "start", unit],
                                   capture_output=True, text=True, timeout=25)
                if r.returncode == 0:
                    return True, "uploader started (%s)" % unit
                return False, (r.stderr or r.stdout or "systemctl failed").strip()
            return False, "no drip unit configured"
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


# ─── uploader settings, tools and one-shot actions ───────────────────────────
def _tool_dir(cfg: dict):
    st = settings(cfg)
    d = Path(st["upload_dir"]) if st["upload_dir"] else None
    return d if (d and d.exists()) else None


def _run_tool(cfg: dict, args: list[str], timeout: int = 900) -> tuple[bool, str]:
    """Run one of the uploader's own scripts and hand back what it printed.

    Everything the panel offers is a script you could run by hand in that
    folder, so there is exactly one implementation of each job.
    """
    d = _tool_dir(cfg)
    if not d:
        return False, "the uploader folder is not on this machine"
    try:
        r = subprocess.run([settings(cfg).get("python") or sys.executable] + args,
                           cwd=str(d), capture_output=True, text=True,
                           timeout=timeout)
        out = (r.stdout or "") + (("\n" + r.stderr) if r.returncode else "")
        return r.returncode == 0, out.strip()[-8000:]
    except subprocess.TimeoutExpired:
        return False, "timed out after %ss — it is probably still running" % timeout
    except Exception as exc:
        return False, str(exc)


def uploader_config(cfg: dict) -> dict:
    """The switches, straight out of uploader_config.json."""
    d = _tool_dir(cfg)
    if not d:
        return {}
    try:
        with open(d / "uploader_config.json", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        return {"_error": str(exc)}


def uploader_config_set(cfg: dict, changes: dict) -> tuple[bool, str]:
    """Change a switch. Values are coerced to the type of the current one, so
    a checkbox cannot turn a number into the string "true"."""
    d = _tool_dir(cfg)
    if not d:
        return False, "the uploader folder is not on this machine"
    path = d / "uploader_config.json"
    try:
        cur = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception as exc:
        return False, "current config unreadable: %s" % exc
    for k, v in (changes or {}).items():
        if k not in cur:
            continue
        old = cur[k]
        try:
            if isinstance(old, bool):
                v = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "on", "yes")
            elif isinstance(old, int) and not isinstance(old, bool):
                v = int(v)
            elif isinstance(old, float):
                v = float(v)
            else:
                v = "" if v is None else str(v)
        except (TypeError, ValueError):
            return False, "%s: %r is not a %s" % (k, v, type(old).__name__)
        cur[k] = v
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(json.dumps(cur, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return True, "saved — the next release picks it up"


def uploader_tools(cfg: dict, what: str) -> tuple[bool, str]:
    """Account, permissions and topic checks (tgtools.py)."""
    wanted = {"account": ["whoami"], "permissions": ["permissions"],
              "topics": ["topics"], "spam": ["spam"],
              "all": ["whoami", "permissions", "topics"]}.get(what)
    if not wanted:
        return False, "unknown check: %r" % what
    return _run_tool(cfg, ["tgtools.py"] + wanted, timeout=300)


ACTIONS = {
    # name          -> (argv, needs the uploader stopped?)
    "decide_tabs":   (["uploader.py", "--decide-tabs"], False),
    "decide_posted": (["rehome.py", "--decide"], True),
    "rehome_dry":    (["rehome.py"], True),
    "rehome_apply":  (["rehome.py", "--apply"], True),
    "seed_sent":     (["rehome.py", "--seed-sent"], True),
    "verify":        (["rehome.py", "--verify"], True),
    "report":        (["uploader.py", "--report"], False),
}


def uploader_action(cfg: dict, name: str) -> tuple[bool, str]:
    entry = ACTIONS.get(name)
    if not entry:
        return False, "unknown action: %r" % name
    argv, needs_stop = entry
    d = _tool_dir(cfg)
    if needs_stop and d and _lock_held(d / ".uploader.lock"):
        return False, ("stop the uploader first — this one edits the state or "
                       "the session that a running upload is holding")
    return _run_tool(cfg, argv, timeout=1800)


def uploader_retry_failed(cfg: dict) -> tuple[bool, str]:
    """Clear the `failed` map so the next run re-attempts those releases.

    They are only in `failed` (never in `done`), so emptying it is enough to
    put them back in the queue; nothing gets double-posted.
    """
    st = settings(cfg)
    p = _upload_paths(st)
    if not (p["state"] and p["state"].exists()):
        return False, "no uploader_state.json"
    if _lock_held(p["dir"] / ".uploader.lock") if os.name != "nt" \
            else _proc_running("uploader.py"):
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
        "settings": uploader_config(cfg),
    }
