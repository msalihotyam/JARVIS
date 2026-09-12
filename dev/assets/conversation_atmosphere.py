"""Generate jarvis/ui/qml/assets/conversation-atmosphere.png.

A static, decorative plate that sits behind the Command Center conversation
surface in the Canonical Space theme: a deep blue-black field, a sparse star
scatter, restrained blue bloom and a planetary limb entering from the lower
left - the local atmosphere of the approved conversation helper. It carries
no text, controls or data; everything interactive stays native QML.

The limb is fitted to the helper's own horizon (traced at its 1448x1086
scale and mapped onto the 1096x823 work surface at 1920x1080). Deterministic:
the same seed always produces the same plate.

    python dev/assets/conversation_atmosphere.py
"""

from pathlib import Path

import numpy as np
from PIL import Image

W, H = 1096, 823
OUT = Path(__file__).resolve().parents[2] / "jarvis" / "ui" / "qml" / "assets" / "conversation-atmosphere.png"

# The helper's horizon, read off its grid, in helper pixels; its panel frame
# starts at (50, 42) and maps onto the work surface at 0.812.
HELPER_LIMB = [(0, 645), (110, 700), (245, 800), (390, 900), (540, 1000), (660, 1086)]
SCALE = 0.812


def fit_circle(points):
    """Least-squares circle through the traced horizon."""
    p = np.array(points, float)
    a = np.c_[2 * p[:, 0], 2 * p[:, 1], np.ones(len(p))]
    b = (p ** 2).sum(axis=1)
    cx, cy, c = np.linalg.lstsq(a, b, rcond=None)[0]
    return cx, cy, np.sqrt(c + cx * cx + cy * cy)


def main() -> None:
    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    rgb = np.zeros((H, W, 3), np.float32)

    # Deep blue-black field, a touch lighter towards the top.
    top, bottom = np.array([0, 11, 24]), np.array([0, 6, 14])
    t = (yy / H)[..., None]
    rgb[:] = top * (1 - t) + bottom * t

    # Restrained blue bloom: above the horizon, and a faint lift up-right.
    def bloom(cx, cy, rx, ry, colour, strength):
        d = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2
        rgb[:] += np.exp(-d)[..., None] * np.array(colour, np.float32) * strength

    bloom(120, 640, 520, 300, (10, 60, 140), 0.55)
    bloom(W * 0.9, H * 0.12, 520, 360, (4, 30, 70), 0.35)

    # Sparse stars: tiny, a few with a soft halo.
    for _ in range(260):
        x, y = rng.uniform(0, W), rng.uniform(0, H)
        v = rng.uniform(0.25, 1.0) ** 2.2
        r = rng.uniform(0.5, 1.2)
        tint = np.array((200, 228, 255) if rng.random() > 0.8 else (235, 245, 255), np.float32)
        x0, x1 = int(max(0, x - 4)), int(min(W, x + 5))
        y0, y1 = int(max(0, y - 4)), int(min(H, y + 5))
        d2 = (xx[y0:y1, x0:x1] - x) ** 2 + (yy[y0:y1, x0:x1] - y) ** 2
        glow = np.exp(-d2 / (2 * r * r)) * v + (np.exp(-d2 / 10.0) * v * 0.12 if v > 0.6 else 0)
        rgb[y0:y1, x0:x1] += glow[..., None] * tint * 0.9

    # The planetary limb.
    limb = [((x - 50) * SCALE, (y - 42) * SCALE) for x, y in HELPER_LIMB]
    cx, cy, radius = fit_circle(limb)
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) - radius     # < 0 inside the planet
    inside = dist < 0
    # Night side: very dark navy with faint surface texture and warm lights
    # gathered near the terminator.
    noise = rng.normal(0, 1, (H // 6 + 2, W // 6 + 2)).astype(np.float32)
    noise = np.kron(noise, np.ones((6, 6), np.float32))[:H, :W]
    depth = np.clip(-dist / 110.0, 0, 1)
    surface = np.array([1, 8, 18], np.float32) + np.array([8, 34, 70], np.float32) * (1 - depth[..., None]) ** 1.6
    surface = surface + noise[..., None] * np.array([2.0, 4.5, 7.5], np.float32) * (1 - depth[..., None] * 0.7)
    rgb[inside] = surface[inside]
    lights = (rng.random((H, W)) > 0.990) & inside & (dist > -260) & (dist < -10)
    rgb[lights] += np.array([255, 196, 120], np.float32) * rng.uniform(0.3, 1.0, lights.sum())[:, None]
    # Atmosphere: a thin bright rim, a blue scatter outside, haze inside.
    rim = np.exp(-(dist / 1.6) ** 2)
    band = np.exp(-((dist + 6.0) / 9.0) ** 2)                      # lit band just inside the edge
    glow = np.exp(-np.abs(dist) / 14.0)
    scatter = np.exp(-np.clip(dist, 0, None) / 90.0) * (dist >= 0)
    haze = np.exp(np.clip(dist, None, 0) / 45.0) * inside
    rgb += rim[..., None] * np.array([215, 244, 255], np.float32) * 0.7
    rgb += band[..., None] * np.array([60, 170, 250], np.float32) * 0.55
    rgb += glow[..., None] * np.array([30, 130, 235], np.float32) * 0.42
    rgb += scatter[..., None] * np.array([12, 80, 180], np.float32) * 0.4
    rgb += haze[..., None] * np.array([14, 80, 170], np.float32) * 0.5

    # Rounded corners, so the plate sits cleanly inside the glass frame.
    alpha = np.ones((H, W), np.float32)
    r = 10
    for (ox, oy) in ((r, r), (W - r - 1, r), (r, H - r - 1), (W - r - 1, H - r - 1)):
        cornerx = (xx < r) if ox == r else (xx > W - r - 1)
        cornery = (yy < r) if oy == r else (yy > H - r - 1)
        d = np.sqrt((xx - ox) ** 2 + (yy - oy) ** 2)
        alpha = np.where(cornerx & cornery, np.clip(r + 0.5 - d, 0, 1), alpha)

    img = np.dstack([np.clip(rgb, 0, 255), alpha * 255]).astype(np.uint8)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img, "RGBA").save(OUT, optimize=True)
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB); limb centre ({cx:.0f}, {cy:.0f}) r={radius:.0f}")


if __name__ == "__main__":
    main()
