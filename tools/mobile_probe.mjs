#!/usr/bin/env node
// Mobile layout probe — measures the real page in a real browser.
//
// The hub's mobile break of 2026-08-17 was invisible to every desktop check
// AND to an empty store: it needed a long status line and a right-aligned own
// message before anything went wrong. So this asserts against MEASURED
// geometry at real phone widths rather than against CSS source.
//
// Deliberately NOT a pytest test. The Python suite is hermetic — no
// subprocess, no network — and spawning a browser would undo that. This is a
// tool with a documented command, run by hand and at the final gate.
//
//   1. start a shadow hub on a scratch store (never the live one):
//        PING_HUB_CONFIG=<scratch>/hub.toml python -m ping_hub.daemon
//   2. node tools/mobile_probe.mjs http://127.0.0.1:<port>/
//
// Exits non-zero on the first failed assertion. No dependencies: it drives an
// already-installed Chrome or Edge over CDP using node's built-in WebSocket.
import { spawn } from "child_process";
import fs from "fs";
import os from "os";
import path from "path";

const HUB = process.argv[2] || "http://127.0.0.1:7805/";
const CDP_PORT = Number(process.env.PROBE_CDP_PORT || 9422);

const BROWSER = [
  process.env.PROBE_BROWSER,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
].filter(Boolean).find(p => { try { return fs.statSync(p).isFile(); } catch { return false; } });

if (!BROWSER) {
  console.error("no Chrome or Edge found. Set PROBE_BROWSER to its path.");
  process.exit(2);
}

// Chris's phone is 1080x2340 at DPR 2.625 -> 412 CSS px. 320 is the narrowest
// phone still in use. 768 and 1024 are past the media query, which is exactly
// where the first candidate fix was measured to be insufficient.
const WIDTHS = [320, 360, 390, 412, 700, 768, 1024];

const sleep = ms => new Promise(r => setTimeout(r, ms));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "hub-mobile-probe-"));
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

// The content shapes that broke it: a long nowrap status line, right-aligned
// own messages, and a token with no break opportunity. An empty store has none
// of these and measures perfectly clean, which is how this shipped.
const SEED = String.raw`(() => {
  const hinfo = document.getElementById("hinfo");
  if (!hinfo) return "no #hinfo";
  hinfo.innerHTML =
    '<div class="hname"><b>heron</b> <span class="badge live">active</span></div>' +
    '<div class="hbadges"><span class="badge">win</span>' +
    '<span class="badge model">fable&middot;xhigh</span>' +
    '<span class="badge ctx">27% &middot; 270k/1.0m</span>' +
    '<span class="badge escb">esc!</span></div>' +
    '<div class="hstatus">idle - STT import SHIPPED at f291b9e (G2 passed, ' +
    'live-proven); awaiting the next order from heron</div>';
  const msgs = document.getElementById("msgs");
  if (!msgs) return "no #msgs";
  msgs.innerHTML = "";
  const add = (cls, text) => { const d = document.createElement("div");
    d.className = "msg " + cls; d.textContent = text; msgs.appendChild(d); };
  add("in",  "G2 REQUESTED - DONE, pushed to origin main and verified.");
  add("out", "this is my own sent message and it must be readable");
  add("in",  "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef");
  add("out", "own message with a path C:\\Users\\<user>\\Tools\\ping-chat-hub");
  return "seeded";
})()`;

const MEASURE = String.raw`(() => {
  const de = document.documentElement, vw = de.clientWidth;
  const rect = e => { const r = e.getBoundingClientRect();
    return { l: Math.round(r.left), r: Math.round(r.right), w: Math.round(r.width) }; };
  const q = s => document.querySelector(s);
  return {
    vw,
    overflow: de.scrollWidth - vw,
    own: [...document.querySelectorAll(".msg.out")].map(rect),
    compose: [...document.querySelectorAll("#compose > *")]
      .filter(e => e.getBoundingClientRect().width > 0)
      .map(e => ({ id: e.id || e.tagName.toLowerCase(), ...rect(e) })),
    box: q("#box") ? rect(q("#box")) : null,
    status: q(".hstatus") ? rect(q(".hstatus")) : null,
  };
})()`;

let ws;
try {
  ws = new WebSocket(await browserWs());
  await new Promise(r => { ws.onopen = r; });
  const cdp = new Cdp(ws);
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" }, null);
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true }, null);
  cdp.sid = sessionId;
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Emulation.setDeviceMetricsOverride",
    { width: 412, height: 915, deviceScaleFactor: 2.625, mobile: true });
  await cdp.send("Page.navigate", { url: HUB });
  await sleep(2500);

  const ev = async expression => {
    const r = await cdp.send("Runtime.evaluate",
      { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 600));
    return r.result.value;
  };

  const seeded = await ev(SEED);
  if (seeded !== "seeded") throw new Error("could not seed the page: " + seeded);

  for (const width of WIDTHS) {
    await cdp.send("Emulation.setDeviceMetricsOverride",
      { width, height: 915, deviceScaleFactor: 2, mobile: width <= 700 });
    await sleep(350);
    const m = await ev(MEASURE);
    console.log(`\n${width}px  (viewport ${m.vw})`);

    check(m.overflow === 0,
      `${width}: no horizontal overflow (scrollWidth - clientWidth = ${m.overflow})`);

    // the assertion for the symptom a width check alone would miss: an own
    // message is right-aligned, so an over-wide column hides it completely
    // while every incoming message still looks fine
    const offscreen = m.own.filter(r => r.l < 0 || r.r > m.vw);
    check(offscreen.length === 0,
      `${width}: every own message inside the viewport ` +
      `(${m.own.map(r => r.l + "-" + r.r).join(", ")})`);

    const spill = m.compose.filter(c => c.l < -0.5 || c.r > m.vw + 0.5);
    check(spill.length === 0,
      `${width}: compose row inside the viewport` +
      (spill.length ? ` — spilling: ${spill.map(c => c.id + " " + c.l + "-" + c.r).join(", ")}` : ""));

    if (width <= 700 && m.box) {
      // the input is the point of the row; the old single-row layout left it
      // 108px at 412 and 78px at 320
      const share = m.box.w / m.vw;
      check(share >= 0.55,
        `${width}: text input is ${Math.round(share * 100)}% of the viewport (want >= 55%)`);
    }
    if (m.status) {
      check(m.status.r <= m.vw + 0.5,
        `${width}: status line within the viewport (right edge ${m.status.r})`);
    }

    // the launcher popover. Measured 2026-08-19 before it was a sheet: 508px
    // wide on a 360px screen, hanging 76px off the left and 72px off the
    // right, covering 121% of the viewport — so there was no outside left to
    // tap, and tap-outside plus Esc were the only ways out. A phone has no Esc.
    const q = await ev(`(async () => {
      document.getElementById("spawn").click();
      await new Promise(r => setTimeout(r, 1500));
      const pop = document.getElementById("pop");
      if (!pop) return { missing: true };
      const b = pop.getBoundingClientRect();
      const x = pop.querySelector("#popclose");
      const xb = x && x.getBoundingClientRect();
      const launch = pop.querySelector("#sp-launch");
      const lb = launch && launch.getBoundingClientRect();
      const out = {
        l: Math.round(b.left), r: Math.round(b.right),
        coverPct: Math.round(100 * (b.width * b.height) / (innerWidth * innerHeight)),
        xw: xb ? Math.round(xb.width) : 0, xh: xb ? Math.round(xb.height) : 0,
        xHit: !!x && document.elementFromPoint(xb.left + xb.width / 2,
                                              xb.top + xb.height / 2) === x,
        launchBottom: lb ? Math.round(lb.bottom) : -1,
        sheet: pop.classList.contains("sheet"),
      };
      x.click();
      await new Promise(r => setTimeout(r, 200));
      out.closed = !document.getElementById("pop");
      return out;
    })()`);
    if (q.missing) {
      check(false, `${width}: launcher popover never opened`);
    } else {
      check(q.l >= 0 && q.r <= m.vw,
        `${width}: launcher popover inside the viewport (${q.l}-${q.r} of ${m.vw})`);
      check(q.xw >= 44 && q.xh >= 44,
        `${width}: close control is a thumb target (${q.xw}x${q.xh}, want 44+)`);
      check(q.xHit, `${width}: close control is on top and hit-testable`);
      check(q.closed, `${width}: the close control actually dismisses it`);
      if (width <= 700) {
        check(q.sheet, `${width}: popover is a sheet on the phone`);
        // covering the whole viewport is what removed tap-outside as an option
        check(q.coverPct < 100,
          `${width}: popover leaves an outside to tap (${q.coverPct}% covered)`);
        check(q.launchBottom > 0 && q.launchBottom <= 915,
          `${width}: Launch is on screen without scrolling (bottom ${q.launchBottom})`);
      }
    }
  }
} catch (e) {
  console.error("probe error:", e.message);
  fails.push("probe threw: " + e.message);
} finally {
  try { if (ws) ws.close(); } catch {}
  proc.kill();
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch {}
}

console.log(fails.length
  ? `\n${fails.length} FAILED:\n  - ${fails.join("\n  - ")}`
  : `\nall checks passed across ${WIDTHS.length} widths`);
process.exit(fails.length ? 1 : 0);
