# Kinodemo

Record a CyTube showing, match it to a local copy of the film, and play the
recording back with the chat replayed in sync — pauses, skips and rewinds
included.

Kinodemo runs entirely on your own computer. It opens a small local web app at
`http://127.0.0.1:8765` and never talks to anything except the CyTube channel
you point it at and your own files.

> **Please ask the channel's admins before recording.** Kinodemo is a tool for
> making personal archives of showings you're already watching, not for
> republishing someone else's stream.

## What it does

1. **Records** a showing: it logs the chat and captures one low-resolution frame
   every few seconds. That's enough to find where in the film the recording
   started, and it keeps the recording small.
2. **Matches** the recording against a film file you already have. Kinodemo
   reads the film once (about a minute per hour), then reuses that index for
   future recordings of the same film.
3. **Plays back** the film with the chat scrolling alongside it. Because the
   chat is anchored to film positions rather than wall-clock times, a pause or
   a skip during the original showing doesn't push the rest of the chat out of
   step.
4. **Clips** let you cut a moment out of a recording — a film-time range plus
   the chat that falls in it — and export it as a small `.clip` file. The clip
   remembers enough about the film to warn you if you point it at a different
   cut.

## Requirements

- **Python 3.9+** with the standard library only (no `pip install` needed for
  the server).
- **ffmpeg** and **ffprobe** on your `PATH` — used to read streams and films.
- **Node.js 18+** — used for the chat logger and the browser-based frame
  capture.
- **Google Chrome or Microsoft Edge** installed normally (not a portable
  build). Kinodemo uses `playwright-core` to drive it, but doesn't download
  its own browser.
- **numpy** — only needed by `framematch.py`. Install with `pip install numpy`
  if it isn't already present.

## Install

### Windows

Download the repo and double-click `start-kinodemo.bat`. It checks for Python
and Node, runs `npm install` the first time, and launches the app.

### Anywhere else

```sh
git clone https://github.com/<you>/kinodemo.git
cd kinodemo
npm install
pip install numpy
python3 kinodemo.py