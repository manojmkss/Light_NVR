#!/usr/bin/env python3
"""LightNVR guardian - watches the NVR from outside Docker, recovers it, and
keeps the host's disk from filling.

Runs on the host as root (systemd: lightnvr-guardian.service, installed and
kept current by install-linux.sh / update-linux.sh), so it keeps working when
the NVR app is hung or dead. Python standard library only.

Every minute it:
- checks each container (running, healthy), the backend's /api/health, the
  Docker daemon, whether continuous cameras are still producing recordings,
  and the disks that matter;
- recovers, one step at a time with a pause after each: restart the failing
  container (Docker itself never restarts an "unhealthy" one), then recreate
  the stack, then restart Docker. It never reboots the machine - when nothing
  helps it says so and keeps retrying, slowly;
- when the NVR has been down for 5 minutes, sends Telegram itself (the NVR's
  own alerts are down with it), and again when it's back.

Once a day (03:30), or whenever a disk passes 90%, it cleans host waste: the
systemd journal, the apt cache, Docker images and build cache nothing uses,
and old /tmp files. If the NVR is down AND the recordings disk reaches 97%,
it deletes the oldest recordings itself (never the last 24 hours) - the NVR
drops their rows when it's back. While the NVR is up, its own free-space
floor does that job and the guardian leaves recordings alone.

It talks to the NVR only through files in <repo>/storage/guardian (the
container's /storage/guardian) - see backend/app/services/housekeeping.py.
To stop the NVR on purpose without the guardian starting it again, either
`systemctl stop lightnvr-guardian` or write a time to storage/guardian/paused-until
(update-linux.sh does that for the length of an update).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime

log = logging.getLogger("lightnvr-guardian")

CHECK_SECONDS = 60
FAILS_BEFORE_ACTION = 3          # consecutive bad checks before doing anything
GRACE_AFTER_ACTION = 180         # let a restart settle before judging again
RESTARTS_PER_HOUR = 3            # then recreate the stack instead
BACKOFF_SECONDS = 900            # when nothing has helped: retry this often
DOCKER_RESTART_EVERY = 1800      # at most one Docker restart per 30 minutes
NO_RECORDINGS_SECONDS = 15 * 60  # a continuous camera with no new file this long is stuck
ALL_STUCK_RESTART_EVERY = 2 * 3600
TELEGRAM_AFTER = 300             # NVR down this long -> tell the user directly
TELEGRAM_REMIND = 6 * 3600
DISK_CLEAN_PERCENT = 90
DISK_EMERGENCY_PERCENT = 97
DISK_EMERGENCY_TARGET = 95
KEEP_NEWEST_SECONDS = 24 * 3600  # emergency deletes never touch the last day
HOST_CLEAN_MIN_GAP = 3600
DAILY_AT = (3, 30)
TMP_MAX_AGE = 7 * 86400
MAX_PAUSE = 2 * 3600             # a pause marker a crashed update left behind expires
CORE = ("lightnvr-backend", "lightnvr-frontend", "lightnvr-nginx")
OWN_IMAGES = ("ghcr.io/manojmkss/light_nvr-", "ollama/ollama:")


def _now() -> float:
    return time.time()


def _clock(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"


def disk_percent(path: str) -> tuple[float, int, int]:
    """(percent used as df shows it, total bytes, available bytes)."""
    st = os.statvfs(path)
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    total = st.f_blocks * st.f_frsize
    return (100.0 * used / (used + avail) if used + avail else 0.0), total, avail


def read_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


class Guardian:
    def __init__(self, repo: str, state_dir: str, run=None, clock=None, http_ok=None, send_telegram=None,
                 disk=None):
        self.repo = os.path.abspath(repo)
        self.state_dir = state_dir
        self.bridge = os.path.join(self.repo, "storage", "guardian")
        self.run = run or self._run
        self.clock = clock or _now
        self.http_ok = http_ok or self._http_ok
        self.send_telegram = send_telegram or self._send_telegram
        self.disk = disk or disk_percent
        self.tmp_root = "/tmp"
        self.state = self._load()
        self.fails: dict[str, int] = {}
        self.recoveries: list[dict] = self.state.get("recoveries", [])
        self._expected: tuple[float, list[str]] = (0.0, list(CORE))
        self._telegram: tuple[float, tuple[str, str] | None] = (0.0, None)

    # ── plumbing ────────────────────────────────────────────────────────────

    def _run(self, args: list[str], timeout: int = 60, env: dict | None = None) -> tuple[int, str]:
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=self.repo,
                               env={**os.environ, **(env or {})})
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, "timed out"
        except OSError as exc:
            return 127, str(exc)

    def _http_ok(self, url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _load(self) -> dict:
        try:
            with open(os.path.join(self.state_dir, "state.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.state["recoveries"] = self.recoveries[-30:]
        self._write_json(os.path.join(self.state_dir, "state.json"), self.state)

    @staticmethod
    def _write_json(path: str, data, mode: int = 0o644) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("Could not write %s: %s", path, exc)

    def env(self) -> dict[str, str]:
        return read_env(os.path.join(self.repo, ".env"))

    def expect(self) -> dict:
        try:
            path = os.path.join(self.bridge, "expect.json")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data["_age"] = self.clock() - os.path.getmtime(path)
            return data
        except (OSError, ValueError):
            return {}

    def paused(self) -> bool:
        try:
            with open(os.path.join(self.bridge, "paused-until"), encoding="utf-8") as f:
                until = float(f.read().strip() or 0)
        except (OSError, ValueError):
            return False
        return self.clock() < min(until, os.path.getmtime(os.path.join(self.bridge, "paused-until")) + MAX_PAUSE)

    def tell(self, message: str, kind: str, notify: bool = True, **extra) -> None:
        """A message for the NVR: shown as an event and sent as an alert."""
        now = self.clock()
        name = f"{int(now * 1000)}-{kind}.json"
        self._write_json(os.path.join(self.bridge, "outbox", name),
                         {"message": message, "kind": kind, "at": now, "notify": notify, **extra})

    def record(self, what: str, why: str, ok: bool, **extra) -> None:
        entry = {"at": self.clock(), "what": what, "why": why, "ok": ok}
        self.recoveries.append(entry)
        log.warning("Recovery: %s (%s) - %s", what, why, "done" if ok else "FAILED")
        # While the NVR is down its alerts are too: the direct Telegram covers it.
        self.tell(f"Guardian: {what} - {why}." + ("" if ok else " That didn't work."), "recovery",
                  notify=self.state.get("down_since") is None, **extra)

    def _recent(self, prefix: str, seconds: float) -> list[dict]:
        cutoff = self.clock() - seconds
        return [r for r in self.recoveries if r["at"] >= cutoff and r["what"].startswith(prefix)]

    # ── checks ──────────────────────────────────────────────────────────────

    def docker_ok(self) -> bool:
        rc, _ = self.run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
        return rc == 0

    def expected_containers(self) -> list[str]:
        """The containers the current compose files define (so one an update
        removed - the describer, say - is not 'missing')."""
        at, names = self._expected
        if self.clock() - at < 600:
            return names
        rc, out = self.run(["docker", "compose", "config", "--format", "json"], timeout=30)
        if rc == 0 and "{" in out:
            try:
                config, _ = json.JSONDecoder().raw_decode(out[out.index("{"):])
                found = [s.get("container_name") for s in (config.get("services") or {}).values()
                         if s.get("container_name")]
                if found:
                    names = found
            except (ValueError, AttributeError):
                pass
        self._expected = (self.clock(), names)
        return names

    def containers(self) -> dict[str, dict]:
        rc, out = self.run(["docker", "ps", "-a", "--filter", "name=lightnvr-",
                            "--format", "{{.Names}}|{{.State}}|{{.Status}}|{{.Image}}"], timeout=30)
        found = {}
        if rc == 0:
            for line in out.splitlines():
                parts = line.strip().split("|")
                if len(parts) == 4:
                    name, state, status, image = parts
                    health = ("unhealthy" if "(unhealthy)" in status else
                              "starting" if "health: starting" in status else
                              "healthy" if "(healthy)" in status else "")
                    found[name] = {"state": state, "health": health, "image": image}
        return found

    def backend_url(self) -> str:
        return f"http://127.0.0.1:{self.env().get('BACKEND_PORT') or 8000}/api/health"

    def problem(self, name: str, info: dict | None, backend_ok: bool) -> str | None:
        if info is None:
            return "missing"
        if info["state"] in ("exited", "dead", "created"):
            return "stopped"
        if info["state"] == "restarting":
            return "crashing"
        if info["health"] == "unhealthy":
            return "unhealthy"
        if name == "lightnvr-backend" and info["state"] == "running" and info["health"] != "starting" \
                and not backend_ok:
            return "not answering"
        return None

    def recordings_stuck(self, exp: dict) -> list[int]:
        """Continuous cameras the NVR sees online but with no new recording
        for a while (as its database sees it - expect.json, every 30 s). An
        offline camera is the camera's problem, not something a restart fixes."""
        if not exp or exp.get("_age", 1e9) > 300:
            return []
        last = exp.get("last_recording") or {}
        online = set(exp.get("online_cameras") or exp.get("continuous_cameras") or [])
        now = self.clock()
        return [c for c in exp.get("continuous_cameras") or [] if c in online
                and now - float(last.get(str(c)) or exp.get("at") or now) > NO_RECORDINGS_SECONDS]

    # ── recovery ────────────────────────────────────────────────────────────

    def compose_env(self, containers: dict) -> dict:
        """Recreate on the version that's running, not on whatever `latest` is."""
        if self.env().get("LIGHTNVR_VERSION"):
            return {}
        image = (containers.get("lightnvr-backend") or {}).get("image", "")
        tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
        return {"LIGHTNVR_VERSION": tag} if tag and tag != "latest" else {}

    def compose_up(self, containers: dict, why: str) -> bool:
        rc, out = self.run(["docker", "compose", "up", "-d", "--no-build", "--pull", "never"], timeout=300,
                           env=self.compose_env(containers))
        if rc != 0:
            log.warning("compose up: %s", out[-500:])
        self.record("recreated the NVR containers", why, rc == 0)
        return rc == 0

    def recover(self, name: str, problem: str, containers: dict) -> None:
        why = f"{name.replace('lightnvr-', '')} was {problem} for {FAILS_BEFORE_ACTION} minutes"
        if problem == "missing":
            if not self._recent("recreated", 3600):
                self.compose_up(containers, why)
            elif not self._recent("recreated", BACKOFF_SECONDS):
                self.compose_up(containers, why + " (again)")
            return
        if len(self._recent(f"restarted {name}", 3600)) < RESTARTS_PER_HOUR:
            verb = "start" if problem == "stopped" else "restart"
            args = ["docker", verb, name] if verb == "start" else ["docker", "restart", "-t", "45", name]
            rc, _ = self.run(args, timeout=120)
            self.record(f"restarted {name}", why, rc == 0)
        elif not self._recent("recreated", 3600):
            self.compose_up(containers, why + f", even after {RESTARTS_PER_HOUR} restarts")
        elif not self._recent(f"restarted {name}", BACKOFF_SECONDS) and not self._recent("recreated", BACKOFF_SECONDS):
            if not self.state.get("gave_up_told"):
                self.tell(f"Guardian: {name.replace('lightnvr-', '')} keeps failing ({problem}); restarts and "
                          "recreating it haven't helped. Still retrying every 15 minutes - it may need a look "
                          "(`docker compose logs`).", "stuck", notify=self.state.get("down_since") is None)
                self.state["gave_up_told"] = True
            rc, _ = self.run(["docker", "restart", "-t", "45", name], timeout=120)
            self.record(f"restarted {name}", why + " (still retrying)", rc == 0)

    def restart_docker(self) -> None:
        if self._recent("restarted Docker", DOCKER_RESTART_EVERY):
            return
        rc, _ = self.run(["systemctl", "restart", "docker"], timeout=180)
        self.record("restarted Docker", f"the Docker service stopped answering for {FAILS_BEFORE_ACTION} minutes",
                    rc == 0)

    # ── disk ────────────────────────────────────────────────────────────────

    def host_paths(self, exp: dict) -> dict[str, str]:
        """The NVR's recording folders as host paths (a network share the
        NVR mounted itself is invisible here, and left out)."""
        env = self.env()

        def host(p: str) -> str | None:
            for prefix, base in (("/storage", os.path.join(self.repo, "storage")),
                                 ("/mnt/primary", env.get("PRIMARY_STORAGE_PATH") or "./primary-storage"),
                                 ("/mnt/backup", env.get("BACKUP_STORAGE_PATH") or "./backup-storage")):
                if p == prefix or p.startswith(prefix + "/"):
                    return os.path.normpath(os.path.join(self.repo, base) + p[len(prefix):])
            return None

        dirs = exp.get("recording_dirs") or {"cache": "/storage/cache", "primary": "/mnt/primary"}
        return {container: h for container in dirs.values() if container and (h := host(container))}

    def disks(self, exp: dict) -> list[dict]:
        rc, root = self.run(["docker", "info", "--format", "{{.DockerRootDir}}"], timeout=20)
        paths = {"system": "/", "docker": root.strip() if rc == 0 and root.strip() else "/var/lib/docker",
                 "nvr": self.repo}
        for i, h in enumerate(self.host_paths(exp).values()):
            paths[f"recordings{i}"] = h
        seen, out = {}, []
        for role, path in paths.items():
            try:
                dev = os.stat(path).st_dev
            except OSError:
                continue
            if dev in seen:
                seen[dev]["roles"].append(role)
                continue
            try:
                pct, total, avail = self.disk(path)
            except OSError:
                continue
            seen[dev] = {"path": path, "roles": [role], "percent": round(pct, 1), "total": total, "free": avail}
            out.append(seen[dev])
        return out

    def host_clean(self, reason: str, docker_up: bool) -> dict:
        before = {d["path"]: d["free"] for d in self.disks({})}
        done = []
        if self.run(["journalctl", "--vacuum-size=200M", "--vacuum-time=14d"], timeout=120)[0] == 0:
            done.append("journal")
        if os.path.exists("/usr/bin/apt-get") and self.run(["apt-get", "clean"], timeout=120)[0] == 0:
            done.append("apt cache")
        if docker_up:
            rc, used = self.run(["docker", "ps", "-a", "--format", "{{.Image}}"], timeout=30)
            in_use = set(used.split()) if rc == 0 else None
            rc, images = self.run(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], timeout=30)
            if in_use is not None and rc == 0:
                for img in images.split():
                    if img.startswith(OWN_IMAGES) and img not in in_use:
                        self.run(["docker", "image", "rm", img], timeout=120)
            self.run(["docker", "image", "prune", "-f"], timeout=300)
            self.run(["docker", "builder", "prune", "-f", "--filter", "until=168h"], timeout=300)
            done.append("unused Docker images")
        n = self.clean_tmp(self.tmp_root)
        if n:
            done.append(f"{n} old /tmp files")
        after = {d["path"]: d["free"] for d in self.disks({})}
        freed = max(0, sum(after.get(p, 0) - f for p, f in before.items()))
        result = {"at": self.clock(), "reason": reason, "freed_bytes": freed, "done": done}
        self.state["last_clean"] = result
        log.info("Host clean-up (%s): %s - freed %.1f MB", reason, ", ".join(done), freed / 1e6)
        if freed > 100 * 1024 * 1024:
            self.tell(f"Guardian: cleaned the host ({reason}) - {', '.join(done)} - freed {freed / 1e9:.1f} GB.",
                      "cleanup", notify=False)
        return result

    def clean_tmp(self, root: str) -> int:
        cutoff = self.clock() - TMP_MAX_AGE
        n = 0
        try:
            dev = os.stat(root).st_dev
        except OSError:
            return 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith("systemd-private-")
                           and not os.path.islink(os.path.join(dirpath, d))]
            for name in filenames:
                p = os.path.join(dirpath, name)
                try:
                    st = os.lstat(p)
                    if (st.st_dev == dev and os.path.isfile(p) and not os.path.islink(p)
                            and st.st_mtime < cutoff and st.st_atime < cutoff):
                        os.remove(p)
                        n += 1
                except OSError:
                    pass
        return n

    def emergency_delete(self, exp: dict) -> int:
        """NVR down, recordings disk nearly full: delete the oldest recordings
        (main file, picture and playback copy together), never the last 24 h,
        on that disk only, until it's back under DISK_EMERGENCY_TARGET."""
        cutoff = self.clock() - KEEP_NEWEST_SECONDS
        candidates = []
        for container, host in self.host_paths(exp).items():
            try:
                if self.disk(host)[0] < DISK_EMERGENCY_PERCENT:
                    continue
                cams = [d for d in os.listdir(host) if d.isdigit()]
            except OSError:
                continue
            for cam in cams:
                folder = os.path.join(host, cam)
                try:
                    names = os.listdir(folder)
                except OSError:
                    continue
                for name in names:
                    if name.endswith(".mp4") and not name.endswith(".sub.mp4"):
                        p = os.path.join(folder, name)
                        try:
                            m = os.path.getmtime(p)
                        except OSError:
                            continue
                        if m < cutoff:
                            candidates.append((m, p, f"{container}/{cam}/{name}", names))
        candidates.sort()
        deleted, freed = [], 0
        for _, path, container_path, siblings in candidates[:2000]:
            try:
                if self.disk(path)[0] < DISK_EMERGENCY_TARGET:
                    continue  # that disk has room again (another may not)
            except OSError:
                continue
            stem = os.path.basename(path)[:-4]
            for name in [os.path.basename(path)] + [s for s in siblings if s.startswith(stem + ".") and s != stem + ".mp4"]:
                p = os.path.join(os.path.dirname(path), name)
                try:
                    freed += os.path.getsize(p)
                    os.remove(p)
                except OSError:
                    pass
            deleted.append(container_path)
        if deleted:
            # The file list lets the NVR drop exactly those rows when it's back.
            self.record(f"deleted the {len(deleted)} oldest recordings",
                        f"the NVR is down and its disk was over {DISK_EMERGENCY_PERCENT}% full "
                        f"({freed / 1e9:.1f} GB freed, nothing from the last 24 hours)", True,
                        deleted_files=deleted)
        return len(deleted)

    # ── telling the user directly ───────────────────────────────────────────

    def telegram_config(self) -> tuple[str, str] | None:
        """Read from the NVR's database, read-only (it's what the NVR's own
        alerts use). Kept in memory only."""
        at, cfg = self._telegram
        if cfg and self.clock() - at < 3600:
            return cfg
        rc, mount = self.run(["docker", "volume", "inspect", "lightnvr-data", "--format", "{{.Mountpoint}}"],
                             timeout=20)
        base = mount.strip() if rc == 0 and mount.strip() else "/var/lib/docker/volumes/lightnvr-data/_data"
        db = os.path.join(base, "lightnvr.db")
        if not os.path.exists(db):
            return cfg
        for mode in ("ro", "ro&immutable=1"):
            try:
                con = sqlite3.connect(f"file:{db}?mode={mode}", uri=True, timeout=5)
                try:
                    row = con.execute("SELECT telegram_enabled, telegram_bot_token, telegram_chat_id "
                                      "FROM alert_settings WHERE id = 1").fetchone()
                finally:
                    con.close()
                cfg = (row[1], row[2]) if row and row[0] and row[1] and row[2] else None
                self._telegram = (self.clock(), cfg)
                return cfg
            except sqlite3.Error:
                continue
        return cfg

    def _send_telegram(self, text: str) -> bool:
        cfg = self.telegram_config()
        if not cfg:
            return False
        token, chat = cfg
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=json.dumps({"chat_id": chat, "text": text}).encode(),
                                     headers={"Content-Type": "application/json"})
        for delay in (0, 10, 30):
            time.sleep(delay)
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    if r.status == 200:
                        return True
            except (urllib.error.URLError, OSError):
                pass
        log.warning("Telegram message could not be sent")
        return False

    def nvr_down_notice(self, nvr_ok: bool) -> None:
        now = self.clock()
        down = self.state.get("down_since")
        if nvr_ok:
            if down is not None:
                if self.state.get("down_told"):
                    self.send_telegram(f"LightNVR is back up (it was down {int((now - down) // 60)} min, "
                                       f"since {_clock(down)}).")
                self.state.update(down_since=None, down_told=None, gave_up_told=None)
            return
        if down is None:
            self.state["down_since"] = down = now
        told = self.state.get("down_told")
        if now - down >= TELEGRAM_AFTER and (told is None or now - told >= TELEGRAM_REMIND):
            tried = [r["what"] for r in self.recoveries if r["at"] >= down]
            text = f"LightNVR is down since {_clock(down)}" + (
                f" - tried: {'; '.join(dict.fromkeys(tried))}" if tried else "") + ". Recording may be stopped."
            if told is not None:
                text = "Still down: " + text
            if self.send_telegram(text):
                self.state["down_told"] = now

    # ── one pass ────────────────────────────────────────────────────────────

    def check(self) -> dict:
        now = self.clock()
        paused = self.paused()
        exp = self.expect()
        docker_up = self.docker_ok()
        status: dict = {"at": now, "paused": paused, "docker": docker_up, "containers": {}, "problems": []}

        # Docker itself
        if not docker_up:
            self.fails["docker"] = self.fails.get("docker", 0) + 1
            status["problems"].append("Docker is not answering")
            if not paused and self.fails["docker"] >= FAILS_BEFORE_ACTION and now >= self.state.get("grace", 0):
                self.restart_docker()
                self.fails["docker"] = 0
                self.state["grace"] = self.clock() + GRACE_AFTER_ACTION
        else:
            self.fails.pop("docker", None)

        containers = self.containers() if docker_up else {}
        backend_ok = self.http_ok(self.backend_url())
        status["backend"] = backend_ok
        acted = False
        if docker_up:
            for name in self.expected_containers():
                info = containers.get(name)
                problem = self.problem(name, info, backend_ok)
                status["containers"][name] = {**(info or {}), "problem": problem}
                if problem is None:
                    self.fails.pop(name, None)
                    continue
                self.fails[name] = self.fails.get(name, 0) + 1
                status["problems"].append(f"{name.replace('lightnvr-', '')} {problem}")
                if (not paused and not acted and self.fails[name] >= FAILS_BEFORE_ACTION
                        and now >= self.state.get("grace", 0)):
                    self.recover(name, problem, containers)
                    acted = True
                    self.fails[name] = 0
                    self.state["grace"] = self.clock() + GRACE_AFTER_ACTION

        # Recording keeps happening (a backend that answers but records nothing)
        stuck = self.recordings_stuck(exp) if backend_ok else []
        status["cameras_without_new_recordings"] = stuck
        online = set(exp.get("online_cameras") or exp.get("continuous_cameras") or [])
        cams = [c for c in exp.get("continuous_cameras") or [] if c in online]
        if stuck and len(stuck) == len(cams):
            self.state.setdefault("all_stuck_since", now)
            if (not paused and not acted and now - self.state["all_stuck_since"] >= 5 * 60
                    and not self._recent("restarted lightnvr-backend", ALL_STUCK_RESTART_EVERY)):
                rc, _ = self.run(["docker", "restart", "-t", "45", "lightnvr-backend"], timeout=120)
                self.record("restarted lightnvr-backend",
                            f"no camera has made a new recording for {NO_RECORDINGS_SECONDS // 60} minutes", rc == 0)
                acted = True
                self.state["grace"] = self.clock() + GRACE_AFTER_ACTION
        else:
            self.state.pop("all_stuck_since", None)
        if stuck:
            status["problems"].append(f"{len(stuck)} camera(s) not recording")

        # Disks
        disks = self.disks(exp)
        status["disks"] = disks
        if not paused:
            if any(d["percent"] >= DISK_CLEAN_PERCENT for d in disks) \
                    and now - (self.state.get("last_clean") or {}).get("at", 0) >= HOST_CLEAN_MIN_GAP:
                self.host_clean(f"a disk passed {DISK_CLEAN_PERCENT}%", docker_up)
                disks = status["disks"] = self.disks(exp)
            down_for = now - self.state["down_since"] if self.state.get("down_since") else 0
            rec = [d for d in disks if any(r.startswith("recordings") for r in d["roles"])]
            if down_for >= 300 and any(d["percent"] >= DISK_EMERGENCY_PERCENT for d in rec):
                self.emergency_delete(exp)
            lt = time.localtime(now)
            today = time.strftime("%Y-%m-%d", lt)
            if (lt.tm_hour, lt.tm_min) >= DAILY_AT and self.state.get("daily") != today:
                self.state["daily"] = today
                self.host_clean("daily", docker_up)

        # The user, when the NVR can't tell them itself
        self.nvr_down_notice(backend_ok or paused)

        status["recoveries"] = self.recoveries[-10:]
        status["last_clean"] = self.state.get("last_clean")
        status["down_since"] = self.state.get("down_since")
        self._write_json(os.path.join(self.bridge, "status.json"), status)
        self._save()
        return status

    def loop(self) -> None:
        log.info("LightNVR guardian watching %s", self.repo)
        while True:
            started = time.monotonic()
            try:
                self.check()
            except Exception:
                log.exception("Check failed")
            time.sleep(max(5.0, CHECK_SECONDS - (time.monotonic() - started)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    ap.add_argument("--state-dir", default=os.environ.get("STATE_DIRECTORY", "/var/lib/lightnvr-guardian"))
    ap.add_argument("--once", action="store_true", help="one check, print the status, exit")
    ap.add_argument("--clean", action="store_true", help="clean host waste now, then exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    g = Guardian(args.repo, args.state_dir)
    if args.clean:
        print(json.dumps(g.host_clean("by hand", g.docker_ok()), indent=2))
        g._save()
    elif args.once:
        print(json.dumps(g.check(), indent=2, default=str))
    else:
        g.loop()


if __name__ == "__main__":
    main()
