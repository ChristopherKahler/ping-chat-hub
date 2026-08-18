#!/usr/bin/env node
// Settings-panel probe — asserts the panel opens NOW and opens ONCE.
//
// Two faults shared one panel. It took ~20s to appear because the gear awaited
// four fetches before appending anything, and one of them resolves the kokoro
// voice list — which, when kokoro's daemon is down, starts it and polls for up
// to 30s while a 310MB model loads. Every impatient click during that wait
// opened another modal; four clicks measured four stacked overlays.
//
// Point this at a shadow hub whose [tts] command is a SLOW STUB, because a
// machine with a warm engine measures clean and proves nothing:
//
//   [tts]
//   command = ["python", "slow_say.py"]     # sleeps 12s, prints two voices
//
//   node tools/settings_probe.mjs http://127.0.0.1:<port>/
//
// Measured against a 12s engine: HEAD 12093ms and 4 overlays; fixed 1ms and 1.
// No dependencies — drives an installed Chrome or Edge over CDP.
import { spawn } from "child_process";
import fs from "fs";
import os from "os";
import path from "path";

const HUB = process.argv[2] || "http://127.0.0.1:7806/";
const CDP_PORT = Number(process.env.PROBE_CDP_PORT || 9452);
const OPEN_BUDGET_MS = 1000;   // the panel must not wait on the speech engine

const BROWSER = [
  process.env.PROBE_BROWSER,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
].filter(Boolean).find(p => { try { return fs.statSync(p).isFile(); } catch { return false; } });
if (!BROWSER) { console.error("no Chrome or Edge found. Set PROBE_BROWSER."); process.exit(2); }

const sleep = ms => new Promise(r => setTimeout(r, ms));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "hub-set-probe-"));
const proc = spawn(BROWSER, ["--headless=new", `--remote-debugging-port=${CDP_PORT}`,
  `--user-data-dir=${profile}`, "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--disable-gpu", "about:blank"], { stdio: "ignore" });

async function browserWs() {
  for (let i = 0; i < 80; i++) {
    try { const r = await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`);
          return (await r.json()).webSocketDebuggerUrl; } catch { await sleep(250); } }
  throw new Error("browser never opened its debug port");
}
class Cdp {
  constructor(ws) { this.ws = ws; this.id = 0; this.w = new Map(); this.sid = null;
    ws.onmessage = e => { const m = JSON.parse(e.data);
      if (m.id && this.w.has(m.id)) { const { res, rej } = this.w.get(m.id); this.w.delete(m.id);
        m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } }; }
  send(method, params = {}, sid = this.sid) { const id = ++this.id;
    return new Promise((res, rej) => { this.w.set(id, { res, rej });
      this.ws.send(JSON.stringify({ id, method, params, ...(sid ? { sessionId: sid } : {}) })); }); }
}

const fails = [];
const check = (ok, what) => { console.log(`  ${ok ? "ok  " : "FAIL"}  ${what}`); if (!ok) fails.push(what); };

let ws;
try {
  ws = new WebSocket(await browserWs());
  await new Promise(r => { ws.onopen = r; });
  const cdp = new Cdp(ws);
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" }, null);
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true }, null);
  cdp.sid = sessionId;
  await cdp.send("Page.enable"); await cdp.send("Runtime.enable");
  await cdp.send("Emulation.setDeviceMetricsOverride",
    { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
  await cdp.send("Page.navigate", { url: HUB });
  await sleep(3000);

  const ev = async expression => {
    const r = await cdp.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 500));
    return r.result.value;
  };

  const one = await ev(`(async () => {
    document.querySelectorAll("#overlay").forEach(o => o.remove());
    const t0 = performance.now();
    document.getElementById("gear").click();
    for (let i = 0; i < 700; i++) {
      if (document.querySelector("#overlay #modal h2")) break;
      await new Promise(r => setTimeout(r, 50));
    }
    const openMs = Math.round(performance.now() - t0);
    return { openMs, overlays: document.querySelectorAll("#overlay").length };
  })()`);
  console.log(`\nsingle click: modal on screen in ${one.openMs} ms`);
  check(one.overlays === 1, `the panel opened (overlays ${one.overlays})`);
  check(one.openMs <= OPEN_BUDGET_MS,
    `panel opens in ${one.openMs} ms, budget ${OPEN_BUDGET_MS} — it must not wait on the speech engine`);

  // the shell is up at ~1ms; the fast sections fill when their three small
  // reads land. That must happen while the speech engine is still asleep.
  const sections = await ev(`(async () => {
    const ov = document.querySelector("#overlay.settings");
    if (!ov) return { body: false };
    const t0 = performance.now();
    for (let i = 0; i < 60; i++) {
      if (ov.querySelector("#reps")) break;
      await new Promise(r => setTimeout(r, 25));
    }
    return { body: !!ov.querySelector("#reps"),
             bodyMs: Math.round(performance.now() - t0),
             note: (ov.querySelector("#voicenote") || {}).textContent || "",
             saveEnabled: !ov.querySelector(".save").disabled };
  })()`);
  check(sections.body === true,
    `the word-fix section rendered in ${sections.bodyMs} ms without waiting for voices`);
  check(sections.saveEnabled === true, "Save is usable while the voice list is still loading");
  console.log(`  note   voice section said: "${sections.note}"`);

  // whether the note is still showing depends on whether the engine was warm
  // when the panel opened, so assert the END state: the list arrives and the
  // note stops claiming to be loading. A note left saying "loading" forever is
  // the same lie as a blank one.
  const settled = await ev(`(async () => {
    const ov = document.querySelector("#overlay.settings");
    for (let i = 0; i < 100; i++) {
      const note = ov.querySelector("#voicenote");
      if (!note) break;
      await new Promise(r => setTimeout(r, 250));
    }
    const sel = ov.querySelector("#s-ttsvoice");
    const note = ov.querySelector("#voicenote");
    return { options: sel ? sel.options.length : 0,
             noteLeft: note ? note.textContent : null };
  })()`);
  check(settled.noteLeft === null,
    `the loading note is cleared once the list arrives (left: ${JSON.stringify(settled.noteLeft)})`);
  check(settled.options >= 1,
    `the voice list is populated (${settled.options} options)`);

  const many = await ev(`(async () => {
    document.querySelectorAll("#overlay").forEach(o => o.remove());
    const g = document.getElementById("gear");
    for (let i = 0; i < 4; i++) { g.click(); await new Promise(r => setTimeout(r, 30)); }
    await new Promise(r => setTimeout(r, 2500));
    return { overlays: document.querySelectorAll("#overlay").length,
             modals: document.querySelectorAll("#overlay #modal").length };
  })()`);
  console.log(`\nfour rapid clicks: ${many.overlays} overlays, ${many.modals} modals`);
  check(many.overlays === 1 && many.modals === 1,
    `four clicks leave exactly one panel (was 4 before the guard)`);
} catch (e) {
  console.error("probe error:", e.message);
  fails.push("probe threw: " + e.message);
} finally {
  try { if (ws) ws.close(); } catch {}
  proc.kill();
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch {}
}

console.log(fails.length ? `\n${fails.length} FAILED:\n  - ${fails.join("\n  - ")}`
                         : `\nall checks passed`);
process.exit(fails.length ? 1 : 0);
