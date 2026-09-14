# VOXEL FC installer for Windows (e.g. ps20).
# Each machine runs fully independently: its own venv, its own models in
# $HOME\.voxelfc\models. Nothing is shared over the network.

$ErrorActionPreference = "Stop"

$RepoDir = Split-Path -Parent $PSScriptRoot
Set-Location $RepoDir

Write-Host "== VOXEL FC: installation (Windows) =="

# --- 1. Check Python 3.10+ ---
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "Python not found in PATH. Install Python 3.10+ (python.org or winget install Python.Python.3.12) before continuing."
    exit 1
}

# --- 2. Check FFmpeg ---
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpeg) {
    Write-Warning "FFmpeg not found."
    Write-Host "Install with: winget install Gyan.FFmpeg   (or choco install ffmpeg)"
    exit 1
}

# --- 3. Create venv ---
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment in .venv ..."
    python -m venv .venv
}
& .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip -q

# --- 4. Detect NVIDIA GPU (CUDA) ---
$Extras = "dropbox,transcribe,diarize"
$hasNvidia = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($hasNvidia) {
    Write-Host "NVIDIA GPU detected: installing with CUDA support."
    $Extras = "$Extras,cuda"
} else {
    Write-Host "No NVIDIA GPU detected (e.g. ps20): installing CPU-only torch/torchaudio (smaller download)."
    python -m pip install -q --index-url https://download.pytorch.org/whl/cpu torch torchaudio
}

Write-Host "Installing the package (extras: $Extras) ..."
python -m pip install -q -e ".[$Extras]"

# --- 5. Create .env from the example, if needed ---
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example. Fill in DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN, and HF_TOKEN."
}

Write-Host ""
Write-Host "Installation complete on this machine."
Write-Host "Models and cache will live in: $HOME\.voxelfc\models"
Write-Host "To use it:"
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  voxelfc --source C:\path\to\audio.mp3"
