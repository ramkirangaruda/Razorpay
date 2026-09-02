// Vanilla JS, no build step, no framework - matches the rest of this repo's
// judge-facing pages (sim/render_trace.py etc.). Every number rendered here
// came from the server's real classify() -> score() -> evaluate() chain via
// /api/decide; this file only formats it.

const state = { cases: [], selected: null, bucket: "ALL", filter: "" };

const $ = (sel) => document.querySelector(sel);
const caseList = $("#caseList");
const caseDetail = $("#caseDetail");
const controls = $("#controls");
const pipeline = $("#pipeline");
const stageTemplate = $("#stageTemplate");

function inr(paise) {
  return "₹" + (paise).toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

async function loadCases() {
  const res = await fetch("/api/cases");
  state.cases = await res.json();
  renderCaseList();
}

function renderCaseList() {
  const q = state.filter.trim().toLowerCase();
  const rows = state.cases.filter((c) => {
    if (state.bucket !== "ALL" && c.bucket !== state.bucket) return false;
    if (!q) return true;
    const hay = `${c.issuer} ${c.error.reason || ""} ${c.error.source || ""} ${c.case_id}`.toLowerCase();
    return hay.includes(q);
  });

  caseList.innerHTML = "";
  for (const c of rows) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.className = "case-item" + (state.selected === c.case_id ? " selected" : "");
    btn.innerHTML = `
      <div class="row1">
        <span>${c.case_id}</span>
        <span class="bucket-pill" data-bucket="${c.bucket}">${c.bucket}</span>
      </div>
      <div class="row2">${c.issuer} · ${c.error.reason || "no reason"} · ${inr(c.amount_inr)}</div>
    `;
    btn.addEventListener("click", () => selectCase(c.case_id));
    li.appendChild(btn);
    caseList.appendChild(li);
  }
}

async function selectCase(caseId) {
  state.selected = caseId;
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

async function runDecision() {
  if (!state.selected) return;
  const btn = $("#runBtn");
  btn.disabled = true;
  btn.textContent = "Running…";
  pipeline.innerHTML = "";

  try {
    const res = await fetch("/api/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        case_id: state.selected,
        classifier: $("#classifierSelect").value,
        attempts_so_far: Number($("#attemptsInput").value) || 0,
        contacts_so_far: Number($("#contactsInput").value) || 0,
        execute_live: $("#liveCheckbox").checked,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      pipeline.innerHTML = `<div class="panel"><p class="skip-note">Request failed: ${err.detail || res.statusText}</p></div>`;
      return;
    }
    const data = await res.json();

    // Staggered reveal - the pipeline runs server-side in well under a
    // second, so this delay is purely presentational (each stage really did
    // already finish by the time we have `data`); it exists so a viewer can
    // read one stage before the next appears, not to fake latency.
    const stages = [renderL1(data.l1), renderL2b(data.l2b), renderL2a(data.l2a), renderL3(data.l3, data.l3_note)];
    stages.forEach((frag, i) => {
      setTimeout(() => pipeline.appendChild(frag), i * 220);
    });
  } finally {
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = "Run decision";
    }, stagesDelay());
  }
}

function stagesDelay() {
  return 4 * 220 + 150;
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
