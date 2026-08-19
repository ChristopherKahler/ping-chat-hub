#!/usr/bin/env node
// Bridge-down probe — does the hub SAY the bridge is down, and does a spawn
// card survive the outage?
//
// Source assertions (tests/test_ping_hub_bridge_card.py) prove the branches
// exist. This proves they RENDER, which is the part that actually failed on
// 2026-08-19: a booting card was thrown away by a timer while the session it
// represented was running fine, and the list showed nothing at all.
//
// Both fetches are stubbed, including every POST. An earlier draft let the
// resolve path through and it wrote spawn_pinned into the operator's live
// settings — a probe that mutates the thing it measures is worse than none.
//
//   node tools/bridge_card_probe.mjs http://127.0.0.1:7799/
import { spawn } from "child_process";
import fs from "fs";
import os from "os";
import path from "path";

const HUB = process.argv[2] || "http://127.0.0.1:7799/";
const CDP_PORT = Number(process.env.PROBE_CDP_PORT || 9473);

const BROWSER = [
  process.env.PROBE_BROWSER,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "/usr/bin/google-chrome",
].filter(Boolean).find(p => { try { return fs.statSync(p).isFile(); } catch { return false; } });
if (!BROWSER) { console.error("no Chrome or Edge found. Set PROBE_BROWSER."); process.exit(2); }

const sleep = ms => new Promise(r => setTimeout(r, ms));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "hub-bridge-probe-"));
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
  await cdp.send("Page.navigate", { url: HUB }); await sleep(2500);

  const ev = async expression => {
    const r = await cdp.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 400));
    return r.result.value;
  };

  const out = await ev(`(async () => {
    const R = {};
    // NOTHING may reach the real hub from here: the resolve path POSTs
    // settings and focus, and this probe must not edit the operator's board
    let ROSTER = [], BRIDGE = { up: true, probed: true, enabled: true, since: "", detail: "" };
    window.fetch = async (url, opts) => {
      const u = String(url);
      if (opts && opts.method === "POST") return { json: async () => ({ ok: true }) };
      if (u.includes("/api/threads")) return { json: async () => ROSTER };
      if (u.includes("/api/bridge"))  return { json: async () => BRIDGE };
      return { json: async () => ({}) };
    };
    tab = "term";
    const cards = () => [...document.querySelectorAll("#threads .th")]
      // \\s, not \s: this whole block is a template literal, and an unknown
      // escape silently loses its backslash — /s+/g then replaced every letter
      // "s" in the page and the probe reported the UI broken when it was fine
      .map(n => n.textContent.replace(/\\s+/g, " ").trim());
    const banner = () => cards().filter(t => t.includes("WSL bridge down"));

    // 1. bridge up: the board says nothing about it
    BRIDGE = { up: true, probed: true, enabled: true, since: "", detail: "" };
    await loadRoster();
    R.quietWhenUp = banner().length;

    // 2. bridge down: it is ANNOUNCED, even with an empty roster - the state a
    //    hub started during an outage is in
    BRIDGE = { up: false, probed: true, enabled: true,
               since: "2026-08-19T07:06:00", detail: "connection refused" };
    await loadRoster();
    R.bannerWhenDown = banner().length;
    R.bannerText = banner()[0] || "";
    R.bannerNotGray = !!document.querySelector("#threads .th.down");

    // 3. not probed yet is NOT down
    BRIDGE = { up: false, probed: false, enabled: true, since: "", detail: "not probed yet" };
    await loadRoster();
    R.quietWhenUnprobed = banner().length;

    // 4. THE REGRESSION: a wsl spawn whose deadline has already passed, while
    //    the bridge is down. The old code deleted this card without a word.
    BRIDGE = { up: false, probed: true, enabled: true, since: "", detail: "refused" };
    spawnWatch = { side: "wsl", cwd: "/home/operator/work", until: Date.now() - 1,
                   sids: {}, parent: "" };
    await loadRoster();
    R.survivedOutage = spawnWatch !== null;
    R.waitingCard = cards().filter(t => t.includes("waiting on bridge")).length;

    // 5. the bridge comes back and the session is on the roster: it resolves
    ROSTER = [{ side: "wsl", title: "ibis", session_id: "s1", cwd: "/home/operator/work",
                projects: [], parent: "", title_display: "" }];
    BRIDGE = { up: true, probed: true, enabled: true, since: "", detail: "" };
    await loadRoster();
    R.resolvedOnReturn = spawnWatch === null;
    R.noBannerAfter = banner().length;

    // 6. bridge healthy, nothing ever appeared: it FAILS VISIBLY, it does not vanish
    ROSTER = [];
    spawnWatch = { side: "win", cwd: "/home/operator/work", until: Date.now() - 1,
                   sids: {}, parent: "" };
    await loadRoster();
    R.failedSurvives = spawnWatch !== null;
    R.failedCard = cards().filter(t => t.includes("spawn unconfirmed")).length;
    R.failedOffersDismiss = !!document.querySelector("#spawndismiss");

    // 7. and only a human clears it
    document.querySelector("#spawndismiss").click();
    R.dismissClears = spawnWatch === null;
    return R;
  })()`);

  if (process.argv.includes("--debug")) console.log(JSON.stringify(out, null, 1));
  console.log("\nbridge-down UI\n");
  check(out.quietWhenUp === 0,        "no banner while the bridge is up");
  check(out.quietWhenUnprobed === 0,  "no banner before anything has been probed");
  check(out.bannerWhenDown === 1,     "bridge down is announced on an empty board");
  check(/since/.test(out.bannerText), "the banner says since when");
  check(out.bannerNotGray,            "it renders as a fault, not as a quiet session");

  console.log("\nthe card that used to vanish\n");
  check(out.survivedOutage,           "an expired wsl spawn SURVIVES a bridge outage");
  check(out.waitingCard === 1,        "and says it is waiting on the bridge");
  check(out.resolvedOnReturn,         "it resolves when the bridge returns with the session");
  check(out.noBannerAfter === 0,      "and the banner clears with it");
  check(out.failedSurvives,           "a spawn that never appeared does NOT disappear");
  check(out.failedCard === 1,         "it says spawn unconfirmed instead");
  check(out.failedOffersDismiss,      "and offers a dismiss control");
  check(out.dismissClears,            "which is the only thing that clears it");

  console.log(fails.length ? `\n${fails.length} FAILED` : "\nall checks passed");
} finally {
  try { ws && ws.close(); } catch {}
  proc.kill();
  // the browser has not finished letting go of its profile yet, and a failed
  // cleanup must never mask the result the probe exists to report
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch {}
}
process.exit(fails.length ? 1 : 0);
