#!/usr/bin/env node
// Quick-launch probe — presets survive a reload, and one tap spawns exactly
// what was configured.
//
// This one WRITES: it saves settings. So it must be pointed at a SHADOW HUB on
// a scratch store, never the operator's board — same rule mobile_probe.mjs
// documents. /api/spawn is stubbed at the last moment so a passing run never
// opens a real terminal; everything before that is the real page talking to a
// real daemon, because "presets survive a restart" cannot be proved against a
// stubbed store.
//
//   1. shadow hub:  PING_HUB_CONFIG=<scratch>/hub.toml python -m ping_hub.daemon
//   2. node tools/quicklaunch_probe.mjs http://127.0.0.1:7805/
import { spawn } from "child_process";
import fs from "fs";
import os from "os";
import path from "path";

const HUB = process.argv[2] || "http://127.0.0.1:7799/";
const CDP_PORT = Number(process.env.PROBE_CDP_PORT || 9484);

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
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "hub-ql-probe-"));
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
    // 360px: the phone is the primary surface for this feature, not a
  // narrower version of the desktop
  await cdp.send("Emulation.setDeviceMetricsOverride",
    { width: 360, height: 780, deviceScaleFactor: 3, mobile: true });
  await cdp.send("Page.navigate", { url: HUB }); await sleep(2500);

  const ev = async expression => {
    const r = await cdp.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 400));
    return r.result.value;
  };

  const NAME = "probe wsl builder";
  // 1. create a preset in settings and SAVE it to the daemon for real
  const made = await ev(`(async () => {
    document.getElementById("gear").click();
    await new Promise(r => setTimeout(r, 2500));
    const ov = document.querySelector("#overlay");
    ov.querySelector("#qladd").click();
    const row = ov.querySelector("#qllist .qlrow");
    row.querySelector(".qlname").value = ${JSON.stringify(NAME)};
    row.querySelector(".qlside").value = "wsl";
    row.querySelector(".qlcwd").value = "/home/operator/work";
    row.querySelector(".qlproj").value = "ping-chat-hub";
    row.querySelector(".qlgated").checked = true;
    row.querySelector(".qlparent").value = "toucan";
    // prove the wipe is fixed: put a key the settings form has no widget for
    hubCfg.card_order = ["win:sentinel"];
    ov.querySelector(".save").click();
    await new Promise(r => setTimeout(r, 1500));
    return { saved: !document.querySelector("#overlay") };
  })()`);

  // 2. RELOAD: persistence is the claim, so re-read it from the daemon
  await cdp.send("Page.navigate", { url: HUB }); await sleep(2600);
  const out = await ev(`(async () => {
    const R = {};
    const stored = await (await fetch("/api/settings")).json();
    R.persisted = (stored.quick_launch || []).map(q => q.name);
    R.payload = (stored.quick_launch || [])[0] && (stored.quick_launch || [])[0].payload;
    R.cardOrderKept = JSON.stringify(stored.card_order || []);
    // 3. the launcher shows it as a one-tap button
    document.getElementById("spawn").click();
    await new Promise(r => setTimeout(r, 1800));
    const pop = document.getElementById("pop");
    // the card carries a name AND the payload summary, so read the name node
    // rather than the whole card's text
    const cards = [...pop.querySelectorAll(".qlbtn")];
    R.buttons = cards.map(b => (b.querySelector(".qlnm b") || b).textContent.trim());
    R.metas = cards.map(b => (b.querySelector(".qlmeta") || {}).textContent || "");
    // 4. one tap = one POST /api/spawn with exactly that payload, no dialog
    let sent = null;
    const real = window.fetch;
    window.fetch = async (url, opts) => {
      if (String(url).includes("/api/spawn") && opts && opts.method === "POST") {
        sent = JSON.parse(opts.body);
        return { json: async () => ({ ok: true, title: "probe-child" }) };
      }
      return real(url, opts);
    };
    pop.querySelector(".qlbtn").click();
    await new Promise(r => setTimeout(r, 900));
    R.sent = sent;
    R.popClosedAfterTap = !document.getElementById("pop");
    R.noExtraDialog = !document.querySelector("#overlay");
    R.spawnWatchArmed = !!spawnWatch && spawnWatch.side === "wsl";
    window.fetch = real;
    return R;
  })()`);

  console.log("\nquick launch\n");
  check(made.saved, "settings saved and closed");
  check(out.persisted.includes(NAME), "the preset survived a full page reload (server-stored)");
  check(out.cardOrderKept === '["win:sentinel"]',
        "saving settings no longer wipes keys it has no widget for");
  check(out.buttons.includes(NAME), "it renders as a one-tap card in the launcher");
  check((out.metas || []).some(m => m.includes("WSL")),
        "the card says what it will boot, not just its name");
  check(!!out.sent, "tapping it POSTs to /api/spawn");
  check(out.sent && out.sent.side === "wsl" && out.sent.gated === true &&
        out.sent.cwd === "/home/operator/work" &&
        out.sent.project === "ping-chat-hub" && out.sent.parent === "toucan",
        "with exactly the configured payload");
  check(out.sent && !("model" in out.sent) && !("prompt" in out.sent),
        "and blank fields are dropped, not sent empty");
  check(out.noExtraDialog, "no second dialog stands between the tap and the spawn");
  check(out.popClosedAfterTap, "the launcher closes itself after firing");
  check(out.spawnWatchArmed, "the booting card is armed, same as a hand-filled launch");

  console.log(fails.length ? `\n${fails.length} FAILED` : "\nall checks passed");
} finally {
  try { ws && ws.close(); } catch {}
  proc.kill();
  // the browser has not finished letting go of its profile yet, and a failed
  // cleanup must never mask the result the probe exists to report
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch {}
}
process.exit(fails.length ? 1 : 0);
