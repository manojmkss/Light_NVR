"""The host guardian (scripts/guardian/lightnvr_guardian.py): it restarts what
hangs, step by step, never acts during an update, tells the user directly when
the NVR is down, and deletes recordings itself only as a last resort - the
oldest first, never the last day."""

import importlib.util
import json
import os
import time

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "guardian", "lightnvr_guardian.py")
_spec = importlib.util.spec_from_file_location("lightnvr_guardian", _PATH)
guardian = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guardian)

HEALTHY = "Up 2 hours (healthy)"


class Host:
    """Docker, the backend and the disk, as the guardian sees them."""

    def __init__(self):
        self.docker = True
        self.backend = True
        self.percent = 50.0
        self.containers = {
            "lightnvr-backend": ["running", HEALTHY, "ghcr.io/manojmkss/light_nvr-backend:0.4.14"],
            "lightnvr-frontend": ["running", "Up 2 hours", "ghcr.io/manojmkss/light_nvr-frontend:0.4.14"],
            "lightnvr-nginx": ["running", HEALTHY, "nginx:alpine"],
        }
        self.compose_names = None  # None: `docker compose config` fails
        self.calls = []
        self.telegrams = []

    def run(self, args, timeout=60, env=None):
        self.calls.append((args, env or {}))
        if args[:2] == ["docker", "info"]:
            return (0, "/nonexistent-docker-root") if self.docker else (1, "Cannot connect")
        if not self.docker and args[0] == "docker":
            return 1, "Cannot connect"
        if args[:3] == ["docker", "ps", "-a"] and "--filter" in args:
            return 0, "\n".join(f"{n}|{s}|{st}|{img}" for n, (s, st, img) in self.containers.items())
        if args[:3] == ["docker", "compose", "config"]:
            if self.compose_names is None:
                return 1, "error"
            return 0, json.dumps({"services": {n: {"container_name": n} for n in self.compose_names}}) + "\nwarn: x"
        return 0, ""

    def actions(self):
        return [a for a, _ in self.calls if a[:2] in (["docker", "restart"], ["docker", "start"], ["systemctl", "restart"])
                or a[:3] == ["docker", "compose", "up"]]


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


def _make(tmp_path, host=None, env=""):
    host = host or Host()
    clock = Clock()
    (tmp_path / "repo" / "storage").mkdir(parents=True)
    (tmp_path / "repo" / ".env").write_text(env)
    (tmp_path / "tmp").mkdir()
    g = guardian.Guardian(str(tmp_path / "repo"), str(tmp_path / "state"), run=host.run, clock=clock,
                          http_ok=lambda url: host.backend, send_telegram=lambda text: host.telegrams.append(text) or True,
                          disk=lambda path: (host.percent, 1000 * 2**30, int((100 - host.percent) * 10 * 2**30)))
    g.tmp_root = str(tmp_path / "tmp")
    g.state["daily"] = time.strftime("%Y-%m-%d", time.localtime(clock.t))  # no daily clean unless a test wants one
    return g, host, clock


def _minutes(g, clock, n):
    for _ in range(n):
        g.check()
        clock.t += 60


def _outbox(tmp_path):
    box = tmp_path / "repo" / "storage" / "guardian" / "outbox"
    return [json.loads(p.read_text()) for p in sorted(box.glob("*.json"))] if box.exists() else []


def test_a_hung_backend_is_restarted_after_three_minutes_and_then_left_to_settle(tmp_path):
    g, host, clock = _make(tmp_path)
    host.backend = False
    _minutes(g, clock, 2)
    assert host.actions() == []  # one or two bad minutes: could be a blip
    _minutes(g, clock, 1)
    assert host.actions() == [["docker", "restart", "-t", "45", "lightnvr-backend"]]
    _minutes(g, clock, 2)
    assert len(host.actions()) == 1  # settling, not judged yet
    msgs = _outbox(tmp_path)
    assert msgs and "restarted lightnvr-backend" in msgs[0]["message"]
    status = json.loads((tmp_path / "repo" / "storage" / "guardian" / "status.json").read_text())
    assert status["backend"] is False and status["recoveries"]


def test_restarts_that_dont_help_escalate_to_recreating_on_the_running_version(tmp_path):
    g, host, clock = _make(tmp_path)
    host.containers["lightnvr-backend"][1] = "Up 3 minutes (unhealthy)"
    _minutes(g, clock, 40)
    acts = host.actions()
    assert acts[:3] == [["docker", "restart", "-t", "45", "lightnvr-backend"]] * 3
    assert acts[3][:3] == ["docker", "compose", "up"] and "--no-build" in acts[3]
    up_env = next(env for a, env in host.calls if a[:3] == ["docker", "compose", "up"])
    assert up_env == {"LIGHTNVR_VERSION": "0.4.14"}  # never whatever `latest` happens to be
    # After that, slow retries - not a restart every few minutes.
    assert len(acts) <= 5


def test_nothing_is_touched_while_an_update_runs(tmp_path):
    g, host, clock = _make(tmp_path)
    marker = tmp_path / "repo" / "storage" / "guardian" / "paused-until"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(int(clock.t + 1800)))
    host.backend = False
    host.containers.pop("lightnvr-frontend")
    _minutes(g, clock, 10)
    assert host.actions() == []
    assert host.telegrams == []
    # A marker a crashed update left behind doesn't pause it forever.
    marker.write_text(str(int(clock.t + 10 * 86400)))
    clock.t += guardian.MAX_PAUSE + 60
    _minutes(g, clock, 4)
    assert host.actions()


def test_a_dead_docker_service_is_restarted_at_most_every_half_hour(tmp_path):
    g, host, clock = _make(tmp_path)
    host.docker = False
    _minutes(g, clock, 20)
    assert host.actions() == [["systemctl", "restart", "docker"]]
    _minutes(g, clock, 20)
    assert host.actions() == [["systemctl", "restart", "docker"]] * 2


def test_a_missing_container_brings_the_stack_back_but_one_an_update_removed_does_not(tmp_path):
    g, host, clock = _make(tmp_path)
    host.compose_names = ["lightnvr-backend", "lightnvr-frontend", "lightnvr-nginx"]
    host.containers["lightnvr-ai-describe"] = ["exited", "Exited (0) 1 day ago", "ollama/ollama:0.40.2"]
    _minutes(g, clock, 5)
    assert host.actions() == []  # not in the compose files any more: not ours to start
    host.containers.pop("lightnvr-nginx")
    _minutes(g, clock, 3)
    assert host.actions()[0][:3] == ["docker", "compose", "up"]


def test_telegram_when_down_five_minutes_a_reminder_later_and_when_back(tmp_path):
    g, host, clock = _make(tmp_path)
    host.docker = False  # the whole stack is gone
    host.backend = False
    _minutes(g, clock, 4)
    assert host.telegrams == []
    _minutes(g, clock, 2)
    assert len(host.telegrams) == 1 and "down since" in host.telegrams[0]
    _minutes(g, clock, 60)
    assert len(host.telegrams) == 1  # no message every minute
    clock.t += guardian.TELEGRAM_REMIND
    _minutes(g, clock, 1)
    assert len(host.telegrams) == 2 and host.telegrams[1].startswith("Still down")
    host.docker = host.backend = True
    _minutes(g, clock, 1)
    assert len(host.telegrams) == 3 and "back up" in host.telegrams[2]
    # Messages for the NVR written while it was down don't alert twice.
    assert all(m["notify"] is False for m in _outbox(tmp_path) if m["kind"] == "recovery")


def _recordings(tmp_path, clock, ages_hours):
    cam = tmp_path / "primary" / "3"
    cam.mkdir(parents=True)
    for i, age in enumerate(ages_hours):
        for ext in (".mp4", ".jpg", ".sub.mp4"):
            f = cam / f"seg{i}{ext}"
            f.write_bytes(b"x" * 10)
            os.utime(f, (clock.t - age * 3600, clock.t - age * 3600))
    return cam


def _expect(tmp_path, clock=None, **extra):
    d = tmp_path / "repo" / "storage" / "guardian"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "expect.json"
    f.write_text(json.dumps({"at": clock.t if clock else time.time(), "continuous_cameras": [3],
                             "recording_dirs": {"cache": "/storage/cache", "primary": "/mnt/primary"}, **extra}))
    if clock:
        os.utime(f, (clock.t, clock.t))  # just written, as the NVR does every 30 s


def test_a_full_disk_with_the_nvr_down_loses_the_oldest_recordings_never_the_last_day(tmp_path):
    g, host, clock = _make(tmp_path, env=f"PRIMARY_STORAGE_PATH={tmp_path / 'primary'}\n")
    cam = _recordings(tmp_path, clock, [72, 48, 30, 20, 2])
    _expect(tmp_path)
    host.percent = 98.0
    deleted = []
    real_remove = os.remove

    def remove(p):
        deleted.append(os.path.basename(p))
        real_remove(p)
        if p.endswith("seg1.mp4"):
            host.percent = 94.0  # enough room again

    host.backend = False
    os.remove = remove
    try:
        _minutes(g, clock, 3)
        assert deleted == []  # the NVR may just be restarting: its own floor handles a full disk
        _minutes(g, clock, 3)
    finally:
        os.remove = real_remove
    left = sorted(p.name for p in cam.iterdir())
    assert "seg0.mp4" not in left and "seg0.jpg" not in left and "seg0.sub.mp4" not in left
    assert "seg1.mp4" not in left
    assert {"seg2.mp4", "seg3.mp4", "seg4.mp4"} <= set(left)  # stopped once there was room; last day untouched
    msg = next(m for m in _outbox(tmp_path) if m.get("deleted_files"))
    assert msg["deleted_files"] == ["/mnt/primary/3/seg0.mp4", "/mnt/primary/3/seg1.mp4"]


def test_the_last_day_is_kept_even_when_the_disk_stays_full(tmp_path):
    g, host, clock = _make(tmp_path, env=f"PRIMARY_STORAGE_PATH={tmp_path / 'primary'}\n")
    cam = _recordings(tmp_path, clock, [20, 10, 1])
    _expect(tmp_path)
    host.percent = 99.0
    host.backend = False
    _minutes(g, clock, 8)
    assert len(list(cam.iterdir())) == 9


def test_a_full_disk_while_the_nvr_is_up_is_left_to_the_nvr(tmp_path):
    g, host, clock = _make(tmp_path, env=f"PRIMARY_STORAGE_PATH={tmp_path / 'primary'}\n")
    cam = _recordings(tmp_path, clock, [72, 48])
    _expect(tmp_path)
    host.percent = 98.0
    _minutes(g, clock, 8)
    assert len(list(cam.iterdir())) == 6
    # ...but host waste is cleaned straight away, at most once an hour.
    vacuums = [a for a, _ in host.calls if a[0] == "journalctl"]
    assert len(vacuums) == 1


def test_recording_that_silently_stopped_on_every_online_camera_restarts_the_backend(tmp_path):
    g, host, clock = _make(tmp_path)
    old = clock.t - 30 * 60

    def minutes(n):
        for _ in range(n):
            # Camera 3 is offline: the NVR knows, and a restart wouldn't help it.
            _expect(tmp_path, clock, continuous_cameras=[1, 2, 3], online_cameras=[1, 2],
                    last_recording={"1": old, "2": old, "3": old})
            _minutes(g, clock, 1)

    minutes(3)
    assert host.actions() == []
    minutes(4)
    assert host.actions() == [["docker", "restart", "-t", "45", "lightnvr-backend"]]
    assert "no camera has made a new recording" in _outbox(tmp_path)[0]["message"]


def test_one_camera_without_recordings_is_reported_not_restarted(tmp_path):
    g, host, clock = _make(tmp_path)
    _expect(tmp_path, clock, continuous_cameras=[1, 2], online_cameras=[1, 2],
            last_recording={"1": clock.t - 30 * 60, "2": clock.t})
    status = g.check()
    assert status["cameras_without_new_recordings"] == [1]
    _minutes(g, clock, 3)
    assert host.actions() == []


def test_daily_clean_prunes_only_unused_lightnvr_images_and_old_tmp_files(tmp_path):
    g, host, clock = _make(tmp_path)
    g.state.pop("daily")
    old, new = tmp_path / "tmp" / "old.bin", tmp_path / "tmp" / "new.bin"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(old, (clock.t - 8 * 86400, clock.t - 8 * 86400))
    images = "ghcr.io/manojmkss/light_nvr-backend:0.4.13\nghcr.io/manojmkss/light_nvr-backend:0.4.14\nredis:7\n"
    real = host.run

    def run(args, timeout=60, env=None):
        if args[:3] == ["docker", "image", "ls"]:
            host.calls.append((args, {}))
            return 0, images
        if args[:4] == ["docker", "ps", "-a", "--format"]:
            host.calls.append((args, {}))
            return 0, "ghcr.io/manojmkss/light_nvr-backend:0.4.14\n"
        return real(args, timeout, env)

    g.run = run
    clock.t = time.mktime(time.strptime(time.strftime("%Y-%m-%d 03:31", time.localtime(clock.t)), "%Y-%m-%d %H:%M"))
    g.check()
    removed = [a[3] for a, _ in host.calls if a[:3] == ["docker", "image", "rm"]]
    assert removed == ["ghcr.io/manojmkss/light_nvr-backend:0.4.13"]
    assert not old.exists() and new.exists()
    assert g.state["last_clean"]["reason"] == "daily"
    g.check()
    assert len([a for a, _ in host.calls if a[0] == "journalctl"]) == 1  # once a day


def test_install_writes_the_service_for_this_checkout_and_off_removes_it(tmp_path):
    import shutil
    import subprocess
    import textwrap

    import pytest

    lib = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "lib-env.sh")
    if shutil.which("bash") is None or shutil.which("python3") is None or getattr(os, "geteuid", lambda: 1)() != 0:
        pytest.skip("needs bash, python3 and root (CI's container)")
    repo = tmp_path / "my nvr"  # a path with a space
    shutil.copytree(os.path.join(os.path.dirname(_PATH)), repo / "scripts" / "guardian")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "systemctl.log"
    (bin_dir / "systemctl").write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "$@" >> "{log}"
        exit 0
        """))
    (bin_dir / "systemctl").chmod(0o755)
    (tmp_path / "run-systemd").mkdir()
    unit, journald = tmp_path / "lightnvr-guardian.service", tmp_path / "journald.d" / "lightnvr.conf"

    def install(env_text=""):
        (repo / ".env").write_text(env_text)
        script = (f'set -euo pipefail; export PATH="{bin_dir}:$PATH" LIGHTNVR_SYSTEMD_RUN="{tmp_path}/run-systemd" '
                  f'LIGHTNVR_GUARDIAN_UNIT="{unit}" LIGHTNVR_JOURNALD_CONF="{journald}"; ENV_FILE="{repo}/.env"; '
                  f'source "{lib}"; install_guardian "{repo}"; echo "STATE=$GUARDIAN_STATE"')
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        return out.stdout

    assert "STATE=on" in install()
    text = unit.read_text()
    assert f'"{repo}/scripts/guardian/lightnvr_guardian.py" --repo "{repo}"' in text
    assert "@REPO@" not in text and "@PYTHON@" not in text
    assert "SystemMaxUse=200M" in journald.read_text()
    calls = log.read_text().splitlines()
    assert "daemon-reload" in calls and "enable lightnvr-guardian" in calls and calls[-1] == "restart lightnvr-guardian"
    log.write_text("")
    assert "STATE=on" in install()  # an update: restarted on the new code, nothing rewritten
    assert log.read_text().splitlines() == ["enable lightnvr-guardian", "restart lightnvr-guardian"]
    assert "STATE=off" in install("GUARDIAN=off\n")
    assert not unit.exists()
