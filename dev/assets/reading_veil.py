"""Generate the reading veil behind an ordinary JARVIS reply.

A nine-slice navy sheet: fully covering over its rounded core, feathering to
nothing over the margin outside it - no perimeter, no tail, no card
silhouette. FeedView stretches its centre over the answer text and keeps the
corners; the strength is the QML opacity (Theme.glassReadingVeil), so the
asset only carries the falloff.

    python dev/assets/reading_veil.py

Writes jarvis/ui/qml/assets/reading-veil.png. Deterministic.
"""

import numpy as np
from PIL import Image

from glass_shells import OUT, downsample, rounded_sdf

SIZE = 120          # canvas
FEATHER = 36        # margin outside the core, where the veil fades to zero
RADIUS = 10         # the core's rounded corners
NAVY = (3, 14, 28)


def veil() -> np.ndarray:
    d, _, _ = rounded_sdf(SIZE, SIZE, FEATHER, RADIUS)
    # Inside the core: full. Outside: an eased falloff that reaches exactly
    # zero half a pixel before the canvas edge, so the stretched nine-slice
    # never shows its own boundary.
    t = np.clip(np.clip(d, 0, None) / (FEATHER - 0.5), 0, 1)
    alpha = (1 - t) ** 2 * (1 + 0.6 * t)          # soft shoulder, long toe
    rgb = np.broadcast_to(np.array(NAVY, np.float32), alpha.shape + (3,))
    return np.clip(np.dstack([downsample(rgb), downsample(alpha) * 255]), 0, 255).astype(np.uint8)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    Image.fromarray(veil(), "RGBA").save(OUT / "reading-veil.png", optimize=True)
    print(f"wrote reading-veil.png {SIZE}x{SIZE} border {FEATHER + RADIUS}")


if __name__ == "__main__":
    main()
