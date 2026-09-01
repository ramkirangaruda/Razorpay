"""
The frontier chart: net value added against harm.

Two axes, because the whole argument is a trade-off and a single number hides
it. Harm on x (customer contacts per invoice — the scarce, irreversible
resource), value added on y. An arm that is up and to the left is doing more
with less.

`0_do_nothing` sits at the origin by construction: it is the reference every
other arm's value is measured against, so it is drawn as a neutral reference
mark rather than a series — giving it a categorical hue would imply it is a
policy competing with the others, and it is the zero.

Backstop is swept across its belief parameters, which traces a curve rather than
a point: the same agent, told to believe more or less strongly in contact
fatigue, buys a different position on the trade-off. That curve IS the product.
A single dot would be a claim about one parameter setting; the curve is a claim
about the mechanism.

Static HTML with inline SVG. No framework, no build step, no CDN, no image
files — the simulation run emits one file that opens from disk.

Usage:
    python -m sim.render_frontier --n 120 --seed 42
"""

from __future__ import annotations

import argparse
import html
import os

from app.scorer import Beliefs
from sim.run_arms import ADVERSARIAL, WorldParams, run

# Categorical slots 1-3 from the reference palette, validated all-pairs in both
# modes (scatter needs all-pairs, not adjacent). do_nothing is deliberately not
# a categorical hue.
#
# There are four arms and only three hues, on purpose. A fourth categorical hue
# cannot clear the all-pairs colour-vision floors in both modes — violet
# collides with blue on the dark surface (delta-E 1.9 for protanopia), magenta
# collides with orange in light and with aqua in dark. That is a documented
# property of the palette rather than a search that went badly.
#
# The fix is better than a fourth hue would have been. C and D are not
# independent policies: they are the SAME policy with the classifier swapped, so
# they share a hue and are separated by fill — hollow for the decision table,
# solid for the model. Colour carries the policy, shape carries the component
# under test, and the pair reads as a pair, which is what the comparison
# actually is.
SERIES = {
    "A_naive": ("Naive (Razorpay default)", "#2a78d6", "#3987e5"),
    "B_rules_only": ("Rules only", "#eb6834", "#d95926"),
    "C_backstop": ("Backstop · table", "#1baf7a", "#199e70"),
    "D_backstop_llm": ("Backstop · LLM", "#1baf7a", "#199e70"),
}

# Arms drawn hollow rather than filled. Shape is the secondary encoding that
# separates the two Backstop variants without a fourth hue.
HOLLOW = {"C_backstop"}

# Direct-label placement, per series. The three arms sit close together and two
# of them share almost the same y, so a single offset rule collides — these are
# hand-placed against the actual geometry and re-checked whenever the numbers
# move. Format: (dx, dy, text-anchor).
LABEL_OFFSET = {
    "A_naive": (-16, 5, "end"),
    "B_rules_only": (14, 4, "start"),
    "C_backstop": (4, 26, "middle"),
    "D_backstop_llm": (-14, -14, "end"),
}

W, H = 720, 400
PAD_L, PAD_R, PAD_T, PAD_B = 74, 34, 30, 58


def esc(s) -> str:
    return html.escape(str(s))


def collect(n: int, seed: int) -> dict:
    """Run the baseline arms plus a belief sweep for the Backstop curve."""
    base_beliefs = Beliefs.from_constants()
    world = WorldParams()

    baseline = run(n, seed, world, base_beliefs)
    floor = baseline["0_do_nothing"]

    points = {
        name: {
            "x": r.contacts / max(1, r.invoices),
            "y": r.value_added_over(floor),
            "attempts": r.attempts,
            "contacts": r.contacts,
            "recovered": r.recovered,
        }
        for name, r in baseline.items()
        if name in SERIES
    }

    # The Backstop curve: same agent, varying only how strongly it believes in
    # contact fatigue. Everything about the world is held fixed.
    curve = []
    for factor in (0.0, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0):
        r = run(n, seed, world, base_beliefs.perturbed({"contact_fatigue_base": factor}))
        c = r["C_backstop"]
        curve.append({
            "factor": factor,
            "x": c.contacts / max(1, c.invoices),
            "y": c.value_added_over(r["0_do_nothing"]),
        })

    adv = run(n, seed, ADVERSARIAL, base_beliefs)
    adv_floor = adv["0_do_nothing"]
    adversarial = {
        name: {"x": r.contacts / max(1, r.invoices), "y": r.value_added_over(adv_floor)}
        for name, r in adv.items()
        if name in SERIES
    }

    return {"points": points, "curve": curve, "adversarial": adversarial}


def svg(points: dict, curve: list) -> str:
    """
    Scatter with a swept curve behind it.

    The y-axis does NOT start at zero, deliberately. Zero is the do-nothing
    reference, and anchoring there squeezes every arm into the top eighth of the
    plot where the differences that the whole page is about become invisible.
    Truncating a value axis is a real hazard for bars, where length encodes
    magnitude; here position encodes a trade-off between two costs and the
    reader is being asked to compare positions, not lengths. The axis is
    labelled with its actual range and the caption says the reference is at
    zero, off the bottom.
    """
    xs = [p["x"] for p in points.values()] + [c["x"] for c in curve]
    ys = [p["y"] for p in points.values()] + [c["y"] for c in curve]
    xr, yr = max(xs) - min(xs), max(ys) - min(ys)
    x0, x1 = min(xs) - xr * 0.18, max(xs) + xr * 0.12
    y0, y1 = min(ys) - yr * 0.22, max(ys) + yr * 0.18

    def px(x: float) -> float:
        return PAD_L + (x - x0) / (x1 - x0) * (W - PAD_L - PAD_R)

    def py(y: float) -> float:
        return H - PAD_B - (y - y0) / (y1 - y0) * (H - PAD_T - PAD_B)

    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
           'aria-label="Net value added against customer contacts per invoice. '
           'Naive sits highest and furthest right; Backstop lowest and furthest left.">']

    for i in range(5):
        y = y0 + (y1 - y0) * i / 4
        out.append(f'<line x1="{PAD_L}" y1="{py(y):.1f}" x2="{W-PAD_R}" y2="{py(y):.1f}" class="grid"/>')
        out.append(f'<text x="{PAD_L-11}" y="{py(y)+4:.1f}" class="ax" text-anchor="end">'
                   f'{y/1e6:.2f}M</text>')
    for i in range(5):
        x = x0 + (x1 - x0) * i / 4
        out.append(f'<text x="{px(x):.1f}" y="{H-PAD_B+20:.1f}" class="ax" '
                   f'text-anchor="middle">{x:.2f}</text>')

    out.append(f'<line x1="{PAD_L}" y1="{H-PAD_B}" x2="{W-PAD_R}" y2="{H-PAD_B}" class="axis"/>')
    out.append(f'<line x1="{PAD_L}" y1="{PAD_T}" x2="{PAD_L}" y2="{H-PAD_B}" class="axis"/>')

    d = " ".join(f"{'M' if i == 0 else 'L'}{px(c['x']):.1f},{py(c['y']):.1f}"
                 for i, c in enumerate(sorted(curve, key=lambda c: c["x"])))
    out.append(f'<path d="{d}" class="curve"/>')
    for c in curve:
        out.append(f'<circle cx="{px(c["x"]):.1f}" cy="{py(c["y"]):.1f}" r="3.5" '
                   f'class="curvept"><title>agent believes contact fatigue x{c["factor"]}: '
                   f'{c["x"]:.2f} contacts/invoice, {c["y"]:,.0f} INR added</title></circle>')

    # Annotate one end of the swept curve only. The other end lands on top of
    # the rules-only marker, and two labels fighting for that spot is worse than
    # one label plus a sentence in the caption. Direction is the thing a reader
    # needs: believing fatigue is real pushes the agent left, to fewer contacts.
    lo = min(curve, key=lambda c: c["x"])
    out.append(f'<text x="{px(lo["x"]):.1f}" y="{py(lo["y"])+21:.1f}" class="tiny" '
               f'text-anchor="middle">agent believes fatigue &times;{lo["factor"]:g}</text>')
    out.append(f'<text x="{px(lo["x"]):.1f}" y="{py(lo["y"])+34:.1f}" class="tiny" '
               'text-anchor="middle">&larr; stronger belief, fewer contacts</text>')

    for name, (label, _, _) in SERIES.items():
        p = points[name]
        cx, cy = px(p["x"]), py(p["y"])
        dx, dy, anchor = LABEL_OFFSET[name]
        out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="9.5" class="ring"/>')
        if name in HOLLOW:
            out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="6" fill="var(--surface)" '
                       f'stroke="var(--{name})" stroke-width="2.5">'
                       f'<title>{esc(label)}: {p["recovered"]} recovered, {p["attempts"]} attempts, '
                       f'{p["contacts"]} contacts, {p["y"]:,.0f} INR added</title></circle>')
        else:
            out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="7" fill="var(--{name})">'
                       f'<title>{esc(label)}: {p["recovered"]} recovered, {p["attempts"]} attempts, '
                       f'{p["contacts"]} contacts, {p["y"]:,.0f} INR added</title></circle>')
        out.append(f'<text x="{cx+dx:.1f}" y="{cy+dy:.1f}" class="lbl" '
                   f'text-anchor="{anchor}">{esc(label)}</text>')

    out.append(f'<text x="{(PAD_L+W-PAD_R)/2:.0f}" y="{H-14}" class="axt" '
               'text-anchor="middle">customer contacts per invoice &rarr; more harm</text>')
    out.append(f'<text x="17" y="{(PAD_T+H-PAD_B)/2:.0f}" class="axt" text-anchor="middle" '
               f'transform="rotate(-90 17 {(PAD_T+H-PAD_B)/2:.0f})">net value added (INR)</text>')
    out.append("</svg>")
    return "\n".join(out)


def table(points: dict, adversarial: dict) -> str:
    rows = ["<table><caption>The same numbers, for readers who would rather have "
            "them than a picture.</caption><tr><th>arm</th><th>value added</th>"
            "<th>contacts/invoice</th><th>recovered</th><th>attempts</th>"
            "<th>value added, adversarial world</th></tr>"]
    for name, (label, _, _) in SERIES.items():
        p, a = points[name], adversarial[name]
        rows.append(
            f"<tr><td><span class='sw' style='background:var(--{name})'></span>{esc(label)}</td>"
            f"<td class='num'>{p['y']:,.0f}</td><td class='num'>{p['x']:.2f}</td>"
            f"<td class='num'>{p['recovered']}</td><td class='num'>{p['attempts']}</td>"
            f"<td class='num'>{a['y']:,.0f}</td></tr>"
        )
    rows.append("</table>")
    return "\n".join(rows)


CSS = """
:root{--bg:#f7f7f5;--fg:#1a1a18;--mut:#6b6b66;--line:#dcdcd6;--card:#fcfcfb;
--A_naive:#2a78d6;--B_rules_only:#eb6834;--C_backstop:#1baf7a;--D_backstop_llm:#1baf7a;--zero:#8a8a83;
--grid:#e6e6e0;--surface:#fcfcfb}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){
--bg:#161614;--fg:#ecebe6;--mut:#9b9a92;--line:#33322d;--card:#1a1a19;
--A_naive:#3987e5;--B_rules_only:#d95926;--C_backstop:#199e70;--D_backstop_llm:#199e70;--zero:#7d7c75;
--grid:#2a2a26;--surface:#1a1a19}}
:root[data-theme="dark"]{--bg:#161614;--fg:#ecebe6;--mut:#9b9a92;--line:#33322d;
--card:#1a1a19;--A_naive:#3987e5;--B_rules_only:#d95926;--C_backstop:#199e70;
--zero:#7d7c75;--grid:#2a2a26;--surface:#1a1a19}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:40px 24px 72px}
h1{font-size:23px;margin:0 0 6px;letter-spacing:-.01em}
.sub{color:var(--mut);margin:0 0 26px;max-width:62ch}
.fig{background:var(--card);border:1px solid var(--line);border-radius:7px;
padding:18px 16px 10px;margin:0 0 12px}
.cap{color:var(--mut);font-size:12.5px;margin:2px 0 26px;max-width:66ch}
.grid{stroke:var(--grid);stroke-width:1}
.axis{stroke:var(--line);stroke-width:1}
.ax{fill:var(--mut);font-size:11px;font-family:ui-monospace,monospace}
.axt{fill:var(--mut);font-size:12px}
.lbl{fill:var(--fg);font-size:12.5px;font-weight:600}
.tiny{fill:var(--mut);font-size:10.5px}
.ring{fill:var(--card)}
.curve{fill:none;stroke:var(--C_backstop);stroke-width:2;stroke-dasharray:5 4;opacity:.75}
.curvept{fill:var(--C_backstop);opacity:.75}
circle{cursor:default}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 0}
caption{text-align:left;color:var(--mut);font-size:12.5px;padding:0 0 8px}
th,td{text-align:right;padding:6px 10px;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;
letter-spacing:.05em}
td.num{font-family:ui-monospace,monospace}
.sw{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:7px}
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
    ap.add_argument("--out", type=str, default="docs/results/frontier.html")
    args = ap.parse_args()

    data = collect(args.n, args.seed)
    pts, curve, adv = data["points"], data["curve"], data["adversarial"]

    naive = pts["A_naive"]
    back = max((pts[k] for k in ("C_backstop", "D_backstop_llm") if k in pts),
               key=lambda p: p["y"])
    val_pct = back["y"] / naive["y"] * 100
    att_pct = back["attempts"] / naive["attempts"] * 100
    cnt_pct = back["contacts"] / naive["contacts"] * 100

    doc = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Backstop &mdash; the frontier</title><style>{CSS}</style></head><body><div class=wrap>
<h1>Doing less, and what it costs</h1>
<p class=sub>Net value added over never acting, against the harm it took to get there.
Up is more money; left is fewer messages to customers. n={args.n}, seed={args.seed},
identical batch for every arm.</p>

<div class=fig>{svg(pts, curve)}</div>
<p class=cap>Filled marker = model classifier, hollow = decision table; same policy, one component swapped. The dashed line is the table arm&rsquo;s belief sweep: one agent, told to believe
contact fatigue is anywhere from zero to eight times our estimate, holding the world
fixed. Believing fatigue is real moves it left, to fewer contacts; the unlabelled
right-hand end of the curve is the agent ignoring fatigue entirely, which lands it
back beside rules-only. Hover any point for its factor. Note the y-axis does
not start at zero &mdash; the do-nothing reference sits at 0 and every arm is far above
it, so the axis is zoomed to the range where the arms actually differ.</p>

{table(pts, adv)}

<h2>What this says</h2>
<p>Backstop captures <b>{val_pct:.0f}%</b> of the naive baseline&rsquo;s value using
<b>{att_pct:.0f}%</b> of the authorisation attempts and <b>{cnt_pct:.0f}%</b> of the
customer contacts. It does not beat Razorpay&rsquo;s default on raw value, and this
page is not arranged to suggest it does &mdash; the naive arm sits highest on the
y-axis and is drawn that way.</p>
<p>The claim is the shape of the curve, not a single win. Whether trading
{100-val_pct:.0f}% of recovered value for {100-cnt_pct:.0f}% fewer customer contacts is
a good deal depends on what a dunning contact actually costs a customer relationship
&mdash; and that is the one number in this model with no published source. The sweep
exists so a reader can pick their own answer and read off the consequence, rather than
taking ours.</p>
<p>The right-hand column is the adversarial world: contact fatigue and issuer
penalties both set to zero, so blind retrying costs nothing beyond the gateway fee.
Naive gains there and Backstop loses ground, which is the correct result &mdash; in a
world where restraint buys nothing, restraint is not worth buying. Reporting it is
worth more than the number is.</p>
<p style="color:var(--mut);font-size:12.5px;margin-top:32px;border-top:1px solid var(--line);padding-top:14px">
Generated by <code>sim/render_frontier.py</code>. Every arm runs the same seeded batch
through the same policy gate; only the decision layer differs.</p>
</div></body></html>"""

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(doc)
    print(f"Wrote {args.out} ({len(doc):,} bytes)")
    print(f"  Backstop: {val_pct:.0f}% of naive value, {att_pct:.0f}% of attempts, "
          f"{cnt_pct:.0f}% of contacts")


if __name__ == "__main__":
    main()
