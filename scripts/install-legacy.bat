@echo off
REM VOXEL FC installer for Windows using plain cmd.exe only,
REM never invoking PowerShell. For older machines or ones with a
REM restricted/blocked PowerShell script execution policy.
setlocal enabledelayedexpansion

set "REPO_DIR=%~dp0.."
cd /d "%REPO_DIR%"

echo == VOXEL FC: installation (Windows, plain cmd) ==

REM --- 1. Check Python ---
where python >nul 2>&1
if errorlevel 1 (
    echo Python not found in PATH.
    echo Install with: winget install Python.Python.3.12
    echo Then, in Settings ^> Apps ^> Advanced app settings ^>
    echo App execution aliases, turn off python.exe and python3.exe
    echo in case Windows opens the Microsoft Store instead of Python.
    goto :fail
)

REM --- 2. Check FFmpeg ---
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo FFmpeg not found in PATH.
    echo Install with: winget install Gyan.FFmpeg
    goto :fail
)

REM --- 3. Create venv ---
if not exist ".venv\Scripts\activate.bat" (
    echo Creating virtual environment in .venv ...
    python -m venv .venv
    if errorlevel 1 goto :fail
)
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip -q

REM --- 4. Detect NVIDIA GPU (CUDA) ---
set "EXTRAS=dropbox,transcribe,diarize"
where nvidia-smi >nul 2>&1
if errorlevel 1 (
    echo No NVIDIA GPU detected: installing CPU-only torch/torchaudio.
    python -m pip install -q --index-url https://download.pytorch.org/whl/cpu torch torchaudio
) else (
    nvidia-smi >nul 2>&1
    if errorlevel 1 (
        echo No NVIDIA GPU detected: installing CPU-only torch/torchaudio.
        python -m pip install -q --index-url https://download.pytorch.org/whl/cpu torch torchaudio
    ) else (
        echo NVIDIA GPU detected: installing with CUDA support.
        set "EXTRAS=%EXTRAS%,cuda"
    )
)

echo Installing the package (extras: %EXTRAS%) ...
python -m pip install -q -e ".[%EXTRAS%]"
if errorlevel 1 goto :fail

REM --- 5. Create .env from the example, if needed ---
if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo Created .env from .env.example. Fill in DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN, and HF_TOKEN.
)

echo.
echo Installation complete on this machine.
echo Models and cache will live in: %USERPROFILE%\.voxelfc\models
echo To use it:
echo   .venv\Scripts\activate.bat
echo   voxelfc --source C:\path\to\audio.mp3
pause
exit /b 0

:fail
echo.
echo Installation failed. See the messages above.
pause
exit /b 1
