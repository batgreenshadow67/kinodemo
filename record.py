#!/usr/bin/env python3
"""Run the chat logger and the frame capture together, into one session folder.

Kinodemo runs this with --session <folder> and stops it by creating a STOP file in that folder.

Usage (from the folder holding record.py, chat_logger.js and framematch.py):
    python record.py                    # run until Ctrl+C
    python record.py --seconds 300      # stop after 5 minutes

Output: recordings/<channel>_<YYYY-MM-DD_HH-MM-SS>/
    chat.jsonl   chat messages ({t, time, username, msg}, t = local unix ms)
    frames/      one <unix-seconds>.jpg per second, plus capture_meta.json
    session.json when the session started and ended (unix seconds)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

# On Windows, keep console children (node, ffmpeg, python) from opening their own windows.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def count_frames(d):
    try:
        return sum(1 for f in os.listdir(d) if f.endswith(".jpg"))
    except OSError:
        return 0


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_URL = "https://8chan.tv/live/201618d3-85d4-4ee8-b9e8-b674ee364fbc/index.m3u8"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="cytu.be")
    ap.add_argument("--channel", default="the-greens-shadowplex")
    ap.add_argument("--url", default=DEFAULT_URL, help="stream (m3u8) URL to capture frames from")
    ap.add_argument("--seconds", type=int, default=0, help="stop after this many seconds (default: until Ctrl+C)")
    ap.add_argument("--outdir", default="recordings")
    ap.add_argument("--session", help="existing folder to write into (Kinodemo passes this); a STOP file in it ends the recording")
    ap.add_argument("--password", help="channel password, if any")
    ap.add_argument("--capture", choices=["auto", "browser", "ffmpeg"], default="auto",
                    help="browser = copy frames from a real browser player (matches what viewers see); "
                         "ffmpeg = read the stream directly; auto = browser, falling back to ffmpeg")
    ap.add_argument("--page", help="player page for browser capture (default: worked out from --url)")
    ap.add_argument("--show", action="store_true", help="show the capture browser window (for debugging)")
    a = ap.parse_args()

    if a.session:
        session = a.session
    else:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        session = os.path.join(a.outdir, f"{a.channel}_{stamp}")
    os.makedirs(session, exist_ok=True)
    stop_file = os.path.join(session, "STOP")
    if os.path.exists(stop_file):
        os.remove(stop_file)
    chat_path = os.path.join(session, "chat.jsonl")
    frames_dir = os.path.join(session, "frames")

    chat_cmd = ["node", os.path.join(HERE, "chat_logger.js"),
                "--host", a.host, "--channel", a.channel, "--out", chat_path]
    if a.password:
        chat_cmd += ["--password", a.password]
    ffmpeg_cmd = [sys.executable, "-u", os.path.join(HERE, "framematch.py"), "capture", a.url, frames_dir,
                  "--stop-file", stop_file]
    if a.seconds:
        ffmpeg_cmd += ["--seconds", str(a.seconds)]

    page = a.page
    if not page:
        m = re.match(r"(https?://[^/]+)/live/([0-9a-fA-F-]{36})/", a.url)
        if m:
            page = f"{m.group(1)}/view/{m.group(2)}/?embedded=True"
    mode = a.capture
    if mode == "auto":
        mode = "browser" if page else "ffmpeg"
    elif mode == "browser" and not page:
        sys.exit("Browser capture needs --page (couldn't work it out from the stream URL).")
    browser_cmd = ["node", os.path.join(HERE, "capture_browser.js"), "--page", page or "",
                   "--out", frames_dir, "--stop-file", stop_file]
    if a.seconds:
        browser_cmd += ["--seconds", str(a.seconds)]
    if a.show:
        browser_cmd += ["--show"]
    cap_cmd = browser_cmd if mode == "browser" else ffmpeg_cmd

    print(f"Session folder: {session}")
    started = time.time()
    chat = subprocess.Popen(chat_cmd, creationflags=CREATE_NO_WINDOW)
    print(f"Capture mode: {mode}")
    cap = subprocess.Popen(cap_cmd, creationflags=CREATE_NO_WINDOW)

    interrupted = False
    try:
        cap.wait()
        if (mode == "browser" and a.capture == "auto" and cap.returncode != 0
                and not os.path.exists(stop_file) and count_frames(frames_dir) < 3):
            print("Browser capture didn't start (see above). Falling back to reading the stream with ffmpeg.")
            mode = "ffmpeg"
            cap = subprocess.Popen(ffmpeg_cmd, creationflags=CREATE_NO_WINDOW)
            cap.wait()
    except KeyboardInterrupt:
        interrupted = True
        try:
            cap.wait(timeout=30)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            cap.kill()
    finally:
        if chat.poll() is None:
            if interrupted:
                try:
                    chat.wait(timeout=5)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    chat.terminate()
            else:
                chat.terminate()
        ended = time.time()
        with open(os.path.join(session, "session.json"), "w") as f:
            json.dump({"started": started, "ended": ended, "host": a.host, "channel": a.channel, "url": a.url, "capture": mode}, f, indent=2)
        print(f"Done. Everything is in {session}")


if __name__ == "__main__":
    main()