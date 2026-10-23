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
# --tags: without it the checkout never learns the release tags and builds from source.
if (-not $DryRun) { git fetch --tags origin $branch }

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
# 2b. The AI service. AI always runs in its own container now, never inside
#     the NVR (it competed with recording there). Docker Desktop can't hand an
#     Intel GPU to Linux containers, so Windows runs the CPU image; choose
#     the device in Settings -> AI -> AI engine. AI_WORKER=off opts out.
# ---------------------------------------------------------------------------

$EnvFile = Join-Path $RepoRoot ".env"
function Get-EnvVar {
    param([string]$Key)
    if (-not (Test-Path $EnvFile)) { return "" }
    $line = Get-Content $EnvFile | Where-Object { $_ -match "^$([regex]::Escape($Key))=" } | Select-Object -Last 1
    if ($line) { return ($line -split "=", 2)[1].Trim() } else { return "" }
}
function Set-EnvVar {
    param([string]$Key, [string]$Value)
    $lines = @()
    if (Test-Path $EnvFile) { $lines = @(Get-Content $EnvFile) }
    $found = $false
    $out = foreach ($l in $lines) {
        if ($l -match "^$([regex]::Escape($Key))=") { $found = $true; "$Key=$Value" } else { $l }
    }
    if (-not $found) { $out = @($out) + "$Key=$Value" }
    # No byte-order mark: docker compose would read it as part of the first key.
    [System.IO.File]::WriteAllLines($EnvFile, [string[]]$out, (New-Object System.Text.UTF8Encoding $false))
}

if (-not $DryRun -and (Get-EnvVar "AI_WORKER") -ne "off") {
    $aiFiles = @("docker-compose.ai.yml", "docker-compose.ai.intel.yml", "docker-compose.ai.nvidia.yml")
    $current = Get-EnvVar "COMPOSE_FILE"
    if (-not $current) { $current = "docker-compose.yml" }
    $parts = @($current -split "[;:]" | Where-Object { $_ -and ($aiFiles -notcontains $_) })
    # Windows compose separates COMPOSE_FILE entries with ';'.
    Set-EnvVar "COMPOSE_FILE" ((@($parts) + "docker-compose.ai.yml") -join ";")
    if ((Get-EnvVar "AI_WORKER_VARIANT_AUTO") -ne "off") { Set-EnvVar "AI_WORKER_VARIANT" "cpu" }
    if (-not (Get-EnvVar "AI_CPUS")) { Set-EnvVar "AI_CPUS" ([string][Math]::Max(1, [int]([Environment]::ProcessorCount / 2))) }
    if (-not (Get-EnvVar "AI_MEM_LIMIT")) { Set-EnvVar "AI_MEM_LIMIT" "1536m" }
    Write-Step "AI service: CPU image (choose it in Settings -> AI -> AI engine)."
}

# ---------------------------------------------------------------------------
# 3. Rebuild + restart
# ---------------------------------------------------------------------------

# Get the running containers onto the code we just checked out. Prebuilt images
# are only published for `v*` release tags, so an image can match this checkout
# only when HEAD *is* such a tag. Blindly pulling would otherwise "succeed"
# against a stale :latest and silently restart the OLD build over the new
# source, with no error to notice.
if (-not $DryRun) {
    $tagAtHead = (git tag --points-at HEAD 2>$null | Where-Object { $_ -match '^v[0-9]' } | Sort-Object | Select-Object -Last 1)

    if ($tagAtHead) {
        # Pull that version by name rather than the moving :latest, so what
        # starts is exactly what this checkout says it is.
        $env:LIGHTNVR_VERSION = $tagAtHead -replace '^v', ''
        Write-Step "Now at release $tagAtHead - fetching its prebuilt images..."
        docker compose pull
        if ($LASTEXITCODE -eq 0) {
            docker compose up -d
        } else {
            Write-Warn2 "Prebuilt images for $tagAtHead aren't published (yet) - building from source instead."
            docker compose up -d --build
        }
    } else {
        # Newer commits than the last release: no published image contains this
        # code, so building is the only way to run what was just pulled.
        Write-Step "This checkout has changes newer than the last release, so no prebuilt image matches it."
        Write-Step "Building from source (slower, but it's the code you just pulled)..."
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
