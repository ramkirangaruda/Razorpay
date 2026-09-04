// Vanilla JS, no build step, no framework - matches the rest of this repo's
// judge-facing pages (sim/render_trace.py etc.). Every number rendered here
// came from the server's real classify() -> score() -> evaluate() chain via
// /api/decide; this file only formats it.

const state = { cases: [], selected: null, bucket: "ALL", filter: "", mode: "preset", reasons: [], issuers: [] };

const $ = (sel) => document.querySelector(sel);
const caseList = $("#caseList");
const caseDetail = $("#caseDetail");
const caseCount = $("#caseCount");
const controls = $("#controls");
const pipeline = $("#pipeline");
const stageTemplate = $("#stageTemplate");

const BUCKET_ORDER = ["CLEAN", "AMBIGUOUS", "CONTEXT"];
const BUCKET_LABEL = { CLEAN: "Clean", AMBIGUOUS: "Ambiguous", CONTEXT: "Context" };

function inr(paise) {
  return "₹" + (paise).toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

async function loadCases() {
  const res = await fetch("/api/cases");
  state.cases = await res.json();
  renderCaseList();
}

async function loadReasons() {
  const res = await fetch("/api/reasons");
  const data = await res.json();
  state.reasons = data.known_reasons;
  state.issuers = data.issuers;
}

function caseCardHTML(c) {
  return `
    <div class="row1">
      <span class="case-id">${c.case_id}</span>
      <span class="bucket-pill" data-bucket="${c.bucket}">${c.bucket}</span>
    </div>
    <div class="row2">${c.issuer} · ${c.error.reason || "no reason"}</div>
    <div class="row3">${inr(c.amount_inr)}</div>
  `;
}

function renderCaseList() {
  const q = state.filter.trim().toLowerCase();
  const rows = state.cases.filter((c) => {
    if (state.bucket !== "ALL" && c.bucket !== state.bucket) return false;
    if (!q) return true;
    const hay = `${c.issuer} ${c.error.reason || ""} ${c.error.source || ""} ${c.case_id}`.toLowerCase();
    return hay.includes(q);
  });

  caseCount.textContent = `${rows.length}`;
  caseList.innerHTML = "";

  const customLi = document.createElement("li");
  const customBtn = document.createElement("button");
  customBtn.className = "case-item custom-case-card" + (state.mode === "custom" ? " selected" : "");
  customBtn.innerHTML = `
    <div class="row1"><span class="custom-plus">+</span><span>Build your own case</span></div>
    <div class="row2">Set the payload yourself, run it through the real pipeline</div>
  `;
  customBtn.addEventListener("click", startCustomCase);
  customLi.appendChild(customBtn);
  caseList.appendChild(customLi);

  function renderRow(c) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.className = "case-item" + (state.mode === "preset" && state.selected === c.case_id ? " selected" : "");
    btn.innerHTML = caseCardHTML(c);
    btn.addEventListener("click", () => selectCase(c.case_id));
    li.appendChild(btn);
    caseList.appendChild(li);
  }

  if (state.bucket === "ALL") {
    for (const b of BUCKET_ORDER) {
      const group = rows.filter((c) => c.bucket === b);
      if (!group.length) continue;
      const header = document.createElement("li");
      header.className = "case-group-label";
      header.innerHTML = `<span>${BUCKET_LABEL[b]}</span><span class="case-group-count">${group.length}</span>`;
      caseList.appendChild(header);
      group.forEach(renderRow);
    }
  } else {
    rows.forEach(renderRow);
  }
}

function classifierOptionsFor(mode) {
  const sel = $("#classifierSelect");
  const modelOpt = sel.querySelector('option[value="model"]');
  if (mode === "custom") {
    modelOpt.disabled = true;
    modelOpt.textContent = "LLM (recorded) — unavailable for custom cases";
    sel.value = "table";
  } else {
    modelOpt.disabled = false;
    modelOpt.textContent = "LLM (recorded)";
  }
}

async function selectCase(caseId) {
  state.mode = "preset";
  state.selected = caseId;
  classifierOptionsFor("preset");
  renderCaseList();
  pipeline.innerHTML = "";

  const res = await fetch(`/api/cases/${encodeURIComponent(caseId)}`);
  const c = await res.json();

  caseDetail.innerHTML = `
    <h3>${c.case_id} <span class="bucket-pill" data-bucket="${c.bucket}">${c.bucket}</span></h3>
    <p class="sub">${c.kind} invoice, ${inr(c.amount_inr)}, ${c.instrument_type} via ${c.issuer}</p>
    <div class="kv-grid">
      <div class="kv"><label>Error code</label><span>${c.error.code}</span></div>
      <div class="kv"><label>Source</label><span>${c.error.source}</span></div>
      <div class="kv"><label>Step</label><span>${c.error.step}</span></div>
      <div class="kv"><label>Reason</label><span>${c.error.reason ?? "null"}</span></div>
      <div class="kv"><label>Prior successful payments</label><span>${c.customer.successful_payments}</span></div>
      <div class="kv"><label>Prior failures (90d)</label><span>${c.customer.prior_failures_90d}</span></div>
    </div>
    <div class="error-box">${c.error.description}</div>
    ${c.ambiguity.length ? `<div class="ambiguity-flags">${c.ambiguity.map((f) => `<span>${f}</span>`).join("")}</div>` : ""}
  `;
  controls.hidden = false;
}

function startCustomCase() {
  state.mode = "custom";
  state.selected = null;
  classifierOptionsFor("custom");
  renderCaseList();
  pipeline.innerHTML = "";

  const reasonOptions = state.reasons
    .map((r) => `<option value="${r.reason}">${r.reason} → ${r.class}</option>`)
    .join("");
  const issuerOptions = state.issuers.map((i) => `<option value="${i}">${i}</option>`).join("");

  caseDetail.innerHTML = `
    <h3>Build your own case</h3>
    <p class="sub">This runs through the exact same classify() → score() → evaluate() chain as
      every seeded case — nothing here is a separate mock path.</p>
    <div class="custom-form">
      <label class="field">
        <span>Reason</span>
        <select id="cfReason">
          <option value="">— none / unmapped (tests the table's fallback) —</option>
          ${reasonOptions}
        </select>
      </label>
      <label class="field">
        <span>Source</span>
        <select id="cfSource">
          <option value="bank">bank</option>
          <option value="customer">customer</option>
          <option value="business">business</option>
          <option value="gateway">gateway</option>
          <option value="razorpay">razorpay</option>
          <option value="network">network</option>
        </select>
      </label>
      <label class="field">
        <span>Step</span>
        <select id="cfStep">
          <option value="payment_initiation">payment_initiation</option>
          <option value="payment_authentication">payment_authentication</option>
          <option value="payment_authorization" selected>payment_authorization</option>
          <option value="payment_capture">payment_capture</option>
        </select>
      </label>
      <label class="field wide">
        <span>Description</span>
        <input id="cfDescription" type="text" value="Payment failed" maxlength="120">
      </label>
      <label class="field">
        <span>Invoice kind</span>
        <select id="cfKind">
          <option value="ONE_TIME">One-time</option>
          <option value="RECURRING">Recurring (e-mandate)</option>
        </select>
      </label>
      <label class="field number">
        <span>Amount (₹)</span>
        <input id="cfAmount" type="number" min="1" step="1" value="1000">
      </label>
      <label class="field">
        <span>Issuer</span>
        <select id="cfIssuer">${issuerOptions}</select>
      </label>
      <label class="field">
        <span>Instrument</span>
        <select id="cfInstrument">
          <option value="card">card</option>
          <option value="upi">upi</option>
          <option value="netbanking">netbanking</option>
        </select>
      </label>
      <label class="field">
        <span>Mastercard advice code</span>
        <select id="cfMac">
          <option value="">none</option>
          <option value="03">03 — do not honour</option>
          <option value="21">21 — lost/stolen</option>
          <option value="24">24</option>
          <option value="25">25</option>
          <option value="26">26</option>
          <option value="27">27</option>
        </select>
      </label>
      <label class="field number">
        <span>Prior successful payments</span>
        <input id="cfSuccesses" type="number" min="0" value="0">
      </label>
      <label class="field number">
        <span>Prior failures (90d)</span>
        <input id="cfFailures" type="number" min="0" value="0">
      </label>
    </div>
  `;
  controls.hidden = false;
}

function stageEl(badgeClass, badgeText, title, sub) {
  const node = stageTemplate.content.cloneNode(true);
  const article = node.querySelector(".stage");
  const badge = node.querySelector(".stage-badge");
  badge.className = "stage-badge " + badgeClass;
  badge.textContent = badgeText;
  node.querySelector(".stage-title").textContent = title;
  node.querySelector(".stage-sub").textContent = sub || "";
  return { fragment: node, article, body: node.querySelector(".stage-body") };
}

function renderL1(l1) {
  const { fragment, body } = stageEl("l1", "L1", "Classification", `via ${l1.classifier === "model" ? "LLM (recorded)" : "decision table"}`);
  body.innerHTML = `
    <div class="kv-grid">
      <div class="kv"><label>Class</label><span>${l1.classification}</span></div>
      <div class="kv"><label>Confidence</label><span>${l1.confidence}</span></div>
      <div class="kv"><label>Recovery bucket</label><span>${l1.recovery_bucket}</span></div>
      <div class="kv"><label>Proposed action</label><span>${l1.proposed_action}</span></div>
    </div>
    <p class="rationale">${l1.rationale}</p>
    ${l1.ambiguity_flags.length ? `<div class="ambiguity-flags">${l1.ambiguity_flags.map((f) => `<span>${f}</span>`).join("")}</div>` : ""}
  `;
  return fragment;
}

function renderL2b(l2b) {
  const { fragment, body } = stageEl("l2b", "L2b", "Expected value", l2b.downgraded ? "narrowed the proposal" : "kept the proposal");
  const rows = l2b.scores
    .map((s) => {
      const cls = s.action === l2b.chosen ? "chosen" : "";
      const termsLine = s.terms.map(([label, v]) => `${label}: ${v.toFixed(2)}`).join("  ·  ");
      return `<tr class="${cls} ev-row">
        <td>${s.action}</td>
        <td class="num">${s.ev.toFixed(2)}</td>
      </tr>
      <tr class="${cls} terms-row"><td colspan="2" class="terms-sub">${termsLine}</td></tr>`;
    })
    .join("");
  body.innerHTML = `<table class="ev-table"><thead><tr><th>action</th><th>EV (₹)</th></tr></thead><tbody>${rows}</tbody></table>`;
  return fragment;
}

function renderL2a(l2a) {
  const badgeCls = l2a.vetoed ? "veto" : l2a.downgraded ? "downgrade" : "pass";
  const badgeText = l2a.vetoed ? "VETO" : l2a.downgraded ? "DOWNGRADE" : "PASS";
  const { fragment, body } = stageEl(`l2a ${badgeCls}`, badgeText, "Policy gate", `permitted: ${l2a.permitted_action}`);
  const rules = l2a.rules_fired.length
    ? `<ul class="rules-fired">${l2a.rules_fired
        .map(
          (r) => `<li class="${r.basis}">
            <span class="rule-name">${r.rule}</span>
            <span class="rule-basis">${r.basis}</span>
            <span>${r.note}</span>
          </li>`
        )
        .join("")}</ul>`
    : `<p class="skip-note">No rule fired - the proposal passed the gate unchanged.</p>`;
  body.innerHTML = rules;
  return fragment;
}

function renderL3(l3, l3Note) {
  if (!l3) {
    const { fragment, body } = stageEl("l3 skip", "L3", "Execution", "not reached");
    body.innerHTML = `<p class="skip-note">${l3Note || "No execution attempted."}</p>`;
    return fragment;
  }
  const pill = l3.live ? `<span class="live-pill">LIVE</span>` : `<span class="fake-pill">FAKE (no network call)</span>`;
  const { fragment, body } = stageEl("l3", "L3", "Execution", l3.outcome);
  body.innerHTML = `
    <p>${pill}</p>
    <div class="l3-fields">
      <div class="kv"><label>Outcome</label><span>${l3.outcome}</span></div>
      <div class="kv"><label>Order / link ID</label><span>${l3.razorpay_order_id ?? "—"}</span></div>
      <div class="kv"><label>Payment ID</label><span>${l3.razorpay_payment_id ?? "—"}</span></div>
      <div class="kv"><label>Replayed</label><span>${l3.replayed}</span></div>
    </div>
    ${l3.error ? `<div class="error-box">${JSON.stringify(l3.error, null, 2)}</div>` : ""}
  `;
  return fragment;
}

// The connector between two stage cards - a dot travelling down a short
// track, self-playing via CSS the moment it's inserted (see flow-travel in
// style.css), labelled with the actual verb from the one-way-valve
// invariant (proposes / narrows / permits / records) rather than a bare
// arrow, so the animation carries the same claim the architecture doc makes.
function connectorEl(label) {
  const div = document.createElement("div");
  div.className = "stage-connector";
  div.innerHTML = `<span class="flow-track"><span class="flow-dot"></span></span><span class="connector-label">${label}</span>`;
  return div;
}

const CONNECTOR_LABELS = ["proposes ↓", "narrows, never widens ↓", "permits ↓", "records ↓"];
const FLOW_STEP = 260; // ms between each stage/connector entering

function renderAudit(entries) {
  const { fragment, body } = stageEl("audit", "LOG", "Audit trail", `${entries.length} row${entries.length === 1 ? "" : "s"} · append-only`);
  if (!entries.length) {
    body.innerHTML = `<p class="skip-note">No rows written.</p>`;
    return fragment;
  }
  const rows = entries
    .map(
      (e) => `<li>
        <span class="audit-actor">${e.actor}</span>
        <span class="audit-event">${e.event_type}</span>
        ${e.rule_name ? `<span class="audit-rule">${e.rule_name}</span>` : ""}
        <span class="audit-time">${new Date(e.created_at + "Z").toLocaleTimeString()}</span>
      </li>`
    )
    .join("");
  body.innerHTML = `
    <ul class="audit-list">${rows}</ul>
    <p class="skip-note">Written by <code>app.audit.AuditLog</code> to a real Customer/Invoice/Attempt row for
    this decision - not a log line, an actual append-only table. See app/audit.py.</p>
  `;
  return fragment;
}

function buildDecideBody() {
  const base = {
    classifier: $("#classifierSelect").value,
    attempts_so_far: Number($("#attemptsInput").value) || 0,
    contacts_so_far: Number($("#contactsInput").value) || 0,
    execute_live: $("#liveCheckbox").checked,
  };
  if (state.mode === "custom") {
    base.custom_case = {
      reason: $("#cfReason").value || null,
      description: $("#cfDescription").value || "Payment failed",
      source: $("#cfSource").value,
      step: $("#cfStep").value,
      amount_inr: Number($("#cfAmount").value) || 0,
      kind: $("#cfKind").value,
      issuer: $("#cfIssuer").value,
      instrument_type: $("#cfInstrument").value,
      mastercard_advice_code: $("#cfMac").value || null,
      successful_payments: Number($("#cfSuccesses").value) || 0,
      prior_failures_90d: Number($("#cfFailures").value) || 0,
    };
  } else {
    base.case_id = state.selected;
  }
  return base;
}

async function runDecision() {
  if (state.mode === "preset" && !state.selected) return;
  const btn = $("#runBtn");
  btn.disabled = true;
  btn.textContent = "Running…";
  pipeline.innerHTML = "";

  try {
    const res = await fetch("/api/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildDecideBody()),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      pipeline.innerHTML = `<div class="panel"><p class="skip-note">Request failed: ${err.detail || res.statusText}</p></div>`;
      return;
    }
    const data = await res.json();
    const auditEntries = await fetch(`/api/audit/${encodeURIComponent(data.invoice_id)}`).then((r) => r.json());

    // Staggered reveal, stages and connectors interleaved - the pipeline runs
    // server-side in well under a second, so this delay is purely
    // presentational (every stage already finished by the time we have
    // `data`); it exists so a viewer can watch the decision move down through
    // each layer instead of seeing five cards pop in at once.
    const stageNodes = [
      renderL1(data.l1),
      renderL2b(data.l2b),
      renderL2a(data.l2a),
      renderL3(data.l3, data.l3_note),
      renderAudit(auditEntries),
    ];
    let slot = 0;
    stageNodes.forEach((frag, i) => {
      setTimeout(() => pipeline.appendChild(frag), slot * FLOW_STEP);
      slot++;
      if (i < CONNECTOR_LABELS.length) {
        const label = CONNECTOR_LABELS[i];
        setTimeout(() => pipeline.appendChild(connectorEl(label)), slot * FLOW_STEP);
        slot++;
      }
    });
  } finally {
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = "Run decision";
    }, stagesDelay());
  }
}

function stagesDelay() {
  return 9 * FLOW_STEP + 250;
}

$("#filter").addEventListener("input", (e) => {
  state.filter = e.target.value;
  renderCaseList();
});

$("#bucketTabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (!btn) return;
  document.querySelectorAll("#bucketTabs .tab").forEach((t) => t.classList.remove("active"));
  btn.classList.add("active");
  state.bucket = btn.dataset.bucket;
  renderCaseList();
});

$("#runBtn").addEventListener("click", runDecision);

loadCases();
loadReasons();

// ---------------------------------------------------------------------
// Story intro - a guided walkthrough shown once before the console
// unlocks. Purely presentational state, kept separate from `state`
// above (which is the console's own data). Progress persists in
// localStorage so a returning viewer isn't forced through it again;
// on first load `body` already carries the `story-mode` class from the
// HTML itself, so there is no flash of the console before this runs.
// ---------------------------------------------------------------------

const storySlides = Array.from(document.querySelectorAll(".story-slide"));
const storyDots = Array.from(document.querySelectorAll(".story-dot"));
const storyBack = $("#storyBack");
const storyNext = $("#storyNext");
let storyIndex = 0;

function showStorySlide(i) {
  storyIndex = Math.max(0, Math.min(storySlides.length - 1, i));
  storySlides.forEach((s, idx) => s.classList.toggle("active", idx === storyIndex));
  storyDots.forEach((d, idx) => d.classList.toggle("active", idx === storyIndex));
  storyBack.style.visibility = storyIndex === 0 ? "hidden" : "visible";
  storyNext.textContent = storyIndex === storySlides.length - 1 ? "Enter the console →" : "Next →";
}

function endStory() {
  document.body.classList.remove("story-mode");
  try { localStorage.setItem("backstop_story_seen", "1"); } catch (e) { /* private mode etc. */ }
}

if (storySlides.length) {
  let alreadySeen = false;
  try { alreadySeen = localStorage.getItem("backstop_story_seen") === "1"; } catch (e) { /* ignore */ }

  if (alreadySeen) {
    document.body.classList.remove("story-mode");
  } else {
    showStorySlide(0);
  }

  storyNext.addEventListener("click", () => {
    if (storyIndex === storySlides.length - 1) endStory();
    else showStorySlide(storyIndex + 1);
  });
  storyBack.addEventListener("click", () => showStorySlide(storyIndex - 1));
  $("#storySkip").addEventListener("click", endStory);
  $("#storySkip").addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") endStory();
  });

  document.addEventListener("keydown", (e) => {
    if (!document.body.classList.contains("story-mode")) return;
    if (e.key === "ArrowRight") storyNext.click();
    if (e.key === "ArrowLeft" && storyIndex > 0) storyBack.click();
    if (e.key === "Escape") endStory();
  });
}
