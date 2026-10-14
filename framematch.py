#!/usr/bin/env python3
"""
M0 spike (WP0.1 and WP0.3): capture frames from a stream, hash film frames,
match the two, and report whether frame matching is reliable enough.

Needs only: python3, numpy, ffmpeg/ffprobe on PATH.

Subcommands
  probe    URL                      show what format a stream URL is (ffprobe)
  capture  URL OUTDIR [--seconds N] grab ~1 frame/s from a stream, files named <unix-seconds>.jpg
  index    FILM OUT.npz             hash frames of the local film (run once per film)
  hashdir  DIR OUT.npz              hash captured frames (<unix-seconds>.jpg)
  hashframe FILE SECONDS            print the dHash of one frame (for the clip guard)
  match    FILM.npz CAPTURE.npz     match captured frames to film positions and report
  selftest                          generate a synthetic film + degraded "stream" and run everything

Typical real run
  python3 framematch.py probe  <stream url>
  python3 framematch.py capture <stream url> cap1 --seconds 300
  python3 framematch.py index  film.mkv film.npz
  python3 framematch.py hashdir cap1 cap1.npz
  python3 framematch.py match  film.npz cap1.npz --csv cap1_matches.csv
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter

import numpy as np

HASH_W, HASH_H = 17, 16            # 17x16 grayscale -> 16x16 horizontal-difference bits = 256 bits
CENTER = 0.8                       # keep the central 80% of the frame (ignores logos, overlays, edges)
POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def run(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)


def need(tool):
    if shutil.which(tool) is None:
        sys.exit(f"{tool} not found on PATH")


def detect_crop(input_args, seconds_limit=None):
    """Find the active picture area (removes letterbox/pillarbox bars) with ffmpeg's cropdetect.
    Returns a 'crop=w:h:x:y' filter string, or None if nothing was detected."""
    cmd = ["ffmpeg", "-hide_banner", "-v", "info"] + input_args
    cmd += ["-vf", "fps=1,cropdetect=limit=24:round=2:reset=0", "-f", "null", "-"]
    if seconds_limit:
        cmd[cmd.index("-vf"):cmd.index("-vf")] = ["-t", str(seconds_limit)]
    out = run(cmd).stderr.decode("utf-8", "replace")
    found = re.findall(r"crop=(\d+:\d+:\d+:\d+)", out)
    if not found:
        return None
    return "crop=" + Counter(found).most_common(1)[0][0]


def hash_filter(fps, crop):
    parts = [f"fps={fps}"] if fps else []
    if crop:
        parts.append(crop)
    parts += [f"crop=iw*{CENTER}:ih*{CENTER}", f"scale={HASH_W}:{HASH_H}:flags=area", "format=gray"]
    return ",".join(parts)


def hash_frames(input_args, fps, crop, passthrough=False):
    """Run ffmpeg once and return (n, 32) uint8 array of packed 256-bit dHashes."""
    cmd = ["ffmpeg", "-hide_banner", "-v", "error"] + input_args
    cmd += ["-vf", hash_filter(fps, crop)]
    if passthrough:
        cmd += ["-fps_mode", "passthrough"]  # one output frame per input image, no duplicating/dropping
    cmd += ["-f", "rawvideo", "-pix_fmt", "gray", "-"]
    p = run(cmd)
    if p.returncode != 0:
        sys.exit("ffmpeg failed:\n" + p.stderr.decode("utf-8", "replace")[-2000:])
    data = np.frombuffer(p.stdout, dtype=np.uint8)
    frame_bytes = HASH_W * HASH_H
    n = len(data) // frame_bytes
    if n == 0:
        sys.exit("no frames decoded")
    imgs = data[: n * frame_bytes].reshape(n, HASH_H, HASH_W).astype(np.int16)
    bits = (imgs[:, :, 1:] > imgs[:, :, :-1]).reshape(n, -1)
    return np.packbits(bits, axis=1)


def hamming_matrix(a, b):
    """Hamming distances between every row of a and every row of b -> (len(a), len(b)) int16."""
    out = np.empty((len(a), len(b)), dtype=np.int16)
    step = max(1, 4_000_000 // max(1, len(b) * a.shape[1]))
    for i in range(0, len(a), step):
        x = np.bitwise_xor(a[i:i + step, None, :], b[None, :, :])
        out[i:i + step] = POPCOUNT[x].sum(axis=2)
    return out


# ----------------------------------------------------------------------------
# subcommands
# ----------------------------------------------------------------------------

def cmd_probe(a):
    need("ffprobe")
    p = run(["ffprobe", "-hide_banner", "-v", "error", "-show_format", "-show_streams",
             "-of", "json", "-rw_timeout", "15000000", a.url])
    if p.returncode != 0:
        sys.exit("ffprobe could not open the URL:\n" + p.stderr.decode("utf-8", "replace")[-1500:])
    info = json.loads(p.stdout)
    print("container:", info["format"].get("format_name"), "|", info["format"].get("format_long_name"))
    for s in info["streams"]:
        if s["codec_type"] == "video":
            print(f"video: {s.get('codec_name')} {s.get('width')}x{s.get('height')} {s.get('avg_frame_rate')} fps")
        elif s["codec_type"] == "audio":
            print(f"audio: {s.get('codec_name')} {s.get('sample_rate')} Hz")
    print("\nIf this worked, ffmpeg can capture the stream directly (no browser needed).")


def cmd_capture(a):
    """Grab one frame per second. With -copyts and wallclock timestamps, the frame's pts is the Unix
    time (in seconds) it was received, and -frame_pts writes that number into the file name."""
    need("ffmpeg")
    os.makedirs(a.outdir, exist_ok=True)
    started = time.time()
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-use_wallclock_as_timestamps", "1", "-copyts"]
    # No ffmpeg -t here: with epoch timestamps, ffmpeg's own duration limits stop almost immediately.
    # The time limit is enforced below instead, with a clean SIGINT so the last frames are flushed.
    cmd += ["-i", a.url, "-vf", "fps=1,scale=640:-2", "-q:v", "3", "-fps_mode", "passthrough",
            "-frame_pts", "1", "-f", "image2", os.path.join(a.outdir, "%d.jpg")]
    print("running:", " ".join(cmd))
    err_path = os.path.join(a.outdir, "ffmpeg_stderr.txt")
    with open(err_path, "w") as err:
        # stdin pipe so we can stop ffmpeg cleanly by sending "q" (works on Windows, where SIGINT can't be sent)
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
        try:
            deadline = time.time() + a.seconds if a.seconds else None
            while True:
                try:
                    proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if deadline and time.time() >= deadline:
                    raise subprocess.TimeoutExpired(cmd, a.seconds)
                if a.stop_file and os.path.exists(a.stop_file):
                    raise KeyboardInterrupt  # asked to stop (Kinodemo's Stop button)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
            except OSError:
                pass
            try:
                proc.wait(timeout=15)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                proc.kill()
    ended = time.time()
    with open(os.path.join(a.outdir, "capture_meta.json"), "w") as f:
        json.dump({"url": a.url, "started": started, "ended": ended, "ffmpeg_returncode": proc.returncode}, f, indent=2)
    files = [x for x in os.listdir(a.outdir) if x.endswith(".jpg")]
    print(f"captured {len(files)} frames in {ended - started:.0f}s (ffmpeg exit {proc.returncode}); ffmpeg messages in {err_path}")


def cmd_index(a):
    need("ffmpeg")
    crop = None if a.no_autocrop else detect_crop(["-ss", "60", "-i", a.film], seconds_limit=120) \
        or detect_crop(["-i", a.film], seconds_limit=60)
    print("film crop:", crop or "none")
    t0 = time.time()
    hashes = hash_frames(["-i", a.film], a.fps, crop)
    times = np.arange(len(hashes)) / a.fps
    np.savez_compressed(a.out, hashes=hashes, times=times, fps=a.fps, source=a.film, crop=crop or "")
    print(f"indexed {len(hashes)} frames ({times[-1] / 60:.1f} min of film) in {time.time() - t0:.0f}s -> {a.out}")


def cmd_hashdir(a):
    need("ffmpeg")
    names = sorted((f for f in os.listdir(a.dir) if re.fullmatch(r"\d+\.jpg", f)), key=lambda f: int(f[:-4]))
    if not names:
        sys.exit("no <unix-seconds>.jpg files found")
    # One ffmpeg process over all jpgs: every file becomes exactly one frame. A concat list is used
    # instead of a glob pattern because many Windows ffmpeg builds don't support globbing.
    # The list is in numeric (= time) order.
    fd, list_path = tempfile.mkstemp(suffix=".txt", prefix="framelist_")
    with os.fdopen(fd, "w") as lf:
        for nm in names:
            full = os.path.abspath(os.path.join(a.dir, nm)).replace("\\", "/").replace("'", "'\\''")
            lf.write(f"file '{full}'\nduration 1\n")
    base_in = ["-f", "concat", "-safe", "0", "-i", list_path]
    try:
        crop = None if a.no_autocrop else detect_crop(base_in)
        print("capture crop:", crop or "none")
        hashes = hash_frames(base_in, None, crop, passthrough=True)
    finally:
        os.remove(list_path)
    if len(hashes) != len(names):
        print(f"warning: {len(names)} files but {len(hashes)} hashed frames; using the first {min(len(hashes), len(names))}")
    n = min(len(hashes), len(names))
    times = np.array([int(f[:-4]) for f in names[:n]], dtype=np.float64)
    np.savez_compressed(a.out, hashes=hashes[:n], times=times, fps=1, source=a.dir, crop=crop or "")
    print(f"hashed {n} captured frames -> {a.out}")


def cmd_hashframe(a):
    """Print the 256-bit dHash of a single frame of a file, as hex. Used by the clip guard:
    a clip stores the hash at its start so the app can warn if a supplied film is a different cut.

    Uses the same autocrop that index/capture use, otherwise the hash would be computed on the
    letterboxed frame and would never match the indexed one."""
    need("ffmpeg")
    # Mirror cmd_index's crop probes so the crop matches whatever indexing would have chosen.
    crop = None if getattr(a, "no_autocrop", False) else (
        detect_crop(["-ss", "60", "-i", a.file], seconds_limit=120)
        or detect_crop(["-i", a.file], seconds_limit=60)
    )
    hashes = hash_frames(["-ss", str(a.seconds), "-i", a.file, "-frames:v", "1"], None, crop)
    if len(hashes) == 0:
        sys.exit("could not read a frame there")
    print(hashes[0].tobytes().hex())


def match_frames(film, cap, exclude_s=10.0):
    """Return dict of arrays: best film time, best dist, second-best dist (from a different part of the film)."""
    fh, ft = film["hashes"], film["times"]
    ch = cap["hashes"]
    d = hamming_matrix(ch, fh)
    best_i = d.argmin(axis=1)
    best_d = d[np.arange(len(ch)), best_i].astype(np.int32)
    second = np.empty(len(ch), dtype=np.int32)
    for k in range(len(ch)):
        mask = np.abs(ft - ft[best_i[k]]) <= exclude_s
        row = d[k].astype(np.int32)
        row[mask] = 9999
        second[k] = row.min() if (~mask).any() else 9999
    return {"film_t": ft[best_i], "dist": best_d, "second": second}


def cmd_match(a):
    film = np.load(a.film_npz, allow_pickle=True)
    cap = np.load(a.cap_npz, allow_pickle=True)
    r = match_frames(film, cap)
    ct = cap["times"]
    n = len(ct)
    margin = np.minimum(r["second"], 999) - r["dist"]
    confident = (r["dist"] <= a.max_dist) & (margin >= a.min_margin)

    print(f"\ncaptured frames: {n} ({(ct[-1] - ct[0]) / 60:.1f} min) vs film: {len(film['times'])} frames "
          f"({film['times'][-1] / 60:.1f} min)")
    print(f"thresholds: distance <= {a.max_dist} of 256 bits, margin >= {a.min_margin}")
    print(f"median best distance: {np.median(r['dist']):.0f} / 256")
    print(f"confident matches: {confident.sum()} / {n} ({100 * confident.mean():.0f}%)")

    if confident.sum() >= 3:
        # Offset = film position minus seconds since the first captured frame. A constant offset means
        # the film played at normal speed; each stretch of constant offset is one "plateau".
        rel = ct - ct[0]
        idx_c = np.flatnonzero(confident)
        off = r["film_t"][idx_c] - rel[idx_c]
        plateaus, start = [], 0
        for j in range(1, len(idx_c) + 1):
            if j == len(idx_c) or abs(off[j] - np.median(off[start:j])) > 2.0:
                if j - start >= 5:
                    plateaus.append((rel[idx_c[start]], rel[idx_c[j - 1]], float(np.median(off[start:j])), j - start))
                start = j
        print(f"stretches where film plays in step with the clock: {len(plateaus)}")
        for t0, t1, o, cnt in plateaus:
            print(f"  capture +{t0:5.0f}s to +{t1:5.0f}s: film position = capture time {o:+.1f}s  ({cnt} matched frames)")
    else:
        print("too few confident matches. Wrong film, very different cut/crop, or the thresholds are too strict.")

    # events: pauses (film time stays put while the clock advances) and jumps
    idx = np.flatnonzero(confident)
    events = []
    run_start = None
    for j in range(1, len(idx)):
        i0, i1 = idx[j - 1], idx[j]
        dc, df = ct[i1] - ct[i0], r["film_t"][i1] - r["film_t"][i0]
        if dc <= 3 and abs(df) < 0.75:
            if run_start is None:
                run_start = i0
        else:
            if run_start is not None:
                events.append(("pause/freeze", ct[run_start], ct[i0], r["film_t"][i0]))
                run_start = None
            if abs(df - dc) > 3 and dc <= 3:
                events.append(("jump", ct[i0], ct[i1], df - dc))
    if run_start is not None:
        events.append(("pause/freeze", ct[run_start], ct[idx[-1]], r["film_t"][idx[-1]]))
    long_events = [e for e in events if e[0] == "jump" or (e[2] - e[1]) >= 3]
    print(f"\ndetected events: {len(long_events)}")
    t_start = ct[0]
    for kind, t0, t1, extra in long_events:
        if kind == "jump":
            print(f"  jump        at +{t0 - t_start:6.0f}s  film moved {extra:+.0f}s relative to the clock")
        else:
            print(f"  pause/freeze +{t0 - t_start:5.0f}s to +{t1 - t_start:5.0f}s ({t1 - t0:.0f}s) at film {extra:.0f}s")

    if a.csv:
        with open(a.csv, "w") as f:
            f.write("capture_unix,film_seconds,distance,second_best,confident\n")
            for k in range(n):
                f.write(f"{ct[k]:.0f},{r['film_t'][k]:.2f},{r['dist'][k]},{r['second'][k]},{int(confident[k])}\n")
        print(f"\nper-frame results -> {a.csv}")
    return r, confident


# ----------------------------------------------------------------------------
# selftest: synthetic film + degraded stream with letterbox, overlay, compression, freeze
# ----------------------------------------------------------------------------

def ff(*args):
    p = run(["ffmpeg", "-hide_banner", "-v", "error", "-y", *args])
    if p.returncode != 0:
        sys.exit("ffmpeg failed:\n" + p.stderr.decode("utf-8", "replace")[-2000:])


def cmd_selftest(a):
    need("ffmpeg")
    work = tempfile.mkdtemp(prefix="kd_selftest_")
    print("workdir:", work)
    film = os.path.join(work, "film.mp4")
    wrong = os.path.join(work, "wrong.mp4")
    stream = os.path.join(work, "stream.mp4")
    capdir = os.path.join(work, "cap")
    os.makedirs(capdir)

    # Small and low-rate on purpose: lavfi's mandelbrot source is very slow at larger sizes.
    enc = ["-c:v", "libx264", "-crf", "26", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    print("1/6 generating synthetic 'film' (2.5 min) and an unrelated 'wrong film'")
    ff("-f", "lavfi", "-i", "mandelbrot=size=320x180:rate=10:maxiter=200", "-t", "150", *enc, film)
    ff("-f", "lavfi", "-i", "mandelbrot=size=320x180:rate=10:maxiter=200:start_x=-0.74:start_y=0.13:start_scale=0.5:end_scale=0.01",
       "-t", "150", *enc, wrong)

    print("2/6 building degraded 'stream' (letterbox, overlay, heavy compression, 10s freeze at film 70s)")
    # film 40-70s, then frame at 70s frozen for 10s (loop 100 frames at 10 fps), then film 70-120s
    graph = (
        "[0:v]trim=start=40:end=70,setpts=PTS-STARTPTS[a];"
        "[0:v]trim=start=70:end=70.1,setpts=PTS-STARTPTS,loop=loop=99:size=1:start=0[b];"
        "[0:v]trim=start=70:end=120,setpts=PTS-STARTPTS[c];"
        "[a][b][c]concat=n=3:v=1:a=0,"
        "scale=240:136,pad=240:180:0:22:black,"
        "drawbox=x=150:y=150:w=85:h=25:color=black@0.7:t=fill,"
        "format=yuv420p[out]"
    )
    ff("-i", film, "-filter_complex", graph, "-map", "[out]", "-c:v", "libx264", "-crf", "34", "-preset", "veryfast", stream)

    print("3/6 extracting 1 frame/s as jpg named by fake Unix time (like the real capture would)")
    base = 1_800_000_000
    ff("-i", stream, "-vf", "fps=1", "-q:v", "6", "-start_number", str(base), os.path.join(capdir, "%d.jpg"))

    print("4/6 indexing film")
    film_npz, wrong_npz, cap_npz = (os.path.join(work, x) for x in ("film.npz", "wrong.npz", "cap.npz"))
    cmd_index(argparse.Namespace(film=film, out=film_npz, fps=2.0, no_autocrop=False))
    cmd_index(argparse.Namespace(film=wrong, out=wrong_npz, fps=2.0, no_autocrop=False))
    print("5/6 hashing captured frames")
    cmd_hashdir(argparse.Namespace(dir=capdir, out=cap_npz, no_autocrop=False))

    print("\n6/6 MATCH against the RIGHT film (expect: ~100% confident, film = capture +40s, then a 10s freeze "
          "at film 70s, then film = capture +30s)")
    right_r, right_c = cmd_match(argparse.Namespace(film_npz=film_npz, cap_npz=cap_npz, csv=None,
                                                    max_dist=a.max_dist, min_margin=a.min_margin))
    print("\n--- MATCH against the WRONG film (expect: few or no confident matches) ---")
    wrong_r, wrong_c = cmd_match(argparse.Namespace(film_npz=wrong_npz, cap_npz=cap_npz, csv=None,
                                                    max_dist=a.max_dist, min_margin=a.min_margin))
    print(f"\nselftest files kept in {work}")
    ok = right_c.mean() > 0.8 and wrong_c.mean() < 0.2
    print("SELFTEST", "PASSED" if ok else "NEEDS ATTENTION (see numbers above)")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe"); p.add_argument("url"); p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("capture"); p.add_argument("url"); p.add_argument("outdir")
    p.add_argument("--seconds", type=int, default=0, help="stop after N seconds (default: run until the stream ends or Ctrl+C)")
    p.add_argument("--stop-file", default="", help="stop cleanly when this file appears")
    p.set_defaults(fn=cmd_capture)

    p = sub.add_parser("index"); p.add_argument("film"); p.add_argument("out")
    p.add_argument("--fps", type=float, default=2.0); p.add_argument("--no-autocrop", action="store_true")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("hashdir"); p.add_argument("dir"); p.add_argument("out")
    p.add_argument("--no-autocrop", action="store_true"); p.set_defaults(fn=cmd_hashdir)

    p = sub.add_parser("hashframe"); p.add_argument("file"); p.add_argument("seconds", type=float)
    p.add_argument("--no-autocrop", action="store_true"); p.set_defaults(fn=cmd_hashframe)

    for name, fn in (("match", cmd_match), ("selftest", cmd_selftest)):
        p = sub.add_parser(name)
        if name == "match":
            p.add_argument("film_npz"); p.add_argument("cap_npz"); p.add_argument("--csv")
        p.add_argument("--max-dist", type=int, default=64, help="max Hamming distance (of 256) to count as a match")
        p.add_argument("--min-margin", type=int, default=12, help="best must beat the next-best (elsewhere) by this much")
        p.set_defaults(fn=fn)

    a = ap.parse_args()
    rc = a.fn(a)
    sys.exit(rc if isinstance(rc, int) else 0)


if __name__ == "__main__":
    main()