# LightNVR AI worker

Runs object detection on **a different machine from the NVR** — typically one
with a GPU. Point LightNVR at it and your NVR box stops doing inference
entirely.

You only need this if you want detection offloaded. LightNVR runs YOLO on its
own CPU out of the box, which is fine for a handful of cameras (detection only
fires on a motion event, never per frame).

## When it's worth it

| Situation | Use |
|---|---|
| A few cameras, x86 mini-PC | **Local (CPU)** — skip this entirely |
| Raspberry Pi, or many cameras | **This worker**, on a GPU box |
| You want a bigger model (`yolov8m`) | **This worker** |

## Setup (on the GPU machine)

```bash
git clone https://github.com/manojmkss/Light_NVR.git
cd Light_NVR/ai-worker
cp .env.example .env      # set AI_WORKER_API_KEY
docker compose up -d --build
```

Check it's alive — note the `device` field:

```bash
curl http://localhost:8811/health
# {"status":"ok","device":"cuda","default_model":"yolov8n",...}
```

`"device":"cpu"` means no GPU was found, and the worker is no faster than the
NVR doing the work itself. Fix your CUDA setup before wiring it up.

### NVIDIA GPU

1. Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) on the host.
2. In `.env`: `BASE_IMAGE=pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime`
3. In `docker-compose.yml`: uncomment the `deploy.resources` GPU block.
4. `docker compose up -d --build` and confirm `"device":"cuda"`.

## Connect LightNVR to it

**Settings → AI → Where it runs → "Another PC with a GPU"**

- Address: `http://<gpu-machine-ip>:8811`
- Password: whatever you set as `AI_WORKER_API_KEY`
- Press **Check it works** — it reports the device and round-trip latency.

## Security

The worker decodes any image POSTed to it and publishes on all interfaces so
the NVR can reach it. On a trusted home LAN that's fine. Otherwise **set
`AI_WORKER_API_KEY`**, and don't port-forward this to the internet — there's no
TLS here, only the shared key.

## What about Ollama / Claude / OpenAI?

Different job, and they're configured separately:

- **This worker** does *object detection* — "is there a person?" — fast enough
  to run on every motion event, and it returns boxes.
- **Ollama / Claude / OpenAI** do *descriptions* — "a delivery person left a
  package at the door". Set those under **Settings → AI → Describe what's
  happening**; they can point at any remote address too.

They're not interchangeable: a vision-language model is far too slow and
expensive to ask about every motion event, and won't give you reliable boxes.
The two are designed to be used together — detection filters the noise,
descriptions explain the handful of events that survive.

## API

```
GET  /health   -> {"status","device","default_model","loaded_models"}
POST /detect   -> multipart: image=<jpeg>, min_confidence=0.5, model=yolov8n
                  X-API-Key: <key>
               <- {"detections":[{"label","confidence","x","y","w","h"}],"took_ms","device"}
```

Coordinates are normalised 0..1 against the frame.
