"""Generate ``docs/images/how-it-works.svg``: the Torus loop as an animated ring.

The README shows the result. Edit the steps or timing here and re-run:

    python scripts/build_flow_svg.py
"""

# The SVG template below is easier to read with its long lines intact.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import itertools
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

W, H = 960, 470
CX, CY = 480, 250
RX, RY = 340, 160
PERIOD = 14.0  # seconds per loop
BW, BH = 214, 66  # step box size

# (key, title, subtitle, parametric angle in degrees, group)
STEPS = [
    ("recall", "Recall", "past theses, claims, dead ends", 228, "work"),
    ("research", "Research", "filings, news, transcripts", 270, "work"),
    ("record", "Record", "sourced claims and theses", 312, "work"),
    ("watch", "Watch", "new SEC filings and news", 48, "away"),
    ("alert", "Alert", "which thesis it hits, and why", 90, "away"),
    ("review", "Review", "supports · weakens · breaks", 132, "away"),
]


def pt(deg: float) -> tuple[float, float]:
    t = math.radians(deg)
    return CX + RX * math.cos(t), CY + RY * math.sin(t)


# Dense polyline around the ellipse, starting at Recall and going clockwise.
start = STEPS[0][3]
N = 720
poly = [pt(start + 360 * i / N) for i in range(N + 1)]
cum = [0.0]
for a, b in itertools.pairwise(poly):
    cum.append(cum[-1] + math.dist(a, b))
total = cum[-1]


def frac_at(deg: float) -> float:
    off = (deg - start) % 360
    return cum[round(off / 360 * N)] / total


path_d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in poly) + " Z"
fracs = [frac_at(s[3]) for s in STEPS] + [1.0]
n = len(STEPS)
key_times = ";".join(f"{i / n:.4f}" for i in range(n + 1))
key_points = ";".join(f"{f:.4f}" for f in fracs)
seg = PERIOD / n

# Direction chevrons halfway between steps.
chevrons = []
for i in range(n):
    a = STEPS[i][3]
    b = STEPS[(i + 1) % n][3]
    span = (b - a) % 360
    mid = a + span / 2
    x, y = pt(mid)
    t = math.radians(mid)
    dx, dy = -RX * math.sin(t), RY * math.cos(t)
    ang = math.degrees(math.atan2(dy, dx))
    chevrons.append(
        f'<path class="chev" d="M-6,-6 L4,0 L-6,6" transform="translate({x:.1f},{y:.1f}) '
        f'rotate({ang:.1f})"/>'
    )


def step_box(i: int, key: str, title: str, sub: str, deg: float, group: str) -> str:
    x, y = pt(deg)
    x0, y0 = x - BW / 2, y - BH / 2
    delay = i * seg
    return f"""
  <g class="step {group}">
    <rect class="box" x="{x0:.1f}" y="{y0:.1f}" width="{BW}" height="{BH}" rx="12"/>
    <rect class="glow" x="{x0:.1f}" y="{y0:.1f}" width="{BW}" height="{BH}" rx="12"
          style="animation-delay:{delay:.2f}s"/>
    <rect class="bar" x="{x0 + 10:.1f}" y="{y0 + 14:.1f}" width="4" height="{BH - 28}" rx="2"/>
    <text class="title" x="{x0 + 24:.1f}" y="{y0 + 29:.1f}">{title}</text>
    <text class="sub" x="{x0 + 24:.1f}" y="{y0 + 49:.1f}">{sub}</text>
  </g>"""


boxes = "".join(step_box(i, *s) for i, s in enumerate(STEPS))

# Spokes between the graph and the steps that read from or write to it.
GW, GH = 210, 84


def spoke(key: str, inward: bool) -> str:
    deg = next(s[3] for s in STEPS if s[0] == key)
    sx, sy = pt(deg)
    # from the step box edge towards the centre box edge
    dx, dy = CX - sx, CY - sy
    k = 1 / max(abs(dx) / (BW / 2 + 4), abs(dy) / (BH / 2 + 4))
    ax, ay = sx + dx * k, sy + dy * k
    k2 = 1 / max(abs(dx) / (GW / 2 + 8), abs(dy) / (GH / 2 + 8))
    bx, by = CX - dx * k2, CY - dy * k2
    if not inward:
        ax, ay, bx, by = bx, by, ax, ay
    return f'<line class="spoke" x1="{ax:.1f}" y1="{ay:.1f}" x2="{bx:.1f}" y2="{by:.1f}" marker-end="url(#arrow)"/>'


spokes = "".join(
    [spoke("record", True), spoke("review", True), spoke("recall", False), spoke("watch", False)]
)

rec_start = 2 * seg / PERIOD * 100
rev_start = 5 * seg / PERIOD * 100


def build_svg() -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-labelledby="t d">
<title id="t">How Torus works</title>
<desc id="d">A loop: you ask about a company; Torus recalls earlier research; Claude researches and records sourced claims and theses in the Torus graph. While you're away, Torus watches new SEC filings and news, raises an alert on the thesis it affects, and you review it; the graph is updated and the next session starts from it.</desc>
<defs>
  <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path class="arrowhead" d="M0,0 L10,5 L0,10 z"/>
  </marker>
  <radialGradient id="halo">
    <stop offset="0" class="halo0"/>
    <stop offset="1" class="halo1"/>
  </radialGradient>
</defs>
<style>
  svg {{
    --text: #1f2328; --muted: #59636e; --line: #d1d9e0; --card: #ffffff;
    --work: #0969da; --away: #8250df; --graph: #1a7f37; --graph-bg: #dafbe1;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif;
  }}
  @media (prefers-color-scheme: dark) {{
    svg {{
      --text: #f0f6fc; --muted: #9198a1; --line: #3d444d; --card: #151b23;
      --work: #4493f8; --away: #ab7df8; --graph: #3fb950; --graph-bg: #12261e;
    }}
  }}
  .track {{ fill: none; stroke: var(--line); stroke-width: 2; stroke-dasharray: 2 7; stroke-linecap: round; }}
  .chev {{ fill: none; stroke: var(--muted); stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; opacity: .7; }}
  .box {{ fill: var(--card); stroke: var(--line); stroke-width: 1.5; }}
  .glow {{ fill: none; stroke-width: 2.5; opacity: 0; animation: lit {PERIOD}s ease-in-out infinite; }}
  .work .glow, .work .bar {{ stroke: var(--work); fill: var(--work); }}
  .away .glow, .away .bar {{ stroke: var(--away); fill: var(--away); }}
  .glow {{ fill-opacity: .07; }}
  .title {{ fill: var(--text); font-size: 17px; font-weight: 600; }}
  .sub {{ fill: var(--muted); font-size: 13px; }}
  .section {{ fill: var(--muted); font-size: 12px; font-weight: 600; letter-spacing: .12em; text-transform: uppercase; }}
  .work-c {{ fill: var(--work); }} .away-c {{ fill: var(--away); }}
  .spoke {{ stroke: var(--line); stroke-width: 1.5; stroke-dasharray: 4 4; }}
  .arrowhead {{ fill: var(--muted); }}
  .graph-box {{ fill: var(--graph-bg); stroke: var(--graph); stroke-width: 2; }}
  .graph-title {{ fill: var(--text); font-size: 19px; font-weight: 700; }}
  .graph-sub {{ fill: var(--muted); font-size: 13px; }}
  .pulse {{ fill: none; stroke: var(--graph); stroke-width: 2; opacity: 0;
            transform-origin: {CX}px {CY}px; animation: pulse {PERIOD}s ease-out infinite; }}
  .dot, .dot-halo {{ animation: hue {PERIOD}s step-end infinite; }}
  .dot-halo {{ opacity: .2; }}
  .ask rect {{ fill: var(--card); stroke: var(--line); stroke-width: 1.5; }}
  .ask .q {{ fill: var(--text); font-size: 14px; font-style: italic; }}
  .ask .who {{ fill: var(--muted); font-size: 12px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; }}
  @keyframes lit {{
    0% {{ opacity: 0; }} 3% {{ opacity: 1; }} 14% {{ opacity: 1; }} 19% {{ opacity: 0; }} 100% {{ opacity: 0; }}
  }}
  @keyframes hue {{
    0% {{ fill: var(--work); }} 50% {{ fill: var(--away); }}
  }}
  @keyframes pulse {{
    0%, {rec_start:.1f}% {{ opacity: 0; transform: scale(1); }}
    {rec_start + 1:.1f}% {{ opacity: .9; transform: scale(1); }}
    {rec_start + 12:.1f}% {{ opacity: 0; transform: scale(1.18); }}
    {rev_start:.1f}% {{ opacity: 0; transform: scale(1); }}
    {rev_start + 1:.1f}% {{ opacity: .9; transform: scale(1); }}
    {rev_start + 12:.1f}%, 100% {{ opacity: 0; transform: scale(1.18); }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .glow, .pulse {{ animation: none; }}
    .mover {{ display: none; }}
  }}
</style>

<path id="ring" class="track" d="{path_d}"/>
{"".join(chevrons)}
{spokes}

<text class="section work-c" x="{CX}" y="{CY - 78}" text-anchor="middle">while you research</text>
<text class="section away-c" x="{CX}" y="{CY + 90}" text-anchor="middle">while you're away</text>

<rect class="pulse" x="{CX - GW / 2}" y="{CY - GH / 2}" width="{GW}" height="{GH}" rx="16"/>
<rect class="graph-box" x="{CX - GW / 2}" y="{CY - GH / 2}" width="{GW}" height="{GH}" rx="16"/>
<text class="graph-title" x="{CX}" y="{CY - 4}" text-anchor="middle">Torus graph</text>
<text class="graph-sub" x="{CX}" y="{CY + 20}" text-anchor="middle">local SQLite · full history</text>

<g class="mover">
  <circle class="dot-halo" r="14">
    <animateMotion dur="{PERIOD}s" repeatCount="indefinite" calcMode="linear" keyPoints="{key_points}" keyTimes="{key_times}"><mpath href="#ring"/></animateMotion>
  </circle>
  <circle class="dot" r="6">
    <animateMotion dur="{PERIOD}s" repeatCount="indefinite" calcMode="linear" keyPoints="{key_points}" keyTimes="{key_times}"><mpath href="#ring"/></animateMotion>
  </circle>
</g>

{boxes}

<g class="ask">
  <rect x="16" y="14" width="250" height="56" rx="12"/>
  <text class="who" x="32" y="36">You ask</text>
  <text class="q" x="32" y="57">“Is NVIDIA still supply-bound?”</text>
</g>


</svg>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "docs" / "images" / "how-it-works.svg",
        help="output .svg path (default: docs/images/how-it-works.svg)",
    )
    args = parser.parse_args(argv)
    args.out.write_text(build_svg(), encoding="utf-8", newline="\n")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
