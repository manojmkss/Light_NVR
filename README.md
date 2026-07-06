# LightNVR

A self-hosted home NVR. FastAPI + SQLite backend, React frontend, FFmpeg for recording, OpenCV for motion detection. Runs anywhere Docker runs: Linux, Windows, Proxmox.

## Quick start

### 1. Get the code onto the machine

**With git** (if you have it):

```bash
git clone https://github.com/manojmkss/Light_NVR.git
cd Light_NVR
```

**Without git** — most Windows PCs won't have git installed, and you don't
need it. Download the code as a ZIP straight from GitHub:

1. Open **https://github.com/manojmkss/Light_NVR**
2. Click the green **`<> Code`** button → **Download ZIP**
3. Extract it. On Windows: right-click the downloaded ZIP → *Extract All*.
   You'll get a folder named `Light_NVR-main`.
4. Open a terminal **inside** that folder. On Windows: hold **Shift**,
   right-click the folder → *Open in Terminal* (or *Open PowerShell window here*).

To update later: with git, `git pull`; without git, download a fresh ZIP and
extract it over the old folder.

### 2. Install it

**Automated (recommended)** — installs Docker if missing, prepares data
directories, optionally opens LAN-only firewall rules, and brings the stack
up in one step:

```bash
# Linux
sudo ./scripts/install-linux.sh
```
```powershell
# Windows — run PowerShell as Administrator, from inside the folder.
# The -ExecutionPolicy Bypass is only needed if you downloaded the ZIP:
# Windows blocks scripts that came from a downloaded file by default.
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

Both prompt before any system-level change (installing Docker, touching the
firewall) — pass `-y`/`-Yes` to skip prompts, or `--dry-run`/`-DryRun` to
preview with no changes at all. Windows note: Docker Desktop's first-run
setup (WSL2 enablement, EULA, sometimes a restart) can't be fully automated
blind — if Docker isn't already installed, the script starts the winget
install and tells you to finish that setup once, then re-run it.

**Manual** — works the same whether you used git or the ZIP; just needs
Docker already installed and running:

```bash
docker compose up -d --build
```

No `.env` file needed either way. Open `https://<host>:8443` (your browser will warn
about the self-signed certificate on first visit — that's expected; accept
it to continue, or install a real cert later from Settings → Security) and
the setup wizard walks you through creating the first admin account, picking
a storage location, and (optionally) scanning for cameras — all through the
browser. The JWT signing secret is generated automatically on first boot and
persisted, so it survives restarts without you having to invent or paste in
a random string.

`.env` is only for advanced/scripted setups (pinning ports, unattended admin
bootstrap, a custom database path) — see `.env.example` for what's available
and why you'd want it. For a permanent home-server install, see
[docs/linux-production-install.md](docs/linux-production-install.md).

## Architecture

| Service  | Role                                                              |
|----------|--------------------------------------------------------------------|
| backend  | FastAPI app: auth, camera management, recording engine, motion detection, storage management, alerts |
| frontend | React SPA, built and served as static files via nginx              |
| nginx    | Reverse proxy: routes `/api` to backend, everything else to frontend |

Data persists across restarts:
- The SQLite database (accounts, cameras, settings, Tailscale state) lives in a named Docker volume (`lightnvr-data`), not a plain host folder — SQLite's WAL mode needs real POSIX locking, which is unreliable across Docker Desktop's Windows/Mac file-sharing layer on a bind mount. Back it up from **Settings → Backup** in the app, not by copying a file directly.
- `./storage` — local recording cache (see Storage management below)
- `./primary-storage`, `./backup-storage` — default local primary/backup destinations

The recording directories are bind-mounted, so `docker compose down && docker compose up -d` (or a host reboot, with Docker configured to start on boot) picks up exactly where it left off — cameras, users, and recordings all survive restarts.

Upgrading from a version that predated the named volume? `scripts/install-linux.sh` / `install-windows.ps1` detect an old `./data/lightnvr.db` and offer to migrate it into the volume automatically (see the scripts for the manual steps if you'd rather not use them).

## Adding a camera

From **Cameras → Add camera** you get two paths:

1. **Auto-discover (ONVIF)** — scans the LAN for ONVIF devices, or connects directly to a camera IP you type in. Either way, once connected with the camera's ONVIF credentials, it lists the camera's media profiles and lets you pick the main (recording) and sub (live view/motion) streams — RTSP URLs are filled in automatically.
2. **Manual RTSP** — paste an RTSP URL directly for cameras without ONVIF support, with a "Test connection" button that probes codec/resolution/audio via ffprobe.

### A note on ONVIF network discovery in Docker

The "Scan network" button uses WS-Discovery, which relies on UDP multicast. Multicast does not cross Docker's default bridge network — so on Windows (Docker Desktop) and many default Linux setups, the scan may find nothing even though your cameras are reachable.

This doesn't block setup: the **"connect directly by IP"** field next to the scan button does a direct (unicast) ONVIF connection, which works fine over the default bridge network and still gives you automatic RTSP/profile configuration. Manual RTSP entry works unconditionally too.

If you specifically want the broadcast scan to work and you're on Linux/Proxmox, add this to the `backend` service in `docker-compose.yml`:

```yaml
backend:
  network_mode: "host"
  # remove the `ports:` entry under backend when using host networking
```

This is Linux-only — Docker Desktop on Windows/Mac doesn't support host networking.

## Recording modes

Set per-camera under Cameras → Edit:
- **Continuous** — records back-to-back 5-minute segments, stream-copied (no re-encode) from the main stream.
- **Motion-triggered** — only records while motion is detected (plus a short post-roll).
- **Off** — no recording; useful with motion detection enabled for alert-only monitoring.

Motion detection (`motion_enabled`) is independent of recording mode — you can run continuous recording *and* get motion alerts/event tagging at the same time. Detection runs on the substream when configured, to keep CPU usage low.

## Storage management

Recordings move through up to three tiers, configured from **Settings → Storage** (admin only):

1. **Cache** (always local, always on) — every recording is written here first. This is the key reliability property: FFmpeg never writes directly to a network path, so a flaky NAS connection can never stall or corrupt an active recording. Capped by `cache_max_gb`; if the cache fills (NAS down for a long time), new recordings pause with an alert rather than overflowing the disk or silently dropping footage.
2. **Primary** — where finished recordings end up within seconds, moved there by a background job. Choose **local dedicated drive**, **SMB/CIFS share**, or **NFS share** as the type. For network shares, the backend mounts the share itself using the credentials you enter — no host-side setup needed. For a local dedicated drive, point the `/mnt/primary` volume in `docker-compose.yml` at your drive's mount path and restart (Docker can't attach a new host path to a running container, so this one edit + restart is unavoidable).
3. **Backup** (optional) — used only when Primary is unreachable, so an extended NAS outage doesn't mean lost footage. This is failover, not mirroring: once a recording lands on Backup it stays there permanently rather than being moved to Primary once it recovers.

Retention/rotation works at two levels:
- **Per-camera** — set "Retention override (days)" when editing a camera (Cameras → Edit). Leave blank to use the global default.
- **Global age limit** — `RETENTION_DAYS` in `.env`, used for any camera without its own override.
- **Global size cap (backstop)** — `MAX_STORAGE_GB` in `.env`, optional; if set, the system-wide oldest recordings are deleted first once total usage exceeds it, regardless of any camera's individual retention setting. This guarantees total usage can't run away even if per-camera settings are misconfigured.

A background job re-checks retention every 30 minutes, and storage health (mount/write probes for cache, primary, and backup) is re-checked every 30 seconds — both reflected live on the Dashboard and in Settings → Storage. A low-storage alert fires when free space on Primary drops below the configurable threshold in Settings → Alerts.

### The SMB/NFS mounting tradeoff

Mounting network shares from inside the backend container (rather than requiring you to mount them on the host first) needs `CAP_SYS_ADMIN` — already added to `docker-compose.yml` — since the `mount` syscall requires it. This is a real privilege increase over a default container. If you'd rather not grant it, mount the share on the host OS yourself and bind-mount it into `/mnt/primary` (or `/mnt/backup`) like a local drive instead, then leave the type as "local" in Settings → Storage.

NFS mounts use `soft` semantics deliberately (not the kernel default `hard`) — a hard mount makes any process touching it block forever if the server goes away, which would defeat the whole point of having a backup tier to fail over to.

## Email alerts

Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `ALERT_EMAIL_TO` in `.env` to enable. Alert toggles and thresholds (motion cooldown, low-storage %) are configurable per-deployment in Settings → Alerts (admin only).

## Accounts

Two roles: `admin` (full control) and `viewer` (read-only: live view, recordings, dashboard). Manage accounts under Settings → Users (admin only).

## Development (without Docker)

Backend:
```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```
Requires `ffmpeg` on PATH. Set env vars from `.env.example` (or rely on the defaults baked into `app/core/config.py` for local dev).

Frontend:
```bash
cd frontend
npm install
npm run dev
```
Proxies `/api` to `http://localhost:8000` by default (see `vite.config.ts`).

## Contributing

Bug reports and PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for
dev setup and conventions. Found a security issue? See
[SECURITY.md](SECURITY.md) instead of opening a public issue.

## License

[MIT](LICENSE)
