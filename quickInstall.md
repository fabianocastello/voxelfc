# Quick guide — VOXEL FC

This guide covers the minimal installation and the project's operational commands:

- process audio files and generate transcripts;
- check active locks on Dropbox;
- safely remove stale locks.

For the full pipeline description, see [README.md](README.md).

---

## 1. Prerequisites

### Linux (Debian/Ubuntu)

```bash
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg git
```

### macOS (Apple Silicon)

Confirm the terminal is using native arm64 Homebrew:

```bash
arch
/opt/homebrew/bin/brew --version
```

Install the dependencies:

```bash
/opt/homebrew/bin/brew install python@3.12 ffmpeg git
```

### Windows

Install Python 3.12 and Git:

```powershell
winget install Python.Python.3.12
winget install Git.Git
```

Confirm that `python` isn't the Microsoft Store alias:

```powershell
python --version
git --version
```

---

## 2. Install the project

### Linux/macOS

```bash
git clone https://github.com/fabianocastello/voxelfc.git ~/voxelfc
cd ~/voxelfc
./scripts/install.sh
source .venv/bin/activate
```

On Apple Silicon macOS, if more than one Python is installed:

```bash
PYTHON_BIN=/opt/homebrew/bin/python3.12 ./scripts/install.sh
source .venv/bin/activate
```

### Windows

```powershell
git clone https://github.com/fabianocastello/voxelfc.git C:\voxelfc
cd C:\voxelfc
scripts\install.bat
.venv\Scripts\Activate.ps1
```

---

## 3. Configure the `.env`

The installer creates `.env` from `.env.example`. Fill in these fields:

```dotenv
DROPBOX_APP_KEY=...
DROPBOX_APP_SECRET=...
DROPBOX_REFRESH_TOKEN=...
HF_TOKEN=...
```

`DROPBOX_REFRESH_TOKEN` is used by the audit commands and by the pipeline.
To generate or renew the token:

```bash
python scripts/dropbox_oauth.py
```

Keep `.env` local, outside of Git, and don't put credentials into logs or reports.

---

## 4. Process an audio file

Activate the virtual environment before running any commands:

```bash
# Linux/macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Process a local file (the default, no flag needed):

```bash
voxelfc --source /path/to/audio.mp3
```

Process a file on Dropbox instead:

```bash
voxelfc --source /Recordings/meeting.mp3 --dropbox
```

Recommended initial test:

```bash
voxelfc \
  --source /path/to/short_audio.mp3 \
  --model-size small \
  --language pt
```

---

## 5. Check locks on Dropbox

The command below uses `.env`, accesses the remote folder, and lists the locks with
machine, file, start time, and elapsed time:

```bash
python ./tools/status.py /_AudioMemosFC/MamyCalls
```

On Windows PowerShell, use the same command:

```powershell
python .\tools\status.py /_AudioMemosFC/MamyCalls
```

The query is read-only on Dropbox. It reads the locks and, when the duration isn't in
the transcript's front matter, temporarily downloads the corresponding audio file to
measure the duration with `ffprobe`; the local temporary file is removed at the end.
No file on Dropbox is created, changed, or removed.

Example with another folder:

```bash
python ./tools/status.py /Other/Folder
```

The Dropbox folder path must start with `/`.

---

## 6. Remove stale locks

### First: simulate

Always review the candidates before removing anything:

```bash
python ./tools/remove_locks.py \
  /_AudioMemosFC/MamyCalls \
  --older_than 10m \
  --dry-run
```

### Then: remove

Once you've confirmed the locks don't correspond to still-running processes:

```bash
python ./tools/remove_locks.py \
  /_AudioMemosFC/MamyCalls \
  --older_than 10m
```

The comparison is strict: only locks **older** than the threshold are removed.
Locks without a parseable date are never removed automatically.

### Accepted formats

```text
30s    30 seconds
10m    10 minutes
1h     1 hour
2h     2 hours
```

### Rejected formats

```text
10
10 m
1 hour
1d
```

The argument must be a positive integer immediately followed by `s`, `m`, or `h`:

```text
--older_than 30s
--older_than 10m
--older_than 1h
```

### Remove every lock, regardless of age

`--older_than` never touches a lock whose start date can't be parsed, no matter how
old it actually is. To clear those too - or to just wipe every lock in a folder
outright - use `--remove_all` instead of `--older_than`:

```bash
python ./tools/remove_locks.py /_AudioMemosFC/MamyCalls --remove_all --dry-run
python ./tools/remove_locks.py /_AudioMemosFC/MamyCalls --remove_all
```

`--remove_all` does no age filtering at all - it removes literally every lock found,
including ones with no parseable date. `--older_than` and `--remove_all` are
mutually exclusive; exactly one is required.

> Safety: before deleting a lock, confirm on the indicated machine that there's no
> real run actually corresponding to it. Removing an active lock can let another
> process start the same audio file in parallel. `--remove_all` in particular does
> not check whether a lock looks active - only use it when you're sure no machine is
> currently mid-job on this folder.

---

## 7. Full audit

To generate the complete report — front matter, conversion metrics, list of
transcripts and locks — use:

```bash
python ./tools/audit.py /_AudioMemosFC/MamyCalls
```

The report is saved locally at:

```text
tools/YYYY-MM-DD-HH-MM__AudioMemosFC_MamyCalls.md
```

This command is also read-only with respect to Dropbox. During the query, it shows
progress on the same line — connecting, listing, reading transcripts, analyzing
locks, and measuring audio files — to make it clear it's still running. To measure
the duration of audio files associated with locks, it may download them temporarily
and use `ffprobe`; the local temporary files are removed at the end. The conversion
metrics only consider audio files with at least 1 minute; the rest remain listed, but
aren't included in the calculations.

---

## 8. Rename legacy transcripts

Optional cleanup: renames `.voxel.txt`/`.transcriptFC.txt` transcripts (from older
versions of the pipeline) to the current `.voxel.md` suffix. Not required for
correctness - the pipeline already recognizes the legacy names as "already done" -
but useful to standardize a folder.

```bash
python ./tools/rename_transcripts.py /_AudioMemosFC/MamyCalls --dry-run
python ./tools/rename_transcripts.py /_AudioMemosFC/MamyCalls
```

If a file with the destination name (`.voxel.md`) already exists, that particular
rename is skipped and reported - never overwritten.

---

## 9. Validate the installation

```bash
python -m py_compile \
  tools/dropbox_lock_utils.py \
  tools/status.py \
  tools/remove_locks.py \
  tools/audit.py \
  tools/rename_transcripts.py
```

Test the help text without accessing Dropbox - every tool now prints its full
description and usage examples (not just a one-line usage summary) on `--help` or
on any usage error:

```bash
python ./tools/status.py --help
python ./tools/remove_locks.py --help
python ./tools/audit.py --help
python ./tools/rename_transcripts.py --help
```

Test format validation without removing anything:

```bash
python ./tools/remove_locks.py \
  /_AudioMemosFC/MamyCalls \
  --older_than 10m \
  --dry-run
```
