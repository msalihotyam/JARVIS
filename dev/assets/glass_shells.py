"""Generate the decorative glass-shell assets for the summary rail and peek.

Static nine-slice frames with a transparent centre - a soft outer glow, a
luminous cyan rim that is brightest where light catches the top-left, and a
faint secondary inner rim - plus a sprite of soft glows for the rail's icon
wells. QML draws every surface, text, icon, row and hit target natively on
top; these only add the light a plain Rectangle cannot (true soft glow).

    python dev/assets/glass_shells.py

Writes into jarvis/ui/qml/assets/. Deterministic.
"""

from pathlib import Path

import numpy as np
from PIL import Image

OUT = Path(__file__).resolve().parents[2] / "jarvis" / "ui" / "qml" / "assets"
SS = 4  # supersampling for clean anti-aliased edges


def rounded_sdf(w, h, inset, radius):
    """Signed distance (px, at 1x) to a rounded rect inset into a w x h canvas;
    negative inside. Also returns the outward normal of the nearest edge."""
    n = SS
    yy, xx = np.mgrid[0:h * n, 0:w * n].astype(np.float32)
    px, py = (xx + 0.5) / n, (yy + 0.5) / n
    cx, cy = w / 2, h / 2
    hx, hy = w / 2 - inset, h / 2 - inset
    qx, qy = np.abs(px - cx) - (hx - radius), np.abs(py - cy) - (hy - radius)
    ox, oy = np.maximum(qx, 0), np.maximum(qy, 0)
    d = np.sqrt(ox ** 2 + oy ** 2) + np.minimum(np.maximum(qx, qy), 0) - radius
    # Outward normal: towards the nearest edge / corner.
    nx = np.where(qx > qy, np.sign(px - cx), 0.0)
    ny = np.where(qy >= qx, np.sign(py - cy), 0.0)
    corner = (qx > 0) & (qy > 0)
    ln = np.sqrt(ox ** 2 + oy ** 2) + 1e-6
    nx = np.where(corner, np.sign(px - cx) * ox / ln, nx)
    ny = np.where(corner, np.sign(py - cy) * oy / ln, ny)
    return d, nx, ny


def downsample(a):
    h, w = a.shape[0] // SS, a.shape[1] // SS
    return a.reshape(h, SS, w, SS, *a.shape[2:]).mean(axis=(1, 3))


def shell(w, h, inset, radius, colour, rim, glow, glow_px, inner_at, inner, inner_glow):
    """A frame: glow outside, a lit rim, a secondary inner rim, soft inner
    edge light. Returns straight-alpha RGBA (uint8)."""
    d, nx, ny = rounded_sdf(w, h, inset, radius)
    # Light falls from the upper left.
    light = 0.55 + 0.45 * np.clip(-ny, 0, 1) + 0.25 * np.clip(-nx, 0, 1) - 0.1 * np.clip(ny, 0, 1)
    rim_a = np.exp(-(d / 0.75) ** 2) * rim * light
    # Soft glow outside, windowed to zero before the canvas edge so a
    # stretched nine-slice never shows its own boundary.
    window = np.clip(1 - np.clip(d, 0, None) / (inset - 0.5), 0, 1) ** 2
    glow_a = np.exp(-np.clip(d, 0, None) / glow_px) * (d > 0) * glow * light * window
    inner_a = np.exp(-((d - inner_at) / 0.6) ** 2) * inner * (0.7 + 0.3 * light) if inner else 0 * d
    edge_a = np.exp(np.clip(d, None, 0) / 3.0) * (d <= 0) * inner_glow * light
    layers = [(glow_a, colour), (edge_a, colour), (inner_a, colour),
              (rim_a, tuple(c + (255 - c) * 0.45 for c in colour))]
    alpha = 1 - np.prod([1 - np.clip(a, 0, 1) for a, _ in layers], axis=0)
    rgb = sum(np.clip(a, 0, 1)[..., None] * np.array(c, np.float32) for a, c in layers)
    weight = sum(np.clip(a, 0, 1) for a, _ in layers)[..., None] + 1e-6
    rgb = rgb / weight
    img = np.dstack([downsample(rgb), downsample(alpha) * 255])
    return np.clip(img, 0, 255).astype(np.uint8)


def well_glow(size, tile, radius, colour, strength, glow_px):
    d, _, _ = rounded_sdf(size, size, (size - tile) / 2, radius)
    margin = (size - tile) / 2 - 0.5
    a = np.exp(-np.clip(d, 0, None) / glow_px) * (d > -1) * strength * np.clip(1 - np.clip(d, 0, None) / margin, 0, 1) ** 2
    rgb = np.broadcast_to(np.array(colour, np.float32), a.shape + (3,))
    return np.clip(np.dstack([downsample(rgb), downsample(a) * 255]), 0, 255).astype(np.uint8)


def save(arr, name):
    Image.fromarray(arr, "RGBA").save(OUT / name, optimize=True)
    print(f"wrote {name} {arr.shape[1]}x{arr.shape[0]} ({(OUT / name).stat().st_size // 1024} KB)")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # Rail card: 14 px radius, a 12 px glow margin; nine-slice border 34.
    save(shell(72, 72, 12, 14, (70, 190, 240), rim=0.85, glow=0.30, glow_px=4.5,
               inner_at=-3.5, inner=0.09, inner_glow=0.12), "rail-card-shell.png")
    save(shell(72, 72, 12, 14, (88, 214, 250), rim=1.0, glow=0.55, glow_px=5.5,
               inner_at=-3.5, inner=0.22, inner_glow=0.2), "rail-card-shell-active.png")
    # Peek: 16 px radius, an 18 px glow margin, a secondary rim 7 px inside;
    # nine-slice border 46.
    save(shell(96, 96, 18, 16, (70, 200, 240), rim=1.0, glow=0.65, glow_px=7.0,
               inner_at=-7.0, inner=0.3, inner_glow=0.12), "peek-shell.png")
    # Icon-well glows, one 80 px frame per module: tasks, updates, focus, plan.
    tints = [(40, 130, 255), (20, 205, 165), (240, 170, 50), (165, 95, 255)]
    frames = [well_glow(80, 44, 12, t, 0.5, 6.0) for t in tints]
    save(np.concatenate(frames, axis=1), "icon-well-glow.png")


if __name__ == "__main__":
    main()
