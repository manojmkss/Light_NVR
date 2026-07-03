# Running LightNVR as a permanent Linux install

This walks through setting LightNVR up as an always-on home NVR on a Linux
box — a mini-PC, an old laptop, a Proxmox VM, or a Raspberry Pi. It goes
further than the README's quick start: network/firewall planning, storage
on a real disk, boot-time recovery, backups, and how to upgrade later
without losing data.

## 1. Choosing hardware

There's no GPU/hardware video acceleration in this project — every stream
that needs decoding (motion detection, live view) is decoded in software by
FFmpeg/OpenCV. The one thing that *is* cheap is continuous recording itself:
it's a stream copy (`-c:v copy`, no re-encode), so it costs almost no CPU
regardless of camera count or resolution.

What actually drives CPU load:
- **Motion detection** — one software decode + OpenCV background-subtraction
  pass per camera with it enabled, running continuously. This is the
  dominant cost. It decodes the *sub*-stream when you've configured one
  (much cheaper than decoding the main 1080p/4K stream), so setting a low-res
  sub-stream on each camera during setup matters more than raw core count.
- **Live view** — decoded on demand, only while someone has the page open.

Rule of thumb: a 4-core x86 mini-PC (or an equivalent N100/Celeron-class
box) comfortably handles a handful of cameras with motion detection on,
using each camera's sub-stream. A Raspberry Pi 4/5 works for 2-4 cameras —
the codebase specifically calls out low-power ARM boards as a target for
the sub-stream-based motion detection design. If you're going past ~6-8
cameras with motion detection on all of them, size up to something with
more cores; there's no way to offload that work to a GPU here today.

Storage: separate from the OS disk if at all possible (see §6) — recordings
are the one thing on this box you don't want sharing an SSD's write cycles
or a single drive's failure domain with the OS.

## 2. Install Docker Engine

LightNVR runs entirely in Docker. On Debian/Ubuntu (including Raspberry Pi
OS, which is Debian-based):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker   # or log out/in for the group change to take effect
docker compose version   # confirms the Compose plugin is present
```

For other distros, follow Docker's own install docs — the convenience
script above covers the common home-server cases. Proxmox: run this inside
an LXC container or VM, not on the Proxmox host itself.

## 3. Get the code onto the box

```bash
git clone https://github.com/<you>/lightnvr.git
cd lightnvr
```

(Swap in your actual repo URL once it's published. If the repo is private,
you'll need `gh auth login` or an SSH deploy key set up first.)

## 4. First boot

```bash
docker compose up -d --build
```

This builds the backend and frontend images and starts all three
containers (backend, frontend, nginx). First build takes a few minutes;
after that, restarts are seconds.

Open `https://<this-box's-LAN-IP>:8443` from another device on your
network. Your browser will warn about the self-signed certificate — that's
expected on first boot; accept it to continue. The setup wizard then walks
you through:
1. Creating the first admin account
2. Picking a storage location (local path is fine to start — you can point
   it at a dedicated drive later, see §6)
3. Optionally scanning for cameras

No `.env` file is required for any of this — see the README if you need one
for pinned ports or scripted/unattended deployment.

## 5. Network and firewall planning

**Default posture is LAN-only, and that's what you want.** Do not forward
port 8443 (or 8080) on your router to the internet — this app holds camera
credentials and your home's video, and a self-signed cert plus password
auth is not what you want directly exposed to the open internet. Use the
built-in remote-access options instead (§7).

A minimal `ufw` setup on the LightNVR box itself:

```bash
sudo ufw allow ssh
sudo ufw allow from 192.168.0.0/16 to any port 8080,8443 proto tcp
sudo ufw enable
```

Adjust the subnet to match your actual LAN range. This lets anyone on your
home network reach the app, but nothing from outside it.

### About ONVIF camera discovery and host networking

The "Scan network" button in Add Camera uses WS-Discovery, which relies on
UDP multicast — multicast doesn't cross Docker's default bridge network, so
the scan may find nothing even though your cameras are reachable. This
doesn't block setup: **"connect directly by IP"** does a direct (unicast)
ONVIF connection instead, works fine over the default bridge network, and
still gets you full automatic stream/codec detection (the backend probes
common ONVIF ports automatically, tries both `rtsp://` and `rtsps://`, and
falls back to the `admin` account if your typed username gets a 401 — so
typing the camera's IP is usually all you need).

The README mentions `network_mode: "host"` as a way to make the multicast
scan itself work, since Linux (unlike Docker Desktop) supports it. **For a
production install, avoid this.** Host networking bypasses Docker's port
publishing entirely, which means the backend's port binds directly to the
host's `0.0.0.0` instead of the loopback-only mapping this project sets up
deliberately (localhost-only, precisely so the API is never reachable
except through nginx's HTTPS/security-header layer). Losing that is a real
regression for a small discovery convenience you don't need — "connect
directly by IP" gets you the same end result safely.

## 6. Storage on a real disk

For anything more than a few days of footage, point Primary storage at a
dedicated drive rather than the same disk as the OS.

**Option A — a second local disk on this box:**
1. Mount it at, say, `/mnt/nvr-storage` on the host (fstab entry, or
   whatever your distro's disk manager gives you).
2. Edit `docker-compose.yml`, change the `primary-storage` bind mount:
   ```yaml
   volumes:
     - /mnt/nvr-storage:/mnt/primary
   ```
3. `docker compose up -d` to recreate the backend container with the new
   mount (Docker can't attach a new host path to a running container, so
   this one edit + restart is unavoidable — it's not a limitation specific
   to this app).
4. In Settings → Storage, set Primary type to **local**.

**Option B — a NAS (SMB or NFS share):** leave the docker-compose volumes
alone. In Settings → Storage, set Primary type to **SMB** or **NFS** and
enter the share path and credentials — the backend mounts the share itself
at runtime (it's granted `CAP_SYS_ADMIN` for exactly this in
`docker-compose.yml`; if you'd rather not grant that, mount the share on
the host OS yourself and bind it into `/mnt/primary` like option A instead,
then leave the type as "local"). NFS mounts use `soft` semantics
deliberately, so an NVR outage can't hang the whole recording pipeline
waiting on a dead network mount.

Consider also enabling the optional **Backup** tier (Settings → Storage) —
a second location (another local path, or a different NAS share) that only
receives new recordings when Primary is unreachable, so an extended
NAS/network outage doesn't mean lost footage during that window.

Retention: set a global default under Settings → Storage, override per
camera under Cameras → Edit if some cameras need to be kept longer/shorter,
and optionally set a global size cap as a backstop that deletes the
system-wide oldest recordings first if total usage ever exceeds it,
regardless of individual camera settings.

## 7. Remote access without port-forwarding

Two options are built in, both under **Settings → Remote Access**:

- **Tailscale** — puts this box on your private Tailscale network (a
  WireGuard mesh VPN). Once enabled with an auth key, you reach the NVR
  from your phone/laptop anywhere as if you were on the home LAN, with no
  router configuration and nothing exposed publicly. This is the
  recommended option for personal remote access.
- **Cloudflare Tunnel** — for sharing access with people who don't have
  Tailscale installed (e.g. family). Runs an outbound-only tunnel through
  Cloudflare, so again nothing is opened on your router.

Both run as backend-managed subprocesses (started/stopped the moment you
toggle them in the GUI) — no docker-compose or `.env` editing needed beyond
what's already in the default `docker-compose.yml` (`NET_ADMIN` +
`/dev/net/tun` are already granted, since Tailscale needs them the moment
you turn it on).

## 8. Surviving reboots and crashes

Docker Compose already sets `restart: unless-stopped` on all three
containers, so a crash or host reboot brings everything back automatically
— you just need the Docker daemon itself to start on boot:

```bash
sudo systemctl enable docker
```

That's the entire "make it survive a reboot" story — no custom systemd unit
needed on top of Compose's own restart policy.

On top of that, the app has its own startup recovery: it checks SQLite
integrity, cleans up any recording file left behind by an unclean shutdown
(power loss mid-write), and resumes normal operation — you'll see lines
like `Recovery: removed N orphaned recording file(s)` in
`docker compose logs backend` after a hard power-cycle. That's expected
self-healing, not an error.

## 9. Backups

Config backups (accounts, camera configs, all settings — not the recorded
video itself) are **on by default**: every 24 hours, keeping the last 14,
landing in your Primary storage location. Check/adjust this under
Settings → Backup, and you can trigger one manually or download it there
too. If your NVR box itself dies, restoring one of these onto a fresh
install gets every camera and setting back without re-entering anything.

The recorded video itself is only as durable as your storage setup from
§6 — a Backup storage tier is the video-durability equivalent of this
config backup.

## 10. Upgrading later

```bash
cd lightnvr
git pull
docker compose up -d --build
```

Any new database columns are added automatically on startup (SQLite schema
sync runs as part of app startup) — there's no separate manual migration
step for normal schema changes. Camera configs, recordings, and accounts
all carry forward untouched since they live in the bind-mounted `data/` and
storage directories, not inside the container image.

Check `docker compose logs backend` after an upgrade to confirm a clean
startup (`Application startup complete`) before considering it done.

## 11. Verifying the install

```bash
docker compose ps                              # all three should show "Up" / "healthy"
curl -sk https://localhost:8443/api/health      # {"status":"ok"}
docker compose logs backend --tail=30           # no repeated errors/warnings
```

Then from a browser on your LAN: log in, add a camera, confirm the live
tile shows video and the status badge goes green (online).

## 12. Troubleshooting

**Camera shows offline in the app but works fine when you open its stream
directly.** Usually a scheme or credential mismatch the auto-detection
missed. Go to Cameras, click **Re-detect** on that camera — it re-runs the
full probe (tries `rtsp://` and `rtsps://`, retries with the `admin`
account on a 401, re-checks the sub-stream and codec) using the
credentials already saved, and updates the camera in place. No need to
delete and re-add it.

**"Could not connect to ONVIF device" when adding a camera manually.**
Leave the port field blank — the backend probes the common ONVIF ports
(80, 8080, 8000, 2020, 8899, 8081) automatically rather than requiring you
to know which one your camera uses.

**Network scan finds nothing.** Expected on Docker's default bridge network
(multicast doesn't cross it) — use "connect directly by IP" with the
camera's address instead of debugging the scan (see §5 for why host
networking isn't the recommended fix).

**Browser still shows the old UI after an upgrade.** The frontend is a PWA
with a service worker cache. Hard-refresh (Ctrl+Shift+R / Cmd+Shift+R) once
after `docker compose up -d --build` picks up a new frontend image.
