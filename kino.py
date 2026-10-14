#!/usr/bin/env python3
"""Pack and unpack .kino files.

A .kino is a zip of one recording session: the session folder's contents at the root, plus a
small kino.json manifest. The extension is cosmetic — it's an ordinary zip and opens anywhere.

Used by kinodemo.py:
  * pack(session_dir, out_path)      zip a session folder into a .kino
  * unpack(kino_path, recordings_dir) extract a .kino into recordings/<name>/
  * peek(kino_path)                  read the manifest without extracting
  * is_kino(path)                    does this file look like one of ours
  * find_in(recordings_dir)          .kino files sitting in a folder

A .kino never carries film information. The film's path and the match results live in demo.json,
which is left out of the archive, so an imported recording always arrives unmatched and the person
who opens it points at their own copy of the film. (Recordings made before this rule existed may
still have a demo.json inside; those are cleared on import if the path doesn't resolve here.)
"""

import json
import os
import re
import shutil
import tempfile
import zipfile

FORMAT = "kino"
VERSION = 1
MANIFEST = "kino.json"
# files that describe this machine's copy of the film, not the recording itself
EXCLUDE = {"kino.json", "demo.json"}

# a recording name is the .kino stem; keep it to the same shape the rest of the app expects
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

# staging folder prefix used during unpack. It intentionally starts with "~", which NAME_RE and
# kinodemo's SESSION_RE both reject, so a crashed import can never leave something that looks
# like a real recording. kinodemo.sweep_staging() removes any leftovers on startup.
STAGING_PREFIX = "~kino_import_"


def _manifest(session_dir):
    """The kino.json to put at the root of the archive."""
    smeta = {}
    try:
        with open(os.path.join(session_dir, "session.json"), encoding="utf-8") as f:
            smeta = json.load(f)
    except (OSError, ValueError):
        pass
    user = {}
    try:
        with open(os.path.join(session_dir, "meta.json"), encoding="utf-8") as f:
            user = json.load(f)
    except (OSError, ValueError):
        pass
    return {
        "format": FORMAT,
        "version": VERSION,
        "name": os.path.basename(session_dir),
        "channel": smeta.get("channel", ""),
        "started": smeta.get("started"),
        "ended": smeta.get("ended"),
        "title": user.get("title", ""),
        "description": user.get("description", ""),
    }


def pack(session_dir, out_path):
    """Zip a session folder into a .kino. Writes to a temp file then renames, so a partial
    archive is never left behind. Returns the number of bytes written.

    demo.json is left out: it holds the film's absolute path and the match results, which are
    specific to the machine the recording was made on."""
    if not os.path.isdir(session_dir):
        raise ValueError("That recording folder doesn't exist.")
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".kino.tmp", dir=out_dir)
    os.close(tmp_fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            # manifest first, so a reader can peek at it without scanning the whole archive
            z.writestr(MANIFEST, json.dumps(_manifest(session_dir), ensure_ascii=False, indent=2))
            for root, _dirs, files in os.walk(session_dir):
                for name in sorted(files):
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, session_dir).replace("\\", "/")
                    if rel in EXCLUDE:
                        continue          # kino.json is written above; demo.json stays behind
                    z.write(full, rel)
        os.replace(tmp_path, out_path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return os.path.getsize(out_path)


def peek(kino_path):
    """Read the manifest out of a .kino without extracting. Returns a dict, or {} if it isn't
    a readable kino. Cheap: only opens the archive, doesn't read the frames."""
    try:
        with zipfile.ZipFile(kino_path) as z:
            with z.open(MANIFEST) as f:
                m = json.loads(f.read().decode("utf-8"))
        if isinstance(m, dict) and m.get("format") == FORMAT:
            return m
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        pass
    return {}


def is_kino(path):
    """True if the file looks like a .kino (has our manifest). Used to filter the recordings
    folder, so an unrelated zip sitting there isn't mistaken for a recording."""
    return bool(peek(path))


def _clear_stale_match(session_dir):
    """Safety net for .kino files made before demo.json was excluded: if the film path recorded
    in there doesn't resolve on this machine, the match is dropped so the user is asked for their
    own copy. Archives made now have no demo.json, so this does nothing for them."""
    path = os.path.join(session_dir, "demo.json")
    try:
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(meta, dict):
        return
    film = meta.get("film")
    if film and os.path.isfile(film):
        return                                  # the film is right here; keep the match
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({}, f)
    except OSError:
        pass


def unpack(kino_path, recordings_dir):
    """Extract a .kino into recordings_dir/<name>/. Refuses if that folder already exists, so an
    import can't overwrite a recording you have. Returns the session name.

    The archive is checked as it's extracted: only regular files, no absolute paths or "..", so a
    malicious .kino can't write outside the recordings folder.

    Staging happens in a "~kino_import_*" folder, whose name is rejected by the app's recording
    name pattern, so a crash mid-import leaves nothing that looks like a real recording."""
    m = peek(kino_path)
    if not m:
        raise ValueError("That file isn't a Kinodemo recording (.kino).")
    name = str(m.get("name") or os.path.splitext(os.path.basename(kino_path))[0]).strip()
    if not NAME_RE.match(name) or name in (".", ".."):
        raise ValueError("That .kino has an unusable recording name.")
    dest = os.path.join(recordings_dir, name)
    if os.path.exists(dest):
        raise ValueError(f'A recording called "{name}" is already there. Rename one of them.')

    staging = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=recordings_dir)
    try:
        with zipfile.ZipFile(kino_path) as z:
            for info in z.infolist():
                rel = info.filename.replace("\\", "/")
                if rel == MANIFEST:
                    continue
                # reject anything that would escape the staging folder
                if rel.startswith("/") or ".." in rel.split("/") or info.is_dir():
                    continue
                target = os.path.join(staging, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        # a kino must at least have a chat log or some frames; otherwise it isn't a recording
        if not (os.path.isfile(os.path.join(staging, "chat.jsonl"))
                or os.path.isdir(os.path.join(staging, "frames"))):
            raise ValueError("That .kino doesn't contain a recording.")
        _clear_stale_match(staging)
        os.replace(staging, dest)
        staging = None
        return name
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


def find_in(recordings_dir):
    """Names of the .kino files sitting directly in the recordings folder."""
    out = []
    try:
        for name in os.listdir(recordings_dir):
            if name.lower().endswith(".kino"):
                out.append(name)
    except OSError:
        pass
    return sorted(out, key=str.lower)