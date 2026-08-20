#!/usr/bin/env node
// Desktop-STT settings probe — measures the two new tabs in a real browser.
//
// The transcripts list is the one thing here that GROWS. Chris asked the
// question that made this probe necessary: does a long history push the modal
// off the screen, or does it get squeezed into a box too short to read? Both
// failures are invisible in the CSS and invisible with an empty store — an
// unseeded history measures perfectly clean, which is exactly how a mobile bug
// ships. So the probe seeds forty long takes through a fetch stub and then
// measures geometry.
//
// Nothing is written: /api/desktop-stt and its history are stubbed, and the
// probe never POSTs.
//
//   node tools/desktop_stt_probe.mjs http://127.0.0.1:7799/
//
// Exits non-zero on the first failed assertion.
import { spawn } from "child_process";
import fs from "fs";
import os from "os";
import path from "path";

const HUB = process.argv[2] || "http://127.0.0.1:7799/";
const CDP_PORT = Number(process.env.PROBE_CDP_PORT || 9427);

const BROWSER = [
  process.env.PROBE_BROWSER,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
].filter(Boolean).find(p => { try { return fs.statSync(p).isFile(); } catch { return false; } });

if (!BROWSER) {
  console.error("no Chrome or Edge found. Set PROBE_BROWSER to its path.");
  process.exit(2);
}

const WIDTHS = [320, 360, 390, 412, 768, 1280];
const sleep = ms => new Promise(r => setTimeout(r, ms));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "hub-dstt-probe-"));
const proc = spawn(BROWSER, ["--headless=new", `--remote-debugging-port=${CDP_PORT}`,
  `--user-data-dir=${profile}`, "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--disable-gpu", "about:blank"], { stdio: "ignore" });

async function browserWs() {
  for (let i = 0; i < 80; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`);
      return (await r.json()).webSocketDebuggerUrl;
    } catch { await sleep(250); }
  }
  throw new Error("browser never opened its debug port");
}

class Cdp {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.waits = new Map(); this.sid = null;
    ws.onmessage = e => {
      const m = JSON.parse(e.data);
      if (m.id && this.waits.has(m.id)) {
        const { res, rej } = this.waits.get(m.id); this.waits.delete(m.id);
        m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
      }
    };
  }
  send(method, params = {}, sessionId = this.sid) {
    const id = ++this.id;
    return new Promise((res, rej) => {
      this.waits.set(id, { res, rej });
      this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }
}

const fails = [];
const check = (ok, what) => {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${what}`);
  if (!ok) fails.push(what);
};

// Open the REAL settings modal with a HEAVY history stubbed in.
const OPEN = String.raw`(async () => {
  document.querySelectorAll("#overlay").forEach(o => o.remove());
  const long = "I think this is going to have to involve picking like five " +
    "second chunks, because the timeline gets very long once you have more " +
    "than a few clips and I need a way to jump around without losing my " +
    "place in the footage while I am working on it.";
  const entries = Array.from({ length: 40 }, (_, i) => ({
    ts: "2026-08-20T09:" + String(10 + (i % 45)).padStart(2, "0") + ":00",
    seconds: 40, words: 46, wpm: 69, lost: i % 7 === 0 ? 1 : 0,
    target: "*Untitled - Notepad", text: (i % 3 ? long : "short one"),
  }));
  const real = window.fetch;
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.startsWith("/api/desktop-stt/history")) return Promise.resolve({ json: async () => ({
      entries, stats: { takes: 40, words: 1840, seconds: 1600, wpm: 69,
                        best_wpm: 121.4, recent_days: 7, recent_takes: 40,
                        recent_wpm: 69 } }) });
    if (s.startsWith("/api/desktop-stt")) return Promise.resolve({ json: async () => ({
      settings: { hotkey: "ctrl+alt+d", mode: "tap", cleanup: true, history: true },
      running: true, pid: 1, state: { hotkey: "ctrl+alt+d" },
      hotkey_live: "ctrl+alt+d", hotkey_registered: true, hotkey_method: "native",
      pending_restart: false }) });
    return real(u, o);
  };
  document.getElementById("gear").click();
  for (let i = 0; i < 60 && !document.querySelector("#modal .stabs button"); i++)
    await new Promise(r => setTimeout(r, 50));
  for (let i = 0; i < 60 && !document.querySelector("#dhist .dtake"); i++)
    await new Promise(r => setTimeout(r, 50));
  window.fetch = real;
  const m = document.querySelector("#modal");
  if (!m) return { error: "modal never rendered" };
  const box = el => { const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, w: r.width, h: r.height, right: r.right, bottom: r.bottom }; };
  const out = { tabs: [], viewportW: innerWidth, viewportH: innerHeight,
                docScrollW: document.documentElement.scrollWidth };
  const btns = [...m.querySelectorAll(".stabs button")];
  out.tabNames = btns.map(b => b.dataset.tab);
  const bar = m.querySelector(".stabs");
  out.barScrollW = bar.scrollWidth; out.barClientW = bar.clientWidth;
  out.btnBoxes = btns.map(b => box(b));
  for (const name of ["dictate", "takes"]) {
    btns.find(b => b.dataset.tab === name).click();
    await new Promise(r => setTimeout(r, 60));
    const panel = m.querySelector('.stab[data-tab="' + name + '"]');
    const rec = { name, panel: box(panel), modal: box(m),
                  modalScrollH: m.scrollHeight, modalClientH: m.clientHeight,
                  panelScrollW: panel.scrollWidth, panelClientW: panel.clientWidth };
    const hist = m.querySelector("#dhist");
    if (name === "takes") {
      rec.hist = box(hist);
      rec.histScrollH = hist.scrollHeight;
      rec.histClientH = hist.clientHeight;
      rec.takeCount = m.querySelectorAll("#dhist .dtake").length;
      const first = m.querySelector("#dhist .dtake");
      rec.firstTake = box(first);
      rec.stat = box(m.querySelector("#dstats"));
      rec.statScrollW = m.querySelector("#dstats").scrollWidth;
      rec.statClientW = m.querySelector("#dstats").clientWidth;
    }
    out.tabs.push(rec);
  }
  return out;
})()`;

(async () => {
  const ws = new WebSocket(await browserWs());
  await new Promise(r => ws.onopen = r);
  const cdp = new Cdp(ws);
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" }, null);
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true }, null);
  cdp.sid = sessionId;
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");

  for (const width of WIDTHS) {
    await cdp.send("Emulation.setDeviceMetricsOverride",
      { width, height: 780, deviceScaleFactor: 1, mobile: width < 700 });
    await cdp.send("Page.navigate", { url: HUB });
    await sleep(1700);
    console.log(`\n${width}px`);
    const { result } = await cdp.send("Runtime.evaluate",
      { expression: OPEN, awaitPromise: true, returnByValue: true });
    const r = result.value;
    if (!r || r.error) { check(false, `open the modal: ${r && r.error}`); continue; }

    check(r.tabNames.join(",") === "hub,dictate,takes",
      `three tabs, in order (${r.tabNames.join(",")})`);
    check(r.barScrollW <= r.barClientW + 1,
      `the tab bar fits without side-scrolling (${r.barScrollW} <= ${r.barClientW})`);
    check(r.btnBoxes.every(b => b.x >= -1 && b.right <= r.viewportW + 1),
      "every tab button is on screen");
    check(r.btnBoxes.every(b => b.h >= 30), "tab buttons stay thumb-sized (>=30px)");
    check(r.docScrollW <= r.viewportW + 1,
      `the page never scrolls sideways (${r.docScrollW} <= ${r.viewportW})`);

    for (const t of r.tabs) {
      check(t.panelScrollW <= t.panelClientW + 1,
        `${t.name}: panel does not overflow its width`);
      check(t.modal.y >= -1 && t.modal.bottom <= r.viewportH + 1,
        `${t.name}: the modal stays inside the viewport ` +
        `(top ${Math.round(t.modal.y)}, bottom ${Math.round(t.modal.bottom)})`);
    }

    const takes = r.tabs.find(t => t.name === "takes");
    check(takes.takeCount === 40, `all 40 seeded takes rendered (${takes.takeCount})`);
    // The two failures Chris named: a list so tall it drags the modal off the
    // screen, or one squeezed so short it cannot be read.
    check(takes.hist.bottom <= r.viewportH + 1,
      `the history list ends on screen (${Math.round(takes.hist.bottom)} <= ${r.viewportH})`);
    check(takes.histClientH >= 150,
      `the history box is tall enough to read (${Math.round(takes.histClientH)}px >= 150)`);
    check(takes.histScrollH > takes.histClientH,
      "a long history scrolls inside its own box rather than stretching the modal");
    check(takes.firstTake.h >= 24 && takes.firstTake.bottom <= takes.hist.bottom + 1,
      "the newest take is visible without scrolling");
    check(takes.statScrollW <= takes.statClientW + 1,
      `the stat tiles wrap instead of overflowing (${takes.statScrollW} <= ${takes.statClientW})`);
    console.log(`      history box ${Math.round(takes.histClientH)}px tall, ` +
                `content ${takes.histScrollH}px, modal ` +
                `${Math.round(takes.modal.h)}px of ${r.viewportH}px viewport`);
  }

  ws.close();
  proc.kill();
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch {}
  console.log(fails.length ? `\n${fails.length} FAILED` : "\nall checks passed");
  process.exit(fails.length ? 1 : 0);
})().catch(e => {
  console.error(e);
  proc.kill();
  process.exit(2);
});
