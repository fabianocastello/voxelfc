@echo off
REM Updates the code without needing git installed: downloads the main
REM branch ZIP directly from GitHub using curl.exe and tar.exe (both
REM bundled with Windows 10 1803+ / Windows 11), extracts it, and replaces
REM the project files, preserving .venv and .env. Then runs voxelfc with
REM the given arguments.
REM
REM Usage: scripts\update-nogit.bat --source C:\path\to\audio.mp3 --model-size large-v3
setlocal

set "REPO_DIR=%~dp0.."
cd /d "%REPO_DIR%"

where curl >nul 2>&1
if errorlevel 1 (
    echo curl.exe not found. It's bundled with Windows 10 1803+/11;
    echo on older versions, install Git for Windows and use update.bat.
    goto :fail
)
where tar >nul 2>&1
if errorlevel 1 (
    echo tar.exe not found. It's bundled with Windows 10 1803+/11;
    echo on older versions, install Git for Windows and use update.bat.
    goto :fail
)

set "ZIP_URL=https://github.com/fabianocastello/voxelfc/archive/refs/heads/main.zip"
set "TMP_ZIP=%TEMP%\voxelfc_update.zip"
set "TMP_EXTRACT=%TEMP%\voxelfc_update_extract"

echo == Downloading the latest code ==
curl -L -o "%TMP_ZIP%" "%ZIP_URL%"
if errorlevel 1 goto :fail

if exist "%TMP_EXTRACT%" rmdir /s /q "%TMP_EXTRACT%"
mkdir "%TMP_EXTRACT%"
tar -xf "%TMP_ZIP%" -C "%TMP_EXTRACT%"
if errorlevel 1 goto :fail

REM GitHub's zip extracts into a subfolder like voxelfc-main
set "EXTRACTED_DIR="
for /d %%D in ("%TMP_EXTRACT%\*") do set "EXTRACTED_DIR=%%D"
if not defined EXTRACTED_DIR goto :fail

echo == Updating files (preserving .venv and .env) ==
xcopy "%EXTRACTED_DIR%\src" "%REPO_DIR%\src" /e /y /i >nul
xcopy "%EXTRACTED_DIR%\scripts" "%REPO_DIR%\scripts" /e /y /i >nul
xcopy "%EXTRACTED_DIR%\tests" "%REPO_DIR%\tests" /e /y /i >nul
copy /y "%EXTRACTED_DIR%\pyproject.toml" "%REPO_DIR%\" >nul
copy /y "%EXTRACTED_DIR%\README.md" "%REPO_DIR%\" >nul
copy /y "%EXTRACTED_DIR%\quickInstall.md" "%REPO_DIR%\" >nul
copy /y "%EXTRACTED_DIR%\.env.example" "%REPO_DIR%\" >nul
copy /y "%EXTRACTED_DIR%\.gitignore" "%REPO_DIR%\" >nul

del /q "%TMP_ZIP%"
rmdir /s /q "%TMP_EXTRACT%"

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
    echo == Reinstalling dependencies and updated entry points ==
    python -m pip install -q -e ".[dropbox,transcribe,diarize]" >nul 2>&1
    if errorlevel 1 goto :fail
)

echo == Running: voxelfc %* ==
voxelfc %*
exit /b %ERRORLEVEL%

:fail
echo.
echo Failed to update the code. See the messages above.
exit /b 1
