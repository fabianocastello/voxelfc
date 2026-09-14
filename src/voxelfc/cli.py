from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

from .config import Config
from .logging_setup import setup_logging
from .pipeline import run_dropbox_batch, run_dropbox_job, run_local_batch, run_local_job


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voxelfc",
        description="Downloads audio, transcribes it, diarizes speakers, and delivers the results.",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Path to the source file OR folder (local or Dropbox). If a folder, "
        "processes every audio file in it. Mutually exclusive with --source-list.",
    )
    parser.add_argument(
        "--source-list",
        default=None,
        help="Path to a local text file listing multiple source folders, one per "
        "line (blank lines and lines starting with '#' are ignored). Each line "
        "starting with '/' is treated as a Dropbox folder; any other line is a "
        "local folder. Every source is processed independently, in its own "
        "environment - --dropbox is ignored in this mode. Mutually exclusive "
        "with --source.",
    )
    parser.add_argument(
        "--dest",
        default=None,
        help="Destination folder (local or Dropbox). Default: same folder as the source.",
    )
    parser.add_argument(
        "--archive",
        default=None,
        help="If processing finishes OK, move (not copy) the original audio and the "
        "generated outputs into this folder (local or Dropbox, per --dropbox). With "
        "--source-list, this is a base name reused per source: each source's "
        "outputs move into <archive>/<source folder name>, in that source's own "
        "environment (local source archives locally, Dropbox source archives on "
        "Dropbox), so a mix of local and Dropbox sources can share one --archive "
        "value.",
    )
    parser.add_argument(
        "--dropbox",
        action="store_true",
        help="Treat --source/--dest as Dropbox paths instead of local paths "
        "(local is the default).",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="When processing a folder, also descend into subfolders (useful for "
        "podcast series with one subfolder per episode).",
    )
    parser.add_argument("--model-size", default="large-v3", help="faster-whisper model (default: large-v3).")
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--compute-type", default="auto")
    parser.add_argument("--language", default=None, help="Language code (e.g. pt). Default: auto-detect.")
    parser.add_argument("--min-speakers", type=int, default=None)
    parser.add_argument("--max-speakers", type=int, default=None)
    parser.add_argument(
        "--min-minutes",
        type=float,
        default=None,
        help="Skip audio files shorter than this (in minutes).",
    )
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=None,
        help="Skip audio files longer than this (in minutes).",
    )
    parser.add_argument("--vtt", action="store_true", help="Also generate a .vtt file.")
    parser.add_argument("--no-txt", action="store_true", help="Don't generate a .txt file.")
    parser.add_argument("--no-srt", action="store_true", help="Don't generate a .srt file.")
    parser.add_argument("--keep-temp", action="store_true", help="Don't delete temporary files at the end.")
    parser.add_argument("--force", action="store_true", help="Reprocess even if already completed before.")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    invoked_as = Path(sys.argv[0]).stem.lower()
    if invoked_as == "supertranscriptfc":
        warnings.warn(
            "The 'supertranscriptfc' command is deprecated; use 'voxelfc' instead.",
            DeprecationWarning,
            stacklevel=2,
        )

    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.source and not args.source_list:
        parser.error("one of --source or --source-list is required")
    if args.source and args.source_list:
        parser.error("--source and --source-list are mutually exclusive")

    config = Config(
        model_size=args.model_size,
        device=args.device,
        compute_type=args.compute_type,
        language=args.language,
        min_speakers=args.min_speakers,
        max_speakers=args.max_speakers,
        min_minutes=args.min_minutes,
        max_minutes=args.max_minutes,
        write_txt=not args.no_txt,
        write_srt=not args.no_srt,
        write_vtt=args.vtt,
        keep_temp=args.keep_temp,
        force=args.force,
    )
    config.ensure_dirs()
    logger = setup_logging(config.logs_dir, verbose=args.verbose)

    if not config.hf_token:
        logger.error(
            "HF_TOKEN not configured (required for diarization with pyannote.audio). "
            "Set it in .env before running (see .env.example)."
        )
        return 1

    try:
        if args.source_list:
            get_dropbox_client = _make_dropbox_client_factory(config, logger)
            return _run_source_list(
                config,
                logger,
                Path(args.source_list),
                args.dest,
                args.archive,
                args.recursive,
                get_dropbox_client,
            )
        if not args.dropbox:
            dest_dir = Path(args.dest) if args.dest else None
            archive_dir = Path(args.archive) if args.archive else None
            source_path = Path(args.source)
            if source_path.is_dir():
                summary = run_local_batch(
                    config, source_path, dest_dir, recursive=args.recursive, archive_dir=archive_dir
                )
                _log_batch_summary(logger, summary)
            else:
                outputs = run_local_job(config, source_path, dest_dir, archive_dir=archive_dir)
                if outputs:
                    logger.info("Done. Files generated: %s", [str(p) for p in outputs])
                else:
                    logger.info(
                        "Nothing to do (already processed, in progress on another machine, "
                        "or outside the configured duration limits)."
                    )
        else:
            if not args.source.startswith("/"):
                logger.error(
                    "'--source %s' doesn't look like a Dropbox path (must start with '/'). "
                    "Remove --dropbox if it's a local file.",
                    args.source,
                )
                return 1
            if not config.has_dropbox_credentials:
                logger.error(
                    "DROPBOX_APP_KEY / DROPBOX_APP_SECRET / DROPBOX_REFRESH_TOKEN "
                    "not configured (see .env.example)."
                )
                return 1
            from .dropbox_client import DropboxClient

            client = DropboxClient(
                config.dropbox_app_key, config.dropbox_app_secret, config.dropbox_refresh_token
            )
            is_folder = client.is_folder(args.source)
            if not is_folder and not client.file_exists(args.source):
                logger.error(
                    "Source not found on Dropbox: %s (check the path, or another machine may "
                    "have already archived/moved it).",
                    args.source,
                )
                return 1
            if is_folder:
                summary = run_dropbox_batch(
                    config,
                    client,
                    args.source,
                    args.dest,
                    recursive=args.recursive,
                    archive_folder=args.archive,
                )
                _log_batch_summary(logger, summary)
            else:
                outputs = run_dropbox_job(
                    config, client, args.source, args.dest, archive_folder=args.archive
                )
                if outputs:
                    logger.info("Done. Files uploaded to Dropbox: %s", outputs)
                else:
                    logger.info(
                        "Nothing to do (already processed, in progress on another machine, "
                        "or outside the configured duration limits)."
                    )
        return 0
    except Exception:
        logger.exception("Failed to process %s", args.source or args.source_list)
        return 1


def _log_batch_summary(logger, summary: dict[str, list]) -> None:
    logger.info(
        "Batch complete: %d processed, %d skipped (already done), %d failed.",
        len(summary["processed"]),
        len(summary["skipped"]),
        len(summary["failed"]),
    )
    if summary["failed"]:
        logger.warning("Files with failures: %s", summary["failed"])


def _make_dropbox_client_factory(config: Config, logger):
    """Lazily builds (and caches) a DropboxClient, only if/when a source in
    the list actually needs one - a source list of purely local folders
    never requires Dropbox credentials."""
    cache: dict = {}

    def get():
        if "client" not in cache:
            if not config.has_dropbox_credentials:
                logger.error(
                    "DROPBOX_APP_KEY / DROPBOX_APP_SECRET / DROPBOX_REFRESH_TOKEN "
                    "not configured (see .env.example)."
                )
                cache["client"] = None
            else:
                from .dropbox_client import DropboxClient

                cache["client"] = DropboxClient(
                    config.dropbox_app_key, config.dropbox_app_secret, config.dropbox_refresh_token
                )
        return cache["client"]

    return get


def _read_source_list(list_path: Path) -> list[str]:
    lines = []
    for raw in list_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) >= 2 and line[0] == line[-1] and line[0] in "\"'":
            line = line[1:-1].strip()
        if line:
            lines.append(line)
    return lines


def _is_dropbox_entry(entry: str) -> bool:
    """A leading '/' alone doesn't disambiguate Dropbox from a local
    absolute path (e.g. /home/user/audio) - so an absolute path that
    actually exists on this machine's filesystem is treated as local;
    everything else starting with '/' is assumed to be a Dropbox path."""
    return entry.startswith("/") and not Path(entry).exists()


def _archive_target(archive: str, basename: str, is_dropbox: bool) -> str | Path:
    """--archive is always relative to its own source's environment: a local
    source archives into a local folder, a Dropbox source archives into a
    Dropbox folder, both named <archive>/<basename> - so a single --archive
    value like './_ready' works for a mix of local and Dropbox sources in
    the same --source-list run."""
    if is_dropbox:
        root = archive.strip()
        if root.startswith("./"):
            root = root[2:]
        root = "/" + root.lstrip("/")
        root = root.rstrip("/")
        return f"{root}/{basename}" if root else f"/{basename}"
    return Path(archive) / basename


def _run_source_list(
    config: Config,
    logger,
    list_path: Path,
    dest: str | None,
    archive: str | None,
    recursive: bool,
    get_dropbox_client,
) -> int:
    if not list_path.is_file():
        logger.error("Source list file not found: %s", list_path)
        return 1

    sources = _read_source_list(list_path)
    if not sources:
        logger.error("Source list %s is empty (or only has comments).", list_path)
        return 1

    logger.info("Source list %s: %d entries to process.", list_path, len(sources))
    any_failed = False
    for i, entry in enumerate(sources, start=1):
        is_dropbox = _is_dropbox_entry(entry)
        basename = entry.rstrip("/").rsplit("/", 1)[-1]
        this_archive = _archive_target(archive, basename, is_dropbox) if archive else None
        logger.info(
            "=== Source %d/%d: %s (%s) ===", i, len(sources), entry, "Dropbox" if is_dropbox else "local"
        )
        try:
            if is_dropbox:
                client = get_dropbox_client()
                if client is None:
                    any_failed = True
                    continue
                if client.is_folder(entry):
                    summary = run_dropbox_batch(
                        config, client, entry, dest, recursive=recursive, archive_folder=this_archive
                    )
                    _log_batch_summary(logger, summary)
                    any_failed = any_failed or bool(summary["failed"])
                else:
                    outputs = run_dropbox_job(config, client, entry, dest, archive_folder=this_archive)
                    logger.info("Done: %s -> %s", entry, outputs or "nothing to do")
            else:
                source_path = Path(entry)
                dest_dir = Path(dest) if dest else None
                if source_path.is_dir():
                    summary = run_local_batch(
                        config, source_path, dest_dir, recursive=recursive, archive_dir=this_archive
                    )
                    _log_batch_summary(logger, summary)
                    any_failed = any_failed or bool(summary["failed"])
                else:
                    outputs = run_local_job(config, source_path, dest_dir, archive_dir=this_archive)
                    logger.info("Done: %s -> %s", entry, [str(p) for p in outputs] if outputs else "nothing to do")
        except Exception:
            logger.exception("Failed to process source %s, continuing with the rest.", entry)
            any_failed = True

    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
