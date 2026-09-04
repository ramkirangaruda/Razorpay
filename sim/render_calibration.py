"""
The calibration plot: does a higher recovery_bucket predict a higher realized
recovery rate. This is cheap to check - the
inputs already exist in sim/data/l1_classifications_seed42.json (via
CachedLLMClassifier) and the batch's own ground truth.

------------------------------------------------------------------------------
What "realized recovery" means here, and why it is not exactly what
P_RECOVERY_BY_BUCKET's own comment says the bucket predicts
------------------------------------------------------------------------------

world_model_constants.P_RECOVERY_BY_BUCKET's comment calls the bucket's base
rate "probability of recovery on the next single attempt." The ground truth
sim/generate_batch.py actually produces per case is coarser than that: one
Bernoulli draw against the world's hazard, recording whether a retry recovers
the invoice at all WITHIN THE RETRY HORIZON, plus a separate roll for whether a
payment link would. There is no per-case estimate of a single-attempt hazard
to compare against - only whether the invoice was ever recoverable, by
whichever route.

So "recovered" here is defined exactly the way sim/generate_batch.py's own CLI
summary line already defines it - retry_attempts_needed is not None, or
link_recovers - and the honest claim this plot can make is narrower than exact
probability calibration: does a higher bucket predict a higher chance the
invoice was recoverable AT ALL, i.e. is the ORDERING respected. The agent's own
belief (P_RECOVERY_BY_BUCKET) is plotted alongside for context, not as a target
line this plot is designed to hit - the two quantities are related but not the
same, and drawing a y=x "perfect calibration" line would overclaim that.

Static HTML with inline SVG, same design system as render_frontier.py /
render_trace.py. No framework, no build step, no CDN.

Usage:
    python -m sim.render_calibration --n 120 --seed 42
"""

from __future__ import annotations

import argparse
import html
import os
from dataclasses import asdict

from app.classifier import CachedLLMClassifier, LookupClassifier
from sim.generate_batch import RETRY_HORIZON, generate_batch
from sim.world_model_constants import P_RECOVERY_BY_BUCKET

BUCKETS = ("VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH")

SERIES = {
    "table": ("Decision table", "#eb6834"),
    "model": ("Model (recorded)", "#1baf7a"),
}

W, H = 720, 380
PAD_L, PAD_R, PAD_T, PAD_B = 60, 30, 26, 54


def esc(s) -> str:
    return html.escape(str(s))


def _recovered(gt: dict) -> bool:
    return bool(gt["retry_attempts_needed"]) or bool(gt["link_recovers"])


def collect(n: int, seed: int) -> dict:
    cases = [asdict(c) for c in generate_batch(n, seed)]

    table = LookupClassifier()
    model = CachedLLMClassifier()

    counts = {name: {b: [0, 0] for b in BUCKETS} for name in SERIES}  # [n, recovered]
    for case in cases:
        recovered = _recovered(case["ground_truth"])
        state = {"attempts": 0, "contacts": 0}
        for name, clf in (("table", table), ("model", model)):
            bucket = clf.classify(case, state).recovery_bucket
            n_, r_ = counts[name][bucket]
            counts[name][bucket] = [n_ + 1, r_ + (1 if recovered else 0)]

    rates = {
        name: {
            b: (cnt[b][1] / cnt[b][0] if cnt[b][0] else None, cnt[b][0])
            for b in BUCKETS
        }
        for name, cnt in counts.items()
    }
    return {"rates": rates, "model_misses": model.misses, "n": len(cases)}


def svg(rates: dict) -> str:
    def px(i: int) -> float:
        return PAD_L + i / (len(BUCKETS) - 1) * (W - PAD_L - PAD_R)

    def py(rate: float) -> float:
        return H - PAD_B - rate * (H - PAD_T - PAD_B)

    out = [
        f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
        'aria-label="Realized recovery rate by predicted bucket, table and model, '
        'against the agent\'s own belief.">'
    ]

    for i in range(5):
        r = i / 4
        out.append(
            f'<line x1="{PAD_L}" y1="{py(r):.1f}" x2="{W-PAD_R}" y2="{py(r):.1f}" class="grid"/>'
        )
        out.append(
            f'<text x="{PAD_L-9}" y="{py(r)+4:.1f}" class="ax" text-anchor="end">{r*100:.0f}%</text>'
        )
    for i, b in enumerate(BUCKETS):
        out.append(
            f'<text x="{px(i):.1f}" y="{H-PAD_B+20:.1f}" class="ax" text-anchor="middle">{b}</text>'
        )

    out.append(f'<line x1="{PAD_L}" y1="{H-PAD_B}" x2="{W-PAD_R}" y2="{H-PAD_B}" class="axis"/>')
    out.append(f'<line x1="{PAD_L}" y1="{PAD_T}" x2="{PAD_L}" y2="{H-PAD_B}" class="axis"/>')

    # The agent's own belief - context, not a target. See module docstring.
    belief_pts = [(i, P_RECOVERY_BY_BUCKET.value[b]) for i, b in enumerate(BUCKETS)]
    d = " ".join(f"{'M' if i == 0 else 'L'}{px(x):.1f},{py(y):.1f}" for i, (x, y) in enumerate(belief_pts))
    out.append(f'<path d="{d}" class="belief"/>')
    for x, y in belief_pts:
        out.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="3" class="beliefpt">'
                   f'<title>agent believes {y:.0%} for {BUCKETS[x]}</title></circle>')
    out.append(f'<text x="{px(4)+6:.1f}" y="{py(belief_pts[-1][1]):.1f}" class="tiny" '
               'text-anchor="start">agent\'s belief</text>')

    for name, (label, color) in SERIES.items():
        pts = [(i, rates[name][b][0], rates[name][b][1]) for i, b in enumerate(BUCKETS) if rates[name][b][0] is not None]
        if len(pts) > 1:
            d = " ".join(f"{'M' if i == 0 else 'L'}{px(x):.1f},{py(y):.1f}" for i, (x, y, _) in enumerate(pts))
            out.append(f'<path d="{d}" class="line" style="stroke:{color}"/>')
        for x, y, n_ in pts:
            out.append(
                f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="{6 + min(n_, 30)/5:.1f}" '
                f'fill="{color}" fill-opacity="0.85"><title>{esc(label)} / {BUCKETS[x]}: '
                f'{y:.0%} of {n_} recovered</title></circle>'
            )

    out.append(f'<text x="{(PAD_L+W-PAD_R)/2:.0f}" y="{H-12}" class="axt" '
               'text-anchor="middle">predicted bucket (ordinal)</text>')
    out.append(f'<text x="14" y="{(PAD_T+H-PAD_B)/2:.0f}" class="axt" text-anchor="middle" '
               f'transform="rotate(-90 14 {(PAD_T+H-PAD_B)/2:.0f})">realized recovery rate</text>')
    out.append("</svg>")
    return "\n".join(out)


def table_html(rates: dict) -> str:
    rows = ["<table><caption>n and realized rate per bucket. A blank cell means no case "
            "landed in that bucket for that classifier.</caption>"
            "<tr><th>bucket</th><th>agent's belief</th>"
            "<th>table: n</th><th>table: realized</th>"
            "<th>model: n</th><th>model: realized</th></tr>"]
    for b in BUCKETS:
        t_rate, t_n = rates["table"][b]
        m_rate, m_n = rates["model"][b]
        t_cell = f"{t_rate:.0%}" if t_rate is not None else "&mdash;"
        m_cell = f"{m_rate:.0%}" if m_rate is not None else "&mdash;"
        rows.append(
            f"<tr><td>{b}</td><td class='num'>{P_RECOVERY_BY_BUCKET.value[b]:.0%}</td>"
            f"<td class='num'>{t_n}</td><td class='num'>{t_cell}</td>"
            f"<td class='num'>{m_n}</td><td class='num'>{m_cell}</td></tr>"
        )
    rows.append("</table>")
    return "\n".join(rows)


CSS = """
:root{--bg:#f7f7f5;--fg:#1a1a18;--mut:#6b6b66;--line:#dcdcd6;--card:#fcfcfb}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){
--bg:#161614;--fg:#ecebe6;--mut:#9b9a92;--line:#33322d;--card:#1a1a19}}
:root[data-theme="dark"]{--bg:#161614;--fg:#ecebe6;--mut:#9b9a92;--line:#33322d;--card:#1a1a19}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:40px 24px 72px}
h1{font-size:23px;margin:0 0 6px;letter-spacing:-.01em}
.sub{color:var(--mut);margin:0 0 26px;max-width:64ch}
.fig{background:var(--card);border:1px solid var(--line);border-radius:7px;
padding:18px 16px 10px;margin:0 0 12px}
.cap{color:var(--mut);font-size:12.5px;margin:2px 0 26px;max-width:66ch}
.grid{stroke:var(--grid,#e6e6e0);stroke-width:1}
.axis{stroke:var(--line);stroke-width:1}
.ax{fill:var(--mut);font-size:11px;font-family:ui-monospace,monospace}
.axt{fill:var(--mut);font-size:12px}
.tiny{fill:var(--mut);font-size:10.5px}
.line{fill:none;stroke-width:2.5}
.belief{fill:none;stroke:var(--mut);stroke-width:1.5;stroke-dasharray:4 4;opacity:.7}
.beliefpt{fill:var(--mut);opacity:.7}
circle{cursor:default}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 0}
caption{text-align:left;color:var(--mut);font-size:12.5px;padding:0 0 8px}
th,td{text-align:right;padding:6px 10px;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
td.num{font-family:ui-monospace,monospace}
h2{font-size:15px;margin:34px 0 10px;text-transform:uppercase;letter-spacing:.08em;
color:var(--mut);font-weight:600}
p{max-width:66ch}
code{background:var(--line);padding:1px 5px;border-radius:3px;
font-family:ui-monospace,monospace;font-size:12.5px}
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=str, default="docs/results/calibration.html")
    args = ap.parse_args()

    data = collect(args.n, args.seed)
    rates = data["rates"]

    doc = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Backstop &mdash; calibration</title><style>{CSS}</style></head><body><div class=wrap>
<h1>Does a higher bucket mean a better chance</h1>
<p class=sub>Realized recovery rate for cases landing in each ordinal bucket, table and
model, against the agent&rsquo;s own belief for context. n={data['n']}, seed={args.seed}.
{data['model_misses']} case(s) had no recorded model classification and fell back to the
table (see app/classifier.CachedLLMClassifier).</p>

<div class=fig>{svg(rates)}</div>
<p class=cap>Marker size is n for that bucket &mdash; several buckets are thin at n=120,
so read the table below alongside the picture rather than the picture alone. The dashed
grey line is the agent&rsquo;s own belief (world_model_constants.P_RECOVERY_BY_BUCKET),
shown for context and not as a target: that constant&rsquo;s own comment calls it the
probability of recovery on the NEXT SINGLE attempt, while &ldquo;realized&rdquo; here means
recoverable at all, by retry or by link, within the simulation&rsquo;s retry horizon &mdash;
the same definition sim/generate_batch.py&rsquo;s own summary line uses. Those are related
but not identical quantities, so this plot is a check on ORDERING (does a higher bucket
predict a higher chance), not on exact probability calibration.</p>

{table_html(rates)}

<h2>What this says</h2>
<p><b>The table orders cleanly; the model does not, and that is worth stating plainly rather
than smoothing over.</b> The table's realized rate is close to monotonic &mdash; VERY_LOW
{rates["table"]["VERY_LOW"][0]:.0%} up to VERY_HIGH {rates["table"]["VERY_HIGH"][0]:.0%},
with one dip at HIGH ({rates["table"]["HIGH"][0]:.0%}, below MEDIUM's
{rates["table"]["MEDIUM"][0]:.0%}). The model's is not: its OWN peak is MEDIUM at
{rates["model"]["MEDIUM"][0]:.0%}, not VERY_HIGH, which realizes lower
({rates["model"]["VERY_HIGH"][0]:.0%}) and sits close to LOW ({rates["model"]["LOW"][0]:.0%}).
A reader relying on the model's recovery_bucket to rank &ldquo;how good does this look&rdquo;
at the top end would be misled by it here.</p>
<p>This is a different axis from the 83%-vs-78% classification accuracy reported elsewhere
in this repo (README) &mdash; that number is about getting the failure
CLASS right; this one is about whether the recovery_bucket ESTIMATE, once a class is
assigned, actually orders realized outcomes. Getting the first right does not guarantee the
second, and this batch shows the model doing better on the first and worse on the second at
the top of the range. Both are true, and un-averaged, on purpose.</p>
<p>Some of the ordering noise is explained by the gap the module docstring flags: the
bucket is meant to rank a SINGLE-attempt hazard, but &ldquo;realized&rdquo; here is recovery
within a {RETRY_HORIZON}-attempt horizon, and those compound differently per class &mdash;
HIGH is dominated by SOFT_FUNDS; MEDIUM mixes SOFT_LIMIT (retryable) with SOFT_AUTH
(link-only, at MEASURED_AUTH_HAZARD_1), and a class with a lower single-attempt hazard but
more eligible attempts can out-realize one with a higher hazard and fewer effective tries.
That explains the table's one dip. It does not obviously explain why the model's VERY_HIGH
underperforms its own MEDIUM, since both classifiers are ranking the same underlying classes
against the same horizon &mdash; the more likely explanation there is that the model spreads
a wider, less selective set of cases into VERY_HIGH than the table does (n={rates["model"]["VERY_HIGH"][1]}
against the table's n={rates["table"]["VERY_HIGH"][1]}), diluting the bucket with cases the
table would not have put there.</p>
<p>Where a bucket is thin (single digits, or VERY_LOW's table n=3), its point is not a
reliable estimate of anything and the table above says so honestly rather than the chart
implying otherwise by drawing a confident line through it anyway.</p>
<p style="color:var(--mut);font-size:12.5px;margin-top:32px;border-top:1px solid var(--line);padding-top:14px">
Generated by <code>sim/render_calibration.py</code>. Table bucket predictions come from
running <code>LookupClassifier</code> fresh over the batch (deterministic, no recording
needed); model bucket predictions replay <code>sim/data/l1_classifications_seed42.json</code>
via <code>CachedLLMClassifier</code>, the same recording every other measured LLM figure in
this repo cites.</p>
</div></body></html>"""

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(doc)
    print(f"Wrote {args.out} ({len(doc):,} bytes)")
    for name in SERIES:
        line = "  " + name + ": " + ", ".join(
            f"{b}={rates[name][b][0]:.0%}(n={rates[name][b][1]})" if rates[name][b][0] is not None
            else f"{b}=-(n=0)"
            for b in BUCKETS
        )
        print(line)


if __name__ == "__main__":
    main()
