# VOXEL FC — File Companion

Downloads an audio file from a Dropbox folder, transcribes it, diarizes the
speakers (`Person 1`, `Person 2`, ...), and uploads `.voxel.md` /
`.srt` / `.voxel.vtt` back to Dropbox.

`voxelfc` is the product's technical name. During the migration, the legacy
`supertranscriptfc` command remains available as a deprecated alias.
`scripts/update.sh` runs `git pull`, re-executes itself when the file itself
was updated, and uses `.venv/bin/python` and `.venv/bin/voxelfc` directly,
avoiding conflicts with Miniconda or another Python on PATH.

Every machine where the project is installed runs **fully independently**:
its own virtual environment, its own models downloaded and stored in
`~/.voxelfc/models` (Linux/macOS) or `%USERPROFILE%\.voxelfc\models`
(Windows). No model or cache is shared between machines.

For a quick install on Linux, macOS, or Windows, see
[quickInstall.md](quickInstall.md).

## Installation

Prerequisites on any machine: Python 3.10+ and FFmpeg in PATH.

### Linux (thor25, leno18) and macOS (MacBook Air M1)

```bash
./scripts/install.sh
```

- Automatically detects an NVIDIA GPU (thor25) and installs `torch` with
  CUDA; on the others (leno18, MacBook) installs the CPU-only variant.
- On the MacBook Air M1, diarization (`pyannote.audio`/torch) uses MPS
  acceleration automatically when available; transcription
  (`faster-whisper`) runs on CPU, since `ctranslate2` doesn't support MPS.
- On Apple Silicon (arm64), the installer also adds `mlx-whisper`, which
  transcribes on the GPU/Neural Engine instead (reportedly 2-4x faster than
  CPU there) and is used automatically whenever installed, falling back to
  `faster-whisper` on CPU if it fails for any reason. This path has not yet
  been validated on real Apple Silicon hardware.

### Windows (ps20)

In PowerShell:

```powershell
.\scripts\install.ps1
```

Or double-click `scripts\install.bat` (a wrapper that calls the script above,
bypassing PowerShell's default execution policy).

Installs CPU-only `torch` (ps20 has no dedicated GPU).

After installing on any platform, edit the `.env` file created from
`.env.example` and fill in:

- `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN` — Dropbox
  App OAuth2 credentials (refresh token flow, no expiration — the SDK
  renews the access token automatically on every call).
- `HF_TOKEN` — Hugging Face token with access to the
  `pyannote/speaker-diarization-3.1` and `pyannote/segmentation-3.0` models
  (accept each one's terms of use on the Hugging Face site before using them).

## Diagnosing an installation

To check whether everything is set up correctly on a given machine
(Python version, venv, FFmpeg, dependencies, GPU/mlx acceleration, `.env`
credentials, live Dropbox connectivity, and `VOXELFC_HOME`):

```bash
source .venv/bin/activate
python utils/voxel_doctor.py
```

## Usage

Before running any command below, activate the virtual environment:

```bash
source .venv/bin/activate          # Linux/macOS
```
```cmd
.venv\Scripts\activate.bat         :: Windows, in cmd.exe
```
```powershell
.venv\Scripts\Activate.ps1         # Windows, in PowerShell
```

Process a local file (this is the default — no flag needed):

```bash
voxelfc --source /path/to/audio.mp3
```

Process a file from Dropbox instead (downloads, processes, and uploads back to
the same source folder) with `--dropbox`:

```bash
voxelfc --source /Recordings/meeting.mp3 --dropbox
```

Specifying a different destination folder on Dropbox:

```bash
voxelfc --source /Recordings/meeting.mp3 --dropbox --dest /Recordings/Transcripts
```

Other useful options: `--model-size`, `--device {auto,cpu,cuda}`,
`--language pt`, `--min-speakers`, `--max-speakers`, `--min-minutes` /
`--max-minutes` (skip audio files outside that duration range — you can use
one, the other, or both), `--vtt` (also generate `.vtt`), `--keep-temp`
(don't delete temporary files), `--force` (reprocess even if already
completed before).

## Compatibility and migration

New installations use `~/.voxelfc` and the `VOXELFC_HOME` variable. Existing
installations that still have `~/.supertranscriptfc` are preserved and can
be used automatically during the transition. To migrate deliberately, stop
the workers and simulate first:

```bash
python scripts/migrate_home.py --from ~/.supertranscriptfc --to ~/.voxelfc --dry-run
python scripts/migrate_home.py --from ~/.supertranscriptfc --to ~/.voxelfc
```

The old directory isn't deleted. In case of rollback, set
`VOXELFC_HOME=~/.supertranscriptfc`. The Dropbox filenames `.voxel.md`,
`.srt`, `.voxel.vtt`, and `.voxel.lock` remain stable so that
already-processed audio files aren't run again. Files written by older
runs as `.voxel.txt`/`.transcriptFC.txt` (transcript) or
`.transcriptFC.lock` (lock) are still recognized as done/locked and are
never renamed in bulk; only new runs write the current `.voxel.md`/
`.voxel.lock` names.


Each stage (download, conversion, transcription, diarization, outputs,
upload) is recorded in `~/.voxelfc/tmp/<job_id>/progress.json`. If the
process is interrupted, running the same command again resumes from where
it left off, without redoing completed stages.

Files already processed successfully are recorded in
`~/.voxelfc/processed_files.json` and aren't reprocessed on subsequent runs
(unless `--force` is used), even after that job's temporary files have been
removed.

## Expected disk space

- ~20 GB permanent in `~/.voxelfc/models` for the models.
- ~10 GB temporary per processing run in `~/.voxelfc/tmp` (original audio +
  intermediate WAV), removed automatically at the end unless `--keep-temp`
  is used.

## Known limitations

Diarization (speaker separation) isn't perfect: audio with background
noise, very similar voices, or overlapping speech can produce incorrect
speaker assignments.
