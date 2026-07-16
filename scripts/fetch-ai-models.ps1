<#
.SYNOPSIS
    Downloads the ONNX model the optional AI layer uses for local (CPU) object
    detection, into the lightnvr-data Docker volume at /data/models.

.DESCRIPTION
    Windows equivalent of scripts/fetch-ai-models.sh - same result, no Git Bash
    or WSL needed.

    Why a script rather than an auto-download at runtime: pulling a model from
    the internet on first motion event would make a security product silently
    fetch a remote binary blob at the least predictable moment, with no operator
    consent. This makes it an explicit, auditable step you run once - and it's
    skippable entirely if you use the "Another PC with a GPU" backend, since the
    ai-worker ships its own models.

    The export runs inside a throwaway container, so nothing is installed on
    your host.

.PARAMETER Model
    Which model to fetch. Default yolov8n (fastest, best for CPU).

.EXAMPLE
    .\scripts\fetch-ai-models.ps1
    .\scripts\fetch-ai-models.ps1 -Model yolov8s

.EXAMPLE
    # If PowerShell blocks the script because it came from a downloaded ZIP:
    powershell -ExecutionPolicy Bypass -File .\scripts\fetch-ai-models.ps1
#>

[CmdletBinding()]
param(
    [ValidateSet("yolov8n", "yolov8s", "yolov8m", "yolo11n", "yolo11s", "yolo11m")]
    [string]$Model = "yolov8n"
)

# Deliberately NOT 'Stop'.
#
# Windows PowerShell 5.1 turns every stderr line from a native exe into an
# ErrorRecord when its output is piped or redirected. pip ("running as root")
# and ultralytics (download progress) both write to stderr during a completely
# successful export - with 'Stop' that aborts the script and reports a working
# export as a failure, which is exactly the trap this script kept falling into.
#
# Native stderr is not an error here. Success is judged on the two things that
# actually mean success: the exit code, and whether the file really landed in
# the volume (Test-ModelInVolume below). Errors we do care about are raised
# explicitly via Write-Die.
$ErrorActionPreference = 'Continue'

function Write-Step { param([string]$m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Warn2 { param([string]$m) Write-Host "!!  $m" -ForegroundColor Yellow }
function Write-Die { param([string]$m) Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

$Volume = "lightnvr-data"

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

try {
    docker version --format '{{.Server.Version}}' 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "daemon not responding" }
} catch {
    Write-Die "Docker isn't running. Start Docker Desktop (wait for the whale icon to stop animating), then re-run this."
}

docker volume inspect $Volume 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Die "Docker volume '$Volume' not found - start LightNVR once first (docker compose up -d), then re-run this."
}

function Test-ModelInVolume {
    param([string]$Name)
    # Echoes a sentinel rather than relying on `test`'s exit code: exit codes
    # from `docker run` get muddled on the way back through PowerShell, and a
    # false negative here would either re-run a 10-minute export needlessly or
    # (at the end) declare a perfectly good export a failure.
    $out = docker run --rm -v "${Volume}:/data" alpine sh -c "[ -f /data/models/$Name.onnx ] && echo FOUND || echo MISSING"
    return ($out -join "").Trim() -eq "FOUND"
}

# Skip the ~2GB build image pull when the model is already installed.
if (Test-ModelInVolume $Model) {
    Write-Step "Already installed: /data/models/$Model.onnx - nothing to do."
    Write-Host ""
    Write-Host "  Enable it at: Settings -> AI -> Use AI -> Where it runs: This machine (CPU)"
    exit 0
}

# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

Write-Step "Exporting $Model to ONNX and installing it into the '$Volume' volume."
Write-Host "    First run pulls a ~2GB build image; the exported model itself is only a few MB." -ForegroundColor DarkGray
Write-Host "    This can take several minutes. " -ForegroundColor DarkGray

# The export runs as a Python script mounted into the container, rather than
# being passed inline to `bash -c`. Inline was tried and is a trap: the string
# crosses PowerShell -> docker CLI -> sh quoting layers, and the embedded
# newlines/heredoc get mangled ("syntax error: unexpected end of file"). A file
# on a volume crosses none of those layers.
#
# Single-quoted here-string so PowerShell leaves $ and backticks alone - this is
# Python source, not PowerShell. The closing '@ must be at column 0.
$runner = @'
set -e
echo "Installing build dependencies (a few minutes on first run)..."
# ultralytics hard-depends on opencv-python (the GUI build), which needs X11
# libs this slim image lacks - without these the import dies with
# "libxcb.so.1: cannot open shared object file". Same two packages the backend
# image installs for exactly this reason.
#
# DEBIAN_FRONTEND (set on the container) stops debconf trying to open an
# interactive dialog it can never have in a non-TTY container and then dumping
# a wall of "unable to initialize frontend" fallback chatter to stderr. It
# always recovered on its own, but it reads like a crash.
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq --no-install-recommends libgl1 libglib2.0-0 >/dev/null 2>&1
echo "Installing ultralytics (this is the slow part)..."
pip install --no-cache-dir --quiet ultralytics onnx onnxruntime
echo "Exporting to ONNX..."
python /work/export.py "$1"
'@

$py = @'
import shutil, sys
from ultralytics import YOLO

model = sys.argv[1]
dest = f"/data/models/{model}.onnx"
m = YOLO(f"/tmp/{model}.pt")
# opset 12 keeps it loadable by the onnxruntime pinned in the backend image.
out = m.export(format="onnx", opset=12, simplify=False, dynamic=False, imgsz=640)
shutil.copy(out, dest)
print(f"Installed {dest}")
'@

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) "lightnvr-export-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    # -Encoding ascii: PowerShell 5.1 writes UTF-16 with a BOM by default, which
    # neither bash nor Python in the container can parse.
    Set-Content -Path (Join-Path $tmp "run.sh") -Value ($runner -replace "`r`n", "`n") -Encoding ascii -NoNewline
    Set-Content -Path (Join-Path $tmp "export.py") -Value ($py -replace "`r`n", "`n") -Encoding ascii -NoNewline

    # 2>&1 is deliberately NOT used here. In PowerShell 5.1 it wraps every
    # stderr line from a native exe in an ErrorRecord - and pip prints a
    # harmless "running as root" warning to stderr, which would abort the
    # script and bury the actual error. Let docker write straight to the
    # console and judge success by $LASTEXITCODE + the file check below.
    docker run --rm `
        -v "${Volume}:/data" `
        -v "${tmp}:/work" `
        -e DEBIAN_FRONTEND=noninteractive `
        -w /tmp `
        python:3.11-slim bash /work/run.sh $Model
    if ($LASTEXITCODE -ne 0) {
        Write-Die "Model export failed - see the output above."
    }
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

# Trust but verify: a zero exit above doesn't prove the file landed in the
# volume rather than only in the container's own /tmp.
if (-not (Test-ModelInVolume $Model)) {
    Write-Die "Export reported success but /data/models/$Model.onnx isn't in the volume."
}

Write-Host ""
Write-Step "Done."
Write-Host "  Now enable it in the web UI:"
Write-Host "    Settings -> AI -> Use AI"
Write-Host "    Where it runs: This machine (CPU)   Model: $Model"
Write-Host "    then press 'Check it works'."
