"""
The decision trace renderer — build priority #1 among the judge-facing artifacts.

One invoice, from failure payload to outcome, with every intermediate decision
shown and every number in the expected value broken out as its own line. This
is the concrete answer to "every money action explainable, bounded and gated",
and it is more persuasive than any architecture diagram because a reader can
check it rather than believe it.

Static HTML with inlined CSS. No framework, no build step, no CDN — the
simulation run emits the file and it opens from disk. A dashboard that needs
`npm install` before a judge can look at it is a dashboard the judge does not
look at.

Usage:
    python -m sim.render_trace --n 120 --seed 42 --out docs/results/trace.html
"""

from __future__ import annotations

import argparse
import html
import json
import os
from dataclasses import asdict
from datetime import datetime

from app.classifier import LookupClassifier
from app.models import FailureClass, InterventionAction
from app.policy import L1Proposal, evaluate
from app.rule_basis import basis_of, citation_of
from app.scorer import Beliefs, ScoreContext, score
from app.stopping_rules import IST
from sim.generate_batch import generate_batch
from sim.run_arms import BackstopArm, InvoiceState, WorldParams, simulate

A = InterventionAction

CSS = """
:root{--bg:#f7f7f5;--fg:#1a1a18;--mut:#6b6b66;--line:#dcdcd6;--card:#fff;
--pos:#1f7a4d;--neg:#a3341f;--reg:#7a4a1f;--back:#3a4a7a;--accent:#2b2b28}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:40px 24px 80px}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:15px;margin:36px 0 12px;text-transform:uppercase;
letter-spacing:.08em;color:var(--mut);font-weight:600}
.sub{color:var(--mut);margin:0 0 28px}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;
padding:18px 20px;margin:0 0 14px}
.kv{display:grid;grid-template-columns:190px 1fr;gap:4px 16px;font-size:13px}
.kv dt{color:var(--mut)}
.kv dd{margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.step{border-left:3px solid var(--line);padding:0 0 0 18px;margin:0 0 22px;position:relative}
.step.vetoed{border-left-color:var(--neg)}
.step.acted{border-left-color:var(--pos)}
.step .t{font-size:12px;color:var(--mut);font-family:ui-monospace,monospace}
.tag{display:inline-block;padding:1px 7px;border-radius:3px;font-size:11px;
font-weight:600;letter-spacing:.04em;vertical-align:1px}
.tag.reg{background:#f4e8da;color:var(--reg)}
.tag.back{background:#e3e8f5;color:var(--back)}
.tag.ok{background:#dff0e6;color:var(--pos)}
.tag.no{background:#f7e0da;color:var(--neg)}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:8px 0 0}
th,td{text-align:right;padding:5px 9px;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
td.num{font-family:ui-monospace,monospace}
tr.win td{background:#f2f8f4;font-weight:600}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.ev{font-family:ui-monospace,monospace;font-size:12.5px}
.note{color:var(--mut);font-size:12.5px;margin:6px 0 0;font-style:italic}
.rule{background:#fdf6f3;border:1px solid #f0dcd4;border-radius:5px;
padding:10px 13px;margin:8px 0 0;font-size:12.5px}
.rule code{font-size:12px}
.cite{color:var(--mut);font-size:11.5px;margin-top:3px}
.out{border:1px solid var(--line);border-left:3px solid var(--accent);
background:var(--card);padding:14px 18px;border-radius:0 6px 6px 0}
.gt{background:#f0f0ec;border:1px dashed var(--line);border-radius:5px;
padding:12px 15px;font-size:12.5px;margin-top:10px}
.gt b{font-weight:600}
code{background:#eeeeea;padding:1px 4px;border-radius:3px;
font-family:ui-monospace,monospace}
.foot{color:var(--mut);font-size:12px;margin-top:40px;border-top:1px solid var(--line);
padding-top:14px}
@media(prefers-color-scheme:dark){
:root{--bg:#161614;--fg:#ecebe6;--mut:#9b9a92;--line:#33322d;--card:#1e1e1b;
--pos:#5fbd88;--neg:#e08469;--reg:#d6a476;--back:#8fa4dd;--accent:#c9c8c0}
.tag.reg{background:#3a2c1c;color:#e0b27f}.tag.back{background:#242c42;color:#9fb3e8}
.tag.ok{background:#1d3327;color:#7fce9d}.tag.no{background:#3a231c;color:#eb9b80}
tr.win td{background:#1d2721}.rule{background:#2a1e1a;border-color:#43302a}
.gt{background:#222220}code{background:#2a2a26}}
"""


def esc(x) -> str:
    return html.escape(str(x))


def money(x: float) -> str:
    cls = "pos" if x > 0 else ("neg" if x < 0 else "")
    return f'<span class="{cls}">{x:+,.2f}</span>'


def render_case(case: dict, beliefs: Beliefs) -> str:
    """
    Re-run one invoice through the real pipeline, capturing every intermediate.

    Deliberately calls the same classifier, scorer and gate the simulation uses
    rather than reproducing their logic for display. A renderer that
    reimplements the decision is a renderer that can disagree with it, and then
    the artifact showing how the system works stops being evidence about the
    system.
    """
    clf = LookupClassifier()
    world = WorldParams()
    arm = BackstopArm(clf, beliefs)
    st = InvoiceState(case=case, now=datetime.fromisoformat(case["failed_at"]))

    out: list[str] = []
    err = case["error"]
    gt = case["ground_truth"]
    cust = case["customer"]

    out.append('<div class="card"><h2 style="margin-top:0">Failure payload</h2>')
    out.append('<dl class="kv">')
    for k in ("code", "description", "source", "step", "reason"):
        v = err.get(k)
        shown = "null" if v is None else esc(v)
        out.append(f"<dt>error.{k}</dt><dd>{shown}</dd>")
    out.append(f'<dt>amount</dt><dd>Rs {case["amount_paise"]/100:,.2f}</dd>')
    out.append(f'<dt>kind</dt><dd>{esc(case["kind"])}</dd>')
    out.append(f'<dt>issuer / instrument</dt><dd>{esc(case["issuer"])} / {esc(case["instrument_type"])}</dd>')
    mac = case.get("mastercard_advice_code")
    out.append(f'<dt>mastercard advice code</dt><dd>{esc(mac) if mac else "absent — itself a signal"}</dd>')
    out.append(f'<dt>customer</dt><dd>{esc(cust["archetype"])}, tenure {cust["tenure_days"]}d, '
               f'{cust["successful_payments"]} successful payments, '
               f'{cust["prior_failures_90d"]} prior failures/90d, '
               f'{cust["prior_contacts_30d"]} contacts/30d</dd>')
    if case.get("ambiguity"):
        out.append(f'<dt>ambiguity</dt><dd>{esc(", ".join(case["ambiguity"]))}</dd>')
    out.append("</dl></div>")

    # ---- the decision loop, instrumented ----
    from datetime import timedelta

    deadline = st.now + timedelta(days=30)
    if case["kind"] == "RECURRING":
        st.predebit_notice_sent_at = st.now - timedelta(hours=30)

    import random as _r
    rng = _r.Random(42)
    from sim.run_arms import ArmResult, _execute, _policy_ctx, _utc

    res = ArmResult(arm="trace")
    step_no = 0

    while not st.closed and st.now < deadline and step_no < 12:
        step_no += 1
        c = clf.classify(case, {"attempts": st.attempts, "contacts": st.contacts})
        sctx = ScoreContext(
            invoice_value_inr=st.amount_inr,
            recovery_bucket=c.recovery_bucket,
            failure_class=c.classification,
            attempt_no=st.attempts + 1,
            contacts_so_far=st.total_contacts,
            days_since_last_contact=st.days_since_last_contact,
            now=st.now,
            is_recurring=case["kind"] == "RECURRING",
            mastercard_advice_code=case.get("mastercard_advice_code"),
        )
        sr = score(c.proposed_action, sctx, beliefs)
        proposal = L1Proposal(c.classification, sr.chosen, None, c.rationale)
        d = evaluate(proposal, _policy_ctx(st, c.classification))

        cls = "vetoed" if d.downgraded else ("acted" if d.permitted_action is not A.STOP_PERMANENT else "")
        out.append(f'<div class="step {cls}">')
        ist = _utc(st.now).astimezone(IST)
        out.append(f'<div class="t">step {step_no} &middot; {st.now:%Y-%m-%d %H:%M} UTC '
                   f'&middot; {ist:%H:%M} IST</div>')

        out.append("<h2>L1 &mdash; classification</h2>")
        out.append(f'<div class="ev">{esc(c.classification.value)} '
                   f'&middot; confidence {esc(c.classification_confidence)} '
                   f'&middot; recovery bucket <b>{esc(c.recovery_bucket)}</b> (ordinal, never a float)</div>')
        out.append(f'<p class="note">{esc(c.rationale)}</p>')

        out.append("<h2>L2b &mdash; expected value</h2>")
        out.append("<table><tr><th>action</th><th>P(rec)</th><th>recovery</th>"
                   "<th>+ lapse avoided</th><th>cost</th><th>issuer</th>"
                   "<th>churn</th><th>EV</th></tr>")
        for sc in sr.scores:
            win = ' class="win"' if sc.action is sr.chosen else ""
            out.append(
                f"<tr{win}><td>{esc(sc.action.value)}</td>"
                f'<td class="num">{sc.p_recovery:.3f}</td>'
                f'<td class="num">{sc.recovery_value:,.2f}</td>'
                f'<td class="num">{sc.lapse_avoided:,.2f}</td>'
                f'<td class="num">-{sc.action_cost:,.2f}</td>'
                f'<td class="num">-{sc.issuer_trust_term:,.2f}</td>'
                f'<td class="num">-{sc.churn_term:,.2f}</td>'
                f'<td class="num">{money(sc.ev)}</td></tr>'
            )
        out.append("</table>")
        best = sr.scores[0]
        if best.timing_note:
            out.append(f'<p class="note">{esc(best.timing_note)}</p>')
        if sr.chosen is A.STOP_PERMANENT:
            out.append('<p class="note">No action has positive expected value. '
                       "Stop is not a rule firing here &mdash; it is what wins.</p>")

        out.append("<h2>L2a &mdash; policy gate</h2>")
        if d.rules_fired:
            for rule, note in zip(d.rules_fired, d.notes):
                basis = basis_of(rule)
                tag = "reg" if basis == "REGULATORY" else "back"
                out.append(f'<div class="rule"><span class="tag {tag}">{esc(basis)}</span> '
                           f'<code>{esc(rule)}</code> &mdash; {esc(note)}'
                           f'<div class="cite">{esc(citation_of(rule))}</div></div>')
            verb = "Vetoed" if d.vetoed else "Downgraded"
            out.append(f'<p class="note">{verb} to <code>{esc(d.permitted_action.value)}</code>'
                       + (f", scheduled {_utc(d.permitted_scheduled_for):%Y-%m-%d %H:%M} UTC "
                          f"({_utc(d.permitted_scheduled_for).astimezone(IST):%H:%M} IST)."
                          if d.permitted_scheduled_for else ".")
                       + "</p>")
        else:
            out.append(f'<div class="ev"><span class="tag ok">PERMITTED</span> '
                       f'<code>{esc(d.permitted_action.value)}</code> &mdash; no rule blocks it</div>')

        sched = d.permitted_scheduled_for
        if sched is not None and _utc(sched) > _utc(st.now):
            st.now = sched.replace(tzinfo=None) if sched.tzinfo else sched
            out.append("</div>")
            continue

        _execute(st, d.permitted_action, rng, world, res)
        last = st.events[-1] if st.events else {}
        tag = "ok" if last.get("outcome") == "SUCCEEDED" else "no"
        out.append(f'<h2>L3 &mdash; execution</h2><div class="ev">'
                   f'<span class="tag {tag}">{esc(last.get("outcome", "-"))}</span> '
                   f'{esc(d.permitted_action.value)}</div>')
        out.append("</div>")

        if st.closed:
            break
        st.now += timedelta(hours=24)

    out.append(f'<div class="out"><b>Outcome:</b> {esc(st.close_reason or "horizon reached")} '
               f"&middot; {st.attempts} attempt(s), {st.contacts} contact(s)</div>")

    out.append('<div class="gt"><b>Ground truth</b> (the agent never saw this) &mdash; '
               f'true class <code>{esc(gt["true_class"])}</code>; '
               f'retries needed: {gt["retry_attempts_needed"] if gt["retry_attempts_needed"] else "never recoverable by retrying"}; '
               f'payment link would {"" if gt["link_recovers"] else "not "}have worked; '
               f'a perfect agent would open with <code>{esc(gt["oracle_action"])}</code>.</div>')
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--cases", type=int, default=3, help="how many invoices to trace")
    ap.add_argument("--out", type=str, default="docs/results/trace.html")
    args = ap.parse_args()

    cases = [asdict(c) for c in generate_batch(args.n, args.seed)]
    beliefs = Beliefs.from_constants()

    # Pick one from each bucket so the artifact shows the clean case, the
    # ambiguous case, and the context-dependent case rather than three of the
    # same thing.
    picked: list[dict] = []
    for bucket in ("CLEAN", "AMBIGUOUS", "CONTEXT"):
        match = next((c for c in cases if c["bucket"] == bucket), None)
        if match:
            picked.append(match)
    picked = picked[: args.cases]

    body = []
    for c in picked:
        body.append(f'<h1>{esc(c["case_id"])} <span style="color:var(--mut);font-weight:400">'
                    f'&middot; {esc(c["bucket"])}</span></h1>')
        body.append('<p class="sub">One invoice, end to end: payload &rarr; classification &rarr; '
                    "expected value with every term shown &rarr; policy verdict &rarr; outcome.</p>")
        body.append(render_case(c, beliefs))
        body.append('<hr style="border:0;border-top:1px solid var(--line);margin:48px 0">')

    doc = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        "<title>Backstop &mdash; decision traces</title>"
        f"<style>{CSS}</style></head><body><div class=wrap>"
        + "\n".join(body)
        + '<p class="foot">Generated by <code>sim/render_trace.py</code> from the same '
          "classifier, scorer and policy gate the simulation runs &mdash; not a "
          "reimplementation. Ground-truth blocks are shown only to let a reader check the "
          "decision; no layer of the agent can see them.</p>"
        "</div></body></html>"
    )

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(doc)
    print(f"Wrote {args.out} ({len(doc):,} bytes, {len(picked)} traces)")


if __name__ == "__main__":
    main()
