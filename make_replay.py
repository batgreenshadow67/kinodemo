#!/usr/bin/env python3
"""Build replay data (per-message film positions + notes) from a match CSV and a chat log.

Used two ways:
  * imported by kinodemo.py:  make_replay.build_replay(csv_path, chat_path, film_label)
  * from the command line, writing replay_data.js:
        python make_replay.py --csv matches.csv --chat recordings\\<session>\\chat.jsonl

How the sync works
------------------
1. The CSV maps capture time (unix seconds) to film position. Confident matches that disagree with
   everything around them (lone wrong matches) are thrown out.
2. The remaining matches form a "film clock": for any moment during the recording it says where the
   film was. Between two matches the clock is interpolated, so a pause holds the film position still
   and a skip or rewind moves it. Where a skip happened between two matches, each side is extended
   at normal speed up to the midpoint.
3. Each chat message is placed at the film position of the moment it was written (field "f").
   The player shows a message once the film reaches "f", so pauses, skips and rewinds no longer
   push the rest of the chat out of step.

Messages CyTube sends as history when the logger joins ("backlog") were written before the
recording started, so they have no captured frame to match against. They are left out. Messages
from the first few seconds of the recording are also left out, so playback doesn't open with a
burst of chat.

Chat times: each logged line has the local receive time ("t") and CyTube's own timestamp ("time").
The difference between them is the clock difference plus network delay. Its low end (10th percentile)
gives the clock difference, and server time + that difference is when the message was written, on
this computer's clock.
"""

import argparse
import bisect
import csv
import json
import statistics

TOL = 1.5          # seconds of slack when judging whether two matches agree
MIN_SEG = 3        # a run of agreeing matches shorter than this, between disagreements, is an outlier
PAUSE_MIN = 3.0    # film standing still at least this long counts as a pause
FAR = 15.0         # a message this far from any confident match is flagged as an estimate
OPEN_SKIP = 0.5    # drop chat from the first half-second of the recording


# ---------------------------------------------------------------------------------------------
# film clock
# ---------------------------------------------------------------------------------------------

def _consistent(a, b):
    """Can film position b follow a (both (capture_s, film_s), a earlier)?
    Yes if the film moved forward by between 0 and the elapsed time: playing, paused, or a pause
    then play. A jump forward or any step back is not consistent."""
    d_cap, d_film = b[0] - a[0], b[1] - a[1]
    return -TOL <= d_film <= d_cap + TOL


def _drop_outliers(pts):
    """pts sorted by capture time. Split wherever neighbours disagree; short stretches between
    disagreements are outliers. Returns (kept, number_dropped)."""
    segs, start = [], 0
    for i in range(1, len(pts) + 1):
        if i == len(pts) or not _consistent(pts[i - 1], pts[i]):
            segs.append((start, i))
            start = i
    good = [s for s in segs if s[1] - s[0] >= MIN_SEG]
    if not good:
        good = [max(segs, key=lambda s: s[1] - s[0])]
    kept = [p for s, e in good for p in pts[s:e]]
    return kept, len(pts) - len(kept)


def _find_pauses(pts, min_len=PAUSE_MIN):
    """Stretches where the film position stays put while the clock runs: [(cap_start, cap_end)]."""
    out, n, s = [], len(pts), 0
    while s < n:
        e = s
        while e + 1 < n and abs(pts[e + 1][1] - pts[s][1]) <= 0.75:
            e += 1
        if e > s and pts[e][0] - pts[s][0] >= min_len:
            out.append((pts[s][0], pts[e][0]))
            s = e + 1
        else:
            s += 1
    return out


def _find_jumps(pts):
    """Places where the film position jumped: [(cap_time, film_moved_relative_to_clock)]."""
    out = []
    for a, b in zip(pts, pts[1:]):
        if not _consistent(a, b):
            out.append((b[0], (b[1] - a[1]) - (b[0] - a[0])))
    return out


class FilmClock:
    def __init__(self, pts):
        self.c = [p[0] for p in pts]
        self.f = [p[1] for p in pts]

    def at(self, t):
        """(film position, distance in seconds to the nearest match) at capture time t."""
        c, f = self.c, self.f
        if t <= c[0]:
            return f[0] - (c[0] - t), c[0] - t
        if t >= c[-1]:
            return f[-1] + (t - c[-1]), t - c[-1]
        i = bisect.bisect_right(c, t)            # c[i-1] <= t < c[i]
        a, b = (c[i - 1], f[i - 1]), (c[i], f[i])
        if _consistent(a, b):
            span = b[0] - a[0]
            frac = (t - a[0]) / span if span else 0.0
            return a[1] + frac * (b[1] - a[1]), min(t - a[0], b[0] - t)
        # a skip happened somewhere in this gap: extend each side at normal speed to the midpoint
        if t - a[0] <= b[0] - t:
            return a[1] + (t - a[0]), t - a[0]
        return b[1] - (b[0] - t), b[0] - t


# ---------------------------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------------------------

def _drop_bursts(msgs, min_run=5, max_gap=0.05):
    """Fallback for logs without server times: remove runs of >= min_run messages whose
    consecutive gaps are <= max_gap seconds (join history arrives in one burst)."""
    keep, dropped, i = [], 0, 0
    while i < len(msgs):
        j = i
        while j + 1 < len(msgs) and msgs[j + 1]["t"] - msgs[j]["t"] <= max_gap:
            j += 1
        if j - i + 1 >= min_run:
            dropped += j - i + 1
        else:
            keep.extend(msgs[i:j + 1])
        i = j + 1
    return keep, dropped


def _read_chat(chat_path):
    rows, unreadable = [], 0
    with open(chat_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                recv = float(d["t"]) / 1000.0
            except (ValueError, KeyError, TypeError):
                unreadable += 1
                continue
            srv = None
            try:
                if d.get("time") is not None:
                    srv = float(d["time"]) / 1000.0
            except (ValueError, TypeError):
                srv = None
            rows.append({"recv": recv, "srv": srv, "backlog": bool(d.get("backlog")),
                         "u": d.get("username", ""), "m": d.get("msg", "")})
    return rows, unreadable


def _server_clock(rows):
    """Estimate (server clock -> this computer's clock) from live messages.
    Returns (delta, spread, n) or (None, 0, n) if there aren't enough live messages."""
    deltas = sorted(r["recv"] - r["srv"] for r in rows if r["srv"] is not None and not r["backlog"])
    n = len(deltas)
    if n < 2:
        return None, 0.0, n
    lo = deltas[int(n * 0.1)]
    hi = deltas[max(int(n * 0.9) - 1, 0)]
    return lo, hi - lo, n


# ---------------------------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------------------------

def build_replay(csv_path, chat_path, film_label="", slack=10.0, pause_chat="pile"):
    """Return a dict for the player. Raises ValueError if the matches can't give a film clock.

    pause_chat: what to do with chat written while the film was paused or frozen.
        "pile" (default) shows it all at the paused frame; "drop" leaves it out."""
    notes = []
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError("The match results are empty.")
    conf = sorted((float(r["capture_unix"]), float(r["film_seconds"])) for r in rows if r["confident"] == "1")
    if len(conf) < 3:
        raise ValueError("Fewer than 3 frames matched confidently, so the timing can't be worked out.")

    cap_start = min(float(r["capture_unix"]) for r in rows)
    cap_end = max(float(r["capture_unix"]) for r in rows)

    pts, outliers = _drop_outliers(conf)
    if len(pts) < 3:
        raise ValueError("Too few consistent matches to work out the timing.")
    clock = FilmClock(pts)
    pauses = _find_pauses(pts)
    jumps = _find_jumps(pts)
    offset = statistics.median(p[1] - p[0] for p in pts)       # kept for older consumers
    start_rel = clock.at(cap_start)[0]

    notes.append(f"Recording starts {int(max(start_rel, 0) // 60)}:{int(max(start_rel, 0) % 60):02d} into the film.")
    notes.append(f"Used {len(pts)} of {len(conf)} confident matches"
                 + (f" ({outliers} thrown out as lone wrong matches)." if outliers else "."))
    events = []
    for a, b in pauses:
        events.append({"kind": "pause", "at": round(a - cap_start, 1), "seconds": round(b - a, 1)})
        notes.append(f"Film paused or frozen at +{a - cap_start:.0f}s for {b - a:.0f}s.")
    for t, moved in jumps:
        events.append({"kind": "jump", "at": round(t - cap_start, 1), "seconds": round(moved, 1)})
        notes.append(f"Film jumped {moved:+.0f}s at +{t - cap_start:.0f}s ({'skipped ahead' if moved > 0 else 'went back'}).")
    events.sort(key=lambda e: e["at"])
    if events:
        notes.append("Chat timing was corrected for these, message by message.")

    # ---- chat ----
    raw, unreadable = _read_chat(chat_path)
    delta, spread, nlive = _server_clock(raw)
    use_server = delta is not None

    msgs, flagged, dups, bursts = [], 0, 0, 0
    if use_server:
        seen = set()
        for r in raw:
            if r["srv"] is None:
                if r["backlog"]:
                    flagged += 1
                    continue
                t = r["recv"]
            else:
                key = (round(r["srv"] * 1000), r["u"], r["m"])
                if key in seen:
                    dups += 1
                    continue
                seen.add(key)
                t = r["srv"] + delta
            # messages CyTube sends as join history were written before the recording; leave them out
            if r["backlog"]:
                flagged += 1
                continue
            msgs.append({"t": t, "u": r["u"], "m": r["m"]})
        notes.append(f"Chat placed using CyTube's own message times ({nlive} live messages gave the clock difference).")
        if spread > 2.0:
            notes.append(f"Message delivery delay varied by {spread:.1f}s, so chat timing may be loose.")
    else:
        for r in raw:
            if r["backlog"]:
                flagged += 1
                continue
            msgs.append({"t": r["recv"], "u": r["u"], "m": r["m"]})
        msgs.sort(key=lambda m: m["t"])
        msgs, bursts = _drop_bursts(msgs)
        notes.append("Chat placed using receive times (the log has too few server timestamps to use them).")

    msgs = [m for m in msgs if cap_start - slack <= m["t"] <= cap_end + slack]
    msgs.sort(key=lambda m: m["t"])

    out, dropped_pause, dropped_open, far = [], 0, 0, 0
    for m in msgs:
        t = m["t"]
        if pause_chat == "drop" and any(a <= t <= b for a, b in pauses):
            dropped_pause += 1
            continue
        film, dist = clock.at(t)
        if film < start_rel + OPEN_SKIP:
            dropped_open += 1            # chat from the opening seconds; dropped so the replay starts clean
            continue
        if dist > FAR:
            far += 1
        out.append({"t": round(t, 3), "f": round(film, 3), "u": m["u"], "m": m["m"]})
    out.sort(key=lambda m: (m["f"], m["t"]))

    if unreadable:
        notes.append(f"Skipped {unreadable} unreadable chat lines.")
    if dups:
        notes.append(f"Removed {dups} duplicate messages (resent as history after a reconnect).")
    if flagged or bursts:
        notes.append(f"Left out {flagged + bursts} messages that were join history from before the recording.")
    if dropped_open:
        notes.append(f"Left out {dropped_open} messages from the first {OPEN_SKIP:.0f}s of the recording.")
    if dropped_pause:
        notes.append(f"Left out {dropped_pause} messages written while the film was paused.")
    elif pauses and pause_chat == "pile":
        n_in = sum(1 for m in msgs if any(a <= m["t"] <= b for a, b in pauses))
        if n_in:
            notes.append(f"{n_in} messages were written during pauses and will appear together at the paused frame.")
    if far:
        notes.append(f"{far} messages are more than {FAR:.0f}s from any confident match, so their timing is an estimate.")
    notes.append(f"{len(out)} chat messages in the replay.")
    if not out:
        notes.append("No chat falls inside the recorded window. Is the chat log from the same session?")

    return {"offset": offset, "offsetRel": start_rel, "capStart": cap_start, "capEnd": cap_end,
            "film": film_label, "messages": out, "notes": notes,
            "drift": bool(events), "events": events}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="CSV written by: framematch.py match ... --csv")
    ap.add_argument("--chat", required=True, help="chat.jsonl written by chat_logger.js / record.py")
    ap.add_argument("--out", default="replay_data.js")
    ap.add_argument("--film", default="", help="label shown in the page (default: none)")
    ap.add_argument("--slack", type=float, default=10.0,
                    help="keep chat from this many seconds before/after the captured frames (default 10)")
    ap.add_argument("--pause-chat", choices=["pile", "drop"], default="pile",
                    help="chat written while the film was paused: pile = show at the paused frame, drop = leave out")
    a = ap.parse_args()
    try:
        data = build_replay(a.csv, a.chat, a.film, a.slack, a.pause_chat)
    except ValueError as e:
        raise SystemExit(str(e))
    for n in data["notes"]:
        print(n)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("window.REPLAY = " + json.dumps(data, ensure_ascii=False) + ";\n")
    print(f"wrote {os.path.abspath(a.out)}")


if __name__ == "__main__":
    main()