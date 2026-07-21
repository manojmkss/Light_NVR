# Running LightNVR as a permanent Linux install

This walks through setting LightNVR up as an always-on home NVR on a Linux
box — a mini-PC, an old laptop, a Proxmox VM, or a Raspberry Pi. It goes
further than the README's quick start: network/firewall planning, storage
on a real disk, boot-time recovery, backups, and how to upgrade later
without losing data.

**Fast path:** once the code is on the box (§3), `sudo ./scripts/install-linux.sh`
does §2 and §4 for you — installs Docker if it's missing, creates the data
directories, optionally opens LAN-only firewall rules (§5), enables Docker
on boot, and brings the stack up, prompting before anything system-level
unless you pass `-y`. Sections 2-5 below are what it automates, spelled out
for understanding, customizing, or doing by hand if you'd rather.

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

Everything in this section runs **on the Linux box** — either sitting at it,
or over SSH from another machine on your network:

```bash
ssh youruser@192.168.68.xx   # the box's LAN IP + your Linux username
```

**Step 1 — make sure git is installed** (the recommended route; jump to
"Without git" below if you'd rather not install it):

```bash
git --version
# "command not found"? On Debian / Ubuntu / Raspberry Pi OS:
sudo apt update && sudo apt install -y git
```

**Step 2 — pick where the code will live.** Your home directory keeps it
owned by you, with no `sudo` needed to work inside it:

```bash
cd ~
```

**Step 3 — clone the repository:**

```bash
git clone https://github.com/manojmkss/Light_NVR.git
```

This downloads the whole project *and* its git history into a new
`Light_NVR/` folder. It's quick — just code, no recordings or Docker images
(those get built/created on first boot). The repo is public, so no login,
token, or SSH key is needed.

**Step 4 — go into the folder and confirm it landed:**

```bash
cd Light_NVR
ls                    # docker-compose.yml, backend/, frontend/, scripts/, ...
git log --oneline -1  # shows the latest commit
```

That's it — the code is on the box. Continue to §4, or just run
`sudo ./scripts/install-linux.sh`, which does §4 and §5 for you.

**Without git** — pull a tarball straight from GitHub instead:

```bash
cd ~
curl -fsSL https://github.com/manojmkss/Light_NVR/archive/refs/heads/main.tar.gz | tar xz
cd Light_NVR-main
```

Same files, minus the history. The tradeoff is updates: with `git clone` you
update later with a single `git pull`; with the tarball you re-download and
extract over the top each time. For a long-lived server the git route is worth
the one-time `apt install git`. (If the repo is ever made private, the git
route via `gh auth login` or an SSH deploy key is easier than a tokenized
tarball URL.)

## 4. First boot

```bash
docker compose up -d --build
```

(This is the one command `scripts/install-linux.sh` runs after handling §2
and §5 for you — run it directly here if you've already got Docker and just
want to start the stack.)

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

You may see `network_mode: "host"` suggested elsewhere as a way to make the
multicast scan itself work, since Linux (unlike Docker Desktop) supports it.
**Don't do this.** It's not just a security tradeoff — it actually breaks
the app. nginx reaches the backend by Docker DNS name (`backend:8000`) over
their shared network; host networking pulls the backend off that network
entirely, so the hostname stops resolving and nginx can no longer reach it
at all. On top of that, the backend's port would bind directly to the
host's `0.0.0.0` instead of the loopback-only mapping this project sets up
deliberately. Both problems are avoided by the option below instead.

### Making "Scan network" itself work: the macvlan override

If you want the actual broadcast scan to find cameras (rather than typing
each camera's IP), use the optional `docker-compose.macvlan.yml` override.
It gives the backend container a second network interface with its own real
IP directly on your LAN — enough for WS-Discovery's multicast probes to
reach your cameras — without touching how nginx reaches the backend on the
existing internal network. Nothing else about the app changes.

**Requirements:** a wired Ethernet connection to this box (macvlan doesn't
work reliably over Wi-Fi — most Wi-Fi drivers/access points reject one
radio presenting multiple MAC addresses). If this box is a VM, the VM's own
virtual NIC behaves like normal Ethernet regardless of what the physical
host underneath uses, but some hypervisors block the extra MAC address by
default — see the troubleshooting note below.

**The easy way — let the installer do it:**
```bash
sudo ./scripts/install-linux.sh --enable-scan
```
It auto-detects your interface, subnet, and gateway, finds and ping-verifies
a free IP, writes all of it (plus `COMPOSE_FILE`, so plain `docker compose
up` transparently loads the macvlan override from then on) to `.env`, and
brings the stack up. Skip to the verification step below. The rest of this
section is what it does under the hood, for doing it by hand or understanding
the pieces.

**1. Find your network details:**
```bash
ip route | grep default    # gives your interface name and gateway
ip -4 addr show            # gives your own address + subnet (CIDR)
```
From the first command, `dev ens18` (or `eth0`, `enp3s0`, etc.) is your
interface name and the `via` address is your gateway. From the second,
find that same interface's `inet` line for your subnet, e.g.
`192.168.68.210/24` means subnet `192.168.68.0/24`.

**2. Pick a free IP** for the container, inside that subnet but outside
your router's DHCP range if you know it. Confirm it's actually unused:
```bash
ping -c1 <candidate-ip>   # no reply = very likely free
```

**3. Add these five values to `.env`** (copy from `.env.example` if you
don't have one yet). `COMPOSE_FILE` is what makes plain `docker compose up`
load the override automatically, so you never need a two-file command:
```bash
LAN_SCAN_INTERFACE=ens18
LAN_SCAN_SUBNET=192.168.68.0/24
LAN_SCAN_GATEWAY=192.168.68.1
LAN_SCAN_BACKEND_IP=192.168.68.250
COMPOSE_FILE=docker-compose.yml:docker-compose.macvlan.yml
```

**4. Bring the stack up:**
```bash
docker compose up -d --build
```

**5. Verify the interface exists:**
```bash
docker compose exec backend ip addr show
```
You should see a second interface carrying your `LAN_SCAN_BACKEND_IP`. No
application changes are needed beyond this — the ONVIF discovery library
polls for local network addresses every few seconds and automatically
starts sending multicast probes out any new interface it finds, including
this one.

**If Scan still finds nothing after this:** on a VM, check whether the
hypervisor is silently dropping the extra MAC address. Proxmox: Datacenter
or VM → **Firewall** tab — either disable it for this VM's network device
or add a rule allowing it. VMware: the port group's **Security** policy —
set "Promiscuous Mode" and "Forged Transmits" to Accept.

## 6. Storage on a real disk

For anything more than a few days of footage, point Primary storage at a
dedicated drive rather than the same disk as the OS.

**Option A — a second local disk on this box:**
1. Mount it at, say, `/mnt/nvr-storage` on the host (fstab entry, or
   whatever your distro's disk manager gives you). Make sure it's actually
   mounted (`df -h /mnt/nvr-storage` should show the disk, not your root
   filesystem) — pointing storage at a path where the disk *isn't* mounted
   silently fills your OS disk instead.
2. Tell the installer to use it — no file editing:
   ```bash
   sudo ./scripts/install-linux.sh --storage-path=/mnt/nvr-storage
   ```
   That writes `PRIMARY_STORAGE_PATH=/mnt/nvr-storage` to `.env` (gitignored)
   and recreates the backend against the new disk. The tracked
   `docker-compose.yml` is left untouched, so upgrades stay clean, and the
   script warns you if the path isn't a real mount. (To do it by hand
   instead: put that one line in `.env` and run `docker compose up -d`.
   Docker can't attach a new host path to a running container, so the
   recreate is unavoidable either way.)
3. In Settings → Storage, set Primary type to **local**.

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
NAS/network outage doesn't mean lost footage during that window. For a local
backup drive, the installer doesn't have a `--backup-storage-path` flag yet
— set it directly: add `BACKUP_STORAGE_PATH=/mnt/nvr-backup` to `.env`, then
`docker compose up -d` to apply it. Same mount-point caution as Primary
applies: confirm `df -h` shows the actual disk before relying on it.

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
cd Light_NVR
./scripts/update-linux.sh
```

That pulls the latest code and rebuilds in one step. It works cleanly
because all your machine-specific config lives in the gitignored `.env`
(storage path, camera-scan settings), never in the tracked
`docker-compose.yml` — so the pull is always a plain fast-forward with
nothing to merge, and your `.env` (and its `COMPOSE_FILE`, if you enabled
scanning) is picked up automatically. If a new version ever needs a new
`.env` value, the script tells you which one rather than failing cryptically.

(Upgrading a box set up the old way, with a hand-edited `docker-compose.yml`?
The script detects that on first run, moves your storage path into `.env`,
and restores the tracked file — a one-time migration, after which updates are
clean. The equivalent by hand is `git pull && docker compose up -d --build`.)

Any new database columns are added automatically on startup (SQLite schema
sync runs as part of app startup) — there's no separate manual migration
step for normal schema changes. Camera configs, recordings, and accounts
all carry forward untouched since they live in the `lightnvr-data` volume and
the storage directories, not inside the container image.

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

**`permission denied while trying to connect to the docker API at
unix:///var/run/docker.sock`.** Your Linux user isn't in the `docker` group
yet, or was just added but this shell session hasn't picked it up. Fastest
unblock: `sudo docker compose up -d --build`. Durable fix so you don't need
`sudo` every time: `sudo usermod -aG docker $USER`, then either `newgrp docker`
(current shell only) or log out and back in — group membership never applies
retroactively to an already-open session. Verify with `groups` afterward;
`docker` should be in the list. `scripts/install-linux.sh` already handles
this for its own run (it installs as root throughout), so this mainly comes
up on a later manual `docker compose` command, e.g. the upgrade steps in §10.

**Camera shows offline in the app but works fine when you open its stream
directly.** Usually a scheme or credential mismatch the auto-detection
missed. Go to Cameras, click **Re-detect** on that camera — it re-runs the
full probe (tries `rtsp://` and `rtsps://`, retries with the `admin`
account on a 401, re-checks the sub-stream and codec) using the
credentials already saved, and updates the camera in place. No need to
delete and re-add it.

**A camera went offline after a router reboot / power cut.** Its DHCP lease
probably changed and it came back on a different IP. LightNVR heals this
itself: a background task notices the camera has been unreachable, scans your
network, matches the device by its ONVIF serial number, and updates the
stored address automatically — you'll see a "moved from … to …" note in the
events. To trigger it immediately instead of waiting, click **Locate** on the
offline camera's row. (This only works for cameras added via ONVIF, since it
needs the device serial to identify them.)

**Best fix of all: reserve DHCP addresses for your cameras.** In your router's
DHCP settings, bind each camera's MAC address to a fixed IP (a "DHCP
reservation" / "static lease"). Then cameras keep the same address across
reboots, and this whole class of problem never happens. The self-healing above
is the safety net for when you haven't.

**"Could not connect to ONVIF device" when adding a camera manually.**
Leave the port field blank — the backend probes the common ONVIF ports
(80, 8080, 8000, 2020, 8899, 8081) automatically rather than requiring you
to know which one your camera uses.

**Network scan finds nothing.** Expected on Docker's default bridge network
(multicast doesn't cross it). Easiest fix: use "connect directly by IP"
with the camera's address instead of debugging the scan. If you specifically
want the scan itself to work, see §5's `docker-compose.macvlan.yml`
walkthrough (do **not** use `network_mode: host` — see §5 for why that
breaks the whole app, not just discovery).

**Browser still shows the old UI after an upgrade.** The frontend is a PWA
with a service worker cache. Hard-refresh (Ctrl+Shift+R / Cmd+Shift+R) once
after `docker compose up -d --build` picks up a new frontend image.
