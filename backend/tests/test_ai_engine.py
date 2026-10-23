"""AI engine: choosing CPU / Intel GPU / NVIDIA GPU from the web page.

Three parts: the install/update scripts (which image, is the GPU connected),
the AI worker (which devices it can use, switching, falling back), and the
NVR's explanations ("the GPU is there but its driver isn't loaded")."""

import importlib.util
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WORKER = os.path.join(ROOT, "ai-worker", "app.py")
LIB_ENV = os.path.join(ROOT, "scripts", "lib-env.sh")

needs_worker = pytest.mark.skipif(not os.path.exists(WORKER), reason="ai-worker source not available")
needs_bash = pytest.mark.skipif(shutil.which("bash") is None or not os.path.exists(LIB_ENV), reason="no bash")


# ── the NVR's explanations ───────────────────────────────────────────────────


def test_hints_explain_the_missing_step():
    from app.api.routes.ai_engine import hints

    assert "Run the update" in hints(None, None)[0]
    vm_no_driver = {"intel_gpu": True, "dri": False, "virt": "vmware"}
    assert "driver isn't loaded" in hints(vm_no_driver, None)[0] and "passed through" in hints(vm_no_driver, None)[0]
    not_connected = {"intel_gpu": True, "dri": True, "virt": "none"}
    engine = {"devices": [{"id": "intel_gpu", "available": False}]}
    assert "run the update" in hints(not_connected, engine)[0]
    assert hints(not_connected, {"devices": [{"id": "intel_gpu", "available": True}]}) == []
    nvidia = {"nvidia_gpu": True, "nvidia_toolkit": False}
    assert "Container Toolkit" in hints(nvidia, None)[0]


# ── the AI worker ────────────────────────────────────────────────────────────


@pytest.fixture()
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_WORKER_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("AI_WORKER_ENGINE_FILE", str(tmp_path / "ai-engine.json"))
    spec = importlib.util.spec_from_file_location("lightnvr_worker_under_test", WORKER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop(spec.name, None)


@needs_worker
def test_devices_say_why_they_cannot_be_used(worker, monkeypatch):
    monkeypatch.setattr(worker, "available_providers", lambda: ["CPUExecutionProvider"])
    opts = {d["id"]: d for d in worker.device_options()}
    assert opts["cpu"]["available"]
    assert not opts["intel_gpu"]["available"] and "update" in opts["intel_gpu"]["reason"]
    monkeypatch.setattr(worker, "available_providers", lambda: ["OpenVINOExecutionProvider", "CPUExecutionProvider"])
    monkeypatch.setattr(worker, "_has_dri", lambda: False)
    assert "No Intel GPU is connected" in {d["id"]: d for d in worker.device_options()}["intel_gpu"]["reason"]
    monkeypatch.setattr(worker, "_has_dri", lambda: True)
    assert {d["id"]: d for d in worker.device_options()}["intel_gpu"]["available"]


@needs_worker
def test_auto_picks_a_clearly_faster_gpu_and_a_failing_gpu_is_never_kept(worker, monkeypatch):
    monkeypatch.setattr(worker, "available_providers", lambda: ["OpenVINOExecutionProvider", "CPUExecutionProvider"])
    monkeypatch.setattr(worker, "_has_dri", lambda: True)
    speeds = {"cpu": {"ok": True, "p50_ms": 450.0}, "intel_gpu": {"ok": True, "p50_ms": 60.0}}
    monkeypatch.setattr(worker, "measure", lambda d, runs=10: speeds[d])
    assert worker.apply_engine("auto")["active"] == "intel_gpu"
    speeds["intel_gpu"] = {"ok": True, "p50_ms": 420.0}  # barely faster: not worth it
    assert worker.apply_engine("auto")["active"] == "cpu"
    speeds["intel_gpu"] = {"ok": False, "error": "CL_DEVICE_NOT_FOUND"}
    st = worker.apply_engine("intel_gpu")
    assert st["active"] == "cpu" and "CL_DEVICE_NOT_FOUND" in st["note"]
    # The choice survives a restart.
    assert '"intel_gpu"' in open(os.environ["AI_WORKER_ENGINE_FILE"]).read()


@needs_worker
def test_a_gpu_that_keeps_failing_hands_over_to_the_cpu(worker, monkeypatch):
    import numpy as np

    class Broken:
        provider = "OpenVINOExecutionProvider"

        def detect(self, img, conf):
            raise RuntimeError("GPU hang")

    class Fine:
        provider = "CPUExecutionProvider"

        def detect(self, img, conf):
            return []

    monkeypatch.setattr(worker, "_new_detector", lambda name, dev, strict: Fine() if dev == "cpu" else Broken())
    worker._engine.update(active="intel_gpu", choice="intel_gpu")
    worker._detectors.clear()
    img = np.zeros((10, 10, 3), np.uint8)
    for _ in range(worker.GPU_FAILS_BEFORE_FALLBACK - 1):
        with pytest.raises(RuntimeError):
            worker._run_detect(img, "yolov8n", 0.5)
    boxes, _ = worker._run_detect(img, "yolov8n", 0.5)  # third failure: switches and answers on the CPU
    assert boxes == [] and worker._engine["active"] == "cpu" and "stopped working" in worker._engine["note"]


# ── the install/update scripts ───────────────────────────────────────────────


def _configure(tmp_path, env_text="", lspci="", dri=False, nvidia=False, toolkit=False, arch="x86_64"):
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)

    def stub(name, body):
        (stubs / name).write_text("#!/bin/sh\n" + body + "\n")
        (stubs / name).chmod(0o755)

    stub("lspci", f"printf '%s\\n' \"{lspci}\"")
    stub("uname", f"echo {arch}")
    stub("systemd-detect-virt", "echo vmware")
    stub("nvidia-smi", "exit 0" if nvidia else "exit 1")
    stub("docker", "echo 'Runtimes: nvidia runc'" if toolkit else "echo 'Runtimes: runc'")
    dev = tmp_path / "dri"
    dev.mkdir(exist_ok=True)
    if dri:
        (dev / "renderD128").write_text("")
    env = tmp_path / ".env"
    env.write_text(env_text)
    script = textwrap.dedent(f"""
        set -euo pipefail
        export PATH="{stubs}:$PATH" LIGHTNVR_DEV_DRI="{dev}"
        ENV_FILE="{env}"
        DOCKER=(docker)
        source "{LIB_ENV}"
        configure_ai_service
        echo "CHOSEN=${{AI_VARIANT_CHOSEN:-}}"
    """)
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    values = dict(line.split("=", 1) for line in env.read_text().splitlines() if "=" in line)
    return values, out.stdout


@needs_bash
def test_update_gives_the_ai_service_an_intel_gpu(tmp_path):
    env, _ = _configure(tmp_path, "COMPOSE_FILE=docker-compose.yml:docker-compose.macvlan.yml\n",
                        lspci="00:02.0 VGA compatible controller: Intel Corporation CoffeeLake-S GT2", dri=True)
    assert env["AI_WORKER_VARIANT"] == "openvino"
    assert env["COMPOSE_FILE"] == ("docker-compose.yml:docker-compose.macvlan.yml:docker-compose.ai.yml:"
                                   "docker-compose.ai.intel.yml")
    assert env["RENDER_GID"].isdigit()


@needs_bash
def test_update_without_a_gpu_still_installs_an_engine_that_can_use_one_later(tmp_path):
    env, _ = _configure(tmp_path)
    assert env["AI_WORKER_VARIANT"] == "openvino" and "docker-compose.ai.intel.yml" not in env["COMPOSE_FILE"]
    assert "docker-compose.ai.yml" in env["COMPOSE_FILE"]


@needs_bash
def test_update_picks_nvidia_only_with_the_container_toolkit(tmp_path):
    env, _ = _configure(tmp_path, nvidia=True, toolkit=True)
    assert env["AI_WORKER_VARIANT"] == "cuda" and env["COMPOSE_FILE"].endswith("docker-compose.ai.nvidia.yml")
    (tmp_path / "x").mkdir()
    env, _ = _configure(tmp_path / "x", nvidia=True, toolkit=False)  # toolkit missing: not usable yet
    assert env["AI_WORKER_VARIANT"] == "openvino"


@needs_bash
def test_update_respects_opt_out_and_a_hand_picked_image(tmp_path):
    env, _ = _configure(tmp_path, "AI_WORKER=off\nCOMPOSE_FILE=docker-compose.yml:docker-compose.ai.yml\n", dri=True)
    assert "COMPOSE_FILE" not in env or "ai" not in env["COMPOSE_FILE"]
    (tmp_path / "y").mkdir()
    env, _ = _configure(tmp_path / "y", "AI_WORKER_VARIANT=cpu\nAI_WORKER_VARIANT_AUTO=off\n", dri=True)
    assert env["AI_WORKER_VARIANT"] == "cpu" and "docker-compose.ai.intel.yml" not in env["COMPOSE_FILE"]
    (tmp_path / "z").mkdir()
    env, _ = _configure(tmp_path / "z", arch="aarch64", dri=True)
    assert env["AI_WORKER_VARIANT"] == "cpu"


@needs_bash
@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs to start as root to drop to a user")
def test_root_owned_env_is_updated_through_sudo_instead_of_failing(tmp_path):
    # The installer runs as root, updates as the user: .env is often root's.
    import pathlib
    import tempfile

    # Somewhere the dropped-to user can reach (pytest's tmp dir is root-only).
    work = pathlib.Path(tempfile.mkdtemp(dir="/tmp"))
    os.chmod(work, 0o777)
    env = work / ".env"
    env.write_text("COMPOSE_FILE=docker-compose.yml\n")
    os.chmod(env, 0o644)  # root-owned, not writable by the user
    log = work / "sudo.log"
    stubs = work / "bin"
    stubs.mkdir()
    (stubs / "sudo").write_text(f"#!/bin/sh\necho \"$*\" >> {log}\nexit 0\n")
    (stubs / "sudo").chmod(0o755)
    script = f'''
        set -euo pipefail
        export PATH="{stubs}:$PATH"
        ENV_FILE="{env}"
        source "{LIB_ENV}"
        env_set AI_WORKER_VARIANT openvino
        env_set COMPOSE_FILE docker-compose.yml:docker-compose.ai.yml
        echo "value=$(env_get COMPOSE_FILE)"
    '''
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         preexec_fn=lambda: (os.setgid(65534), os.setuid(65534)))
    assert out.returncode == 0, out.stderr  # no "touch: Permission denied" abort
    calls = log.read_text()
    assert "touch" in calls and "tee -a" in calls and "sed -i" in calls
    assert "belongs to root" in out.stderr and out.stderr.count("belongs to root") == 1
