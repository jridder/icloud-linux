#!/usr/bin/env python3
"""Generate the syncing icon and its rotation frames.

Two circular arrows are knocked out of the cloud. The panel extension
cycles sync-frames/*-syncing-<n>-symbolic.svg while syncing.
Run from this directory: ./make-sync-icons.py
"""
import math
import os

out = os.path.dirname(os.path.abspath(__file__))
CLOUD = 'M4 14h8.5a3.5 3.5 0 0 0 .4-6.98A5 5 0 0 0 3.3 7.6 3.25 3.25 0 0 0 4 14z'
CX, CY, R, W = 8.3, 10.0, 2.85, 1.6

def pt(deg, r=R):
    a = math.radians(deg)
    return CX + r * math.cos(a), CY + r * math.sin(a)

def arrow(start, sweep):
    """Arc from start going clockwise by sweep degrees, arrowhead at the end."""
    x0, y0 = pt(start); x1, y1 = pt(start + sweep)
    arc = f'<path d="M{x0:.2f} {y0:.2f}A{R} {R} 0 0 1 {x1:.2f} {y1:.2f}" fill="none" stroke="#000" stroke-width="{W}"/>'
    end = start + sweep
    tip = pt(end + 34)                       # ahead along the circle
    outer, inner = pt(end, R + 1.75), pt(end, R - 1.75)
    head = f'<path d="M{outer[0]:.2f} {outer[1]:.2f}L{tip[0]:.2f} {tip[1]:.2f}L{inner[0]:.2f} {inner[1]:.2f}z" fill="#000"/>'
    return arc + head

def svg(rotation):
    glyph = f'<g transform="rotate({rotation} {CX} {CY})">{arrow(200, 105)}{arrow(20, 105)}</g>'
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
            f'<defs><mask id="m"><rect width="16" height="16" fill="#fff"/>{glyph}</mask></defs>'
            f'<path fill="#2e3436" mask="url(#m)" d="{CLOUD}"/></svg>\n')

with open(f'{out}/io.github.jridder.ICloudLinux-syncing-symbolic.svg', 'w') as f:
    f.write(svg(0))
# Two-fold symmetric glyph: 180° covers a full visual turn.
for i in range(8):
    with open(f'{out}/sync-frames/io.github.jridder.ICloudLinux-syncing-{i}-symbolic.svg', 'w') as f:
        f.write(svg(i * 22.5))
