#!/bin/bash

# launchd/cron run with a minimal PATH that doesn't include Homebrew's bin
# dirs, even though ffmpeg/git/etc. work fine in an interactive shell -
# this caused a real "ffmpeg not found in PATH" failure in production.
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

lockPath="/Users/fcastell/voxelfc/.update.lock"

createLock() {
    (set -C; umask 077; echo "$$" > "$lockPath") 2>/dev/null
}

if ! /usr/bin/pmset -g batt | /usr/bin/grep -q "AC Power"; then
    exit 0
fi

if ! createLock; then
    existingPid=$(cat "$lockPath" 2>/dev/null)

    if [ -n "$existingPid" ] && kill -0 "$existingPid" 2>/dev/null; then
        echo "Already running (pid $existingPid), skipping."
        exit 0
    fi

    rm -f "$lockPath"

    if ! createLock; then
        echo "Could not acquire lock, skipping."
        exit 0
    fi
fi

trap 'rm -f "$lockPath"' EXIT

cd /Users/fcastell/voxelfc || exit 1
source .venv/bin/activate
bash scripts/update.sh "$@"
