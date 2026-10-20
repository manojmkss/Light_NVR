"""Model code shared by the NVR backend and the LightNVR AI worker.

Pure numpy / OpenCV / onnxruntime - no web framework, no database - so the
same detector decoding, face pipeline and engine selection run identically
whether inference happens in the NVR process or in the ai-worker sidecar.
"""
