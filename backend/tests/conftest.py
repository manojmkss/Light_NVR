import os
import sys

# Tests import the app package the same way the container does (cwd = backend/).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_camera_gate():
    """The per-camera gate is process-wide: one test's simulated camera
    failures must not leave a camera "resting" in the next test."""
    from app.services.camera_gate import camera_gate

    camera_gate._states.clear()
    yield
    camera_gate._states.clear()
