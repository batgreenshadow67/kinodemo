#!/usr/bin/env node
/*
 * Fetch a CyTube channel's emote list the way a viewer sees it: open the channel page in a real
 * browser, wait for the CyTube client to finish loading, then read CHANNEL.emotes.
 *
 * This is the same thing you'd get by pasting this in the channel's console:
 *   JSON.stringify(CHANNEL.emotes.map(f => ({ name: f.name, image: f.image })))
 *
 * Some channels have no emotes in their socketconfig at all (they're added by a bot, or pulled in
 * from elsewhere), so the live client is the only reliable source.
 *
 * Usage:
 *   node fetch_emotes.js --host <site-domain> --channel <channel> --out <file> [--show] [--chrome-path <chrome.exe>]
 *
 * Exit codes: 0 ok, 3 couldn't start the browser, 4 the page loaded but no emotes appeared.
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
if (!args.host || !args.channel || !args.out) {
  console.error('Usage: node fetch_emotes.js --host <domain> --channel <channel> --out <file> [--show]');
  process.exit(2);
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function launch() {
  const base = { headless: !args.show, args: ['--mute-audio', '--window-size=1280,760'] };
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

async function main() {
  const browser = await launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
  const url = `https://${args.host}/r/${encodeURIComponent(args.channel)}`;
  console.log('Opening', url);
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });

  // Wait for the CyTube client to populate CHANNEL.emotes. Some channels have none at all, so we
  // also stop early once the client is clearly up and the list is still empty after a grace period.
  let emotes = null, clientSeen = false;
  const deadline = Date.now() + 45000;
  while (Date.now() < deadline) {
    const st = await page.evaluate(() => {
      try {
        if (typeof CHANNEL === 'undefined') return { ready: false };
        const list = Array.isArray(CHANNEL.emotes) ? CHANNEL.emotes : [];
        return {
          ready: true,
          emotes: list.map(f => ({ name: f.name, image: f.image })),
        };
      } catch (e) { return { ready: false }; }
    }).catch(() => ({ ready: false }));
    if (st.ready) {
      if (!clientSeen) { clientSeen = true; console.log('CyTube client loaded.'); }
      if (st.emotes.length) { emotes = st.emotes; break; }
    }
    await sleep(500);
  }

  if (!emotes) {
    // Distinguish "the client never loaded" from "it loaded but this channel has no emotes".
    console.error(clientSeen
      ? `The channel loaded but CHANNEL.emotes is empty. Either the channel has no emotes, or ` +
        `they're added after login. Try opening the page with --show and checking manually.`
      : `The CyTube client never loaded on ${url}. Check the channel name, or open it manually with --show.`);
    await browser.close();
    process.exit(clientSeen ? 4 : 3);
  }

  // De-duplicate by name, keeping the first definition. Protocol-relative image URLs are kept as-is
  // (the player upgrades them when rendering).
  const seen = new Set(), out = [];
  for (const e of emotes) {
    if (!e || !e.name || !e.image || seen.has(e.name)) continue;
    seen.add(e.name);
    out.push({ name: e.name, image: e.image });
  }

  fs.mkdirSync(path.dirname(args.out), { recursive: true });
  fs.writeFileSync(args.out, JSON.stringify(
    { channel: args.channel, fetched: Date.now(), source: 'browser', emotes: out }, null, 2));

  console.log(`Saved ${out.length} emotes for channel "${args.channel}" to ${args.out}`);
  await browser.close();
}

main().catch(err => { console.error(err.message); process.exit(3); });