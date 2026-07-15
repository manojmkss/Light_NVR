# LightNVR for Home Assistant

A monitoring integration for [LightNVR](https://github.com/manojmkss/Light_NVR) - camera
live view, motion/connectivity sensors, and system stats in Home Assistant. Everything is
read through LightNVR's own authenticated REST API (fast polling, no MQTT broker
required); nothing in this integration connects to a camera's raw RTSP stream directly.

**This is a monitoring-only integration.** It cannot change any camera setting (recording
mode, motion detection, enabled/disabled) - by design, so a login with a low-privilege
**viewer** account is all it needs and is what's recommended during setup.

## What you get

- One **camera** entity per LightNVR camera (live view via the always-on substream), plus
  an optional disabled-by-default **HD** camera entity for the on-demand main stream.
- **Motion** and **Connectivity** binary sensors per camera.
- A **Last motion** timestamp sensor per camera.
- System sensors (on one shared "hub" device): CPU, memory, storage used/free, uptime,
  motion events today, recordings today, recording failures today, cameras offline,
  estimated days until storage is full.

## Requirements

- A running LightNVR instance reachable from your Home Assistant box (same LAN, or via
  Tailscale/whatever remote access you already use).
- A LightNVR account - **viewer** role is enough and recommended (Settings → Users in the
  LightNVR app to create one, if you only have the admin account so far).

## Installation (manual - this folder isn't on HACS)

1. Copy the `custom_components/lightnvr/` folder from this repository into your Home
   Assistant configuration directory, so you end up with:
   ```
   <config>/custom_components/lightnvr/manifest.json
   <config>/custom_components/lightnvr/__init__.py
   ... (the rest of the files in this folder)
   ```
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration**, search for **LightNVR**.
4. Enter your LightNVR server's host/IP, port (8443 by default), and the viewer account's
   username/password. Leave **Verify SSL certificate** off unless you've installed a real
   certificate on LightNVR (it ships a self-signed one by default - this is expected, not
   a problem).
5. One device appears per camera, plus a hub device for system-wide sensors.

## Notes

- **Polling, not push.** State updates on an interval (cameras/motion every ~10s, system
  stats every ~60s by default) rather than instantly - configurable via the integration's
  **Configure** button (Settings → Devices & Services → LightNVR → Configure).
- **Self-signed certificate is the default and that's fine.** LightNVR ships one out of
  the box; this integration is built to work with it as-is.
- **A camera disappearing from LightNVR** (deleted) makes its entities go unavailable
  rather than disappearing from Home Assistant outright, so any automations/dashboard
  customizations referencing them aren't silently destroyed. Remove them manually from
  Settings → Devices & Services if you want them gone for good.
- **Reauthentication**: if the saved password stops working (changed on the LightNVR
  side), Home Assistant will prompt you to re-enter it - look for a notification on the
  integration in Settings → Devices & Services.
- Not included in this version, by design (would require an admin account and carries
  real risk of an automation accidentally disabling recording): switches/selects to
  change recording mode, toggle motion detection, or enable/disable a camera from Home
  Assistant, and a clip-export action. If you want that despite the risk, open an issue.
