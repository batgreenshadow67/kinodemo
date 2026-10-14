#!/usr/bin/env node
/*
 * Capture one frame every few seconds the way a viewer sees the stream: open the player page in a
 * real browser and copy the <video> element's current picture. Frames are saved as
 * <unix-seconds>.jpg (480 px wide, grayscale, lower JPEG quality), the same names framematch.py's
 * capture uses, so matching works unchanged.
 *
 * Usage:
 *   node capture_browser.js --page <player page url> --out <frames dir> [--seconds N]
 *                           [--stop-file <path>] [--show] [--chrome-path <chrome.exe>]
 *
 * Needs Google Chrome or Microsoft Edge installed (a regular install, not a bundled browser,
 * so that H.264 video plays). Exit codes: 0 ok, 3 couldn't start the browser or find the video.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright-core');

const args = {};
for (let i = 2; i < process.argv.length; i++) {
  const a = process.argv[i];
  if (!a.startsWith('--')) continue;
  const k = a.slice(2);
  args[k] = (k === 'show') ? true : process.argv[++i];
}
if (!args.page || !args.out) {
  console.error('Usage: node capture_browser.js --page <url> --out <dir> [--seconds N] [--stop-file F] [--show]');
  process.exit(2);
}
const seconds = Number(args.seconds || 0);
fs.mkdirSync(args.out, { recursive: true });

let stopping = false;
process.on('SIGINT', () => { stopping = true; });
process.on('SIGTERM', () => { stopping = true; });

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function launch() {
  const base = {
    headless: !args.show,
    args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio', '--window-size=1280,760'],
  };
  const tries = args['chrome-path']
    ? [{ ...base, executablePath: args['chrome-path'] }]
    : [{ ...base, channel: 'chrome' }, { ...base, channel: 'msedge' }];
  let lastErr;
  for (const opts of tries) {
    try { return await chromium.launch(opts); } catch (e) { lastErr = e; }
  }
  throw new Error('Could not start Chrome or Edge. Install Google Chrome, or pass --chrome-path.\n' +
                  String(lastErr && lastErr.message).split('\n')[0]);
}

// Find the frame that holds the <video> (it may be nested in an iframe).
async function findVideoFrame(page) {
  for (const f of page.frames()) {
    try { if (await f.$('video')) return f; } catch (e) { /* frame navigating */ }
  }
  return null;
}

async function main() {
  const browser = await launch();
  const started = Date.now() / 1000;
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
  console.log('Opening', args.page);
  await page.goto(args.page, { waitUntil: 'domcontentloaded', timeout: 45000 });

  // Wait for a playing video.
  let frame = null;
  const deadline = Date.now() + 60000;
  let clicked = false;
  while (Date.now() < deadline && !stopping) {
    frame = await findVideoFrame(page);
    if (frame) {
      const st = await frame.evaluate(() => {
        const v = document.querySelector('video');
        v.muted = true;
        if (v.paused) v.play().catch(() => {});
        return { ready: v.readyState, paused: v.paused, w: v.videoWidth, h: v.videoHeight, t: v.currentTime };
      }).catch(() => null);
      if (st && st.ready >= 2 && st.w > 0 && !st.paused) break;
      if (st && !clicked && Date.now() > deadline - 50000) {   // after ~10 s, try a click like a viewer would
        clicked = true;
        await page.mouse.click(640, 360).catch(() => {});
      }
    }
    frame = null;
    await sleep(500);
  }
  if (!frame) {
    const kinds = page.frames().map(f => f.url()).join('\n  ');
    console.error('No playing video found on the page within 60 s. Frames seen:\n  ' + kinds);
    await browser.close();
    process.exit(3);
  }
  console.log('Video is playing. Capturing a frame every 3 seconds.');

  const INTERVAL = 3;    // seconds between captured frames
  let saved = 0, useScreenshot = false, lastAt = 0;
  while (!stopping) {
    if (seconds && Date.now() / 1000 - started >= seconds) break;
    if (args['stop-file'] && fs.existsSync(args['stop-file'])) break;
    // wait until the next interval boundary so frame names stay evenly spaced
    const nowMs = Date.now();
    const wait = INTERVAL * 1000 - (nowMs % (INTERVAL * 1000)) + 5;
    await sleep(wait);
    let t, buf;
    try {
      if (!useScreenshot) {
        const r = await frame.evaluate(() => {
          const v = document.querySelector('video');
          if (!v || !v.videoWidth) return null;
          v.muted = true; v.volume = 0;   // keep it silent even if the player tries to unmute
          const w = 480, h = Math.round(w * v.videoHeight / v.videoWidth / 2) * 2;
          const c = document.createElement('canvas'); c.width = w; c.height = h;
          const ctx = c.getContext('2d');
          ctx.drawImage(v, 0, 0, w, h);
          // convert to grayscale so the JPEG is smaller; the matcher works on luminance anyway
          try {
            const img = ctx.getImageData(0, 0, w, h);
            const d = img.data;
            for (let i = 0; i < d.length; i += 4) {
              const y = (d[i] * 0.299 + d[i + 1] * 0.587 + d[i + 2] * 0.114) | 0;
              d[i] = d[i + 1] = d[i + 2] = y;
            }
            ctx.putImageData(img, 0, 0);
          } catch (e) { /* canvas read blocked; keep the colour frame */ }
          return { t: Date.now(), d: c.toDataURL('image/jpeg', 0.72) };   // throws if the canvas is tainted
        });
        if (r) { t = r.t; buf = Buffer.from(r.d.split(',')[1], 'base64'); }
      } else {
        await frame.evaluate(() => { const v = document.querySelector('video'); if (v) { v.muted = true; v.volume = 0; } }).catch(() => {});
        t = Date.now();
        buf = await frame.locator('video').first().screenshot({ type: 'jpeg', quality: 72 });
      }
    } catch (e) {
      if (!useScreenshot) {
        console.log('Reading the video directly was blocked; using screenshots instead.');
        useScreenshot = true;
      } else {
        console.error('Frame grab failed:', String(e.message).split('\n')[0]);
        // the player may have reloaded; look for the video again
        frame = (await findVideoFrame(page)) || frame;
      }
      continue;
    }
    if (!buf) continue;
    const sec = Math.floor(t / 1000);
    // one frame per INTERVAL seconds; skip if we somehow land in the same slot
    if (sec - lastAt < INTERVAL) continue;
    lastAt = sec;
    fs.writeFileSync(path.join(args.out, sec + '.jpg'), buf);
    if (++saved % 10 === 0) console.log(`${saved} frames captured`);
  }

  const ended = Date.now() / 1000;
  fs.writeFileSync(path.join(args.out, 'capture_meta.json'),
    JSON.stringify({ page: args.page, started, ended, mode: useScreenshot ? 'browser-screenshot' : 'browser-canvas' }, null, 2));
  console.log(`captured ${saved} frames in ${Math.round(ended - started)}s`);
  await browser.close();
}

main().catch(err => { console.error(err.message); process.exit(3); });