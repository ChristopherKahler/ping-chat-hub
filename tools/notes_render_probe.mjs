#!/usr/bin/env node
// Release-notes renderer probe.
//
// The notes box shows markdown that arrives from a GitHub release. Two things
// have to hold and neither is visible by reading the regexes:
//
//   1. It renders. Chris was looking at literal ## and ** in the modal.
//   2. Nothing in the notes can become markup. A release body is not trusted
//      input just because it is usually ours, and this function builds HTML
//      from a string by hand.
//
// The function is pulled straight out of hub.html so the thing under test is
// the thing that ships.
//
//   node tools/notes_render_probe.mjs
//
// Exits non-zero on the first failed assertion.
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const here = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(here, "..", "src", "ping_hub", "hub.html"), "utf8");
const m = html.match(/function renderMarkdown\(text\)[\s\S]*?\n}\n/);
if (!m) {
  console.error("renderMarkdown is not in hub.html any more");
  process.exit(2);
}
const renderMarkdown = eval("(" + m[0].replace(/^function renderMarkdown/, "function") + ")");

const fails = [];
const check = (ok, what) => {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${what}`);
  if (!ok) fails.push(what);
};

const NOTES = [
  "## Install",
  "",
  "```bash",
  "pip install --upgrade https://example.com/very/long/claude_chat-0.2.0-py3-none-any.whl",
  "```",
  "",
  "What's new",
  "- **desktop STT** tab with *history*",
  "- inline `word=word*` fixes",
  "  a wrapped continuation of that bullet",
  "",
  "1. first",
  "2. second",
  "",
  "---",
  "**Full Changelog**: https://github.com/o/r/compare/v0.1.0...v0.2.0",
].join("\n");

const out = renderMarkdown(NOTES);

console.log("\nstructure");
check(/<h3>Install<\/h3>/.test(out), "a ## heading becomes a heading, not literal hashes");
check(/<pre><code>pip install/.test(out), "a fenced block becomes a pre");
check(/<strong>desktop STT<\/strong>/.test(out), "** becomes bold");
check(/<em>history<\/em>/.test(out), "* becomes italic");
check(/<code>word=word\*<\/code>/.test(out), "backticks become code, asterisk inside untouched");
check(/<ul>[\s\S]*<\/ul>/.test(out) && /<ol>[\s\S]*<\/ol>/.test(out), "both list kinds render");
check(/<li>inline <code>word=word\*<\/code> fixes a wrapped continuation of that bullet<\/li>/.test(out),
      "a wrapped line folds into its bullet instead of starting a paragraph");
check(/<hr>/.test(out), "--- becomes a rule");
check(/<a href="https:\/\/github\.com\/o\/r\/compare/.test(out), "a bare URL becomes a link");

console.log("\nthe fence is inert");
const fence = out.match(/<pre><code>([\s\S]*?)<\/code><\/pre>/)[1];
check(!/<a /.test(fence), "a URL inside a fenced block is NOT linkified");
check(fence.includes("claude_chat-0.2.0-py3-none-any.whl"), "the install line survives intact");

console.log("\nnothing in the notes can become markup");
// Testing for the SUBSTRING "onerror=" is useless: escaped text still contains
// it, harmlessly, inside &lt;img src=x onerror=alert(1)&gt;. What matters is
// whether a real TAG was produced that this function did not intend to write.
// So: every tag in the output must be on the allowlist, and no tag may carry
// an event-handler attribute or a javascript: href.
const ALLOWED = new Set(["p", "h1", "h2", "h3", "ul", "ol", "li", "strong",
                         "em", "code", "pre", "a", "hr", "br"]);

function unsafeTags(out) {
  const bad = [];
  for (const m of out.matchAll(/<\/?([a-zA-Z][a-zA-Z0-9]*)([^>]*)>/g)) {
    const [full, name, attrs] = m;
    if (!ALLOWED.has(name.toLowerCase())) { bad.push("tag " + full); continue; }
    if (/\son[a-zA-Z]+\s*=/.test(attrs)) bad.push("handler " + full);
    if (/href\s*=\s*"\s*javascript:/i.test(attrs)) bad.push("js href " + full);
  }
  return bad;
}

for (const [name, payload] of [
  ["a script tag", "<script>alert(1)</script>"],
  ["an img onerror", "<img src=x onerror=alert(1)>"],
  ["an svg onload", "<svg/onload=alert(1)>"],
  ["a javascript: link", "[click](javascript:alert(1))"],
  ["emphasis carrying a quote", '**bold" onmouseover="alert(1)**'],
  // This one was a REAL hole, not a hypothetical: a URL may contain a quote,
  // the URL is written into href="...", and escaping only &<> left the quote
  // free to close the attribute and add a handler to a genuine anchor.
  ["a quote inside a link URL", '[click](https://a.com/" onmouseover="alert(1))'],
  ["a quote inside a bare URL", 'see https://a.com/"onload="x'],
  ["a quote inside a heading", '## a "quoted" heading'],
]) {
  const bad = unsafeTags(renderMarkdown(payload));
  check(bad.length === 0, `${name}: ${bad.length ? bad.join("; ") : "no unsafe tag produced"}`);
}

check(!/<a [^>]*\shref="[^"]*"[^>]*"/.test(renderMarkdown('[x](https://a.com/"y)')),
      "a quote in a URL cannot terminate the href early");

console.log("\nedge cases");
check(renderMarkdown("") === "", "empty notes render to nothing, not to '<p></p>'");
check(renderMarkdown(null) === "", "null notes do not throw");
check(!/undefined/.test(renderMarkdown("plain line")), "a plain line renders clean");

console.log(fails.length ? `\n${fails.length} FAILED` : "\nall checks passed");
process.exit(fails.length ? 1 : 0);
