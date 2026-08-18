#!/usr/bin/env node
// Clear-modal probe — measures the confirm modal in a real browser.
//
// The modal grew a fourth button ("Close out first", offered only when a
// session has no handoff). `#modal .actions` is a flex row that does not
// wrap, so a row that fit three buttons on a phone is exactly the shape that
// broke the roster on 2026-08-17: fine on a desktop, fine with an empty
// store, over the edge on Chris's phone. CSS source cannot answer it —
// measured geometry can.
//
// The real modal builder runs. Only /api/clear-preview is stubbed, to pin the
// case under test (confirmable session, NO handoff = the four-button row);
// nothing is POSTed and no session is touched.
//
//   node tools/clear_modal_probe.mjs http://127.0.0.1:7799/
//
// Exits non-zero on the first failed assertion.
import { spawn } from "child_process";
import fs from "fs";
import os from "os";
import path from "path";

const HUB = process.argv[2] || "http://127.0.0.1:7799/";
const CDP_PORT = Number(process.env.PROBE_CDP_PORT || 9424);

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

const WIDTHS = [320, 360, 390, 412, 768];
const sleep = ms => new Promise(r => setTimeout(r, ms));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "hub-clear-probe-"));
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

// Open the REAL modal: stub only the preview fetch, then click the real
// button and let hub.html build what it builds.
const OPEN = found => String.raw`(async () => {
  const FOUND = ` + (found ? "true" : "false") + String.raw`;
  document.querySelectorAll("#overlay").forEach(o => o.remove());
  // sel is a module-level let, so it cannot be set from outside: the
  // session is selected the way Chris selects one, by clicking its card.
  // The stub goes on FIRST so that click's /api/focus never reaches the live
  // hub -- this probe must not move his cockpit's focus.
  const real = window.fetch;
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.startsWith("/api/clear-preview")) return Promise.resolve({ json: async () => ({
      title: "probe", side: "win", reapable: true, reason: "",
      handoff: FOUND
        ? { found: true, via: "title", slug: "2026-08-18-1600-probe-thing",
            headline: "probe: a handoff this session would resume",
            detail: "named by title" }
        : { found: false, detail: "no handoff document names this session" } }) });
    if (s.startsWith("/api/focus")) return Promise.resolve({ json: async () => ({ ok: true }) });
    return real(u, o);
  };
  const card = document.querySelector("#list .th") || document.querySelector(".th");
  if (!card) { window.fetch = real; return { error: "no session card in the roster" }; }
  card.click();
  await new Promise(r => setTimeout(r, 300));
  document.getElementById("clearb").click();
  for (let i = 0; i < 40 && !document.querySelector("#modal .actions button"); i++)
    await new Promise(r => setTimeout(r, 50));
  window.fetch = real;
  const m = document.querySelector("#modal");
  if (!m) return { error: "modal never rendered" };
  const row = m.querySelector(".actions");
  const btns = [...row.querySelectorAll("button")].map(b => {
    const r = b.getBoundingClientRect();
    return { text: b.textContent.trim(), x: r.x, right: r.right, w: r.width, h: r.height };
  });
  const mr = m.getBoundingClientRect(), rr = row.getBoundingClientRect();
  return {
    buttons: btns,
    hasEndClose: !!m.querySelector("#do-endclose"),
    rowScrollW: row.scrollWidth, rowClientW: row.clientWidth,
    modalRight: mr.right, modalLeft: mr.x,
    rowTop: rr.y, rowBottom: rr.bottom,
    docScrollW: document.documentElement.scrollWidth,
    viewportW: window.innerWidth,
  };
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
    await sleep(1600);
    console.log(`\n${width}px`);
    const seen = {};
    for (const found of [false, true]) {
      const label = found ? "with a handoff" : "no handoff";
      const r = await cdp.send("Runtime.evaluate",
        { expression: OPEN(found), awaitPromise: true, returnByValue: true });
      const v = r.result.value;
      if (!v || v.error) { check(false, `${width}px ${label}: ${v ? v.error : "no result"}`); continue; }
      seen[found] = v;
      const want = found ? 3 : 4;
      check(v.hasEndClose === !found,
        `${width}px ${label}: close-out button ${found ? "withheld" : "offered"}`);
      check(v.buttons.length === want,
        `${width}px ${label}: ${want} buttons in the row (got ${v.buttons.length})`);
      check(v.docScrollW <= v.viewportW + 1,
        `${width}px ${label}: the page does not scroll sideways (${v.docScrollW} <= ${v.viewportW})`);
      for (const b of v.buttons) {
        check(b.x >= v.modalLeft - 1 && b.right <= v.modalRight + 1,
          `${width}px ${label}: "${b.text}" stays inside the modal ` +
          `(${Math.round(b.x)}..${Math.round(b.right)} in ${Math.round(v.modalLeft)}..${Math.round(v.modalRight)})`);
      }
    }
    // The bar is the page's OWN button height, not a number this probe made
    // up: the three-button row is what shipped, so the fourth button must not
    // change how any of them measure.
    if (seen[false] && seen[true]) {
      const base = Math.max(...seen[true].buttons.map(b => b.h));
      const grew = seen[false].buttons.filter(b => b.h > base + 2).map(b => b.text);
      check(grew.length === 0,
        `${width}px: no button is squeezed into extra lines by the fourth ` +
        `(baseline ${Math.round(base)}px, taller: ${grew.join(", ") || "none"})`);
    }
  }

  ws.close();
  try { proc.kill(); } catch { /* already gone */ }
  // the browser releases its profile lazily; a failed cleanup is not a failed
  // measurement, and throwing here would bury the results
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* temp dir */ }
  console.log(`\n${fails.length} failed`);
  process.exit(fails.length ? 1 : 0);
})().catch(e => {
  console.error(e);
  try { proc.kill(); } catch { /* already gone */ }
  process.exit(2);
});
