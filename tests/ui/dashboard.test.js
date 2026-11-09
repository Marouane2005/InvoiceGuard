"use strict";
// Drives the REAL dashboard.js in a DOM against the REAL server process, with a stand-in for the AI provider.
const test = require("node:test");
const assert = require("node:assert");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { JSDOM, VirtualConsole } = require("jsdom");

const PY = process.env.PYTHON || "python3";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(fn, what, timeout = 10000) {
  const t0 = Date.now();
  for (;;) {
    try { const v = fn(); if (v) return v; } catch (_) { /* keep waiting */ }
    if (Date.now() - t0 > timeout) throw new Error("timed out waiting for: " + what);
    await sleep(25);
  }
}

function freePort() {
  return new Promise((resolve) => { const s = net.createServer().listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => resolve(p)); }); });
}

function startProvider() {
  let inflight = 0, maxInflight = 0, calls = 0;
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", async () => {
      calls++; inflight++; maxInflight = Math.max(maxInflight, inflight);
      const doc = Buffer.from(JSON.parse(body).messages[0].content[0].source.data, "base64").toString("latin1");
      await sleep(60);
      const tag = crypto.createHash("md5").update(doc).digest("hex").slice(0, 6).toUpperCase();
      const vendor = doc.includes("XSS") ? '<img src=x onerror="window.__pwned=1">' : doc.includes("ALPHA") ? "Alpha Supplies" : "Beta Traders";
      const flagged = doc.includes("FLAG");
      const invoice = {
        invoice_metadata: { invoice_number: "N-" + tag, vendor_name: vendor, issue_date: "2025-03-01", due_date: "2025-03-31", currency: "USD" },
        line_items: [{ description: "Item", quantity: 4, unit_price: 25, total_amount: flagged ? 25 : 100 }],
        financial_summary: { subtotal: flagged ? 25 : 100, discount_amount: null, shipping_amount: null, tax_amount: 10, rounding_adjustment: null, grand_total: 110 },
        ocr_warnings: [],
      };
      const payload = JSON.stringify({ model: "stand-in", stop_reason: "tool_use", usage: { input_tokens: 500, output_tokens: 100 },
        content: [{ type: "tool_use", name: "record_invoice", input: invoice }] });
      inflight--;
      res.writeHead(200, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) });
      res.end(payload);
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({
    server, port: server.address().port, calls: () => calls, maxInflight: () => maxInflight, resetMax: () => { maxInflight = 0; },
  })));
}

let stack;
async function startStack() {
  const provider = await startProvider();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ig-ui-"));
  const port = await freePort();
  const adminToken = "ui-test-admin-token-0123456789";
  const env = { ...process.env, INVOICEGUARD_DATA_DIR: dir, INVOICEGUARD_ADMIN_TOKEN: adminToken, ANTHROPIC_API_KEY: "sk-ant-ui-test",
    ANTHROPIC_BASE_URL: `http://127.0.0.1:${provider.port}`, INVOICEGUARD_RATE_LIMIT_PER_MIN: "1000", INVOICEGUARD_MAX_CONCURRENT_READS: "4" };
  const child = spawn(PY, ["-m", "invoiceguard.cli", "serve", "--port", String(port)], { env, cwd: os.tmpdir(), stdio: ["ignore", "pipe", "pipe"] });
  let log = "";
  child.stdout.on("data", (d) => (log += d));
  child.stderr.on("data", (d) => (log += d));
  const base = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 200; i++) {
    try { if ((await fetch(base + "/readyz")).ok) break; } catch (_) { /* not up yet */ }
    await sleep(50);
    if (i === 199) throw new Error("server did not start:\n" + log);
  }
  return { base, provider, adminToken, log: () => log,
    async stop() { child.kill(); await new Promise((r) => child.once("exit", r)); provider.server.close(); fs.rmSync(dir, { recursive: true, force: true }); } };
}

async function newKey() {
  const res = await fetch(stack.base + "/admin/tenants", { method: "POST", headers: { "X-Admin-Token": stack.adminToken, "Content-Type": "application/json" },
    body: JSON.stringify({ name: "UI Test " + crypto.randomBytes(3).toString("hex"), default_currency: "USD" }) });
  return (await res.json()).api_key;
}

async function openDashboard(key) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (e) => errors.push(e.message));
  const dom = await JSDOM.fromURL(stack.base + "/", { runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole,
    beforeParse(window) {
      let pending = 0;
      window.__pending = () => pending;
      window.fetch = async (u, o) => { pending++; try { return await fetch(new URL(String(u), stack.base), o); } finally { pending--; } };
      window.FormData = FormData;
      window.URL.createObjectURL = () => "about:blank";   // jsdom cannot load blob: addresses; about:blank always loads
      window.URL.revokeObjectURL = () => {};
      window.__downloads = [];
      window.HTMLAnchorElement.prototype.click = function () { window.__downloads.push(this.getAttribute("download")); };
      if (key) window.sessionStorage.setItem("ig_key", key);
    } });
  if (dom.window.document.readyState !== "complete") await new Promise((r) => dom.window.addEventListener("load", r));
  const document = dom.window.document;
  const ui = { window: dom.window, document, errors, $: (id) => document.getElementById(id), all: (sel) => [...document.querySelectorAll(sel)],
    status: () => document.getElementById("status").textContent, text: (sel) => [...document.querySelectorAll(sel)].map((n) => n.textContent).join(" | ") };
  return ui;
}

const signIn = (ui, key) => { ui.$("key").value = key; ui.$("keyform").dispatchEvent(new ui.window.Event("submit", { cancelable: true, bubbles: true })); };
let counter = 0;
const pdf = (marker) => new File([Buffer.from(`%PDF-1.4\n% ${marker} #${++counter}\n%%EOF\n`)], `inv-${counter}.pdf`, { type: "application/pdf" });
function choose(ui, files) {
  const input = ui.$("file");
  Object.defineProperty(input, "files", { value: files, configurable: true });
  input.dispatchEvent(new ui.window.Event("change"));
}
const input = (ui, sel, value) => { const n = ui.document.querySelector(sel); n.value = value; n.dispatchEvent(new ui.window.Event("input", { bubbles: true })); return n; };

async function settle(ui) {
  // let the page finish what it started (follow-up requests, re-rendering) before the window is closed
  let quiet = 0;
  for (let i = 0; i < 100 && quiet < 4; i++) { await sleep(50); quiet = ui.window.__pending() === 0 ? quiet + 1 : 0; }
}

async function withDashboard(fn, { signedIn = true } = {}) {
  const key = await newKey();
  const ui = await openDashboard(signedIn ? key : undefined);
  try {
    if (signedIn) await waitFor(() => !ui.$("app").hidden, "dashboard to open");
    await fn(ui, key);
    await settle(ui);
    const real = ui.errors.filter((m) => !/Not implemented: navigation/.test(m));
    assert.deepStrictEqual(real, [], "the page raised script errors");
  } finally { ui.window.close(); }
}

test.before(async () => { stack = await startStack(); });
test.after(async () => { await stack.stop(); });

test("a wrong key is refused with a clear message and a right key opens the app", () => withDashboard(async (ui, key) => {
  signIn(ui, "ig_wrong");
  await waitFor(() => /not valid/.test(ui.status()), "an error for the wrong key");
  assert.strictEqual(ui.$("status").className, "error");
  assert.strictEqual(ui.$("app").hidden, true);
  signIn(ui, key);
  await waitFor(() => !ui.$("app").hidden, "app to open");
  assert.strictEqual(ui.$("keyform").hidden, true);
  assert.strictEqual(ui.$("key").value, "");
  assert.strictEqual(ui.window.sessionStorage.getItem("ig_key"), key);
}, { signedIn: false }));

test("the session survives a reload and signing out clears it", () => withDashboard(async (ui) => {
  ui.$("signout").click();
  assert.strictEqual(ui.window.sessionStorage.getItem("ig_key"), null);
}));

test("a clean invoice is read and nothing is queued", () => withDashboard(async (ui) => {
  choose(ui, [pdf("ALPHA")]);
  await waitFor(() => /1 file: 1 no problems\./.test(ui.status()), "the upload summary");
  await waitFor(() => ui.$("empty").hidden === false && ui.all("#list button").length === 0, "the empty review queue");
}));

test("a misread invoice is flagged, opened automatically, and its fields and original are shown", () => withDashboard(async (ui) => {
  choose(ui, [pdf("FLAG ALPHA")]);
  await waitFor(() => /1 need review/.test(ui.status()), "the upload summary");
  await waitFor(() => ui.all("#detail .issues li").length > 0, "the review detail to open");
  assert.match(ui.text("#list button"), /High priority/);
  assert.match(ui.text("#list button"), /Alpha Supplies/);
  assert.match(ui.text("#detail .issues li"), /4 x 25/);
  assert.strictEqual(ui.document.querySelector('[data-k="vendor_name"]').value, "Alpha Supplies");
  assert.strictEqual(ui.document.querySelector('[data-k="subtotal"]').value, "25");
  assert.strictEqual(ui.document.querySelector('[data-k="discount_amount"]').value, "");
  assert.strictEqual(ui.document.querySelector("#detail iframe").getAttribute("src"), "about:blank");
  await waitFor(() => ui.$("count").textContent === "(1)", "the review count");
}));

test("claiming needs a name, then marks the item in review", () => withDashboard(async (ui) => {
  choose(ui, [pdf("FLAG ALPHA")]);
  await waitFor(() => ui.$("claim"), "the claim button");
  ui.$("reviewer").value = "";
  ui.$("claim").click();
  await waitFor(() => /Enter your name/.test(ui.status()), "the name error");
  ui.$("reviewer").value = "Amina";
  ui.$("claim").click();
  await waitFor(() => ui.status() === "Claimed." && ui.$("release"), "the claim to complete");
  await waitFor(() => /in review/.test(ui.text("#list button")), "the list to show the item as in review");
  ui.$("release").click();
  await waitFor(() => ui.status() === "Released." && ui.$("claim"), "the release to complete");
}));

test("resolving needs a note, refuses a correction that is still wrong, and accepts a right one", () => withDashboard(async (ui) => {
  choose(ui, [pdf("FLAG ALPHA")]);
  await waitFor(() => ui.$("resolve"), "the resolve button");
  ui.$("resolve").click();
  await waitFor(() => /Write a short note/.test(ui.status()), "the note error");
  ui.$("note").value = "checked against the PDF";
  ui.$("resolve").click();
  await waitFor(() => /still has problems/.test(ui.status()), "the server's refusal");
  assert.strictEqual(ui.$("status").className, "error");
  input(ui, '[data-f="total_amount"]', "100");
  input(ui, '[data-k="subtotal"]', "100");
  ui.$("resolve").click();
  await waitFor(() => ui.status() === "Resolved with your corrections.", "the resolution");
  await waitFor(() => ui.all("#list button").length === 0, "the resolved item to leave the queue");
  assert.match(ui.text("#detail"), /Pick the next invoice/);
  ui.$("tab-all").click();
  await waitFor(() => /Checked by a person/.test(ui.text("#all-list button")), "the reviewed invoice in All invoices");
}));

test("an invoice can be rejected with a note", () => withDashboard(async (ui) => {
  choose(ui, [pdf("FLAG ALPHA")]);
  await waitFor(() => ui.$("reject"), "the reject button");
  ui.$("note").value = "not our invoice";
  ui.$("reject").click();
  await waitFor(() => ui.status() === "Rejected.", "the rejection");
  ui.$("tab-all").click();
  await waitFor(() => ui.all("#all-list button").length === 1, "the list");
  input(ui, "#statusfilter", "dismissed");
  ui.$("statusfilter").dispatchEvent(new ui.window.Event("change", { bubbles: true }));
  await waitFor(() => /Rejected/.test(ui.text("#all-list button")) && ui.all("#all-list button").length === 1, "the rejected filter");
}));

test("many files upload in parallel but never more than three at a time", () => withDashboard(async (ui) => {
  stack.provider.resetMax();
  const files = [1, 2, 3, 4, 5].map(() => pdf("ALPHA")).concat([pdf("FLAG ALPHA"), pdf("FLAG BETA")]);
  choose(ui, files);
  await waitFor(() => /7 files: 5 no problems, 2 need review\./.test(ui.status()), "the batch summary");
  assert.strictEqual(ui.all("#uploads li").length, 7);
  assert.ok(ui.all("#uploads li").every((li) => /: (no problems found|2 thing|1 thing)/.test(li.textContent) || /need checking/.test(li.textContent)));
  assert.ok(stack.provider.maxInflight() <= 3 && stack.provider.maxInflight() >= 2, "max in flight was " + stack.provider.maxInflight());
  await waitFor(() => ui.$("count").textContent === "(2)", "the review count to update");
  assert.strictEqual(ui.all("#detail .issues li").length, 0, "no automatic selection when several need review");
}));

test("the same file twice is recognised and costs nothing", () => withDashboard(async (ui) => {
  const file = pdf("ALPHA");
  choose(ui, [file]);
  await waitFor(() => /1 no problems/.test(ui.status()), "the first upload");
  const before = stack.provider.calls();
  choose(ui, [file]);
  await waitFor(() => /1 already processed/.test(ui.status()), "the second upload");
  assert.match(ui.text("#uploads li"), /already processed/);
  assert.strictEqual(stack.provider.calls(), before);
}));

test("All invoices pages, searches and filters", () => withDashboard(async (ui) => {
  const files = Array.from({ length: 27 }, (_, i) => pdf(i % 2 === 0 ? "ALPHA" : "BETA"));   // 14 Alpha, 13 Beta
  choose(ui, files);
  await waitFor(() => /27 files: 27 no problems\./.test(ui.status()), "the batch", 30000);
  ui.$("tab-all").click();
  await waitFor(() => ui.all("#all-list button").length === 25, "the first page");
  assert.strictEqual(ui.$("more").hidden, false);
  ui.$("more").click();
  await waitFor(() => ui.all("#all-list button").length === 27, "the second page");
  assert.strictEqual(ui.$("more").hidden, true);
  input(ui, "#q", "alpha");
  await waitFor(() => ui.all("#all-list button").length === 14 && /Alpha Supplies/.test(ui.text("#all-list button")), "the search result");
  assert.ok(!/Beta/.test(ui.text("#all-list button")));
  input(ui, "#q", "zzz-nobody");
  await waitFor(() => ui.all("#all-list button").length === 0 && !ui.$("all-empty").hidden, "the empty search message");
}));

test("a finished invoice opens read-only next to its original", () => withDashboard(async (ui) => {
  choose(ui, [pdf("ALPHA")]);
  await waitFor(() => /1 no problems/.test(ui.status()), "the upload");
  ui.$("tab-all").click();
  await waitFor(() => ui.all("#all-list button").length === 1, "the list");
  ui.all("#all-list button")[0].click();
  await waitFor(() => ui.all("#detail .badge").length === 1, "the detail");
  assert.strictEqual(ui.text("#detail .badge"), "No problems found");
  assert.match(ui.text("#detail dd"), /Alpha Supplies/);
  assert.match(ui.text("#detail dd"), /110/);
  assert.strictEqual(ui.$("resolve"), null);
  assert.strictEqual(ui.all("#detail iframe").length, 1);
}));

test("a flagged invoice opened from All invoices goes straight to review", () => withDashboard(async (ui) => {
  choose(ui, [pdf("FLAG ALPHA"), pdf("FLAG BETA")]);
  await waitFor(() => /2 need review/.test(ui.status()), "the batch");
  ui.$("tab-all").click();
  await waitFor(() => ui.all("#all-list button").length === 2, "the list");
  ui.all("#all-list button")[0].click();
  await waitFor(() => ui.$("resolve"), "the review detail");
  assert.ok(ui.all("#detail .issues li").length > 0);
}));

test("hostile text from an invoice is shown as text and never runs", () => withDashboard(async (ui) => {
  choose(ui, [pdf("XSS")]);
  await waitFor(() => /1 no problems/.test(ui.status()), "the upload");
  ui.$("tab-all").click();
  await waitFor(() => ui.all("#all-list button").length === 1, "the list");
  assert.match(ui.text("#all-list button"), /<img src=x onerror=/);
  ui.all("#all-list button")[0].click();
  await waitFor(() => ui.all("#detail dd").length > 0, "the detail");
  assert.match(ui.text("#detail dd"), /<img src=x onerror=/);
  assert.strictEqual(ui.all("img").length, 0);
  assert.strictEqual(ui.window.__pwned, undefined);
}));

test("exporting downloads a CSV", () => withDashboard(async (ui) => {
  choose(ui, [pdf("ALPHA")]);
  await waitFor(() => /1 no problems/.test(ui.status()), "the upload");
  ui.$("export").click();
  await waitFor(() => /Downloaded invoices\.csv/.test(ui.status()), "the export");
  assert.deepStrictEqual(ui.window.__downloads, ["invoices.csv"]);
}));

test("an unsupported file gets a plain explanation and the page stays usable", () => withDashboard(async (ui) => {
  choose(ui, [new File([Buffer.from("just some text")], "notes.txt", { type: "text/plain" })]);
  await waitFor(() => /not supported/.test(ui.text("#uploads li")), "the per-file message");
  await waitFor(() => ui.$("status").className === "error", "the error status");
  choose(ui, [pdf("ALPHA")]);
  await waitFor(() => /1 no problems/.test(ui.status()), "a following upload");
}));

test("more than 50 files are refused up front without sending anything", () => withDashboard(async (ui) => {
  const before = stack.provider.calls();
  choose(ui, Array.from({ length: 51 }, () => pdf("ALPHA")));
  await waitFor(() => /up to 50 files/.test(ui.status()), "the limit message");
  assert.strictEqual(stack.provider.calls(), before);
}));

test("the server log from all of this holds no keys and no invoice content", () => {
  const log = stack.log();
  for (const secret of ["sk-ant-ui-test", "Alpha Supplies", "Beta Traders", "inv-", "onerror"]) assert.ok(!log.includes(secret), "log leaked: " + secret);
});
