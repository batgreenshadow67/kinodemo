#!/usr/bin/env python3
"""Kinodemo: a local web app to record a CyTube showing, match it to a film, and play it back with chat.

Run:   python3 kinodemo.py       (opens http://127.0.0.1:8765 in your browser)
Needs the other files in this folder: app.html, record.py, framematch.py, chat_logger.js, make_replay.py, kino.py, clip.py
Only the Python standard library is used. The server listens on this computer only.
"""

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import make_replay
import kino
import clip as clipmod

HERE = os.path.dirname(os.path.abspath(__file__))
REC_DIR = os.path.join(HERE, "recordings")
IDX_DIR = os.path.join(HERE, "film_index")
EMOTE_DIR = os.path.join(HERE, "emotes")
EXPORT_DIR = os.path.join(HERE, "exports")
CLIP_DIR = os.path.join(HERE, "clips")
CLIP_EXPORT_DIR = os.path.join(HERE, "clip_exports")
FILTERS_PATH = os.path.join(HERE, "chat_filters.json")
SETTINGS_PATH = os.path.join(HERE, "settings.json")
CLIP_LEAD = 3.0
CLIP_TRAIL = 3.0
HASH_MAX_DIST = 64

# what a browser can decode. conservative: only the well-supported combinations.
PLAYABLE_CONTAINERS = {"mp4", "m4v", "webm"}
PLAYABLE_VIDEO = {"h264", "avc1", "hevc", "h265", "vp8", "vp9", "av1"}
PLAYABLE_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}

DEFAULTS = {
    "host": "cytu.be",
    "channel": "the-greens-shadowplex",
    "url": "https://8chan.tv/live/201618d3-85d4-4ee8-b9e8-b674ee364fbc/index.m3u8",
    "film_dir": "",
    "emote_pack": "",
    "kino_dir": "",
    "clip_dir": "",
    "nnd": {
        "enabled": True,
        "show_images": True,
        "discard_when_full": False,
        "ignore_overlap": False,
        "show_usernames": False,
        "opacity": 70,
        "order": "random",
        "direction": "rtl",
        "max_messages": 125,
        "font_size": 32,
        "max_image_height": 128,
        "v_gap": 4,
    },
}
SESSION_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
NAME_RE = re.compile(r"^(?P<channel>.+)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<h>\d{2})-(?P<m>\d{2})-(?P<s>\d{2})$")
CHANNEL_RE = re.compile(r"^[A-Za-z0-9_\-]{1,50}$")

# Staging dirs used by kino.py / clip.py while importing. The prefix contains a character that
# SESSION_RE rejects, so a crash mid-import can never leave something that looks like a real
# recording or clip. sweep_staging() removes any leftovers on startup.
STAGING_PREFIXES = ("~kino_import_", "~clip_import_")


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class JobError(Exception):
    pass


shutdown_event = threading.Event()
CURRENT_PORT = None


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------

def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def load_settings():
    s = dict(DEFAULTS)
    s.update(read_json(SETTINGS_PATH, {}) or {})
    nnd = dict(DEFAULTS["nnd"])
    saved = s.get("nnd")
    if isinstance(saved, dict):
        nnd.update({k: v for k, v in saved.items() if k in DEFAULTS["nnd"]})
    s["nnd"] = nnd
    return s


def save_settings(update):
    s = load_settings()
    s.update(update)
    write_json(SETTINGS_PATH, s)


def sweep_staging():
    """Remove leftover staging folders from a previous crashed import. The prefixes used by
    kino.py and clip.py intentionally don't match SESSION_RE, so even if this never ran they
    wouldn't show up as recordings/clips; this just keeps the folders tidy."""
    for parent in (REC_DIR, CLIP_DIR):
        if not os.path.isdir(parent):
            continue
        for name in os.listdir(parent):
            if any(name.startswith(p) for p in STAGING_PREFIXES):
                shutil.rmtree(os.path.join(parent, name), ignore_errors=True)


def session_dir(name):
    if not isinstance(name, str) or not SESSION_RE.match(name) or name in (".", ".."):
        raise ApiError("Bad recording name.")
    d = os.path.join(REC_DIR, name)
    if not os.path.isdir(d):
        raise ApiError("That recording no longer exists.", 404)
    return d


def count_lines(path):
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def count_frames(frames_dir):
    try:
        return sum(1 for f in os.listdir(frames_dir) if f.endswith(".jpg"))
    except OSError:
        return 0


def tail_text(path, nbytes=4000):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def tail_messages(path, n=6):
    out = []
    for line in tail_text(path, 20000).splitlines()[-n:]:
        try:
            d = json.loads(line)
            out.append({"u": d.get("username", ""), "m": d.get("msg", "")})
        except ValueError:
            pass
    return out


def read_session_meta(d):
    m = read_json(os.path.join(d, "meta.json"), {}) or {}
    return {"title": str(m.get("title", "") or ""), "description": str(m.get("description", "") or "")}


def write_session_meta(d, title, description):
    write_json(os.path.join(d, "meta.json"), {"title": title, "description": description})


# emote packs

def parse_emotes(data):
    if isinstance(data, dict) and isinstance(data.get("emotes"), list):
        data = data["emotes"]
    if not isinstance(data, list):
        return []
    out = []
    for e in data:
        if isinstance(e, dict) and e.get("name") and e.get("image"):
            out.append({"name": str(e["name"]), "image": str(e["image"])})
    return out


def emote_packs():
    if not os.path.isdir(EMOTE_DIR):
        return []
    names = []
    for name in os.listdir(EMOTE_DIR):
        if name.lower().endswith((".json", ".txt")):
            names.append(os.path.splitext(name)[0])
    return sorted(names, key=str.lower)


def active_emote_pack():
    packs = emote_packs()
    if not packs:
        return ""
    chosen = str(load_settings().get("emote_pack") or "")
    if chosen in packs:
        return chosen
    return packs[0]


def read_emote_pack(name):
    if not name:
        return None
    for ext in (".json", ".txt"):
        path = os.path.join(EMOTE_DIR, name + ext)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8-sig") as f:
                    return parse_emotes(json.load(f))
            except (OSError, ValueError):
                return None
    return None


def emotes_for(session):
    d = os.path.join(REC_DIR, session)
    own = parse_emotes(read_json(os.path.join(d, "emotes.json"), None))
    if own:
        return own, "session", ""
    pack = active_emote_pack()
    e = read_emote_pack(pack)
    if e is None:
        return [], None, pack
    return e, "pack", pack


# .kino

def extract_dropped_kinos():
    for fname in kino.find_in(REC_DIR):
        path = os.path.join(REC_DIR, fname)
        try:
            if kino.is_kino(path):
                kino.unpack(path, REC_DIR)
        except (ValueError, OSError):
            pass
        except Exception:
            pass


# .clip

def clip_dir(name):
    if not isinstance(name, str) or not SESSION_RE.match(name) or name in (".", ".."):
        raise ApiError("Bad clip name.")
    d = os.path.join(CLIP_DIR, name)
    if not os.path.isdir(d):
        raise ApiError("That clip no longer exists.", 404)
    return d


def read_clip_meta(d):
    return read_json(os.path.join(d, "clip.json"), {}) or {}


def read_clip_film(d):
    m = read_json(os.path.join(d, "demo.json"), {}) or {}
    film = str(m.get("film") or "")
    return film if film and os.path.isfile(film) else ""


def write_clip_film(d, film):
    write_json(os.path.join(d, "demo.json"), {"film": film, "film_name": os.path.basename(film)})


def resolve_clip_film(d):
    """The film to play a clip against: its own if set, else its source recording's. Resolved
    once and saved to the clip, so the picker is only needed when neither has one."""
    film = read_clip_film(d)
    if film:
        return film
    meta = read_clip_meta(d)
    src = str(meta.get("source") or "").strip()
    if src and SESSION_RE.match(src):
        srcdir = os.path.join(REC_DIR, src)
        if os.path.isdir(srcdir):
            smeta = read_json(os.path.join(srcdir, "demo.json"), {}) or {}
            sf = str(smeta.get("film") or "")
            if sf and os.path.isfile(sf):
                write_clip_film(d, sf)
                return sf
    return ""


def extract_dropped_clips():
    for fname in clipmod.find_in(CLIP_DIR):
        path = os.path.join(CLIP_DIR, fname)
        try:
            if clipmod.is_clip(path):
                clipmod.unpack(path, CLIP_DIR)
        except (ValueError, OSError):
            pass
        except Exception:
            pass


def count_clips_for(session_name):
    """How many clips came from a given recording. Cheap: reads each clip.json."""
    n = 0
    if not os.path.isdir(CLIP_DIR):
        return 0
    for name in os.listdir(CLIP_DIR):
        d = os.path.join(CLIP_DIR, name)
        if not os.path.isdir(d):
            continue
        meta = read_clip_meta(d)
        if str(meta.get("source") or "") == session_name:
            n += 1
    return n


def list_clips():
    out = []
    if not os.path.isdir(CLIP_DIR):
        return out
    extract_dropped_clips()
    for name in os.listdir(CLIP_DIR):
        d = os.path.join(CLIP_DIR, name)
        if not os.path.isdir(d) or not SESSION_RE.match(name):
            continue
        meta = read_clip_meta(d)
        if not meta:
            continue
        out.append({
            "name": name,
            "title": str(meta.get("title") or ""),
            "description": str(meta.get("description") or ""),
            "created": meta.get("created") or "",
            "source": str(meta.get("source") or ""),
            "film_name_hint": str(meta.get("film_name_hint") or ""),
            "film_start": meta.get("film_start"),
            "film_end": meta.get("film_end"),
            "messages": count_lines(os.path.join(d, "chat.jsonl")),
            "playable": bool(resolve_clip_film(d)),
            "sort": str(meta.get("created") or ""),
        })
    out.sort(key=lambda c: c["sort"], reverse=True)
    return out


def frame_hash(film, seconds):
    py = [sys.executable, "-u", os.path.join(HERE, "framematch.py")]
    try:
        p = subprocess.run(py + ["hashframe", film, str(seconds)],
                           cwd=HERE, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        return ""
    if p.returncode != 0:
        return ""
    return p.stdout.decode("utf-8", "replace").strip()


def hash_close(a, b, max_dist=HASH_MAX_DIST):
    if not a or not b:
        return None
    try:
        ba, bb = bytes.fromhex(a), bytes.fromhex(b)
    except ValueError:
        return None
    if len(ba) != len(bb):
        return None
    bits = sum(bin(x ^ y).count("1") for x, y in zip(ba, bb))
    return bits <= max_dist


def probe_film(path):
    """Ask ffprobe what's in a file and say whether a browser is likely to play it.
    Returns {ok, playable, container, video, audio, reason}. Never raises."""
    out = {"ok": False, "playable": False, "container": "", "video": "", "audio": "", "reason": ""}
    if not shutil.which("ffprobe"):
        out["reason"] = "ffprobe wasn't found on your PATH, so the file can't be checked."
        return out
    try:
        p = subprocess.run(["ffprobe", "-hide_banner", "-v", "error", "-show_format", "-show_streams",
                            "-of", "json", "--", path],
                           capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        out["reason"] = "ffprobe couldn't read the file."
        return out
    if p.returncode != 0:
        out["reason"] = "ffprobe couldn't read the file."
        return out
    try:
        info = json.loads(p.stdout.decode("utf-8", "replace"))
    except ValueError:
        out["reason"] = "ffprobe returned something unexpected."
        return out

    fmt = (info.get("format") or {}).get("format_name", "")
    video = ""
    audio = ""
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and not video:
            video = (s.get("codec_name") or "").lower()
        elif s.get("codec_type") == "audio" and not audio:
            audio = (s.get("codec_name") or "").lower()
    out.update(ok=True, container=fmt, video=video, audio=audio)

    # mp4/mov/mkv/etc can all be "mov,mp4,m4a,3gp,3g2,mj2" or similar in ffprobe's naming
    container_ok = any(c in fmt for c in PLAYABLE_CONTAINERS)
    video_ok = video in PLAYABLE_VIDEO
    audio_ok = (not audio) or (audio in PLAYABLE_AUDIO)

    if container_ok and video_ok and audio_ok:
        out["playable"] = True
        return out

    if video and not video_ok:
        out["reason"] = f"the video is {video}, which browsers can't play."
    elif audio and not audio_ok:
        out["reason"] = f"the audio is {audio}, which browsers can't play."
    elif not container_ok and fmt:
        out["reason"] = f"the container is {fmt}, which browsers can't play."
    else:
        out["reason"] = "this file's format isn't one browsers can play."
    return out


def create_clip(session, title, description, film_start, film_end):
    d = session_dir(session)
    demo = read_json(os.path.join(d, "demo.json"), {}) or {}
    replay = read_json(os.path.join(d, "replay.json"), {}) or {}
    if not demo.get("film") or not replay:
        raise ApiError("Match this recording with a film before making a clip from it.")
    try:
        start = max(0.0, float(film_start))
        end = float(film_end)
    except (TypeError, ValueError):
        raise ApiError("Bad clip start or end.")
    if end <= start:
        raise ApiError("The clip's end has to be after its start.")

    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    name = f"{session}_{stamp}"[:120]
    cd = os.path.join(CLIP_DIR, name)
    if os.path.exists(cd):
        raise ApiError("A clip with that name already exists.")

    lo, hi = start - CLIP_LEAD, end + CLIP_TRAIL
    messages = [m for m in replay.get("messages", [])
                if lo <= float(m.get("f", 0)) <= hi]
    os.makedirs(cd)
    try:
        with open(os.path.join(cd, "chat.jsonl"), "w", encoding="utf-8") as f:
            for m in messages:
                f.write(json.dumps({"t": m.get("t"), "f": m.get("f"),
                                    "u": m.get("u", ""), "m": m.get("m", "")},
                                   ensure_ascii=False) + "\n")
        meta = {
            "format": "clip",
            "version": 1,
            "name": name,
            "title": str(title or "").strip()[:200],
            "description": str(description or "").strip()[:2000],
            "created": datetime.now().isoformat(timespec="seconds"),
            "source": session,
            "film_name_hint": os.path.basename(demo.get("film", "")) if demo.get("film") else "",
            "film_start": round(start, 3),
            "film_end": round(end, 3),
            "lead": CLIP_LEAD,
            "trail": CLIP_TRAIL,
            "hash": frame_hash(demo["film"], start),
        }
        write_json(os.path.join(cd, "clip.json"), meta)
    except Exception:
        shutil.rmtree(cd, ignore_errors=True)
        raise
    return name


# ----------------------------------------------------------------------------
# recorder
# ----------------------------------------------------------------------------

rec_lock = threading.Lock()
recorder = {"proc": None, "session": None, "started": None, "ended": None}


def rec_running():
    p = recorder["proc"]
    return p is not None and p.poll() is None


def start_recording(o):
    channel = str(o.get("channel", "")).strip()
    if not CHANNEL_RE.match(channel):
        raise ApiError("Channel names can only use letters, numbers, - and _.")
    host = re.sub(r"^https?://", "", str(o.get("host", "")).strip().lower()).rstrip("/")
    if not re.fullmatch(r"[a-z0-9.\-]+(:\d+)?", host):
        raise ApiError("The site should look like cytu.be.")
    url = str(o.get("url", "")).strip()
    if not re.match(r"https?://\S+$", url):
        raise ApiError("The stream URL has to start with http:// or https://.")
    try:
        minutes = o.get("minutes")
        seconds = int(float(minutes) * 60) if minutes not in (None, "") else 0
        if seconds < 0:
            raise ValueError
    except (ValueError, TypeError):
        raise ApiError("Stop after should be a number of minutes, or empty.")
    password = str(o.get("password") or "")
    if not shutil.which("ffmpeg"):
        raise ApiError("ffmpeg wasn't found on your PATH, so frames can't be captured.")
    if not shutil.which("node"):
        raise ApiError("Node.js wasn't found on your PATH, so chat can't be logged.")

    with rec_lock:
        if rec_running():
            raise ApiError("A recording is already running.")
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        name = f"{channel}_{stamp}"
        session = os.path.join(REC_DIR, name)
        os.makedirs(session)
        cmd = [sys.executable, "-u", os.path.join(HERE, "record.py"),
               "--host", host, "--channel", channel, "--url", url, "--session", session]
        if seconds:
            cmd += ["--seconds", str(seconds)]
        if password:
            cmd += ["--password", password]
        log = open(os.path.join(session, "record.log"), "w", encoding="utf-8")
        proc = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        log.close()
        recorder.update(proc=proc, session=name, started=time.time(), ended=None)
    save_settings({"host": host, "channel": channel, "url": url})
    return name


def stop_recording():
    with rec_lock:
        if not rec_running():
            return
        open(os.path.join(REC_DIR, recorder["session"], "STOP"), "w").close()


def rec_status():
    with rec_lock:
        name = recorder["session"]
        if not name:
            return {"session": None, "running": False}
        running = rec_running()
        if not running and recorder["ended"] is None:
            recorder["ended"] = time.time()
        d = os.path.join(REC_DIR, name)
        chat = os.path.join(d, "chat.jsonl")
        end = time.time() if running else recorder["ended"]
        log_lines = [l for l in tail_text(os.path.join(d, "record.log"), 3000).splitlines() if l.strip()]
        return {
            "session": name,
            "running": running,
            "stopping": running and os.path.exists(os.path.join(d, "STOP")),
            "started": recorder["started"],
            "elapsed": end - recorder["started"],
            "frames": count_frames(os.path.join(d, "frames")),
            "messages": count_lines(chat),
            "last": tail_messages(chat),
            "log": log_lines[-8:],
            "returncode": None if running else recorder["proc"].returncode,
        }


# ----------------------------------------------------------------------------
# recordings list
# ----------------------------------------------------------------------------

def list_sessions():
    out = []
    if not os.path.isdir(REC_DIR):
        return out
    extract_dropped_kinos()
    current = recorder["session"] if rec_running() else None
    for name in os.listdir(REC_DIR):
        d = os.path.join(REC_DIR, name)
        if not os.path.isdir(d) or not SESSION_RE.match(name):
            continue
        m = NAME_RE.match(name)
        channel, started = name, None
        if m:
            channel = m.group("channel")
            started = datetime.strptime(f"{m.group('date')} {m.group('h')}:{m.group('m')}:{m.group('s')}",
                                        "%Y-%m-%d %H:%M:%S")
        smeta = read_json(os.path.join(d, "session.json"), {}) or {}
        frames = count_frames(os.path.join(d, "frames"))
        duration = (smeta["ended"] - smeta["started"]) if "ended" in smeta and "started" in smeta else None
        demo = read_json(os.path.join(d, "demo.json"))
        user_meta = read_session_meta(d)
        out.append({
            "name": name,
            "title": user_meta["title"],
            "description": user_meta["description"],
            "channel": channel,
            "started": started.strftime("%a %b %d, %H:%M") if started else "",
            "sort": started.isoformat() if started else datetime.fromtimestamp(os.path.getmtime(d)).isoformat(),
            "duration": duration if duration is not None else (frames or None),
            "frames": frames,
            "messages": count_lines(os.path.join(d, "chat.jsonl")),
            "clips": count_clips_for(name),
            "running": name == current,
            "demo": ({k: demo.get(k) for k in ("film", "film_name", "pct", "confident", "frames", "start_film", "warning")}
                     if demo else None),
        })
    out.sort(key=lambda s: s["sort"], reverse=True)
    return out


# ----------------------------------------------------------------------------
# match job
# ----------------------------------------------------------------------------

job = {"id": 0, "state": "idle", "session": None, "step": "", "log": [], "error": None, "result": None}


def job_log(line):
    job["log"].append(line)
    if len(job["log"]) > 400:
        del job["log"][:100]


def run_cmd(cmd, label):
    job_log("> " + label)
    p = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace")
    out = []
    for line in p.stdout:
        line = line.rstrip()
        if line:
            job_log(line)
            out.append(line)
    p.wait()
    return p.returncode, "\n".join(out)


def prepare_job(name, film):
    try:
        d = session_dir(name)
        if not os.path.isfile(film):
            raise JobError(f"The film file wasn't found: {film}")
        if count_frames(os.path.join(d, "frames")) < 5:
            raise JobError("This recording has too few captured frames to match.")
        py = [sys.executable, "-u", os.path.join(HERE, "framematch.py")]

        key = hashlib.sha1(f"{film}|{os.path.getsize(film)}|{int(os.path.getmtime(film))}".encode("utf-8")).hexdigest()[:16]
        os.makedirs(IDX_DIR, exist_ok=True)
        film_npz = os.path.join(IDX_DIR, key + ".npz")
        job["step"] = "Reading the film"
        if os.path.exists(film_npz):
            job_log("This film was read before, reusing that.")
        else:
            job_log("Reading the film for the first time. This takes about a minute per hour of film.")
            rc, _ = run_cmd(py + ["index", film, film_npz], "read the film")
            if rc != 0 or not os.path.exists(film_npz):
                raise JobError("Couldn't read the film file. Details are below.")

        job["step"] = "Hashing the captured frames"
        cap_npz = os.path.join(d, "cap.npz")
        rc, _ = run_cmd(py + ["hashdir", os.path.join(d, "frames"), cap_npz], "hash captured frames")
        if rc != 0 or not os.path.exists(cap_npz):
            raise JobError("Couldn't read the captured frames. Details are below.")

        job["step"] = "Matching frames to the film"
        csv_path = os.path.join(d, "matches.csv")
        rc, report = run_cmd(py + ["match", film_npz, cap_npz, "--csv", csv_path], "match")
        m = re.search(r"confident matches: (\d+) / (\d+) \((\d+)%\)", report)
        if rc != 0 or not m:
            raise JobError("Matching failed. Details are below.")
        confident, frames, pct = int(m.group(1)), int(m.group(2)), int(m.group(3))

        job["step"] = "Building the playback data"
        try:
            data = make_replay.build_replay(csv_path, os.path.join(d, "chat.jsonl"), os.path.basename(film))
        except ValueError as e:
            raise JobError(f"{e} Check that this is the same film (and the same cut) that was playing.")
        for n in data["notes"]:
            job_log(n)

        warning = None
        if pct < 50:
            warning = f"Only {pct}% of frames matched. This may be the wrong film or a different cut."
        elif data["drift"]:
            warning = "The film was paused or skipped during this recording. The chat timing was corrected for it, but moments right around those points may be a second or two off."

        # a note if the film can't actually be played in a browser
        probe = probe_film(film)
        if probe.get("ok") and not probe.get("playable"):
            warning = (warning + " " if warning else "") + \
                      f"This film can't be played in a browser ({probe['reason']}). " \
                      f"Convert it to MP4 to watch the replay."

        old = read_json(os.path.join(d, "demo.json"), {}) or {}
        write_json(os.path.join(d, "replay.json"), data)
        meta = {"film": film, "film_name": os.path.basename(film), "pct": pct, "confident": confident,
                "frames": frames, "start_film": data["offsetRel"], "report": report, "warning": warning,
                "playable": bool(probe.get("playable")),
                "prepared": datetime.now().isoformat(timespec="seconds"),
                "shift": old.get("shift", load_settings().get("default_shift", 0))}
        write_json(os.path.join(d, "demo.json"), meta)
        save_settings({"film_dir": os.path.dirname(film)})
        job["result"] = {k: meta[k] for k in ("pct", "confident", "frames", "start_film", "report", "warning", "film_name", "playable")}
        job["state"] = "done"
    except JobError as e:
        job.update(state="error", error=str(e))
    except ApiError as e:
        job.update(state="error", error=str(e))
    except Exception as e:
        traceback.print_exc()
        job.update(state="error", error=f"Unexpected error: {e}")


def start_prepare(name, film):
    session_dir(name)
    if job["state"] == "running":
        raise ApiError("Another match is still running.")
    if rec_running() and recorder["session"] == name:
        raise ApiError("Stop this recording before matching it.")
    film = os.path.normpath(str(film or "").strip().strip('"').strip("'"))
    if not film or film == ".":
        raise ApiError("Choose the film file first.")
    if not os.path.isfile(film):
        raise ApiError("That film file wasn't found. Check the path.")
    job.update(id=job["id"] + 1, state="running", session=name, step="Starting", log=[], error=None, result=None)
    threading.Thread(target=prepare_job, args=(name, film), daemon=True).start()


# ----------------------------------------------------------------------------
# restart
# ----------------------------------------------------------------------------

def port_answers(port, timeout=0.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(b"GET /api/state HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            head = s.recv(64)
            return head.startswith(b"HTTP/")
    except OSError:
        return False


def restart_server():
    """Restart by shutting the old server down first, waiting for its port to be released, then
    starting a fresh child on the same port. Doing it in this order is what makes the browser's
    reload land on the new server instead of a dead port. If the new child fails to come up, the
    old server is already gone; the caller shows the error and the user can start Kinodemo again.

    Returns the number of seconds the client should wait before reloading.
    """
    port = CURRENT_PORT
    if port is None:
        raise ApiError("Can't restart: no port is known.")

    if rec_running():
        stop_recording()
        deadline = time.time() + 30
        while rec_running() and time.time() < deadline:
            time.sleep(0.25)

    # 1. signal the old server to stop, and wait for the port to be free
    threading.Timer(0.2, shutdown_event.set).start()
    deadline = time.time() + 10
    while time.time() < deadline:
        if not port_answers(port, 0.25):
            break
        time.sleep(0.2)
    else:
        raise ApiError("The old server didn't shut down in time. Try closing Kinodemo and starting it again.")

    # 2. start the new child on the same port. It will exit non-zero if the port is somehow taken.
    cmd = [sys.executable, os.path.abspath(__file__), "--port", str(port), "--no-browser"]
    try:
        child = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise ApiError(f"The old server has stopped, but the new one couldn't start: {e}. "
                       f"Start Kinodemo again to bring it back.")

    deadline = time.time() + 10
    while time.time() < deadline:
        if child.poll() is not None:
            raise ApiError("The old server has stopped, but the new one exited immediately. "
                           "Check kinodemo.py for errors, then start Kinodemo again.")
        if port_answers(port, 0.4):
            return 2.0
        time.sleep(0.25)

    try:
        child.terminate()
        child.wait(timeout=5)
    except Exception:
        try: child.kill()
        except Exception: pass
    raise ApiError("The old server has stopped, but the new one didn't come up in time. "
                   "Start Kinodemo again to bring it back.")


# ----------------------------------------------------------------------------
# native file dialogs
# ----------------------------------------------------------------------------

OPEN_KINDS = {"video", "kino", "clip"}
SAVE_KINDS = {"save-kino", "save-clip"}

PICK_CODE = r"""
import sys, tkinter as tk
from tkinter import filedialog
root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
initial = sys.argv[1] if len(sys.argv) > 1 else ''
kind = sys.argv[2] if len(sys.argv) > 2 else 'video'
name = sys.argv[3] if len(sys.argv) > 3 else ''
if kind == 'kino':
    p = filedialog.askopenfilename(title='Choose a .kino recording', initialdir=initial or '.',
        filetypes=[('Kinodemo recordings', '*.kino'), ('All files', '*.*')])
elif kind == 'clip':
    p = filedialog.askopenfilename(title='Choose a .clip', initialdir=initial or '.',
        filetypes=[('Kinodemo clips', '*.clip'), ('All files', '*.*')])
elif kind == 'save-kino':
    p = filedialog.asksaveasfilename(title='Save recording as', initialdir=initial or '.',
        initialfile=name, defaultextension='.kino',
        filetypes=[('Kinodemo recordings', '*.kino'), ('All files', '*.*')])
elif kind == 'save-clip':
    p = filedialog.asksaveasfilename(title='Save clip as', initialdir=initial or '.',
        initialfile=name, defaultextension='.clip',
        filetypes=[('Kinodemo clips', '*.clip'), ('All files', '*.*')])
else:
    p = filedialog.askopenfilename(title='Choose the film file', initialdir=initial or '.',
        filetypes=[('Video files', '*.mp4 *.mkv *.avi *.mov *.webm *.m4v *.wmv'), ('All files', '*.*')])
print(p)
"""


def run_picker(start, kind):
    start = str(start or "").strip().strip('"')
    dir_arg, name_arg = "", ""
    if kind in SAVE_KINDS:
        name_arg = os.path.basename(start) or ""
        dir_arg = os.path.dirname(start)
    else:
        dir_arg = os.path.dirname(start) if start and not os.path.isdir(start) else start
    if not dir_arg or not os.path.isdir(dir_arg):
        if kind == "save-kino":
            dir_arg = load_settings().get("kino_dir") or ""
        elif kind == "save-clip":
            dir_arg = load_settings().get("clip_dir") or ""
        else:
            dir_arg = load_settings().get("film_dir") or ""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run([sys.executable, "-c", PICK_CODE, dir_arg, kind, name_arg],
                           capture_output=True, env=env, timeout=600)
    except subprocess.TimeoutExpired:
        raise ApiError("The file dialog timed out.")
    if p.returncode != 0:
        raise ApiError("The file dialog isn't available on this setup. Paste the path into the box instead.")
    path = p.stdout.decode("utf-8", "replace").strip()
    return os.path.normpath(path) if path else ""


def pick_file(start, kind="video"):
    return run_picker(start, kind if kind in OPEN_KINDS else "video")


def save_file(start, kind="save-kino"):
    return run_picker(start, kind if kind in SAVE_KINDS else "save-kino")


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Kinodemo"

    def log_message(self, *args):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_page(self, path):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            raise ApiError("app.html is missing from the Kinodemo folder.", 500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_range(self, path):
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m:
                s, e = m.groups()
                if s == "" and e != "":
                    start = max(0, size - int(e))
                else:
                    start = int(s or 0)
                    if e:
                        end = min(int(e), size - 1)
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        remaining = end - start + 1
        with open(path, "rb") as f:
            f.seek(start)
            while remaining > 0:
                chunk = f.read(min(262144, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("HEAD")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        try:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if host not in ("127.0.0.1", "localhost"):
                raise ApiError("Not allowed.", 403)
            path = urlparse(self.path).path
            if method == "GET":
                self.route_get(path)
            elif method == "HEAD":
                self.route_head(path)
            else:
                if "application/json" not in (self.headers.get("Content-Type") or ""):
                    raise ApiError("Expected JSON.", 415)
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    raise ApiError("Bad request.")
                self.route_post(path, body if isinstance(body, dict) else {})
        except ApiError as e:
            self.send_json({"error": str(e)}, e.status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as e:
            traceback.print_exc()
            self.close_connection = True
            try:
                self.send_json({"error": f"Server error: {e}"}, 500)
            except Exception:
                pass

    def route_head(self, path):
        m = re.fullmatch(r"/clipfilm/([^/]+)", path)
        if m:
            try:
                d = clip_dir(unquote(m.group(1)))
                if resolve_clip_film(d):
                    self.send_response(200)
                else:
                    self.send_response(404)
            except ApiError:
                self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def route_get(self, path):
        if path in ("/", "/index.html"):
            return self.send_page(os.path.join(HERE, "app.html"))
        if path == "/emote-fx.css":
            local = os.path.join(HERE, "emote-fx.css")
            if os.path.isfile(local):
                return self.send_range(local)
            raise ApiError("No local emote-fx.css found.", 404)
        m = re.fullmatch(r"/exports/([^/]+\.kino)", path)
        if m:
            p = os.path.join(EXPORT_DIR, unquote(m.group(1)))
            if not os.path.isfile(p):
                raise ApiError("Not found.", 404)
            return self.send_range(p)
        m = re.fullmatch(r"/clip-exports/([^/]+\.clip)", path)
        if m:
            p = os.path.join(CLIP_EXPORT_DIR, unquote(m.group(1)))
            if not os.path.isfile(p):
                raise ApiError("Not found.", 404)
            return self.send_range(p)
        if path == "/api/state":
            return self.send_json({
                "settings": load_settings(),
                "recorder": rec_status(),
                "tools": {"ffmpeg": bool(shutil.which("ffmpeg")), "node": bool(shutil.which("node"))},
            })
        if path == "/api/sessions":
            return self.send_json({"sessions": list_sessions()})
        if path == "/api/clips":
            return self.send_json({"clips": list_clips()})
        m = re.fullmatch(r"/api/clip/([^/]+)", path)
        if m:
            d = clip_dir(unquote(m.group(1)))
            meta = read_clip_meta(d)
            messages = []
            try:
                with open(os.path.join(d, "chat.jsonl"), encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            messages.append(json.loads(line))
            except (OSError, ValueError):
                pass
            return self.send_json({**meta, "messages": messages, "film": resolve_clip_film(d)})
        if path == "/api/job":
            return self.send_json({k: (job[k][-300:] if k == "log" else job[k])
                                   for k in ("id", "state", "session", "step", "log", "error", "result")})
        if path == "/api/filters":
            return self.send_json({"filters": read_json(FILTERS_PATH, []) or []})
        if path == "/api/nnd":
            return self.send_json({"settings": load_settings()["nnd"]})
        m = re.fullmatch(r"/api/demo/([^/]+)", path)
        if m:
            d = session_dir(unquote(m.group(1)))
            meta, data = read_json(os.path.join(d, "demo.json")), read_json(os.path.join(d, "replay.json"))
            if not meta or not data:
                raise ApiError("This recording hasn't been matched with a film yet.", 404)
            return self.send_json({**data, "shift": meta.get("shift", 0), "film_name": meta.get("film_name", "")})
        if path == "/api/emotes":
            return self.send_json({"packs": emote_packs(), "active": active_emote_pack()})
        m = re.fullmatch(r"/api/emotes/([^/]+)", path)
        if m:
            name = unquote(m.group(1))
            try:
                session_dir(name)
                emotes, source, pack = emotes_for(name)
            except ApiError:
                pack = active_emote_pack()
                e = read_emote_pack(pack) or []
                emotes, source = e, ("pack" if e else None)
            return self.send_json({"source": source, "pack": pack, "emotes": emotes})
        m = re.fullmatch(r"/film/([^/]+)", path)
        if m:
            d = session_dir(unquote(m.group(1)))
            meta = read_json(os.path.join(d, "demo.json"), {}) or {}
            film = meta.get("film")
            if not film or not os.path.isfile(film):
                raise ApiError("The film file for this recording wasn't found. Match it with a film again.", 404)
            return self.send_range(film)
        m = re.fullmatch(r"/clipfilm/([^/]+)", path)
        if m:
            d = clip_dir(unquote(m.group(1)))
            film = resolve_clip_film(d)
            if not film:
                raise ApiError("No film is set for this clip yet. Pick one to play it.", 404)
            return self.send_range(film)
        raise ApiError("Not found.", 404)

    def route_post(self, path, body):
        if path == "/api/record/start":
            return self.send_json({"session": start_recording(body)})
        if path == "/api/record/stop":
            stop_recording()
            return self.send_json({"ok": True})
        if path == "/api/pick-file":
            return self.send_json({"path": pick_file(body.get("start"), body.get("kind") or "video")})
        if path == "/api/save-file":
            return self.send_json({"path": save_file(body.get("start"), body.get("kind") or "save-kino")})
        if path == "/api/probe-film":
            film = os.path.normpath(str(body.get("film") or "").strip().strip('"'))
            if not film or not os.path.isfile(film):
                raise ApiError("That film file wasn't found.")
            return self.send_json(probe_film(film))
        if path == "/api/prepare":
            start_prepare(body.get("session"), body.get("film"))
            return self.send_json({"ok": True})
        if path == "/api/shift":
            d = session_dir(body.get("session"))
            meta = read_json(os.path.join(d, "demo.json"))
            if meta is None:
                raise ApiError("This recording hasn't been matched yet.", 404)
            try:
                meta["shift"] = max(-300.0, min(300.0, float(body.get("shift", 0))))
            except (TypeError, ValueError):
                raise ApiError("Bad shift value.")
            write_json(os.path.join(d, "demo.json"), meta)
            save_settings({"default_shift": meta["shift"]})
            return self.send_json({"ok": True})
        if path == "/api/session/update":
            d = session_dir(body.get("session"))
            title = str(body.get("title") or "").strip()[:200]
            description = str(body.get("description") or "").strip()[:2000]
            write_session_meta(d, title, description)
            return self.send_json({"ok": True})
        if path == "/api/session/delete":
            name = body.get("session")
            d = session_dir(name)
            if rec_running() and recorder["session"] == name:
                raise ApiError("Stop this recording before deleting it.")
            if job["state"] == "running" and job["session"] == name:
                raise ApiError("A match is running for this recording. Wait for it to finish.")
            shutil.rmtree(d, ignore_errors=False)
            return self.send_json({"ok": True})
        if path == "/api/session/export":
            name = body.get("session")
            d = session_dir(name)
            if rec_running() and recorder["session"] == name:
                raise ApiError("Stop this recording before exporting it.")
            dest = str(body.get("path") or "").strip()
            if dest:
                dest = os.path.normpath(dest)
                if not dest.lower().endswith(".kino"):
                    dest += ".kino"
                parent = os.path.dirname(dest)
                if not parent or not os.path.isdir(parent):
                    raise ApiError("That folder doesn't exist.")
                size = kino.pack(d, dest)
                save_settings({"kino_dir": parent})
                return self.send_json({"ok": True, "written": dest, "size": size})
            os.makedirs(EXPORT_DIR, exist_ok=True)
            out = os.path.join(EXPORT_DIR, name + ".kino")
            kino.pack(d, out)
            return self.send_json({"ok": True, "download": "/exports/" + name + ".kino",
                                   "size": os.path.getsize(out)})
        if path == "/api/session/import":
            src = str(body.get("path") or "").strip()
            candidate = ""
            if src:
                p = os.path.normpath(src)
                if os.path.isfile(p) and p.lower().endswith(".kino"):
                    candidate = p
            if not candidate:
                base = os.path.basename(src)
                for folder in (REC_DIR, EXPORT_DIR):
                    p = os.path.join(folder, base)
                    if os.path.isfile(p) and p.lower().endswith(".kino"):
                        candidate = p
                        break
            if not candidate:
                raise ApiError("That .kino wasn't found.")
            name = kino.unpack(candidate, REC_DIR)
            return self.send_json({"ok": True, "session": name})
        if path == "/api/clip/create":
            name = create_clip(
                body.get("session"),
                body.get("title"),
                body.get("description"),
                body.get("film_start"),
                body.get("film_end"),
            )
            return self.send_json({"ok": True, "clip": name})
        if path == "/api/clip/film":
            d = clip_dir(body.get("clip"))
            meta = read_clip_meta(d)
            film = os.path.normpath(str(body.get("film") or "").strip().strip('"'))
            if not film or not os.path.isfile(film):
                raise ApiError("That film file wasn't found.")
            write_clip_film(d, film)
            save_settings({"film_dir": os.path.dirname(film)})
            verdict = None
            if meta.get("hash") and meta.get("film_start") is not None:
                got = frame_hash(film, meta["film_start"])
                verdict = hash_close(meta["hash"], got)
            return self.send_json({"ok": True, "film": film, "match": verdict})
        if path == "/api/clip/update":
            d = clip_dir(body.get("clip"))
            meta = read_clip_meta(d)
            if not meta:
                raise ApiError("That clip no longer exists.", 404)
            meta["title"] = str(body.get("title") or "").strip()[:200]
            meta["description"] = str(body.get("description") or "").strip()[:2000]
            write_json(os.path.join(d, "clip.json"), meta)
            return self.send_json({"ok": True})
        if path == "/api/clip/delete":
            name = body.get("clip")
            d = clip_dir(name)
            shutil.rmtree(d, ignore_errors=False)
            return self.send_json({"ok": True})
        if path == "/api/clip/export":
            name = body.get("clip")
            d = clip_dir(name)
            dest = str(body.get("path") or "").strip()
            if dest:
                dest = os.path.normpath(dest)
                if not dest.lower().endswith(".clip"):
                    dest += ".clip"
                parent = os.path.dirname(dest)
                if not parent or not os.path.isdir(parent):
                    raise ApiError("That folder doesn't exist.")
                size = clipmod.pack(d, dest)
                save_settings({"clip_dir": parent})
                return self.send_json({"ok": True, "written": dest, "size": size})
            os.makedirs(CLIP_EXPORT_DIR, exist_ok=True)
            out = os.path.join(CLIP_EXPORT_DIR, name + ".clip")
            clipmod.pack(d, out)
            return self.send_json({"ok": True, "download": "/clip-exports/" + name + ".clip",
                                   "size": os.path.getsize(out)})
        if path == "/api/clip/import":
            src = str(body.get("path") or "").strip()
            candidate = ""
            if src:
                p = os.path.normpath(src)
                if os.path.isfile(p) and p.lower().endswith(".clip"):
                    candidate = p
            if not candidate:
                base = os.path.basename(src)
                for folder in (CLIP_DIR, CLIP_EXPORT_DIR):
                    p = os.path.join(folder, base)
                    if os.path.isfile(p) and p.lower().endswith(".clip"):
                        candidate = p
                        break
            if not candidate:
                raise ApiError("That .clip wasn't found.")
            name = clipmod.unpack(candidate, CLIP_DIR)
            return self.send_json({"ok": True, "clip": name})
        if path == "/api/emotes/select":
            pack = str(body.get("pack") or "").strip()
            if pack not in emote_packs():
                raise ApiError("That emote pack wasn't found.")
            save_settings({"emote_pack": pack})
            return self.send_json({"ok": True, "active": pack})
        if path == "/api/nnd":
            cur = load_settings()["nnd"]
            new = dict(cur)
            for k in DEFAULTS["nnd"]:
                if k in body:
                    new[k] = body[k]
            new["enabled"] = bool(new.get("enabled"))
            new["show_images"] = bool(new.get("show_images"))
            new["discard_when_full"] = bool(new.get("discard_when_full"))
            new["ignore_overlap"] = bool(new.get("ignore_overlap"))
            new["show_usernames"] = bool(new.get("show_usernames"))
            try:
                new["opacity"] = max(0, min(100, int(float(new.get("opacity", 70)))))
                new["max_messages"] = max(1, min(2000, int(float(new.get("max_messages", 125)))))
                new["font_size"] = max(6, min(200, int(float(new.get("font_size", 32)))))
                new["max_image_height"] = max(6, min(2000, int(float(new.get("max_image_height", 128)))))
                new["v_gap"] = max(0, min(200, int(float(new.get("v_gap", 4)))))
            except (TypeError, ValueError):
                raise ApiError("Bad numeric value in NND settings.")
            if new["order"] not in ("random", "top"): new["order"] = "random"
            if new["direction"] not in ("rtl", "ltr"): new["direction"] = "rtl"
            save_settings({"nnd": new})
            return self.send_json({"ok": True, "settings": new})
        if path == "/api/restart":
            wait = restart_server()
            return self.send_json({"ok": True, "wait": wait})
        if path == "/api/quit":
            if rec_running():
                stop_recording()
            threading.Timer(0.3, shutdown_event.set).start()
            return self.send_json({"ok": True})
        raise ApiError("Not found.", 404)


def main():
    global CURRENT_PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    os.makedirs(REC_DIR, exist_ok=True)
    os.makedirs(EMOTE_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    os.makedirs(CLIP_DIR, exist_ok=True)
    os.makedirs(CLIP_EXPORT_DIR, exist_ok=True)
    sweep_staging()
    server = None
    for port in range(a.port, a.port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit(f"Couldn't open a port between {a.port} and {a.port + 9}. Close the other Kinodemo window?")
    server.daemon_threads = True
    CURRENT_PORT = server.server_address[1]
    url = f"http://127.0.0.1:{CURRENT_PORT}/"
    print(f"Kinodemo is running at {url}")
    print("Leave this window open while you use it. Press Ctrl+C here to quit.")
    if not a.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        while not shutdown_event.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if rec_running():
            print("Stopping the recording in progress...")
            stop_recording()
            try:
                recorder["proc"].wait(timeout=30)
            except Exception:
                pass
        server.shutdown()
        server.server_close()
        print("Kinodemo has stopped.")


if __name__ == "__main__":
    main()