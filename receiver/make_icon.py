#!/usr/bin/env python3
"""
make_icon.py — generates the ANUSA Scanner macOS app icon (appicon.icns).

Design: a dark rounded-square (macOS squircle) with the app's orange scan
reticle — four corner brackets — framing a sample ANU student number,
"u8221537" (stylised orange "u" + monospace digits), swept by a glowing orange
scan beam. Mirrors the phone app's live scanner UI.

Run:  python3 make_icon.py       (writes appicon.icns + appicon_preview.png)
Deps: Pillow  (pip install pillow)
"""

import os
import subprocess
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))

# ── palette (matches the PWA) ─────────────────────────────────────────────────
# ANUSA blue-and-white icon: blue squircle, white reticle + number + beam.
BG_TOP   = (74, 96, 231)    # ANUSA blue (lighter top)
BG_BOT   = (49, 63, 191)    # deeper blue (bottom)
ORANGE   = (255, 255, 255)  # reticle + "u" — white (name kept to avoid churn)
ORANGE_L = (255, 255, 255)  # beam core — white
INK      = (255, 255, 255)  # digits — white
RIM      = (255, 255, 255)  # subtle rim light

NUMBER = "8221537"          # sample ANU student number
S      = 4                  # supersample factor
BASE   = 1024               # master size (design units)
N      = BASE * S           # working canvas


def u(v):
    return int(round(v * S))


def rounded(draw, box, r, **kw):
    draw.rounded_rectangle([u(box[0]), u(box[1]), u(box[2]), u(box[3])],
                           radius=u(r), **kw)


def font(size, kind="mono"):
    tries = {
        "mono":    [("/System/Library/Fonts/Menlo.ttc", 1),
                    ("/System/Library/Fonts/Menlo.ttc", 0)],
        "rounded": [("/System/Library/Fonts/SFNSRounded.ttf", 0),
                    ("/System/Library/Fonts/SFCompactRounded.ttf", 0),
                    ("/System/Library/Fonts/Menlo.ttc", 1)],
    }[kind]
    for path, idx in tries:
        try:
            return ImageFont.truetype(path, u(size), index=idx)
        except Exception:
            continue
    return ImageFont.load_default()


def build_master(full_bleed=False):
    # full_bleed=True fills the whole canvas (for PWA home-screen / maskable icons);
    # otherwise it's a rounded macOS squircle with a transparent margin.
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))

    # ── body with a vertical gradient ─────────────────────────────────────────
    margin, radius = 44, 210
    grad = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    gpix = grad.load()
    for y in range(N):
        t = y / (N - 1)
        gpix_row = (int(BG_TOP[0] + (BG_BOT[0] - BG_TOP[0]) * t),
                    int(BG_TOP[1] + (BG_BOT[1] - BG_TOP[1]) * t),
                    int(BG_TOP[2] + (BG_BOT[2] - BG_TOP[2]) * t), 255)
        for x in range(N):
            gpix[x, y] = gpix_row

    if full_bleed:
        img.paste(grad, (0, 0))
    else:
        mask = Image.new("L", (N, N), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [u(margin), u(margin), u(BASE - margin), u(BASE - margin)],
            radius=u(radius), fill=255)
        img.paste(grad, (0, 0), mask)

    # reticle frame (design units) — landscape, wide enough for 8 glyphs
    fx0, fx1, fy0, fy1 = 150, 874, 356, 668
    fcy = (fy0 + fy1) / 2
    beam_y = fy1 - 34

    # ── glow layer (beam bloom), blurred ─────────────────────────────────────
    glow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    rounded(ImageDraw.Draw(glow),
            [fx0 - 20, beam_y - 20, fx1 + 20, beam_y + 20], 20,
            fill=ORANGE + (200,))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(u(13))))

    d = ImageDraw.Draw(img)

    # ── scan-reticle corner brackets ─────────────────────────────────────────
    L, T, rr = 104, 30, 15
    corners = [
        ([fx0, fy0, fx0 + L, fy0 + T], [fx0, fy0, fx0 + T, fy0 + L]),          # TL
        ([fx1 - L, fy0, fx1, fy0 + T], [fx1 - T, fy0, fx1, fy0 + L]),          # TR
        ([fx0, fy1 - T, fx0 + L, fy1], [fx0, fy1 - L, fx0 + T, fy1]),          # BL
        ([fx1 - L, fy1 - T, fx1, fy1], [fx1 - T, fy1 - L, fx1, fy1]),          # BR
    ]
    for h, v in corners:
        rounded(d, h, rr, fill=ORANGE + (255,))
        rounded(d, v, rr, fill=ORANGE + (255,))

    # ── "u" (stylised, orange) + number (mono, white), auto-fit inside frame ──
    pad = 58                                    # clearance from the brackets
    avail = u(fx1 - fx0) - u(pad) * 2
    gap_f = 0.07                                # gap as a fraction of digit size

    def measure(fs_d):
        f_d = font(fs_d, "mono")
        f_u = font(fs_d * 1.18, "rounded")
        wu = d.textlength("u", font=f_u)
        wd = d.textlength(NUMBER, font=f_d)
        return f_d, f_u, wu, wd, wu + u(fs_d) * gap_f + wd

    fs_d = 130
    f_d, f_u, wu, wd, total = measure(fs_d)
    fs_d *= avail / total                        # scale to fit the available width
    f_d, f_u, wu, wd, total = measure(fs_d)

    gap = u(fs_d) * gap_f
    x0 = u(512) - total / 2
    baseline = u((fy0 + beam_y) / 2) + u(fs_d) * 0.35   # centre digit caps vertically
    d.text((x0, baseline), "u", font=f_u, fill=ORANGE + (255,), anchor="ls")
    d.text((x0 + wu + gap, baseline), NUMBER, font=f_d, fill=INK + (255,),
           anchor="ls")

    # ── scan-beam core (crisp) ───────────────────────────────────────────────
    rounded(d, [fx0 - 6, beam_y - 4, fx1 + 6, beam_y + 4], 4,
            fill=ORANGE_L + (255,))

    # ── rim light on the squircle edge (macOS icon only) ─────────────────────
    if not full_bleed:
        ring = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        ImageDraw.Draw(ring).rounded_rectangle(
            [u(margin) + u(1), u(margin) + u(1),
             u(BASE - margin) - u(1), u(BASE - margin) - u(1)],
            radius=u(radius), outline=RIM + (40,), width=u(2))
        img = Image.alpha_composite(img, ring)

    return img.resize((BASE, BASE), Image.LANCZOS)


def build_pwa_icons():
    """Write the phone PWA home-screen icons (full-bleed, matching the Mac icon)."""
    pwa_dir = os.path.join(HERE, "..", "icons")
    if not os.path.isdir(pwa_dir):
        return
    bleed = build_master(full_bleed=True)
    for px, name in [(192, "icon-192.png"), (512, "icon-512.png"),
                     (180, "apple-touch-icon.png")]:
        bleed.resize((px, px), Image.LANCZOS).save(os.path.join(pwa_dir, name))
    print("wrote PWA icons →", os.path.normpath(pwa_dir))


def main():
    master = build_master()
    master.save(os.path.join(HERE, "appicon_preview.png"))

    iconset = os.path.join(HERE, "appicon.iconset")
    os.makedirs(iconset, exist_ok=True)
    specs = [(16, "16x16"), (32, "16x16@2x"), (32, "32x32"), (64, "32x32@2x"),
             (128, "128x128"), (256, "128x128@2x"), (256, "256x256"),
             (512, "256x256@2x"), (512, "512x512"), (1024, "512x512@2x")]
    for px, name in specs:
        master.resize((px, px), Image.LANCZOS).save(
            os.path.join(iconset, f"icon_{name}.png"))

    icns = os.path.join(HERE, "appicon.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
    print("wrote", icns)

    build_pwa_icons()


if __name__ == "__main__":
    main()
