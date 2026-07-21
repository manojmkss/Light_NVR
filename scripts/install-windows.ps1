<#
.SYNOPSIS
    LightNVR - automated Windows install.

.DESCRIPTION
    Checks/installs Docker Desktop, prepares the bind-mount directories,
    optionally opens LAN-only (Private/Domain profile) firewall rules, and
    brings the stack up. Mirrors scripts/install-linux.sh for the pieces
    Windows can actually automate - Docker Desktop's first-run setup
    (WSL2 enablement, EULA, a possible reboot) has real limits this script
    is honest about rather than trying to force through blindly.

    Remote access (Tailscale / Cloudflare Tunnel) is deliberately NOT
    handled here - it's a post-install step in the app itself (Settings ->
    Remote Access), started the moment you enable it there.

.PARAMETER Yes
    Don't prompt before system-level changes (Docker install, firewall rules).

.PARAMETER SkipFirewall
    Never touch firewall rules.

.PARAMETER DryRun
    Print what would happen; make no changes at all.

.EXAMPLE
    Right-click PowerShell -> Run as Administrator, then:
    .\scripts\install-windows.ps1 -Yes
#>

[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$SkipFirewall,
    [switch]$DryRun,
    [string]$StoragePath = ""
)

$ErrorActionPreference = 'Stop'

function Write-Step  { param([string]$Msg) Write-Host "==> $Msg" -ForegroundColor Cyan }
function Write-Warn2 { param([string]$Msg) Write-Host "!!  $Msg" -ForegroundColor Yellow }
function Write-Die   { param([string]$Msg) Write-Host "ERROR: $Msg" -ForegroundColor Red; exit 1 }

# Idempotent .env upsert - mirrors env_set in scripts/lib-env.sh so a Windows
# install writes the same machine-specific config into .env (gitignored) that
# the Linux flow does, keeping docker-compose.yml pristine and `git pull` clean.
function Set-EnvVar {
    param([string]$Key, [string]$Value)
    if ($DryRun) { Write-Host "[DRY RUN] set $Key=$Value in .env" -ForegroundColor DarkGray; return }
    $envFile = Join-Path $RepoRoot ".env"
    if (-not (Test-Path $envFile)) { New-Item -ItemType File -Path $envFile | Out-Null }
    $lines = @(Get-Content $envFile -ErrorAction SilentlyContinue)
    $out = New-Object System.Collections.Generic.List[string]
    $found = $false
    foreach ($line in $lines) {
        if ($line -match "^$([regex]::Escape($Key))=") { $out.Add("$Key=$Value"); $found = $true }
        else { $out.Add($line) }
    }
    if (-not $found) { $out.Add("$Key=$Value") }
    Set-Content -Path $envFile -Value $out -Encoding utf8
}

function Confirm-Step {
    param([string]$Prompt)
    if ($Yes) { return $true }
    $reply = Read-Host "$Prompt [y/N]"
    return ($reply -match '^[Yy]$')
}

function Invoke-MaybeDry {
    param([string]$Description, [scriptblock]$Action)
    if ($DryRun) {
        Write-Host "[DRY RUN] $Description" -ForegroundColor DarkGray
    } else {
        & $Action
    }
}

# ---------------------------------------------------------------------------
# 0. Preconditions
# ---------------------------------------------------------------------------

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Die "Re-run this from an elevated PowerShell (right-click PowerShell -> Run as Administrator)."
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if (-not (Test-Path (Join-Path $RepoRoot "docker-compose.yml"))) {
    Write-Die "docker-compose.yml not found in $RepoRoot - run this from inside the cloned repo."
}

Write-Step "Installing LightNVR from $RepoRoot"
if ($DryRun) { Write-Warn2 "Dry run - no changes will actually be made." }

# ---------------------------------------------------------------------------
# 1. Docker Desktop
# ---------------------------------------------------------------------------

$dockerOk = $false
try {
    docker compose version | Out-Null
    if ($LASTEXITCODE -eq 0) { $dockerOk = $true }
} catch { $dockerOk = $false }

if ($dockerOk) {
    Write-Step "Docker + Compose plugin already installed and running - skipping."
} else {
    $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($dockerCmd) {
        Write-Step "Docker is installed but the daemon isn't responding - trying to start Docker Desktop."
        $dockerExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
        if (Test-Path $dockerExe) {
            Invoke-MaybeDry "Start Docker Desktop" { Start-Process $dockerExe }
            if (-not $DryRun) {
                Write-Step "Waiting for the Docker daemon (up to 2 minutes - first start can be slow)..."
                $ready = $false
                for ($i = 0; $i -lt 24; $i++) {
                    try {
                        docker info | Out-Null
                        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
                    } catch {}
                    Start-Sleep -Seconds 5
                }
                if (-not $ready) {
                    Write-Die "Docker Desktop didn't come up in time. Start it manually (it may need first-run setup/EULA acceptance), then re-run this script."
                }
            }
        } else {
            Write-Die "Docker Desktop isn't at the expected path. Start it manually from the Start menu, then re-run this script."
        }
    } else {
        Write-Step "Docker Desktop is not installed."
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if ($winget -and (Confirm-Step "Install Docker Desktop now via winget?")) {
            Invoke-MaybeDry "winget install Docker.DockerDesktop" {
                winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
            }
            Write-Warn2 "Docker Desktop needs WSL2 and usually a sign-out/restart plus first-run EULA acceptance before it's usable."
            Write-Warn2 "Start Docker Desktop from the Start menu, finish its first-run setup, then re-run this script."
            if (-not $DryRun) { exit 0 }
        } else {
            Write-Die "Install Docker Desktop manually from https://www.docker.com/products/docker-desktop/ (requires WSL2), then re-run this script."
        }
    }
}

# ---------------------------------------------------------------------------
# 2. Bind-mount directories
# ---------------------------------------------------------------------------
# Note: /data (the SQLite DB + Tailscale state) is a named Docker volume, not
# a bind-mount directory - see docker-compose.yml for why (WAL-mode locking
# is unreliable across Docker Desktop's Windows<->Linux file-sharing layer).
# Nothing to create here for it; Docker creates the volume itself.

Write-Step "Preparing data directories."
foreach ($dir in @("storage", "primary-storage", "backup-storage", "certs")) {
    $path = Join-Path $RepoRoot $dir
    Invoke-MaybeDry "Create $path" { New-Item -ItemType Directory -Force -Path $path | Out-Null }
}

# ---------------------------------------------------------------------------
# 2b. Storage location
# ---------------------------------------------------------------------------
# Recordings go to PRIMARY_STORAGE_PATH (default: the in-repo folder). Point
# it at another drive by setting that one .env value - no compose edit. On
# Windows a Docker Desktop bind source is a host path like D:\NVR (or /d/NVR
# in the daemon's view); pass it with -StoragePath, or leave default.

if (-not $StoragePath -and -not $Yes) {
    Write-Step "Storage: where should recordings be written?"
    Write-Host "  Enter a host path on a dedicated drive (e.g. D:\NVR_Storage),"
    Write-Host "  or leave blank to keep recordings in the default in-repo folder."
    $StoragePath = Read-Host "  Storage path"
}
if ($StoragePath) {
    Invoke-MaybeDry "Create $StoragePath" { New-Item -ItemType Directory -Force -Path $StoragePath | Out-Null }
    Set-EnvVar "PRIMARY_STORAGE_PATH" $StoragePath
    Write-Step "Recordings will be stored at: $StoragePath"
} else {
    Write-Step "Storage: using the default in-repo folder (.\primary-storage)."
}

# ---------------------------------------------------------------------------
# 3. Migrate an old bind-mounted .\data into the named volume, if present
# ---------------------------------------------------------------------------
# Handles upgrading an existing install that predates the named-volume
# change: if there's a database sitting in the old .\data\ bind-mount
# location and the named volume is missing or empty, offer to copy it
# forward so accounts/cameras survive the upgrade instead of silently
# starting fresh against an empty volume. The old .\data\ folder is left
# untouched either way - this only ever copies forward, never deletes.

$VolumeName = "lightnvr-data"
$OldDataDb = Join-Path $RepoRoot "data\lightnvr.db"

if ((Test-Path $OldDataDb) -and (-not $DryRun)) {
    $volumeHasData = $false
    docker volume inspect $VolumeName | Out-Null
    if ($LASTEXITCODE -eq 0) {
        docker run --rm -v "${VolumeName}:/check" alpine sh -c "[ -f /check/lightnvr.db ]" | Out-Null
        if ($LASTEXITCODE -eq 0) { $volumeHasData = $true }
    }

    if ($volumeHasData) {
        Write-Step "Named volume '$VolumeName' already has a database - not touching the old data\ folder."
    } elseif (Confirm-Step "Found an existing database at data\lightnvr.db (pre-upgrade layout). Migrate it into the new named volume before starting?") {
        Write-Step "Migrating data\ into the '$VolumeName' volume..."
        docker volume create $VolumeName | Out-Null
        $oldDataDir = Join-Path $RepoRoot "data"
        docker run --rm -v "${oldDataDir}:/from:ro" -v "${VolumeName}:/to" alpine sh -c "cp -a /from/. /to/"
        Write-Step "Migrated. data\ is left in place as a safety copy - remove it yourself once you've confirmed everything works."
    } else {
        Write-Warn2 "Skipping migration - the backend will start with an EMPTY database until you migrate data\ manually."
    }
} elseif ((Test-Path $OldDataDb) -and $DryRun) {
    Write-Host "[DRY RUN] Would check whether data\lightnvr.db needs migrating into the '$VolumeName' volume." -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# 4. Firewall (Private/Domain profiles only - never Public/internet-facing)
# ---------------------------------------------------------------------------

if ($SkipFirewall) {
    Write-Step "Skipping firewall setup (-SkipFirewall)."
} else {
    $existing = Get-NetFirewallRule -DisplayName "LightNVR Web UI" -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Step "Firewall rule 'LightNVR Web UI' already exists - skipping."
    } elseif (Confirm-Step "Add a firewall rule allowing ports 8080/8443 on trusted (Private/Domain) networks?") {
        Invoke-MaybeDry "New-NetFirewallRule LightNVR Web UI" {
            New-NetFirewallRule -DisplayName "LightNVR Web UI" -Direction Inbound -Protocol TCP `
                -LocalPort 8080, 8443 -Action Allow -Profile Private, Domain | Out-Null
        }
    }
}

# ---------------------------------------------------------------------------
# 5. Bring the stack up
# ---------------------------------------------------------------------------

# Prefer prebuilt images from GHCR (a ~1-minute pull, no build); fall back to
# building from source if they can't be fetched.
Write-Step "Fetching prebuilt images and starting LightNVR (first run can take a few minutes)."
if ($DryRun) {
    Write-Host "[DRY RUN] docker compose pull; docker compose up -d" -ForegroundColor DarkGray
} else {
    docker compose pull
    if ($LASTEXITCODE -eq 0) {
        docker compose up -d
    } else {
        Write-Warn2 "Prebuilt images not available - building from source instead (slower)."
        docker compose up -d --build
    }
    if ($LASTEXITCODE -ne 0) { Write-Die "docker compose up failed - see the output above." }
}

# ---------------------------------------------------------------------------
# 6. Wait for it to come up
# ---------------------------------------------------------------------------

if (-not $DryRun) {
    Write-Step "Waiting for the backend to become healthy..."
    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        curl.exe -sk https://localhost:8443/api/health 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 3
    }
    if (-not $ready) { Write-Warn2 "Health check didn't succeed within 2 minutes - check 'docker compose logs backend'." }
}

# ---------------------------------------------------------------------------
# 7. Summary
# ---------------------------------------------------------------------------

$lanIp = $null
try {
    $candidate = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.PrefixOrigin -in @('Dhcp', 'Manual') -and $_.InterfaceAlias -notmatch 'Loopback|vEthernet|WSL' } |
        Select-Object -First 1
    if ($candidate) { $lanIp = $candidate.IPAddress }
} catch {}
if (-not $lanIp) { $lanIp = "<this-machine's-LAN-IP>" }

Write-Host ""
Write-Step "Done."
Write-Host "  Open:  https://${lanIp}:8443"
Write-Host "  Your browser will warn about the self-signed certificate on first visit - that's expected."
Write-Host "  The setup wizard creates your first admin account and walks through storage/cameras."
Write-Host ""
Write-Host "  The database lives in the '$VolumeName' Docker volume, not a plain folder - use the"
Write-Host "  in-app Backup feature (Settings -> Backup) to back it up, not a direct file copy."
Write-Host ""
Write-Host "  Machine-specific config is in .env (gitignored); docker-compose.yml is untouched,"
Write-Host "  so updates stay clean:  .\scripts\update-windows.ps1"
Write-Host ""
Write-Host "  Camera 'Scan network' (auto-discovery) isn't available on Windows/Docker Desktop -"
Write-Host "  its network layer can't put the container on your LAN. Add cameras by IP instead"
Write-Host "  (fully auto-detected). Automatic scanning works on a Linux host (see docs)."
Write-Host ""
Write-Host "  For remote access away from home (no port-forwarding needed), see"
Write-Host "  Settings -> Remote Access in the app once you're logged in (Tailscale or Cloudflare Tunnel)."
Write-Host ""
Write-Host "  Full reference: docs/linux-production-install.md (Windows-specific notes in the README)."
