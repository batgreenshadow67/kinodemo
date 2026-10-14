#!/usr/bin/env python3
"""Pack and unpack .clip files.

A .clip is a zip of one moment from a recording: a film-time window, the chat that falls in it,
and enough to check that a film the user supplies is the same cut. The extension is cosmetic —
it's an ordinary zip and opens anywhere.

Used by kinodemo.py:
  * pack(path, out_path)          write a clip
  * unpack(clip_path, clips_dir)  extract a .clip into clips/<name>/
  * peek(clip_path)               read the manifest without extracting
  * is_clip(path)                 does this file look like one of ours
  * find_in(clips_dir)            .clip files sitting in a folder

A clip carries a film *hint* (the file name the maker knew) and a frame hash at the clip's start,
used to warn if the film someone points at is a different cut. Neither is a path.
"""

import json
import os
import re
import shutil
import tempfile
import zipfile

FORMAT = "clip"
VERSION = 1
MANIFEST = "clip.json"
CHAT = "chat.jsonl"
# files that describe this machine's copy of the film, not the clip itself
EXCLUDE = {MANIFEST}

NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

# staging folder prefix used during unpack. It intentionally starts with "~", which NAME_RE and
# kinodemo's SESSION_RE both reject, so a crashed import can never leave something that looks
# like a real clip. kinodemo.sweep_staging() removes any leftovers on startup.
STAGING_PREFIX = "~clip_import_"


def pack(clip_dir, out_path):
    """Zip a clip folder into a .clip. Writes to a temp file then renames, so a partial archive
    is never left behind. Returns the number of bytes written."""
    if not os.path.isdir(clip_dir):
        raise ValueError("That clip folder doesn't exist.")
    if not os.path.isfile(os.path.join(clip_dir, MANIFEST)):
        raise ValueError("That clip has no clip.json.")
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".clip.tmp", dir=out_dir)
    os.close(tmp_fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr(MANIFEST, json.dumps(
                json.load(open(os.path.join(clip_dir, MANIFEST), encoding="utf-8")),
                ensure_ascii=False, indent=2))
            for root, _dirs, files in os.walk(clip_dir):
                for name in sorted(files):
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, clip_dir).replace("\\", "/")
                    if rel in EXCLUDE:
                        continue
                    z.write(full, rel)
        os.replace(tmp_path, out_path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return os.path.getsize(out_path)


def peek(clip_path):
    """Read the manifest out of a .clip without extracting. Returns a dict, or {} if it isn't
    a readable clip."""
    try:
        with zipfile.ZipFile(clip_path) as z:
            with z.open(MANIFEST) as f:
                m = json.loads(f.read().decode("utf-8"))
        if isinstance(m, dict) and m.get("format") == FORMAT:
            return m
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        pass
    return {}


def is_clip(path):
    """True if the file looks like a .clip (has our manifest). Used to filter the clips folder,
    so an unrelated zip sitting there isn't mistaken for a clip."""
    return bool(peek(path))


def unpack(clip_path, clips_dir):
    """Extract a .clip into clips_dir/<name>/. Refuses if that folder already exists, so an
    import can't overwrite a clip you have. Returns the clip name.

    The archive is checked as it's extracted: only regular files, no absolute paths or "..", so a
    malicious .clip can't write outside the clips folder.

    Staging happens in a "~clip_import_*" folder, whose name is rejected by the app's clip name
    pattern, so a crash mid-import leaves nothing that looks like a real clip."""
    m = peek(clip_path)
    if not m:
        raise ValueError("That file isn't a Kinodemo clip (.clip).")
    name = str(m.get("name") or os.path.splitext(os.path.basename(clip_path))[0]).strip()
    if not NAME_RE.match(name) or name in (".", ".."):
        raise ValueError("That .clip has an unusable name.")
    dest = os.path.join(clips_dir, name)
    if os.path.exists(dest):
        raise ValueError(f'A clip called "{name}" is already there. Rename one of them.')

    staging = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=clips_dir)
    try:
        with zipfile.ZipFile(clip_path) as z:
            for info in z.infolist():
                rel = info.filename.replace("\\", "/")
                if rel == MANIFEST:
                    continue
                if rel.startswith("/") or ".." in rel.split("/") or info.is_dir():
                    continue
                target = os.path.join(staging, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        if not os.path.isfile(os.path.join(staging, MANIFEST)):
            raise ValueError("That .clip doesn't contain a clip.")
        os.replace(staging, dest)
        staging = None
        return name
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


def find_in(clips_dir):
    """Names of the .clip files sitting directly in the clips folder."""
    out = []
    try:
        for name in os.listdir(clips_dir):
            if name.lower().endswith(".clip"):
                out.append(name)
    except OSError:
        pass
    return sorted(out, key=str.lower)