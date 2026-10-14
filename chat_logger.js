#!/usr/bin/env node
/*
 * Minimal CyTube chat logger: records chat messages only.
 *
 * Usage:
 *   npm install
 *   node chat_logger.js --host <site-domain> --channel <channel> [--out <file>] [--name demo-recorder] [--password pw]
 *
 * Output: recordings/<channel>_<YYYY-MM-DD_HH-MM-SS>.jsonl (override with --out).
 * One JSON object per line:
 *   { t: <local receive time, unix ms>, time: <server timestamp, unix ms>, username, msg }
 *
 * Emotes are not collected here. Kinodemo reads them from the central emotes/ folder instead;
 * drop an export file there (see kinodemo.py).
 *
 * Please ask the channel admins before recording.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const { io } = require('socket.io-client');

// --- args ---
const args = {};
for (let i = 2; i < process.argv.length; i++) {
  if (process.argv[i].startsWith('--')) args[process.argv[i].slice(2)] = process.argv[++i];
}
const { host, channel } = args;
const guestName = args.name || 'demo-recorder';
const password = args.password;

if (!host || !channel) {
  console.error('Usage: node chat_logger.js --host <site-domain> --channel <channel> [--out <file>]');
  process.exit(2);
}

// Default: recordings/<channel>_<YYYY-MM-DD_HH-MM-SS>.jsonl (local time)
const pad = n => String(n).padStart(2, '0');
const d = new Date();
const stamp = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}_${pad(d.getHours())}-${pad(d.getMinutes())}-${pad(d.getSeconds())}`;
const outFile = args.out || path.join('recordings', `${channel}_${stamp}.jsonl`);
fs.mkdirSync(path.dirname(outFile), { recursive: true });

let count = 0;
let connectedAt = 0;   // when the socket last (re)connected
const BACKLOG_MS = 1500; // chat history arrives in a burst right after joining

async function main() {
  // Ask the site which socket server this channel uses
  const res = await fetch(`https://${host}/socketconfig/${channel}.json`);
  if (!res.ok) throw new Error(`socketconfig request failed: HTTP ${res.status}`);
  const config = await res.json();

  const servers = (config.servers || []).filter(s => !s.ipv6Only);
  const server = servers.find(s => s.secure) || servers[0];
  if (!server) throw new Error('No usable socket server found');

  const socket = io(server.url, { transports: ['websocket'], reconnection: true });

  // Runs on first connect and every reconnect
  socket.on('connect', () => {
    connectedAt = Date.now();
    console.log('Connected, joining channel...');
    socket.emit('joinChannel', { name: channel });
    if (password) socket.emit('channelPassword', password);
    socket.emit('login', { name: guestName });
  });

  socket.on('chatMsg', data => {
    const now = Date.now();
    const line = { t: now, time: data.time, username: data.username, msg: data.msg };
    // History CyTube sends on join is not live chat; flag it so replays can skip it.
    if (now - connectedAt < BACKLOG_MS) line.backlog = true;
    fs.appendFileSync(outFile, JSON.stringify(line) + '\n');
    count++;
    console.log(`${data.username}: ${data.msg}`);
  });

  socket.on('needPassword', () => console.error('Channel needs a password: use --password'));
  socket.on('connect_error', err => console.error(`Connection error: ${err.message}`));
  socket.on('disconnect', reason => console.log(`Disconnected: ${reason}`));

  process.on('SIGINT', () => {
    console.log(`\n${count} messages written to ${outFile}`);
    process.exit(0);
  });
}

main().catch(err => {
  console.error(err.message);
  process.exit(1);
});