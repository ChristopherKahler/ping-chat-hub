#!/usr/bin/env node
// Accordion probe — parent/child grouping, collapse, and the tab split.
//
// Two things here are the whole reason it exists, and both were found by
// running it rather than by reading the code:
//
//   1. The grouping markers (_child/_under/_kids) live on the roster objects
//      and outlive a render. Squads showed 1 of 3 children because two were
//      still flagged as collapsed children from the previous Terminals render.
//   2. The FIRST version of this probe read the brood badge AFTER switching to
//      Squads, when no Terminals card existed — it reported an empty result
//      that looked like a missing badge. A probe that measures the wrong
//      moment is worse than no probe, so every capture below names the tab it
//      was taken in.
//
//   node tools/accordion_probe.mjs http://127.0.0.1:<shadow-port>/
import { spawn } from "child_process";
import fs from "fs";
import os from "os";
import path from "path";

const HUB = process.argv[2] || "http://127.0.0.1:7805/";
const CDP_PORT = Number(process.env.PROBE_CDP_PORT || 9461);

const BROWSER = [
  process.env.PROBE_BROWSER,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
].filter(Boolean).find(p => { try { return fs.statSync(p).isFile(); } catch { return false; } });
if (!BROWSER) { console.error("no Chrome or Edge found. Set PROBE_BROWSER."); process.exit(2); }

const sleep = ms => new Promise(r => setTimeout(r, ms));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "hub-acc-probe-"));
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
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

let ws;
try {
  ws = new WebSocket(await browserWs());
  await new Promise(r => { ws.onopen = r; });
  const cdp = new Cdp(ws);
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" }, null);
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true }, null);
  cdp.sid = sessionId;
  await cdp.send("Page.enable"); await cdp.send("Runtime.enable");
  // 412px: the accordion has to hold up on the phone, which is a first-class
  // surface here, not a smaller version of the desktop
  await cdp.send("Emulation.setDeviceMetricsOverride",
    { width: 412, height: 915, deviceScaleFactor: 2, mobile: true });
  await cdp.send("Page.navigate", { url: HUB }); await sleep(2500);

  const ev = async expression => {
    const r = await cdp.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 400));
    return r.result.value;
  };

  const out = await ev(`(async () => {
    roster = [
      {side:"win",title:"heron",     parent:"",             active:true,   projects:[]},
      {side:"win",title:"builder-a", parent:"heron",        esc:1,         projects:[]},
      {side:"win",title:"builder-b", parent:"heron",        watching:true, projects:[]},
      {side:"win",title:"orphan",    parent:"ghost-parent",                projects:[]},
      {side:"win",title:"solo",      parent:"",  squad:"worldsys",         projects:[]}
    ];
    const names = () => [...document.querySelectorAll("#threads .th .name")]
      .map(n => n.textContent.replace(/[^a-z-]/g, ""));
    const orch = () => [...document.querySelectorAll("#threads .badge.orch")]
      .map(b => b.textContent.trim());
    const discSize = () => { const d = document.querySelector("#threads .disc");
      if (!d) return null; const r = d.getBoundingClientRect();
      return { w: Math.round(r.width), h: Math.round(r.height) }; };

    tab = "term"; localStorage.removeItem("acc-collapsed"); renderList();
    const expanded = names(), disc = discSize();
    document.querySelector("#threads .disc").click();
    // captured WHILE Terminals is rendered -- the first version of this probe
    // read it after switching tabs and reported a badge that was simply not
    // on screen yet
    const collapsed = names(), brood = orch();
    tab = "squad"; renderList();
    const squads = names(), broodInSquads = orch();
    tab = "term"; renderList();
    // the drawer is translated off-screen when closed at 412px, so widths
    // measured with it shut are negative and any "fits" assertion passes for
    // the wrong reason. Open it: that is when the accordion is actually on
    // screen on a phone.
    document.getElementById("side").classList.add("open");
    // the drawer slides in over 0.18s; measuring immediately catches it
    // mid-transform and every card reads as outside the viewport
    await new Promise(r => setTimeout(r, 400));
    return { expanded, collapsed, squads, brood, broodInSquads, disc,
             rects: [...document.querySelectorAll("#threads .th")]
        .map(e => { const r = e.getBoundingClientRect();
                    return { l: Math.round(r.left), r: Math.round(r.right) }; }) };
  })()`);

  console.log("\nTerminals, expanded:", out.expanded.join(", "));
  check(same(out.expanded, ["heronactive", "builder-aboot", "builder-bwatching",
                            "orphanboot", "soloboot"]),
    "children render under their parent; orphan and squad-tagged render top level");

  console.log("Terminals, collapsed:", out.collapsed.join(", "));
  check(same(out.collapsed, ["heronactive", "orphanboot", "soloboot"]),
    "collapsing hides exactly the children, and nothing else");

  console.log("Squads:", out.squads.join(", "));
  check(same(out.squads, ["builder-aboot", "builder-bwatching", "orphanboot"]),
    "Squads shows EVERY child, including ones collapsed in Terminals");
  check(out.squads.length === 3,
    "a marker left over from the Terminals render is not hiding children here");

  console.log("brood badge (in Terminals):", JSON.stringify(out.brood));
  check(out.brood.length === 1 && /2/.test(out.brood[0]),
    "a collapsed parent reports how many children it swallowed");
  check(/esc/.test(out.brood[0] || ""),
    "a collapsed brood does NOT swallow a screaming child");
  check(out.broodInSquads.length === 0,
    "no brood badge in Squads, where nothing is collapsed");

  check(out.disc && out.disc.w >= 44 && out.disc.h >= 44,
    `the disclosure is a 44px thumb target (${JSON.stringify(out.disc)})`);
  const off = out.rects.filter(r => r.l < 0 || r.r > 412);
  check(out.rects.length > 0 && off.length === 0,
    `every card sits inside 412px with the drawer open (${out.rects.length} cards, ` +
    `${off.length} outside)`);
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
