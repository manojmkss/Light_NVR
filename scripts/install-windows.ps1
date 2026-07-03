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
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Write-Step  { param([string]$Msg) Write-Host "==> $Msg" -ForegroundColor Cyan }
function Write-Warn2 { param([string]$Msg) Write-Host "!!  $Msg" -ForegroundColor Yellow }
function Write-Die   { param([string]$Msg) Write-Host "ERROR: $Msg" -ForegroundColor Red; exit 1 }

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

Write-Step "Preparing data directories."
foreach ($dir in @("data", "storage", "primary-storage", "backup-storage", "certs")) {
    $path = Join-Path $RepoRoot $dir
    Invoke-MaybeDry "Create $path" { New-Item -ItemType Directory -Force -Path $path | Out-Null }
}

# ---------------------------------------------------------------------------
# 3. Firewall (Private/Domain profiles only - never Public/internet-facing)
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
# 4. Bring the stack up
# ---------------------------------------------------------------------------

Write-Step "Building and starting LightNVR (this can take a few minutes on first run)."
Invoke-MaybeDry "docker compose up -d --build" {
    docker compose up -d --build
    if ($LASTEXITCODE -ne 0) { Write-Die "docker compose up failed - see the output above." }
}

# ---------------------------------------------------------------------------
# 5. Wait for it to come up
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
# 6. Summary
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
Write-Host "  For remote access away from home (no port-forwarding needed), see"
Write-Host "  Settings -> Remote Access in the app once you're logged in (Tailscale or Cloudflare Tunnel)."
Write-Host ""
Write-Host "  Full reference: docs/linux-production-install.md (Windows-specific notes in the README)."
