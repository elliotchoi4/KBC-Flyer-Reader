"""
Generate assets/icon.ico — a simple open-book mark in the app's
terracotta accent on a white circle.

Run from the project root:
    python assets/make_icon.py

Produces assets/icon.ico (multi-resolution) and assets/icon_preview.png.
The shapes are drawn on a large canvas and downsampled so the small
sizes stay crisp.
"""
from __future__ import annotations

import os
from PIL import Image, ImageDraw

# Warm Paper palette (kept in sync with src/theme.py).
ACCENT = (217, 119, 66)        # #d97742 terracotta
ACCENT_DARK = (176, 87, 31)    # spine / outline
PAGE_LINE = (255, 253, 248)    # near-white page lines
WHITE = (255, 255, 255)
RING = (240, 201, 168)         # faint terracotta ring around the circle

S = 1024  # supersampled working size


def _smoothstep_curve(p0, p1, ctrl, n=24):
    """Sample a quadratic Bezier from p0 to p1 with control point ctrl."""
    pts = []
    for i in range(n + 1):
        t = i / n
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * ctrl[0] + t ** 2 * p1[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * ctrl[1] + t ** 2 * p1[1]
        pts.append((x, y))
    return pts


def draw_icon() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # --- White circle background ---------------------------------------
    cx, cy = S / 2, S / 2
    r = S * 0.49
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE)
    # faint ring for definition on light backgrounds
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=RING, width=int(S * 0.012))

    # --- Open book -----------------------------------------------------
    # Geometry on the 1024 canvas. The book opens upward with a central
    # fold; each page is a leaf rising outward.
    spine_top = (512, 430)
    spine_bot = (512, 716)
    L_out_top = (150, 360)
    L_out_bot = (150, 582)
    R_out_top = (874, 360)
    R_out_bot = (874, 582)

    # Left page: inner-top -> (curved top edge) -> outer-top -> outer-bot
    #            -> (curved bottom edge) -> inner-bottom
    l_top = _smoothstep_curve(spine_top, L_out_top, (336, 344))
    l_bot = _smoothstep_curve(L_out_bot, spine_bot, (336, 612))
    left_poly = l_top + [L_out_bot] + l_bot
    d.polygon(left_poly, fill=ACCENT)

    # Right page (mirror).
    r_top = _smoothstep_curve(spine_top, R_out_top, (688, 344))
    r_bot = _smoothstep_curve(R_out_bot, spine_bot, (688, 612))
    right_poly = r_top + [R_out_bot] + r_bot
    d.polygon(right_poly, fill=ACCENT)

    # Center fold (spine) — a darker accent line for a 3D hint.
    d.line([spine_top, spine_bot], fill=ACCENT_DARK, width=int(S * 0.020))

    # Page text lines — a few short near-white strokes on each page.
    for i, frac in enumerate((0.42, 0.58, 0.74)):
        yy = spine_top[1] + (spine_bot[1] - spine_top[1]) * frac
        inset = 70 + i * 8
        # left page line
        d.line([(214 + inset, yy + 10), (468, yy - 8)],
               fill=PAGE_LINE, width=int(S * 0.013))
        # right page line
        d.line([(556, yy - 8), (810 - inset, yy + 10)],
               fill=PAGE_LINE, width=int(S * 0.013))

    return img


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    big = draw_icon()

    # Preview PNG (handy for eyeballing).
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(here, "icon_preview.png"))

    # Multi-resolution .ico.
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    frames = [big.resize(sz, Image.LANCZOS) for sz in sizes]
    ico_path = os.path.join(here, "icon.ico")
    frames[0].save(ico_path, format="ICO", sizes=sizes)
    print("Wrote", ico_path)
    print("Wrote", os.path.join(here, "icon_preview.png"))


if __name__ == "__main__":
    main()
