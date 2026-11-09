"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const state = { key: sessionStorage.getItem("ig_key") || "", current: null, previewUrl: null, view: "review", all: { offset: 0, total: 0, token: 0 } };
  const MAX_FILES = 50, UPLOAD_PARALLEL = 3, PAGE = 25;
  const META = [["vendor_name", "Vendor"], ["invoice_number", "Invoice number"], ["issue_date", "Issue date (YYYY-MM-DD)"],
    ["due_date", "Due date (YYYY-MM-DD)"], ["currency", "Currency (3 letters)"]];
  const SUMMARY = [["subtotal", "Subtotal"], ["discount_amount", "Discount"], ["shipping_amount", "Shipping"], ["tax_amount", "Tax"],
    ["rounding_adjustment", "Rounding"], ["grand_total", "Total"]];
  const STATUS_TEXT = { clean: "No problems found", needs_review: "Needs review", reviewed: "Checked by a person", dismissed: "Rejected" };
  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

  function el(tag, props = {}, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v === true) node.setAttribute(k, "");
      else if (v !== false && v != null) node.setAttribute(k, v);
    }
    for (const kid of kids.flat()) if (kid != null) node.append(kid);
    return node;
  }

  function say(message, isError = false) {
    const s = $("status");
    s.textContent = message;
    s.className = isError ? "error" : "";
  }

  async function api(path, { method = "GET", json, body, raw = false } = {}) {
    const headers = { Authorization: "Bearer " + state.key };
    if (json !== undefined) headers["Content-Type"] = "application/json";
    const res = await fetch(path, { method, headers, body: json !== undefined ? JSON.stringify(json) : body });
    if (!res.ok) {
      let message = "Something went wrong (HTTP " + res.status + "). Try again.";
      try { const e = (await res.json()).error; message = e.message + " " + e.hint; } catch (_) { /* keep default */ }
      throw new Error(message);
    }
    return raw ? res : res.json();
  }

  async function run(fn) {
    try { await fn(); } catch (err) { say(err.message, true); }
  }

  function money(n, currency) {
    try { return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(n); }
    catch (_) { return Number(n).toFixed(2) + (currency ? " " + currency : ""); }
  }

  function toNumber(text, label) {
    const t = String(text).trim();
    if (t === "") return null;
    const n = Number(t);
    if (!Number.isFinite(n)) throw new Error("Enter a number for " + label + ".");
    return n;
  }
  const toText = (text) => (String(text).trim() === "" ? null : String(text).trim());
  const show = (v) => (v == null ? "" : String(v));

  // ---- views -------------------------------------------------------------------------------
  function setView(name) {
    state.view = name;
    for (const v of ["review", "all"]) {
      $("tab-" + v).setAttribute("aria-selected", String(v === name));
      $("panel-" + v).hidden = v !== name;
    }
    return name === "all" ? loadAll(true) : loadQueue();
  }

  // ---- review queue ------------------------------------------------------------------------
  async function loadQueue() {
    const { items } = await api("/v1/review-queue");
    $("count").textContent = items.length ? "(" + items.length + ")" : "";
    $("empty").hidden = items.length > 0;
    const list = $("list");
    list.replaceChildren();
    for (const it of items) {
      const amount = it.grand_total == null ? "No total" : money(it.grand_total, it.currency);
      const current = state.current && state.current.id === it.id;
      list.append(el("li", {}, el("button", { class: "item sev-" + it.severity, type: "button", "aria-current": String(!!current),
        onclick: () => run(() => select(it.id)) },
        el("span", { class: "who", text: it.vendor_name || "Unknown vendor" }),
        el("span", { class: "amt", text: amount }),
        el("span", { class: "sev", text: cap(it.severity) + " priority" + (it.status === "in_review" ? ", in review" : "") }),
        el("span", { class: "num", text: it.invoice_number ? "No. " + it.invoice_number : "No number" }))));
    }
  }

  // ---- all invoices ------------------------------------------------------------------------
  async function loadAll(reset) {
    if (reset) { state.all.offset = 0; $("all-list").replaceChildren(); }
    const token = ++state.all.token;
    const params = new URLSearchParams({ limit: String(PAGE), offset: String(state.all.offset) });
    if ($("q").value.trim()) params.set("q", $("q").value.trim());
    if ($("statusfilter").value) params.set("status", $("statusfilter").value);
    const data = await api("/v1/invoices?" + params);
    if (token !== state.all.token) return;   // a newer search replaced this one
    for (const rec of data.invoices) {
      const meta = rec.invoice.invoice_metadata || {}, summary = rec.invoice.financial_summary || {};
      $("all-list").append(el("li", {}, el("button", { class: "item st-" + rec.status, type: "button", onclick: () => run(() => openInvoice(rec.document_id)) },
        el("span", { class: "who", text: meta.vendor_name || "Unknown vendor" }),
        el("span", { class: "amt", text: summary.grand_total == null ? "No total" : money(summary.grand_total, meta.currency) }),
        el("span", { class: "sev", text: STATUS_TEXT[rec.status] || rec.status }),
        el("span", { class: "num", text: meta.invoice_number ? "No. " + meta.invoice_number : "No number" }))));
    }
    state.all.offset += data.invoices.length;
    state.all.total = data.total;
    $("all-empty").hidden = data.total > 0;
    $("more").hidden = state.all.offset >= data.total;
  }

  async function openInvoice(documentId) {
    const rec = await api("/v1/invoices/" + documentId);
    if (rec.status === "needs_review") {
      const { items } = await api("/v1/review-queue?limit=200");
      const item = items.find((i) => i.document_id === documentId);
      if (item) { await select(item.id); return; }
    }
    await showReadOnly(rec);
  }

  async function showReadOnly(rec) {
    state.current = null;
    const inv = rec.invoice, meta = inv.invoice_metadata || {}, summary = inv.financial_summary || {};
    const facts = el("dl", { class: "facts" });
    const add = (label, value) => { facts.append(el("dt", { text: label }), el("dd", { text: value == null || value === "" ? "Not read" : String(value) })); };
    for (const [k, label] of META) add(label, meta[k]);
    for (const [k, label] of SUMMARY) add(label, summary[k]);
    const body = el("tbody", {});
    for (const li of Array.isArray(inv.line_items) ? inv.line_items : []) {
      body.append(el("tr", {}, ...["description", "quantity", "unit_price", "total_amount"].map((f) => el("td", { text: show(li[f]) }))));
    }
    const table = el("table", {}, el("thead", {}, el("tr", {}, ...["Description", "Qty", "Unit price", "Total"].map((t) => el("th", { text: t })))), body);
    const root = el("div", {}, el("span", { class: "badge", text: STATUS_TEXT[rec.status] || rec.status }),
      rec.issues.length ? el("div", {}, el("h3", { text: "What was flagged" }), el("ul", { class: "issues" }, ...rec.issues.map((t) => el("li", { text: t })))) : null,
      el("h3", { text: "Details" }), facts, el("h3", { text: "Line items" }), table);
    $("detail").replaceChildren(el("div", { class: "split" }, await loadPreview(rec), root));
  }

  // ---- review detail -----------------------------------------------------------------------
  async function loadPreview(item) {
    if (state.previewUrl) { URL.revokeObjectURL(state.previewUrl); state.previewUrl = null; }
    const box = el("div", { class: "preview" });
    try {
      const res = await api("/v1/invoices/" + item.document_id + "/file", { raw: true });
      const blob = await res.blob();
      state.previewUrl = URL.createObjectURL(blob);
      const isPdf = (res.headers.get("content-type") || "").includes("pdf");
      box.append(isPdf ? el("iframe", { src: state.previewUrl, title: "Original invoice" })
                       : el("img", { src: state.previewUrl, alt: "Original invoice" }));
    } catch (_) { box.append(el("p", { class: "empty", text: "The original file is not available." })); }
    return box;
  }

  function itemRow(item = {}) {
    const input = (name, value) => el("input", { "data-f": name, value: show(value), "aria-label": name.replace("_", " ") });
    const row = el("tr", {},
      el("td", {}, input("description", item.description)), el("td", {}, input("quantity", item.quantity)),
      el("td", {}, input("unit_price", item.unit_price)), el("td", {}, input("total_amount", item.total_amount)),
      el("td", {}, el("button", { class: "btn small", type: "button", text: "Remove", onclick: () => row.remove() })));
    return row;
  }

  function collect(root) {
    const get = (name) => root.querySelector('[data-k="' + name + '"]').value;
    const meta = {};
    for (const [k] of META) meta[k] = toText(get(k));
    const summary = {};
    for (const [k, label] of SUMMARY) summary[k] = toNumber(get(k), label);
    const items = [...root.querySelectorAll("tbody tr")].map((tr, i) => {
      const f = (n) => tr.querySelector('[data-f="' + n + '"]').value;
      return { description: toText(f("description")), quantity: toNumber(f("quantity"), "quantity on line " + (i + 1)),
        unit_price: toNumber(f("unit_price"), "unit price on line " + (i + 1)), total_amount: toNumber(f("total_amount"), "total on line " + (i + 1)) };
    });
    return { invoice_metadata: meta, line_items: items, financial_summary: summary, ocr_warnings: [] };
  }

  async function select(id) {
    const item = await api("/v1/review-queue/" + id);
    state.current = item;
    const inv = item.invoice;
    const root = el("div", {});
    const fields = el("div", { class: "fields" });
    for (const [k, label] of META) fields.append(el("label", {}, label, el("input", { "data-k": k, value: show(inv.invoice_metadata[k]) })));
    for (const [k, label] of SUMMARY) fields.append(el("label", {}, label, el("input", { "data-k": k, inputmode: "decimal", value: show(inv.financial_summary[k]) })));
    const body = el("tbody", {});
    for (const li of Array.isArray(inv.line_items) ? inv.line_items : []) body.append(itemRow(li));
    const table = el("table", {}, el("thead", {}, el("tr", {}, ...["Description", "Qty", "Unit price", "Total", ""].map((t) => el("th", { text: t })))), body);

    const reviewer = el("input", { id: "reviewer", value: sessionStorage.getItem("ig_name") || item.assignee || "" });
    const note = el("textarea", { id: "note", rows: "3" });
    const finish = (label, fn) => (() => run(async () => { await fn(); say(label); state.current = null; $("detail").replaceChildren(el("p", { class: "empty", text: "Pick the next invoice from the list." })); await refreshLists(); }));
    const requireNote = () => { if (!note.value.trim()) throw new Error("Write a short note saying what you checked."); return note.value.trim(); };
    const post = (action, json) => api("/v1/review-queue/" + id + "/" + action, { method: "POST", json });

    const actions = el("div", { class: "actions" },
      el("label", {}, "Your name", reviewer), el("label", {}, "Note (required to resolve or reject)", note),
      el("div", { class: "row" },
        item.status === "open" ? el("button", { id: "claim", class: "btn", type: "button", text: "Claim this invoice", onclick: () => run(async () => {
          if (!reviewer.value.trim()) throw new Error("Enter your name to claim this invoice.");
          sessionStorage.setItem("ig_name", reviewer.value.trim());
          await post("claim", { assignee: reviewer.value.trim() }); say("Claimed."); await select(id); }) }) : null,
        item.status === "in_review" ? el("button", { id: "release", class: "btn", type: "button", text: "Release", onclick: () => run(async () => { await post("release"); say("Released."); await select(id); }) }) : null,
        el("button", { id: "resolve", class: "btn primary", type: "button", text: "Save corrections and resolve", onclick: finish("Resolved with your corrections.", () => post("resolve", { note: requireNote(), corrected_invoice: collect(root) })) }),
        el("button", { id: "approve", class: "btn", type: "button", text: "Approve as extracted", onclick: finish("Approved as extracted.", () => post("resolve", { note: requireNote() })) }),
        el("button", { id: "reject", class: "btn danger", type: "button", text: "Reject invoice", onclick: finish("Rejected.", () => post("dismiss", { note: requireNote() })) })));

    root.append(
      el("h3", { text: "What needs checking" }),
      el("ul", { class: "issues" }, ...item.issues.map((t) => el("li", { text: t }))),
      fields, el("h3", { text: "Line items" }), table,
      el("button", { class: "btn small", type: "button", text: "Add line", onclick: () => body.append(itemRow()) }), actions);

    const preview = await loadPreview(item);
    $("detail").replaceChildren(el("div", { class: "split" }, preview, root));
    await loadQueue();
  }

  async function refreshLists() {
    await loadQueue();
    if (state.view === "all") await loadAll(true);
  }

  // ---- upload, export, sign in ------------------------------------------------------------
  async function uploadMany(files) {
    if (files.length > MAX_FILES) { say("Choose up to " + MAX_FILES + " files at a time.", true); return; }
    const list = $("uploads");
    list.replaceChildren();
    const rows = files.map((f) => { const li = el("li", { text: f.name + ": waiting" }); list.append(li); return { f, li }; });
    const tally = { clean: 0, needs_review: 0, cached: 0, failed: 0 };
    let lastFlagged = null;
    const pending = rows.slice();
    const worker = async () => {
      for (let row = pending.shift(); row; row = pending.shift()) {
        row.li.textContent = row.f.name + ": reading...";
        try {
          const form = new FormData();
          form.append("file", row.f);
          const r = await api("/v1/invoices", { method: "POST", body: form });
          tally[r.cached ? "cached" : r.status] += 1;
          if (!r.cached && r.status === "needs_review") lastFlagged = r.review_item_id;
          row.li.textContent = row.f.name + ": " + (r.cached ? "already processed" : r.status === "clean" ? "no problems found" : r.issues.length + " thing(s) need checking");
        } catch (err) {
          tally.failed += 1;
          row.li.textContent = row.f.name + ": " + err.message;
          row.li.className = "error";
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(UPLOAD_PARALLEL, rows.length) }, worker));
    const parts = [["clean", "no problems"], ["needs_review", "need review"], ["cached", "already processed"], ["failed", "failed"]]
      .filter(([k]) => tally[k]).map(([k, label]) => tally[k] + " " + label);
    say(files.length + " file" + (files.length === 1 ? "" : "s") + ": " + parts.join(", ") + ".", tally.failed > 0);
    await refreshLists();
    if (tally.needs_review === 1 && lastFlagged) await select(lastFlagged);
  }

  async function exportCsv() {
    const res = await api("/v1/export.csv", { raw: true });
    const url = URL.createObjectURL(await res.blob());
    const a = el("a", { href: url, download: "invoices.csv" });
    document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    say("Downloaded invoices.csv with checked and reviewed invoices.");
  }

  async function enter() {
    await api("/v1/settings");
    sessionStorage.setItem("ig_key", state.key);
    $("keyform").hidden = true; $("signout").hidden = false; $("app").hidden = false;
    say("");
    await setView("review");
  }

  $("keyform").addEventListener("submit", (e) => { e.preventDefault(); state.key = $("key").value.trim(); $("key").value = ""; run(enter); });
  $("signout").addEventListener("click", () => { sessionStorage.clear(); state.key = ""; location.reload(); });
  $("upload").addEventListener("click", () => $("file").click());
  $("file").addEventListener("change", (e) => { const files = [...e.target.files]; e.target.value = ""; if (files.length) run(() => uploadMany(files)); });
  $("export").addEventListener("click", () => run(exportCsv));
  $("tab-review").addEventListener("click", () => run(() => setView("review")));
  $("tab-all").addEventListener("click", () => run(() => setView("all")));
  let timer;
  $("q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => run(() => loadAll(true)), 300); });
  $("statusfilter").addEventListener("change", () => run(() => loadAll(true)));
  $("more").addEventListener("click", () => run(() => loadAll(false)));
  if (state.key) run(enter).then(() => { if ($("app").hidden) { state.key = ""; sessionStorage.removeItem("ig_key"); } });
})();
