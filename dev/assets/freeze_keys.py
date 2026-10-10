"""Cut the freeze-key skin art (Gate 6G S2, skins 3 and 4) into layers.

Each Astra source shows the switch twice, side by side: running (lever up)
on the left, frozen (lever down) on the right. The two halves line up at a
fixed horizontal shift. A skin is three same-size layers QML stacks:

* plate - the frozen half, except where the down lever lies, which comes
  from the running half (so no lever is baked in);
* up    - the running half's lever, alpha-masked, swung about ``pivot``;
* down  - the frozen half's lever, alpha-masked, swung about ``pivot``.

    python dev/assets/freeze_keys.py <dir with toggle_v1.png, knife_torn_v1.png>

Writes jarvis/ui/qml/assets/freeze-<skin>-{plate,up,down}.png. Deterministic.
The source PNGs stay where they are (build/6g/freeze, not in the repository).
Each run prints the pivot fraction FreezeKeySwitch.qml uses for that skin.
"""

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parents[2] / "jarvis" / "ui" / "qml" / "assets"

# Source pixels. crop = (x0, y0, x1, y1) of the running half; shift = where the
# frozen half sits relative to it; up / down = lever shapes (("rect"|"ellipse"|"diff",
# box), ...) in the running / frozen half; pivot = the hinge line (source y).
SKINS = {
    "toggle": {
        "source": "toggle_v1.png", "shift": 613, "crop": (262, 140, 692, 860), "pivot": 470,
        "up": (("ellipse", (380, 296, 546, 440)), ("rect", (405, 360, 520, 505))),
        "down": (("ellipse", (380, 515, 552, 680)), ("rect", (408, 420, 522, 600))),
    },
    "knife": {
        "source": "knife_torn_v1.png", "shift": 674, "crop": (90, 25, 780, 910), "pivot": 505,
        "up": (("rect", (400, 25, 500, 225)), ("diff", (275, 210, 625, 470))),
        "down": (("diff", (275, 535, 625, 705)), ("rect", (395, 685, 505, 905))),
    },
}
FEATHER = 3         # px of soft mask edge, in source pixels
SCALE = 0.5         # layers are stored at half the source size: still >2x the largest on-screen size


def mask(shapes, origin, running, frozen):
    """The lever's area. "diff" keeps, inside its box, only what differs
    between the two halves (brass on slate), closed into solid parts, so the
    plate swap never shows a patch of the other half's texture."""
    m = Image.new("L", running.size, 0)
    draw = ImageDraw.Draw(m)
    ox, oy = origin
    change = ImageChops.difference(running, frozen).convert("L").point(lambda v: 255 if v > 40 else 0)
    change = change.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.MinFilter(5))
    for kind, (x0, y0, x1, y1) in shapes:
        box = (x0 - ox, y0 - oy, x1 - ox, y1 - oy)
        if kind == "diff":
            m.paste(change.crop(box), box[:2], change.crop(box))
        else:
            (draw.ellipse if kind == "ellipse" else draw.rectangle)(box, fill=255)
    return m.filter(ImageFilter.GaussianBlur(FEATHER))


def matte(picture: Image.Image) -> Image.Image:
    """Alpha that drops the navy grid backdrop: a pixel is backdrop when it is
    both dark and blue; metal is neutral or bright. Holes inside the silhouette
    (dark sockets, shadows on the plate) are filled, so only the outside goes."""
    rgb = np.asarray(picture.convert("RGB")).astype(np.float32)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    blue = np.clip((b - r - 8) / 14, 0, 1)
    dark = np.clip((95 - (r + g + b) / 3) / 40, 0, 1)
    solid = Image.fromarray(np.where(blue * dark < 0.5, 255, 0).astype(np.uint8)).copy()  # writable
    ImageDraw.floodfill(solid, (0, 0), 128)
    inside = Image.fromarray(np.where(np.asarray(solid) == 128, 0, 255).astype(np.uint8))
    return inside.filter(ImageFilter.GaussianBlur(1))


def cut(source_dir: Path, name: str, spec: dict) -> None:
    image = Image.open(source_dir / spec["source"]).convert("RGB")
    x0, y0, x1, y1 = spec["crop"]
    shift = spec["shift"]
    running = image.crop((x0, y0, x1, y1))
    frozen = image.crop((x0 + shift, y0, x1 + shift, y1))
    up = mask(spec["up"], (x0, y0), running, frozen)
    down = mask(spec["down"], (x0, y0), running, frozen)
    # The two halves were lit a little differently: the running half's pixels
    # that fill the down lever's place get the frozen half's LOCAL brightness
    # (a wide blur of each, measured away from both levers), so the swap
    # leaves no lighter patch. Fine texture is below on-screen resolution.
    calm = Image.fromarray(np.where(np.asarray(ImageChops.lighter(up, down)) < 8, 255, 0).astype(np.uint8))

    def local(picture):
        wide = ImageFilter.GaussianBlur(24)
        weight = np.asarray(calm.filter(wide), np.float32)[..., None] / 255 + 1e-3
        masked = Image.composite(picture, Image.new("RGB", picture.size), calm)
        return np.asarray(masked.filter(wide), np.float32) / weight

    gain = (local(frozen) + 1) / (local(running) + 1)
    matched = Image.fromarray(np.clip(np.asarray(running, np.float32) * gain, 0, 255).astype(np.uint8))
    plate = Image.composite(matched, frozen, down)
    layers = {"plate": plate.convert("RGBA"), "up": running.convert("RGBA"), "down": frozen.convert("RGBA")}
    layers["plate"].putalpha(matte(plate))
    layers["up"].putalpha(ImageChops.multiply(up, matte(running)))
    layers["down"].putalpha(ImageChops.multiply(down, matte(frozen)))
    size = (round(running.width * SCALE), round(running.height * SCALE))
    for layer, picture in layers.items():
        picture.resize(size, Image.LANCZOS).save(OUT / f"freeze-{name}-{layer}.png", optimize=True)
    print(f"{name}: {size[0]}x{size[1]}, pivot {(spec['pivot'] - y0) / (y1 - y0):.4f}")


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    for skin, spec in SKINS.items():
        cut(folder, skin, spec)
