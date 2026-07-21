<#
.SYNOPSIS
    LightNVR - one-command updater (Windows).

.DESCRIPTION
    Pulls the latest code from GitHub and rebuilds the stack, preserving your
    local .env configuration. Clean because every machine-specific setting
    lives in the gitignored .env (written by install-windows.ps1), never in
    the tracked docker-compose.yml - so the pull is a plain fast-forward.

.PARAMETER Yes
    Don't pause for confirmation before pulling/rebuilding.

.PARAMETER DryRun
    Show what would happen; change nothing.

.EXAMPLE
    .\scripts\update-windows.ps1
#>

[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Write-Step  { param([string]$Msg) Write-Host "==> $Msg" -ForegroundColor Cyan }
function Write-Warn2 { param([string]$Msg) Write-Host "!!  $Msg" -ForegroundColor Yellow }
function Write-Die   { param([string]$Msg) Write-Host "ERROR: $Msg" -ForegroundColor Red; exit 1 }
function Confirm-Step {
    param([string]$Prompt)
    if ($Yes) { return $true }
    return ((Read-Host "$Prompt [y/N]") -match '^[Yy]$')
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
if (-not (Test-Path (Join-Path $RepoRoot "docker-compose.yml"))) {
    Write-Die "docker-compose.yml not found in $RepoRoot - run this from inside the cloned repo."
}

git rev-parse --git-dir *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Die "This isn't a git checkout - update only works on a 'git clone' install, not a ZIP download."
}

# ---------------------------------------------------------------------------
# 1. Require a clean tracked tree (the .env-driven layout means there should
#    never be tracked-file edits to preserve; .env is gitignored and safe)
# ---------------------------------------------------------------------------

$dirty = git status --porcelain | Where-Object { $_ -notmatch '^\?\?' }
if ($dirty) {
    Write-Warn2 "You have local changes to tracked files that would block a clean update:"
    $dirty | ForEach-Object { Write-Host "    $_" }
    Write-Die "Revert them (git checkout -- <file>), then re-run. Your .env is gitignored and safe."
}

# ---------------------------------------------------------------------------
# 2. Pull
# ---------------------------------------------------------------------------

$branch = (git rev-parse --abbrev-ref HEAD).Trim()
Write-Step "Fetching latest on '$branch'..."
if (-not $DryRun) { git fetch origin $branch }

$behind = (git rev-list --count "HEAD..origin/$branch").Trim()
if ($behind -eq "0" -and -not $DryRun) {
    Write-Step "Already up to date. Rebuilding anyway to pick up any local image changes."
} else {
    Write-Step "$behind new commit(s) to apply."
    if (-not (Confirm-Step "Pull and rebuild now?")) { Write-Die "Aborted." }
    if (-not $DryRun) {
        git merge --ff-only "origin/$branch"
        if ($LASTEXITCODE -ne 0) { Write-Die "Can't fast-forward (local history diverged). Resolve manually, then re-run." }
    }
}

# ---------------------------------------------------------------------------
# 3. Rebuild + restart
# ---------------------------------------------------------------------------

Write-Step "Fetching updated images and restarting..."
if (-not $DryRun) {
    docker compose pull
    if ($LASTEXITCODE -eq 0) {
        docker compose up -d
    } else {
        Write-Warn2 "Prebuilt images not available - building from source instead (slower)."
        docker compose up -d --build
    }
    if ($LASTEXITCODE -ne 0) { Write-Die "docker compose up failed - see the output above." }

    Write-Step "Waiting for the backend to become healthy..."
    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        curl.exe -sk https://localhost:8443/api/health 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 3
    }
    if ($ready) { Write-Step "Update complete - backend healthy." }
    else { Write-Warn2 "Health check didn't pass within 2 minutes - check 'docker compose logs backend'." }
}

Write-Host ""
Write-Step "Tip: the app is a PWA and caches in the browser. If the UI looks"
Write-Host "    unchanged after updating, hard-refresh once with Ctrl+Shift+R."
