// Render-verify dashboard/index.html in jsdom against a fixture data.json.
// Optional dev check: renders dashboard/index.html in jsdom against a fixture and
// asserts the DOM. Needs `npm i jsdom` (not a runtime dependency).
// Usage: node tests/render_check.js [index.html] [fixture.json]
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const here = __dirname;
const htmlPath = process.argv[2] || path.join(here, "..", "dashboard", "index.html");
const dataPath = process.argv[3] || path.join(here, "fixture-data.json");
const html = fs.readFileSync(htmlPath, "utf8");
const fixture = JSON.parse(fs.readFileSync(dataPath, "utf8"));

// Cheap guard against the classic footgun: passing data.json as the first
// argument, which "renders" an empty document and then fails every assertion.
if (!html.includes('id="points"')) {
  console.error(
    `not a netwatch dashboard page: ${htmlPath}\n` +
    `usage: node tests/render_check.js [dashboard/index.html] [data.json]`
  );
  process.exit(2);
}

const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push("jsdomError: " + e.message));
vc.on("error", (...a) => errors.push("console.error: " + a.join(" ")));

const dom = new JSDOM(html, {
  url: "https://example.test/netwatch/",
  runScripts: "dangerously",
  pretendToBeVisual: true,
  virtualConsole: vc,
  beforeParse(window) {
    window.fetch = () =>
      Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(fixture) });
  },
});

const fails = [];
const ok = (cond, label, extra) => {
  if (cond) console.log("  PASS  " + label);
  else { fails.push(label + (extra ? " — " + extra : "")); console.log("  FAIL  " + label + (extra ? " — " + extra : "")); }
};

setTimeout(() => {
  const d = dom.window.document;
  const q = (s) => d.querySelectorAll(s);
  console.log("\nrendered DOM:");
  ok(errors.length === 0, "no JS errors", errors.join(" | "));
  ok(q(".pt").length === fixture.targets.length, `point cards = ${fixture.targets.length}`, "got " + q(".pt").length);
  ok(q(".pt.up").length === 3, "3 up cards", "got " + q(".pt.up").length);
  ok(q(".pt.degraded").length === 1, "1 degraded card", "got " + q(".pt.degraded").length);
  ok(q(".pt.down").length === 1, "1 down card", "got " + q(".pt.down").length);
  ok(q("polyline").length >= 4, "sparkline polylines drawn", "got " + q("polyline").length);
  ok(q("#wan-chart rect").length > 10, "wan bar chart drawn", "got " + q("#wan-chart rect").length);
  ok(/482/.test(d.getElementById("wan-mbps").textContent), "wan mbps rendered",
     d.getElementById("wan-mbps").textContent);
  ok(q("#services .s").length === 3, "3 service rows", "got " + q("#services .s").length);
  ok(q("#services .dot.bad").length === 1, "1 failing service dot");
  ok(/something is down/.test(d.getElementById("sub").textContent), "header status = down",
     d.getElementById("sub").textContent);
  ok(d.getElementById("dot").className.includes("bad"), "header dot is red");
  ok(/netwatch · netwatch-host/.test(d.title), "title branded from data.host", d.title);
  ok(/uptime|down|degraded|collecting/.test(d.body.textContent), "no blank body");

  // Visible text only — inline <script> source would trip every naive check.
  const clone = d.body.cloneNode(true);
  clone.querySelectorAll("script").forEach((s) => s.remove());
  const text = clone.textContent;
  ["undefined", "NaN", "[object", "{{", "}}"].forEach((bad) => {
    ok(!text.includes(bad), `no '${bad}' in visible text`);
  });

  console.log("\n" + (fails.length ? `FAILED (${fails.length})` : "ALL CHECKS PASSED"));
  process.exit(fails.length ? 1 : 0);
}, 700);
