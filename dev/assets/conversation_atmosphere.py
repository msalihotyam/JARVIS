"""Generate jarvis/ui/qml/assets/conversation-atmosphere.png.

A static, decorative plate that sits behind the Command Center conversation
surface in the Canonical Space theme: a deep blue-black field, a sparse star
scatter, restrained blue bloom, and the supplied textured Earth limb
(dev/assets/phase33-earth-limb-source.webp) entering from the lower left -
the local atmosphere of the approved conversation helper. It carries no
text, controls or data; everything interactive stays native QML.

The Earth is placed so its horizon enters the surface's left edge at ~61 %
of the height and passes behind the workflow's lower left, close to the
helper's horizon, and sits bottom-left so the plate crops gracefully at
every supported size. Deterministic: the same inputs give the same plate.

    python dev/assets/conversation_atmosphere.py
"""

from pathlib import Path

import numpy as np
from PIL import Image

W, H = 1096, 823
OUT = Path(__file__).resolve().parents[2] / "jarvis" / "ui" / "qml" / "assets" / "conversation-atmosphere.png"

EARTH = Path(__file__).resolve().parent / "phase33-earth-limb-source.webp"
EARTH_SCALE = 0.80            # source 1672x941 -> 1338x753 on the 1096x823 surface
EARTH_HORIZON_AT_LEFT = 385   # where the source's horizon meets its own left edge
EARTH_ENTRY_Y = 430           # where it should meet the surface's left edge
EARTH_TILT = 13.0             # degrees clockwise about that point: the helper's steeper horizon
EARTH_OPACITY = 0.82          # restrained: the conversation text stays dominant


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

    bloom(120, 600, 520, 300, (10, 60, 140), 0.22)
    bloom(W * 0.9, H * 0.12, 520, 360, (4, 30, 70), 0.22)

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

    # The textured Earth limb: the supplied production asset, scaled, tilted
    # about the point where its horizon meets the surface's left edge, and
    # composited once. The planet body covers the bottom-left corner.
    src = Image.open(EARTH).convert("RGBA")
    ew, eh = round(src.width * EARTH_SCALE), round(src.height * EARTH_SCALE)
    src = src.resize((ew, eh), Image.LANCZOS)
    pivot_y = EARTH_HORIZON_AT_LEFT * EARTH_SCALE
    # A canvas the size of the surface, with the pivot at (0, EARTH_ENTRY_Y).
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer.paste(src, (0, round(EARTH_ENTRY_Y - pivot_y)), src)
    layer = layer.rotate(-EARTH_TILT, resample=Image.BICUBIC, center=(0, EARTH_ENTRY_Y))
    e = np.asarray(layer).astype(np.float32)
    # The thin atmospheric haze fades more than the planet body (alpha
    # gamma), so labels over the glow stay legible.
    a = (e[..., 3:4] / 255.0) ** 1.6 * EARTH_OPACITY
    rgb[:] = rgb * (1 - a) + e[..., :3] * a

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
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
