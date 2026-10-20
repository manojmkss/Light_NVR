"""ONNX Runtime sessions on the best available hardware.

Preference order for "auto": CUDA (NVIDIA) > OpenVINO (Intel iGPU/Arc/CPU) >
ROCm/MIGraphX (AMD) > CPU. The provider that is actually used is reported
back, because "the GPU image started" is not proof the GPU is doing the work.
"""

import logging
import os

logger = logging.getLogger(__name__)

PROVIDER_ALIASES = {
    "cuda": "CUDAExecutionProvider",
    "tensorrt": "TensorrtExecutionProvider",
    "openvino": "OpenVINOExecutionProvider",
    "rocm": "ROCMExecutionProvider",
    "migraphx": "MIGraphXExecutionProvider",
    "directml": "DmlExecutionProvider",
    "cpu": "CPUExecutionProvider",
}
AUTO_ORDER = ["CUDAExecutionProvider", "OpenVINOExecutionProvider", "MIGraphXExecutionProvider",
              "ROCMExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]


def available_providers() -> list[str]:
    try:
        import onnxruntime as ort

        return list(ort.get_available_providers())
    except Exception:
        return []


def choose_providers(preference: str = "auto") -> list:
    """Provider list for InferenceSession. A specific preference falls back to
    CPU so a misconfigured GPU still detects (slowly) instead of not at all."""
    avail = available_providers()
    pref = (preference or "auto").lower()
    if pref != "auto":
        name = PROVIDER_ALIASES.get(pref, pref)
        chosen = [name] if name in avail else []
    else:
        chosen = [p for p in AUTO_ORDER if p in avail and p != "CPUExecutionProvider"][:1]
    providers: list = []
    for p in chosen:
        if p == "OpenVINOExecutionProvider":
            # GPU when an Intel GPU is present, else OpenVINO's CPU path (still
            # usually faster than the default CPU provider on Intel chips).
            providers.append((p, {"device_type": os.environ.get("OPENVINO_DEVICE", "GPU" if _has_render_node() else "CPU")}))
        else:
            providers.append(p)
    providers.append("CPUExecutionProvider")
    return providers


def _has_render_node() -> bool:
    try:
        return any(n.startswith("renderD") for n in os.listdir("/dev/dri"))
    except OSError:
        return False


def make_session(path: str, preference: str = "auto", threads: int | None = None):
    """Returns (session, provider actually in use)."""
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    if threads:
        opts.intra_op_num_threads = threads
    providers = choose_providers(preference)
    try:
        session = ort.InferenceSession(path, opts, providers=providers)
    except Exception:
        logger.exception("Could not create session with %s - falling back to CPU", providers)
        session = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
    used = session.get_providers()[0] if session.get_providers() else "CPUExecutionProvider"
    return session, used


def device_label(provider: str) -> str:
    return {
        "CUDAExecutionProvider": "cuda",
        "TensorrtExecutionProvider": "tensorrt",
        "OpenVINOExecutionProvider": "openvino",
        "MIGraphXExecutionProvider": "rocm",
        "ROCMExecutionProvider": "rocm",
        "DmlExecutionProvider": "directml",
    }.get(provider, "cpu")
