from __future__ import annotations

import logging
import os
import shutil
import socket
import time
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from .audio import convert_to_wav, probe_duration_seconds
from .config import AUDIO_EXTENSIONS, Config
from .diarize import diarize_audio
from .dropbox_client import SourceNotFoundError
from .merge import assign_speakers
from .naming import build_output_stem
from .outputs import parse_subtitle_text, write_plain_txt, write_srt, write_txt, write_vtt
from .progress import format_duration
from .state import JobState, ProcessedRegistry, compute_job_id
from .transcribe import transcribe_audio

logger = logging.getLogger("voxelfc")

# A long job (a multi-hour audio file on CPU) can legitimately take more
# than a day; beyond that we consider the lock abandoned (e.g. the machine
# was shut down mid-job) and let another machine pick it up.
LOCK_STALE_AFTER = timedelta(hours=36)

TRANSCRIPT_SUFFIX = ".voxel.md"
VTT_SUFFIX = ".voxel.vtt"
LOCK_SUFFIX = ".voxel.lock"
# Older runs wrote the transcript as "<stem>.voxel.txt" or, before that,
# "<stem>.transcriptFC.txt" (which also used ".transcriptFC.lock" for the
# lock). All recognized as "already done"/"locked" so files already
# produced under any of those names aren't reprocessed or double-claimed,
# but new runs only ever write TRANSCRIPT_SUFFIX/LOCK_SUFFIX.
LEGACY_TRANSCRIPT_SUFFIXES = (".voxel.txt", ".transcriptFC.txt")
LEGACY_LOCK_SUFFIX = ".transcriptFC.lock"


def _make_lock_content() -> str:
    hostname = socket.gethostname()
    started_at = datetime.now().isoformat()
    return f"Processing on: {hostname}\nStarted at: {started_at}\n"


def _is_lock_stale(lock_content: str) -> bool:
    try:
        iso_timestamp = lock_content.splitlines()[1].split("Started at: ", 1)[1]
        locked_at = datetime.fromisoformat(iso_timestamp)
    except (IndexError, ValueError):
        return True
    return datetime.now() - locked_at > LOCK_STALE_AFTER


def _trimmed_stem_name(name: str) -> str | None:
    """If name has leading/trailing whitespace in its stem (e.g. the file is
    literally named 'audio .mp3'), returns the trimmed name ('audio.mp3').
    Returns None if there's nothing to trim. Only the stem is touched - the
    extension is left exactly as it is."""
    dot_index = name.rfind(".")
    if dot_index <= 0:
        return None
    stem, ext = name[:dot_index], name[dot_index:]
    trimmed = stem.strip()
    if not trimmed or trimmed == stem:
        return None
    return f"{trimmed}{ext}"


def _trim_companion_subtitles_local(old_audio_path: Path, new_audio_path: Path) -> None:
    """If a pre-existing .vtt/.srt sits next to the audio under its OLD
    (untrimmed) name, renames it to match the audio's new trimmed name
    too - otherwise _find_existing_subtitle_local's exact-stem match
    would never find it again after the audio itself gets trimmed."""
    for ext in ("vtt", "srt"):
        old_subtitle = old_audio_path.with_suffix(f".{ext}")
        if not old_subtitle.exists():
            continue
        new_subtitle = new_audio_path.with_suffix(f".{ext}")
        if new_subtitle.exists():
            logger.warning(
                "Can't trim whitespace from companion '%s': '%s' already exists there.",
                old_subtitle.name,
                new_subtitle.name,
            )
            continue
        old_subtitle.rename(new_subtitle)
        logger.info("Renamed companion subtitle '%s' -> '%s' too.", old_subtitle.name, new_subtitle.name)


def _ensure_local_name_trimmed(path: Path) -> Path:
    new_name = _trimmed_stem_name(path.name)
    if new_name is None:
        return path
    new_path = path.with_name(new_name)
    if new_path.exists():
        logger.warning(
            "Can't trim whitespace from '%s': '%s' already exists there.", path.name, new_name
        )
        return path
    path.rename(new_path)
    logger.info("Renamed '%s' -> '%s' (trimmed whitespace before the extension).", path.name, new_name)
    _trim_companion_subtitles_local(path, new_path)
    return new_path


def _trim_companion_subtitles_dropbox(dropbox_client, old_path: str, new_path: str) -> None:
    """Dropbox equivalent of _trim_companion_subtitles_local."""
    old_purepath = PurePosixPath(old_path)
    new_purepath = PurePosixPath(new_path)
    for ext in ("vtt", "srt"):
        old_subtitle = str(old_purepath.with_suffix(f".{ext}"))
        if not dropbox_client.file_exists(old_subtitle):
            continue
        new_subtitle = str(new_purepath.with_suffix(f".{ext}"))
        if dropbox_client.file_exists(new_subtitle):
            logger.warning(
                "Can't trim whitespace from companion '%s': '%s' already exists there.",
                old_subtitle,
                new_subtitle,
            )
            continue
        dropbox_client.move_file(old_subtitle, new_subtitle)
        logger.info("Renamed companion subtitle '%s' -> '%s' too.", old_subtitle, new_subtitle)


def _ensure_dropbox_name_trimmed(dropbox_client, path: str) -> str:
    purepath = PurePosixPath(path)
    new_name = _trimmed_stem_name(purepath.name)
    if new_name is None:
        return path
    new_path = str(purepath.with_name(new_name))
    if dropbox_client.file_exists(new_path):
        logger.warning("Can't trim whitespace from '%s': '%s' already exists there.", path, new_path)
        return path
    dropbox_client.move_file(path, new_path)
    logger.info("Renamed '%s' -> '%s' (trimmed whitespace before the extension).", path, new_path)
    _trim_companion_subtitles_dropbox(dropbox_client, path, new_path)
    return new_path


def _find_existing_subtitle_local(source: Path) -> tuple[str, Path] | None:
    """Looks for a pre-existing .vtt or .srt sitting right next to the
    source audio, sharing its exact filename stem (e.g. Dropbox's own
    automatic audio transcription writes 'recording.vtt' next to
    'recording.m4a'). Returns (extension, path), preferring .vtt, or None."""
    for ext in ("vtt", "srt"):
        candidate = source.with_suffix(f".{ext}")
        if candidate.exists():
            return ext, candidate
    return None


def _find_existing_subtitle_dropbox(dropbox_client, source_path: str) -> tuple[str, str] | None:
    """Dropbox equivalent of _find_existing_subtitle_local."""
    source_purepath = PurePosixPath(source_path)
    stem_path = source_purepath.parent / source_purepath.stem
    for ext in ("vtt", "srt"):
        candidate = f"{stem_path}.{ext}"
        if dropbox_client.file_exists(candidate):
            return ext, candidate
    return None


def _build_outputs_from_existing_subtitle(
    config: Config,
    subtitle_text: str,
    subtitle_name: str,
    audio_name: str,
    output_stem: str,
    output_dir: Path,
) -> list[Path]:
    """Builds outputs directly from an already-existing .vtt/.srt instead
    of running conversion/transcription/diarization - used when a matching
    subtitle already sits next to the source audio (e.g. produced by
    Dropbox's own automatic transcription)."""
    segments = parse_subtitle_text(subtitle_text)
    metadata = {
        "system": "VoxelFC 1.0",
        "audio_file": audio_name,
        "processed_date": datetime.now().strftime("%Y-%m-%d"),
        "running_on": socket.gethostname(),
        "remarks": f"Used pre-existent transcript in {subtitle_name}",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    output_paths: list[Path] = []
    if config.write_txt:
        output_paths.append(
            write_plain_txt(segments, output_dir / f"{output_stem}{TRANSCRIPT_SUFFIX}", metadata=metadata)
        )
    return output_paths


def _local_target(
    source: Path, dest_dir: Path | None
) -> tuple[str, Path, Path, list[Path], Path, Path]:
    """For a local audio file, computes the output base name and the paths
    where the transcript and lock (current and legacy names) would live at
    the destination. legacy_transcript_paths lists every legacy name,
    newest first."""
    source = source.resolve()
    resolved_dest_dir = (dest_dir or source.parent).resolve()
    stem = build_output_stem(source.stem, source.parent.name)
    transcript_path = resolved_dest_dir / f"{stem}{TRANSCRIPT_SUFFIX}"
    legacy_transcript_paths = [resolved_dest_dir / f"{stem}{suffix}" for suffix in LEGACY_TRANSCRIPT_SUFFIXES]
    lock_path = resolved_dest_dir / f"{stem}{LOCK_SUFFIX}"
    legacy_lock_path = resolved_dest_dir / f"{stem}{LEGACY_LOCK_SUFFIX}"
    return stem, resolved_dest_dir, transcript_path, legacy_transcript_paths, lock_path, legacy_lock_path


def _dropbox_target(
    source_path: str, dest_folder: str | None
) -> tuple[str, str, str, list[str], str, str]:
    """For a Dropbox audio file, computes the output base name and the
    remote paths where the transcript and lock (current and legacy names)
    would live at the destination. legacy_transcript_paths lists every
    legacy name, newest first."""
    source_purepath = PurePosixPath(source_path)
    stem = build_output_stem(source_purepath.stem, source_purepath.parent.name)
    resolved_dest_folder = dest_folder or str(source_purepath.parent)
    if resolved_dest_folder != "/":
        resolved_dest_folder = resolved_dest_folder.rstrip("/")
    transcript_path = f"{resolved_dest_folder}/{stem}{TRANSCRIPT_SUFFIX}"
    legacy_transcript_paths = [f"{resolved_dest_folder}/{stem}{suffix}" for suffix in LEGACY_TRANSCRIPT_SUFFIXES]
    lock_path = f"{resolved_dest_folder}/{stem}{LOCK_SUFFIX}"
    legacy_lock_path = f"{resolved_dest_folder}/{stem}{LEGACY_LOCK_SUFFIX}"
    return stem, resolved_dest_folder, transcript_path, legacy_transcript_paths, lock_path, legacy_lock_path


def process_file(
    config: Config,
    input_path: Path,
    output_stem: str,
    work_dir: Path,
    source_ref: str,
) -> list[Path]:
    """Runs the full pipeline (convert -> transcribe -> diarize -> merge ->
    generate outputs) on an audio file already available on disk. Returns
    the list of output files generated in work_dir/output.

    Resumable: each stage is recorded in progress.json inside work_dir, and
    re-runs skip stages already completed (unless config.force)."""
    job_id = compute_job_id(source_ref)
    state = JobState(job_id=job_id, source_ref=source_ref, work_dir=work_dir)
    state.load()
    if config.force:
        state.completed_stages.clear()

    wav_path = work_dir / "audio_16k_mono.wav"
    output_dir = work_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    input_size_mb = input_path.stat().st_size / 1_000_000
    logger.info(
        "[%s] Source file: %s (%.1f MB, modified %s)",
        job_id,
        input_path.name,
        input_size_mb,
        datetime.fromtimestamp(input_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
    )

    if not state.is_done("converted"):
        stage_start = time.monotonic()
        logger.info("[%s] Converting audio to WAV...", job_id)
        convert_to_wav(input_path, wav_path)
        state.mark_done("converted", converted_seconds=time.monotonic() - stage_start)
    else:
        logger.info("[%s] Conversion already done, skipping.", job_id)
    conversion_seconds = state.data.get("converted_seconds", 0.0)

    audio_duration = probe_duration_seconds(wav_path)
    logger.info("[%s] Audio duration: %s", job_id, format_duration(audio_duration))

    audio_minutes = audio_duration / 60
    if config.min_minutes is not None and audio_minutes < config.min_minutes:
        logger.info(
            "[%s] Audio shorter than the configured minimum (%s < %.1f min), skipping.",
            job_id,
            format_duration(audio_duration),
            config.min_minutes,
        )
        return []
    if config.max_minutes is not None and audio_minutes > config.max_minutes:
        logger.info(
            "[%s] Audio longer than the configured maximum (%s > %.1f min), skipping.",
            job_id,
            format_duration(audio_duration),
            config.max_minutes,
        )
        return []

    if not state.is_done("transcribed"):
        stage_start = time.monotonic()
        segments, detected_language, detected_language_probability, translated_to_english = transcribe_audio(
            wav_path,
            model_size=config.model_size,
            device=config.device,
            compute_type=config.compute_type,
            language=config.language,
        )
        transcription_seconds = time.monotonic() - stage_start
        transcript_data = [{"start": s.start, "end": s.end, "text": s.text} for s in segments]
        state.mark_done(
            "transcribed",
            transcript=transcript_data,
            language=detected_language,
            language_probability=detected_language_probability,
            translated_to_english=translated_to_english,
            transcribed_seconds=transcription_seconds,
        )
    else:
        logger.info("[%s] Transcription already done, skipping.", job_id)
        transcript_data = state.data["transcript"]
        detected_language = state.data.get("language")
        detected_language_probability = state.data.get("language_probability")
        translated_to_english = state.data.get("translated_to_english", False)
    transcription_seconds = state.data.get("transcribed_seconds", 0.0)

    if not state.is_done("diarized"):
        stage_start = time.monotonic()
        turns = diarize_audio(
            wav_path,
            hf_token=config.hf_token,
            min_speakers=config.min_speakers,
            max_speakers=config.max_speakers,
        )
        diarization_seconds = time.monotonic() - stage_start
        turns_data = [{"start": t.start, "end": t.end, "speaker": t.speaker} for t in turns]
        state.mark_done("diarized", turns=turns_data, diarized_seconds=diarization_seconds)
    else:
        logger.info("[%s] Diarization already done, skipping.", job_id)
        turns_data = state.data["turns"]
    diarization_seconds = state.data.get("diarized_seconds", 0.0)

    from .diarize import SpeakerTurn
    from .transcribe import TranscriptSegment

    segments = [TranscriptSegment(**s) for s in transcript_data]
    turns = [SpeakerTurn(**t) for t in turns_data]
    labeled_segments = assign_speakers(segments, turns)

    total_seconds = conversion_seconds + transcription_seconds + diarization_seconds
    metadata = {
        "system": "VoxelFC 1.0",
        "audio_file": input_path.name,
        "processed_date": datetime.now().strftime("%Y-%m-%d"),
        "running_on": socket.gethostname(),
        "model": config.model_size,
        "language_detected": detected_language or "unknown",
        # Written as a quoted string, not a bare YAML number: Obsidian's
        # Properties panel renders bare numeric front-matter values with a
        # locale-formatted decimal separator (e.g. "0,97" on a pt-BR
        # system) even though the raw file always uses a period - a quoted
        # string is shown verbatim instead.
        "language_probability": (
            f"{detected_language_probability:.2f}"
            if detected_language_probability is not None
            else None
        ),
    }
    if config.language:
        # Transcription was forced into this language rather than using the
        # naturally detected one (language_detected above still reflects
        # what was actually spoken) - the output may effectively be a
        # translation into config.language rather than a faithful
        # transcription, so this must be explicit.
        metadata["force_language"] = config.language
    if translated_to_english:
        metadata["remarks"] = "non-latin languages converted to English"
    metadata.update(
        {
            "audio_duration": format_duration(audio_duration),
            "speakers_detected": len({s.speaker for s in labeled_segments}),
            "conversion_time": format_duration(conversion_seconds),
            "transcription_time": format_duration(transcription_seconds),
            "diarization_time": format_duration(diarization_seconds),
            "total_time": format_duration(total_seconds),
        }
    )

    output_paths: list[Path] = []
    if config.write_txt:
        output_paths.append(
            write_txt(labeled_segments, output_dir / f"{output_stem}{TRANSCRIPT_SUFFIX}", metadata=metadata)
        )
    if config.write_srt:
        output_paths.append(write_srt(labeled_segments, output_dir / f"{output_stem}.srt"))
    if config.write_vtt:
        output_paths.append(write_vtt(labeled_segments, output_dir / f"{output_stem}{VTT_SUFFIX}"))

    state.mark_done("outputs_written", outputs=[str(p) for p in output_paths])
    logger.info("[%s] Outputs generated: %s", job_id, [p.name for p in output_paths])
    return output_paths


def _move_file_local(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / src.name
    if dest_path.exists():
        dest_path.unlink()
    shutil.move(str(src), str(dest_path))
    return dest_path


def cleanup_work_dir(work_dir: Path, keep_temp: bool) -> None:
    if keep_temp:
        logger.info("keep_temp enabled, keeping %s", work_dir)
        return
    logger.info("Cleaning up temporary files: %s", work_dir)
    shutil.rmtree(work_dir, ignore_errors=True)


def run_local_job(
    config: Config, source: Path, dest_dir: Path | None, archive_dir: Path | None = None
) -> list[Path]:
    """Processes a local audio file and copies the outputs to dest_dir (or
    to the source file's own folder, if dest_dir isn't given).

    If archive_dir is given and processing finishes successfully, the
    original audio AND the generated files are moved (not copied) into that
    folder - useful for "emptying out" a monitored folder over time. If
    archive_dir isn't given, the source file is never removed."""
    config.ensure_dirs()
    source = _ensure_local_name_trimmed(source.resolve())
    output_stem, dest_dir, transcript_path, legacy_transcript_paths, _lock_path, _legacy_lock_path = (
        _local_target(source, dest_dir)
    )
    source_ref = f"local:{source}"
    job_id = compute_job_id(source_ref)
    registry = ProcessedRegistry(config.state_file)

    existing_legacy = next((p for p in legacy_transcript_paths if p.exists()), None)
    if not config.force and (transcript_path.exists() or existing_legacy is not None):
        existing = transcript_path if transcript_path.exists() else existing_legacy
        logger.info("Transcript already exists (%s), skipping: %s", existing.name, source)
        return []

    work_dir = config.tmp_dir / job_id
    existing_subtitle = _find_existing_subtitle_local(source)
    if existing_subtitle is not None:
        _kind, subtitle_path = existing_subtitle
        logger.info(
            "[%s] Found pre-existing transcript (%s); skipping conversion and transcription.",
            job_id,
            subtitle_path.name,
        )
        output_paths = _build_outputs_from_existing_subtitle(
            config,
            subtitle_path.read_text(encoding="utf-8"),
            subtitle_path.name,
            source.name,
            output_stem,
            work_dir / "output",
        )
    else:
        output_paths = process_file(
            config, input_path=source, output_stem=output_stem, work_dir=work_dir, source_ref=source_ref
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    final_paths = []
    for p in output_paths:
        dest_path = dest_dir / p.name
        shutil.copy2(p, dest_path)
        final_paths.append(dest_path)

    if archive_dir is not None and final_paths:
        archive_dir = Path(archive_dir).resolve()
        # Move every file sharing this stem in dest_dir, not just the outputs
        # generated by this run - picks up leftovers from earlier runs too
        # (e.g. a .vtt from a run that used --vtt, even if this run didn't).
        matching = [
            p
            for p in sorted(dest_dir.glob(f"{output_stem}.*"))
            if p.resolve() != source
            and not p.name.endswith(LOCK_SUFFIX)
            and not p.name.endswith(LEGACY_LOCK_SUFFIX)
        ]
        moved_paths = [_move_file_local(p, archive_dir) for p in matching]
        _move_file_local(source, archive_dir)
        logger.info("[%s] Audio and outputs moved to: %s", job_id, archive_dir)
        final_paths = moved_paths

    registry.mark_processed(job_id, source_ref, [str(p) for p in final_paths])
    cleanup_work_dir(work_dir, config.keep_temp)
    return final_paths


def run_dropbox_job(
    config: Config,
    dropbox_client,
    source_path: str,
    dest_folder: str | None,
    archive_folder: str | None = None,
) -> list[str]:
    """Processes a Dropbox audio file: download -> pipeline -> upload
    outputs -> clean up temporary files. dest_folder defaults to the same
    folder as the source file.

    If archive_folder is given and processing finishes successfully, the
    original audio AND the freshly uploaded outputs are moved (not copied)
    into that folder on Dropbox itself."""
    config.ensure_dirs()
    source_path = _ensure_dropbox_name_trimmed(dropbox_client, source_path)
    source_ref = f"dropbox:{source_path}"
    job_id = compute_job_id(source_ref)
    registry = ProcessedRegistry(config.state_file)

    (
        stem,
        dest_folder,
        transcript_remote_path,
        legacy_transcript_remote_paths,
        lock_remote_path,
        legacy_lock_remote_path,
    ) = _dropbox_target(source_path, dest_folder)

    existing_legacy_remote = next(
        (p for p in legacy_transcript_remote_paths if dropbox_client.file_exists(p)), None
    )
    if not config.force and (
        dropbox_client.file_exists(transcript_remote_path) or existing_legacy_remote is not None
    ):
        existing = transcript_remote_path if dropbox_client.file_exists(transcript_remote_path) else existing_legacy_remote
        logger.info("Transcript already exists (%s), skipping: %s", existing, source_path)
        return []

    if not config.force:
        # Check the current lock name first, then fall back to the legacy
        # name - a lock created by a not-yet-updated machine should still be
        # respected, but we only ever write/delete the current name below.
        lock_content = dropbox_client.read_text_file(lock_remote_path) or dropbox_client.read_text_file(
            legacy_lock_remote_path
        )
        if lock_content and not _is_lock_stale(lock_content):
            logger.info(
                "Already being processed by another machine (%s), skipping: %s",
                lock_content,
                source_path,
            )
            return []
        if lock_content:
            logger.warning(
                "Stale lock found (%s) - assuming abandoned and proceeding: %s",
                lock_content,
                source_path,
            )

    dropbox_client.ensure_folder(dest_folder)
    dropbox_client.write_text_file(lock_remote_path, _make_lock_content())
    try:
        work_dir = config.tmp_dir / job_id
        state = JobState(job_id=job_id, source_ref=source_ref, work_dir=work_dir)
        state.load()

        existing_subtitle = _find_existing_subtitle_dropbox(dropbox_client, source_path)
        if existing_subtitle is not None:
            _kind, subtitle_remote_path = existing_subtitle
            logger.info(
                "[%s] Found pre-existing transcript (%s); skipping download/conversion/transcription.",
                job_id,
                subtitle_remote_path,
            )
            output_paths = _build_outputs_from_existing_subtitle(
                config,
                dropbox_client.read_text_file(subtitle_remote_path),
                PurePosixPath(subtitle_remote_path).name,
                PurePosixPath(source_path).name,
                stem,
                work_dir / "output",
            )
        else:
            local_input = work_dir / Path(source_path).name
            if not state.is_done("downloaded"):
                try:
                    dropbox_client.download_file(source_path, local_input)
                except SourceNotFoundError:
                    logger.warning(
                        "[%s] %s no longer exists on Dropbox - another machine likely "
                        "already processed/archived it, skipping.",
                        job_id,
                        source_path,
                    )
                    return []
                state.mark_done("downloaded")
            else:
                logger.info("[%s] Download already done, skipping.", job_id)

            output_paths = process_file(
                config, input_path=local_input, output_stem=stem, work_dir=work_dir, source_ref=source_ref
            )

        if not state.is_done("uploaded"):
            uploaded = []
            for p in output_paths:
                remote_path = f"{dest_folder}/{p.name}"
                dropbox_client.upload_file(p, remote_path)
                uploaded.append(remote_path)
            state.mark_done("uploaded", uploaded=uploaded)
        else:
            uploaded = state.data["uploaded"]
            logger.info("[%s] Upload already done, skipping.", job_id)

        if archive_folder is not None and uploaded:
            archive_folder = archive_folder.rstrip("/") if archive_folder != "/" else archive_folder
            if not state.is_done("archived"):
                dropbox_client.ensure_folder(archive_folder)
                # Move every file sharing this stem in dest_folder, not just
                # the outputs this run produced - picks up leftovers from
                # earlier runs too (e.g. a .vtt from a run that used --vtt,
                # even if this run didn't).
                matching = [
                    p
                    for p in dropbox_client.list_files_matching_stem(dest_folder, stem)
                    if p != source_path and p != lock_remote_path and p != legacy_lock_remote_path
                ]
                moved = []
                for remote_path in matching:
                    filename = remote_path.rsplit("/", 1)[-1]
                    moved.append(dropbox_client.move_file(remote_path, f"{archive_folder}/{filename}"))
                audio_filename = PurePosixPath(source_path).name
                dropbox_client.move_file(source_path, f"{archive_folder}/{audio_filename}")
                state.mark_done("archived", archived=moved)
                uploaded = moved
                logger.info("[%s] Audio and outputs moved to: %s", job_id, archive_folder)
            else:
                uploaded = state.data["archived"]
                logger.info("[%s] Archiving already done, skipping.", job_id)

        registry.mark_processed(job_id, source_ref, uploaded)
        cleanup_work_dir(work_dir, config.keep_temp)
        return uploaded
    finally:
        dropbox_client.delete_file(lock_remote_path)


def _relative_archive_dir(audio_path: Path, source_dir: Path, archive_dir: Path) -> Path:
    """For --recursive batches, mirrors the audio file's subfolder (relative
    to the scanned root) under archive_dir, instead of flattening every
    episode into one folder - avoids collisions when different subfolders
    reuse the same filename (e.g. every episode's audio literally named
    'audio.mp3')."""
    try:
        relative_parent = audio_path.resolve().parent.relative_to(source_dir)
    except ValueError:
        relative_parent = Path(".")
    return archive_dir / relative_parent


def _relative_archive_folder(audio_path: str, source_folder: str, archive_folder: str) -> str:
    """Dropbox equivalent of _relative_archive_dir."""
    source_root = source_folder.rstrip("/") or "/"
    parent = str(PurePosixPath(audio_path).parent)
    if source_root == "/":
        relative = parent.lstrip("/")
    elif parent == source_root or parent.startswith(source_root + "/"):
        relative = parent[len(source_root):].lstrip("/")
    else:
        relative = ""
    archive_root = archive_folder.rstrip("/") if archive_folder != "/" else archive_folder
    return f"{archive_root}/{relative}" if relative else archive_root


def _iter_local_audio_files(source_dir: Path, recursive: bool) -> list[Path]:
    pattern_fn = source_dir.rglob if recursive else source_dir.glob
    files = [
        p for p in pattern_fn("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    return sorted(files)


def _remove_empty_dirs_local(root: Path) -> list[Path]:
    """Removes empty subdirectories under root, bottom-up, leaving root
    itself in place even if it ends up empty - used after --archive moves
    files out of a folder tree, so processed subfolders (e.g. one per
    podcast episode with --recursive) don't linger behind empty."""
    removed = []
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        current = Path(dirpath)
        if current == root:
            continue
        try:
            if not any(current.iterdir()):
                current.rmdir()
                removed.append(current)
                logger.info("Removed empty folder: %s", current)
        except OSError:
            pass
    return removed


def run_local_batch(
    config: Config,
    source_dir: Path,
    dest_dir: Path | None,
    recursive: bool = False,
    archive_dir: Path | None = None,
) -> dict[str, list]:
    """Processes every audio file in a local directory. Continues to the
    next file even if one fails; returns a summary of files processed,
    skipped (already done), and failed."""
    source_dir = source_dir.resolve()
    files = [_ensure_local_name_trimmed(p) for p in _iter_local_audio_files(source_dir, recursive)]

    to_work_on: list[Path] = []
    n_transcript = 0
    n_lock = 0
    for audio_path in files:
        _, _, transcript_path, legacy_transcript_paths, lock_path, legacy_lock_path = _local_target(
            audio_path, dest_dir
        )
        has_transcript = transcript_path.exists() or any(p.exists() for p in legacy_transcript_paths)
        has_lock = lock_path.exists() or legacy_lock_path.exists()
        n_transcript += has_transcript
        n_lock += has_lock
        if config.force or not (has_transcript or has_lock):
            to_work_on.append(audio_path)

    logger.info(
        "%s: %d audio, %d transcript, %d locks, %d to work on",
        source_dir,
        len(files),
        n_transcript,
        n_lock,
        len(to_work_on),
    )

    summary: dict[str, list] = {"processed": [], "skipped": [], "failed": []}
    for i, audio_path in enumerate(to_work_on, start=1):
        logger.info("--- File %d/%d: %s ---", i, len(to_work_on), audio_path.name)
        this_archive_dir = (
            _relative_archive_dir(audio_path, source_dir, archive_dir)
            if archive_dir is not None
            else None
        )
        try:
            outputs = run_local_job(config, audio_path, dest_dir, archive_dir=this_archive_dir)
        except Exception:
            logger.exception("Failed to process %s, continuing with the rest.", audio_path)
            summary["failed"].append(str(audio_path))
            continue
        if outputs:
            summary["processed"].append(str(audio_path))
        else:
            summary["skipped"].append(str(audio_path))

    if archive_dir is not None:
        _remove_empty_dirs_local(source_dir)

    return summary


def run_dropbox_batch(
    config: Config,
    dropbox_client,
    source_folder: str,
    dest_folder: str | None,
    recursive: bool = False,
    archive_folder: str | None = None,
) -> dict[str, list]:
    """Processes every audio file in a Dropbox folder. Continues to the
    next file even if one fails; returns a summary of files processed,
    skipped (already done), and failed."""
    files = [
        _ensure_dropbox_name_trimmed(dropbox_client, p)
        for p in dropbox_client.list_audio_files(source_folder, recursive=recursive)
    ]

    to_work_on: list[str] = []
    n_transcript = 0
    n_lock = 0
    for audio_path in files:
        _, _, transcript_path, legacy_transcript_paths, lock_path, legacy_lock_path = _dropbox_target(
            audio_path, dest_folder
        )
        has_transcript = dropbox_client.file_exists(transcript_path) or any(
            dropbox_client.file_exists(p) for p in legacy_transcript_paths
        )
        has_lock = False
        if not has_transcript:
            lock_content = dropbox_client.read_text_file(lock_path) or dropbox_client.read_text_file(
                legacy_lock_path
            )
            has_lock = bool(lock_content) and not _is_lock_stale(lock_content)
        n_transcript += has_transcript
        n_lock += has_lock
        if config.force or not (has_transcript or has_lock):
            to_work_on.append(audio_path)

    logger.info(
        "%s: %d audio, %d transcript, %d locks, %d to work on",
        source_folder,
        len(files),
        n_transcript,
        n_lock,
        len(to_work_on),
    )

    summary: dict[str, list] = {"processed": [], "skipped": [], "failed": []}
    for i, audio_path in enumerate(to_work_on, start=1):
        logger.info("--- File %d/%d: %s ---", i, len(to_work_on), audio_path)
        this_archive_folder = (
            _relative_archive_folder(audio_path, source_folder, archive_folder)
            if archive_folder is not None
            else None
        )
        try:
            outputs = run_dropbox_job(
                config, dropbox_client, audio_path, dest_folder, archive_folder=this_archive_folder
            )
        except Exception:
            logger.exception("Failed to process %s, continuing with the rest.", audio_path)
            summary["failed"].append(audio_path)
            continue
        if outputs:
            summary["processed"].append(audio_path)
        else:
            summary["skipped"].append(audio_path)

    if archive_folder is not None:
        dropbox_client.remove_empty_subfolders(source_folder)

    return summary
