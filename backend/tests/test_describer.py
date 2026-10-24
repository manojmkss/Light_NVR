"""Descriptions on this NVR: the machine check, the one-at-a-time describer,
and what it's spent on."""

import asyncio
import os
import shutil
import subprocess
import textwrap

import pytest

from app.api.routes.describer import assess, mem_limit_mb
from app.services.ai import vlm

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB_ENV = os.path.join(ROOT, "scripts", "lib-env.sh")
needs_bash = pytest.mark.skipif(shutil.which("bash") is None or not os.path.exists(LIB_ENV), reason="no bash")


# ── what the page says about this machine ───────────────────────────────────


def test_a_four_core_ten_gb_box_gets_the_small_model_and_honest_limits():
    hw = {"cores": 4, "ram_mb": 9942, "describer": "on", "describer_mem_limit": "5g",
          "nvidia_gpu": False, "nvidia_toolkit": False}
    out = assess(hw, reachable=True, installed={"qwen3-vl:2b-instruct"})
    by = {m["name"]: m for m in out["models"]}
    assert out["state"] == "ready" and out["recommended"] == "qwen3-vl:2b-instruct"
    assert "4 cores, 10 GB RAM" in out["machine"] and "no GPU" in out["machine"]
    assert by["qwen3-vl:2b-instruct"]["fit"] == "good" and by["qwen3-vl:2b-instruct"]["installed"]
    assert by["qwen3-vl:4b-instruct"]["fit"] == "no" and "5.0 GB" in by["qwen3-vl:4b-instruct"]["fit_note"]
    assert by["qwen3-vl:8b-instruct"]["fit"] == "no" and "NVIDIA" in by["qwen3-vl:8b-instruct"]["fit_note"]


def test_a_gpu_box_is_recommended_the_biggest_model_that_fits():
    hw = {"cores": 8, "ram_mb": 32000, "describer": "on", "describer_mem_limit": "8g",
          "nvidia_gpu": True, "nvidia_toolkit": True}
    out = assess(hw, reachable=True, installed=set())
    assert out["recommended"] == "qwen3-vl:8b-instruct"
    assert all(m["fit"] == "good" for m in out["models"])


def test_the_page_says_why_it_cannot_run_here():
    small = {"cores": 2, "ram_mb": 3900, "describer": "too_small",
             "describer_why": "needs at least 4 CPU cores; this machine has 2"}
    out = assess(small, reachable=False, installed=set())
    assert out["state"] == "too_small" and "this machine has 2" in out["why"]
    assert assess({"describer": "off"}, False, set())["state"] == "off"
    out = assess(None, reachable=False, installed=set())
    assert out["state"] == "not_installed" and "Run the update" in out["why"]


def test_compose_memory_limits_are_read():
    assert mem_limit_mb("4500m") == 4500 and mem_limit_mb("8g") == 8192 and mem_limit_mb(None) == 5120


# ── the describer itself ─────────────────────────────────────────────────────


def _local_settings(model="qwen3-vl:2b"):
    from types import SimpleNamespace

    return SimpleNamespace(vlm_provider="ollama", vlm_url=vlm.LOCAL_DESCRIBER_URL, vlm_model=model)


def test_local_describer_takes_one_picture_at_a_time_and_skips_when_behind(monkeypatch):
    import cv2
    import numpy as np

    sent = []
    running = {"now": 0, "max": 0}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "  A man  carries a parcel to the door. "}}

    class Client:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
            sent.append((url, json, self.timeout))
            await asyncio.sleep(0.05)
            running["now"] -= 1
            return Resp()

    monkeypatch.setattr(vlm.httpx, "AsyncClient", Client)
    big = cv2.imencode(".jpg", np.zeros((1520, 2688, 3), np.uint8))[1].tobytes()

    async def go():
        s = _local_settings()
        assert vlm.is_local(s) and vlm.timeout_for(s) == vlm.LOCAL_TIMEOUT_SECONDS
        return await asyncio.gather(*(vlm.describe_frame(s, big, ["person"], "Gate") for _ in range(4)),
                                    return_exceptions=True)

    results = asyncio.run(go())
    texts = [r for r in results if isinstance(r, str)]
    skipped = [r for r in results if isinstance(r, vlm.DescriberBusy)]
    assert texts[0] == "A man carries a parcel to the door."
    assert len(texts) == vlm.LOCAL_MAX_WAITING and len(skipped) == 2  # fell behind: skipped, not queued
    assert running["max"] == 1  # never two at once on a CPU
    url, body, timeout = sent[0]
    assert url.endswith("/api/chat") and body["think"] is False and timeout == vlm.LOCAL_TIMEOUT_SECONDS
    assert body["options"]["num_ctx"] == 4096  # not the model's 256K default
    img = cv2.imdecode(np.frombuffer(__import__("base64").b64decode(body["messages"][0]["images"][0]), np.uint8), 1)
    assert max(img.shape[:2]) == vlm.LOCAL_MAX_SIDE  # a smaller picture: much faster on a CPU


def test_a_remote_describer_is_not_treated_as_local():
    from types import SimpleNamespace

    other = SimpleNamespace(vlm_provider="ollama", vlm_url="http://192.168.1.20:11434", vlm_model="x")
    assert not vlm.is_local(other) and vlm.timeout_for(other) == 30.0


def test_local_describer_is_spent_on_people_and_vehicles_only():
    from tests.test_motion_event import Harness, person, settings
    from app.services.ai.types import DetectedObject

    calls = []

    async def describe(_s, jpeg, labels, cam, prompt):
        calls.append(labels)
        return "Something happens."

    def run(objects, describe_it=True):
        h = Harness(objects, settings(vlm_enabled=True, vlm_provider="ollama", vlm_url=vlm.LOCAL_DESCRIBER_URL,
                                      vlm_model="qwen3-vl:2b", alert_on_objects_only=False))

        async def go():
            s = h.session()
            s._describe = describe
            s.start()
            await asyncio.sleep(0.8)
            await s.stop_monitoring()
            return await s.build_summary(describe=describe_it)

        return asyncio.run(go())

    run(lambda t: [DetectedObject("dog", 0.8, 0.3 + t * 0.05, 0.5, 0.1, 0.1)])
    assert calls == []  # a dog in the yard isn't worth a minute of CPU
    run(lambda t: [person(0.3 + t * 0.05)], describe_it=False)
    assert calls == []  # part of a multi-camera visit: the visit summary gets the description
    summary = run(lambda t: [person(0.3 + t * 0.05)])
    assert calls == [["person"]] and "Something happens." in summary.message


# ── the update script ────────────────────────────────────────────────────────


def _describer(tmp_path, env_text, cores, ram_mb, nvidia=False):
    env = tmp_path / ".env"
    env.write_text(env_text)
    script = textwrap.dedent(f"""
        set -euo pipefail
        ENV_FILE="{env}"
        export LIGHTNVR_CORES={cores} LIGHTNVR_RAM_MB={ram_mb}
        HW_NVIDIA={str(nvidia).lower()} HW_NVIDIA_TOOLKIT={str(nvidia).lower()}
        source "{LIB_ENV}"
        configure_describer
        echo "STATE=$DESCRIBER_STATE WHY=$DESCRIBER_WHY"
    """)
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return dict(line.split("=", 1) for line in env.read_text().splitlines() if "=" in line), out.stdout


@needs_bash
def test_update_adds_the_describer_only_where_it_fits(tmp_path):
    base = "COMPOSE_FILE=docker-compose.yml:docker-compose.ai.yml:docker-compose.ai.intel.yml\n"
    env, out = _describer(tmp_path, base, cores=4, ram_mb=9942)
    assert "STATE=on" in out
    assert env["COMPOSE_FILE"] == "docker-compose.yml:docker-compose.ai.yml:docker-compose.ai.intel.yml:docker-compose.describe.yml"
    assert env["DESCRIBE_CPUS"] == "2" and env["DESCRIBE_MEM_LIMIT"] == "5g"
    # Running the update again changes nothing.
    env2, _ = _describer(tmp_path, (tmp_path / ".env").read_text(), cores=4, ram_mb=9942)
    assert env2["COMPOSE_FILE"] == env["COMPOSE_FILE"]

    env, out = _describer(tmp_path, base, cores=2, ram_mb=16000)
    assert "STATE=too_small" in out and "this machine has 2" in out and "describe" not in env["COMPOSE_FILE"]
    env, out = _describer(tmp_path, base, cores=8, ram_mb=3900)
    assert "STATE=too_small" in out and "8 GB of RAM" in out


@needs_bash
def test_update_respects_describer_off_and_gives_it_the_nvidia_gpu(tmp_path):
    on = "COMPOSE_FILE=docker-compose.yml:docker-compose.describe.yml\nDESCRIBER=off\n"
    env, out = _describer(tmp_path, on, cores=8, ram_mb=32000)
    assert "STATE=off" in out and "COMPOSE_FILE" not in env  # back to just docker-compose.yml
    env, _ = _describer(tmp_path, "", cores=8, ram_mb=32000, nvidia=True)
    assert env["COMPOSE_FILE"].endswith("docker-compose.describe.yml:docker-compose.describe.nvidia.yml")
    assert env["DESCRIBE_MEM_LIMIT"] == "8g"
