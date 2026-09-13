"""Generate the Reactor A core layers (jarvis/ui/qml/assets/core-*.png)
and the housing geometry QML reads (components/ReactorGeometry.qml).

The header core is a stack of pre-rendered, transparent raster layers that
QML only rotates, fades and scales - no shaders, no per-frame drawing. Each
layer is authored on a square canvas whose side is a fixed fraction of D,
the core's nominal diameter (canvas 640 px = 1.0 D unless noted), so QML
sizes every layer as `width: s * fraction` and the stack stays in
proportion at any size. Cool blue / blue-cyan only.

    python dev/assets/reactor_core.py

Writes into jarvis/ui/qml/assets/ and jarvis/ui/qml/components/.
Deterministic.
"""

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parents[2] / "jarvis" / "ui" / "qml" / "assets"
QML = OUT.parent / "components" / "ReactorGeometry.qml"
SS = 3                      # supersampling for anti-aliased strokes
N = 640                     # canvas side for 1.0 D layers

DEEP = (11, 29, 51)         # dark steel-blue material
EDGE = (45, 110, 160)       # material edge light
BLUE = (58, 168, 255)       # structural blue
BRIGHT = (127, 212, 255)    # highlight blue
WHITE = (230, 247, 255)     # white-blue, points and short arcs only


def rgba(colour, alpha):
    return (*colour, int(round(255 * alpha)))


def layer(n=N):
    return Image.new("RGBA", (n * SS, n * SS), (0, 0, 0, 0))


def finish(im, n=N, blur=0.0):
    """Downsample a supersampled layer; optional soft blur (px at 1x)."""
    out = im.resize((n, n), Image.LANCZOS)
    if blur:
        out = out.filter(ImageFilter.GaussianBlur(blur))
    return out


def over(base, top):
    return Image.alpha_composite(base, top)


def glow(im, sigma, gain, n=N):
    """A blurred copy of `im` at 1x, alpha scaled by `gain` (soft light)."""
    soft = finish(im, n).filter(ImageFilter.GaussianBlur(sigma))
    a = np.asarray(soft).astype(np.float32)
    a[..., 3] = np.clip(a[..., 3] * gain, 0, 255)
    return Image.fromarray(a.astype(np.uint8), "RGBA")


def arc(draw, c, r, width, start, sweep, fill):
    """Annular arc centred at c (SS coordinates), radius r, stroke `width`."""
    box = [c - r, c - r, c + r, c + r]
    draw.arc(box, start, start + sweep, fill=fill, width=max(1, int(round(width))))


def radial(draw, c, r0, r1, ang, fill, width=1):
    a = np.deg2rad(ang)
    draw.line([(c + r0 * np.cos(a), c + r0 * np.sin(a)), (c + r1 * np.cos(a), c + r1 * np.sin(a))],
              fill=fill, width=max(1, int(round(width))))


def disc_falloff(n, r_in, r_out, colour, alpha, cx=None, cy=None, power=1.0):
    """A filled disc, opaque out to r_in, fading to zero at r_out (1x px)."""
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32) + 0.5
    cx = n / 2 if cx is None else cx
    cy = n / 2 if cy is None else cy
    d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    t = np.clip((r_out - d) / max(r_out - r_in, 1e-3), 0, 1) ** power
    out = np.zeros((n, n, 4), np.float32)
    out[..., :3] = colour
    out[..., 3] = 255 * alpha * t
    return Image.fromarray(out.astype(np.uint8), "RGBA")


def gaussian_spot(n, sigma, colour, alpha, cx=None, cy=None):
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32) + 0.5
    cx = n / 2 if cx is None else cx
    cy = n / 2 if cy is None else cy
    d2 = (xx - cx) ** 2 + (yy - cy) ** 2
    out = np.zeros((n, n, 4), np.float32)
    out[..., :3] = colour
    out[..., 3] = 255 * alpha * np.exp(-d2 / (2 * sigma ** 2))
    return Image.fromarray(out.astype(np.uint8), "RGBA")


def wedge(n, r0, r1, centre_deg, sigma_deg, colour, alpha, rings=()):
    """A soft angular highlight between radii r0..r1 (1x px), gaussian in
    angle; brighter where it crosses the given ring radii."""
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32) + 0.5
    dx, dy = xx - n / 2, yy - n / 2
    r = np.sqrt(dx ** 2 + dy ** 2)
    ang = (np.degrees(np.arctan2(dy, dx)) - centre_deg + 180) % 360 - 180
    a = np.exp(-(ang / sigma_deg) ** 2 / 2)
    band = np.clip((r - r0) / 10, 0, 1) * np.clip((r1 - r) / 10, 0, 1)
    profile = 0.28 + 0.0 * r
    for rr, w, g in rings:
        profile = profile + g * np.exp(-((r - rr) / w) ** 2 / 2)
    out = np.zeros((n, n, 4), np.float32)
    out[..., :3] = colour
    out[..., 3] = 255 * alpha * a * band * np.clip(profile, 0, 1)
    return Image.fromarray(out.astype(np.uint8), "RGBA")


def save(im, name):
    im.save(OUT / name, optimize=True)
    print(f"  {name:<28} {im.width}x{im.height}")


# -- L1: the dark centre bed (1.0 D canvas, disc 0.56 D) -----------------------
def bed():
    # Opaque to 0.24 D, a short soft edge to 0.285 D. Flat material: not a sphere.
    im = disc_falloff(N, 0.24 * N, 0.285 * N, (3, 9, 18), 1.0, power=1.4)
    save(im, "core-bed.png")


# -- L2: the optical chamber (0.5 D canvas, chamber 0.36 D) --------------------
def chamber():
    n = 320
    c = n * SS / 2
    R = 0.36 * N / 2 * SS          # 115.2 px at 1x
    # Depth: a dark navy disc, slightly lighter toward the upper left.
    base = disc_falloff(n, R / SS - 1.5, R / SS + 0.5, (4, 11, 22), 1.0)
    depth = gaussian_spot(n, 62, (12, 30, 54), 0.85, cx=n / 2 - 22, cy=n / 2 - 26)
    inner = gaussian_spot(n, 30, (22, 70, 120), 0.35)
    # Keep the lighting inside the disc.
    mask = np.asarray(base)[..., 3:4].astype(np.float32) / 255
    lit = np.asarray(over(depth, inner)).astype(np.float32)
    lit[..., 3:4] *= mask
    im = over(base, Image.fromarray(lit.astype(np.uint8), "RGBA"))
    # The rim: continuous, never uniform - drawn as many short arcs whose
    # alpha follows the light (upper-left and lower-right catch more).
    rim = layer(n); d = ImageDraw.Draw(rim)
    for k in range(120):
        a0 = k * 3
        light = 0.5 + 0.5 * max(np.cos(np.deg2rad(a0 + 1.5 - 205)), 0) ** 2 + 0.22 * max(np.cos(np.deg2rad(a0 + 1.5 - 20)), 0) ** 2
        arc(d, c, R - 1.5 * SS, 2.4 * SS, a0, 3.4, rgba(BLUE, min(light, 1.0)))
    im = over(im, glow(rim, 3.0, 0.5, n))
    im = over(im, finish(rim, n))
    # Four fixed focus marks: hairline, quiet, close to the focus.
    marks = layer(n); d = ImageDraw.Draw(marks)
    for a in (0, 90, 180, 270):
        radial(d, c, 0.052 * N * SS, 0.072 * N * SS, a, rgba(BRIGHT, 0.42), 1.0 * SS)
    im = over(im, finish(marks, n))
    save(im, "core-chamber.png")

    # The rim highlights: two short opposing light arcs (their own layer,
    # so the 11.3 s rim change is one opacity animation).
    hi = layer(n); d = ImageDraw.Draw(hi)
    for a0, sw in ((196, 26), (16, 20)):
        arc(d, c, R - 1.5 * SS, 2.4 * SS, a0, sw, rgba(WHITE, 0.95))
    out = over(glow(hi, 4.0, 0.7, n), finish(hi, n))
    save(out, "core-rim-hi.png")

    # Very slow internal light: one soft off-centre lobe, rotated by QML.
    lobe = gaussian_spot(n, 26, (40, 120, 190), 0.28, cx=n / 2 + 34, cy=n / 2 - 10)
    lo = np.asarray(lobe).astype(np.float32)
    lo[..., 3:4] *= disc_mask(n, R / SS - 6, R / SS - 2)
    save(Image.fromarray(lo.astype(np.uint8), "RGBA"), "core-chamber-light.png")


def disc_mask(n, r_in, r_out):
    return np.asarray(disc_falloff(n, r_in, r_out, (0, 0, 0), 1.0))[..., 3:4].astype(np.float32) / 255


# -- the focus: a small dense point in a thin light bowl (0.2 D canvas) --------
def focus():
    n = 128                          # 0.2 D
    px = n / 0.2 / N                 # px per (1/N D) ... 1 D = 640 px -> here 1 D = 640 px too
    core_r = 0.035 * N / 2 * px      # focus radius in canvas px (11.2)
    im = gaussian_spot(n, core_r * 0.62, WHITE, 1.0)
    im = over(gaussian_spot(n, core_r * 1.25, BRIGHT, 0.55), im)
    # A thin bowl of light around the point, then dark again: no filled disc.
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32) + 0.5
    r = np.sqrt((xx - n / 2) ** 2 + (yy - n / 2) ** 2)
    bowl = np.zeros((n, n, 4), np.float32); bowl[..., :3] = BLUE
    bowl[..., 3] = 255 * 0.5 * np.exp(-((r - core_r * 1.9) / (core_r * 0.55)) ** 2 / 2)
    im = over(Image.fromarray(bowl.astype(np.uint8), "RGBA"), im)
    save(im, "core-focus.png")
    # The wider, softer halo the state intensity drives.
    save(gaussian_spot(256, 46, (42, 150, 230), 0.5), "core-focus-glow.png")


# -- L3: inner mechanical structure (0.48-0.60 D) ------------------------------
SECTOR_SPAN = 46          # degrees lit per sector; 14 degree dark gaps
SECTOR_R0, SECTOR_R1 = 0.24, 0.30   # the housing band, as fractions of D
BEZEL = 4                 # canvas px of dark housing kept around an emitting face (~1.9 px at 1920)


def sectors():
    c = N * SS / 2
    r0, r1 = SECTOR_R0 * N * SS, SECTOR_R1 * N * SS
    band = r1 - r0
    im = layer(); d = ImageDraw.Draw(im)
    for k in range(6):
        a = -90 + 60 * k - SECTOR_SPAN / 2
        arc(d, c, (r0 + r1) / 2, band, a, SECTOR_SPAN, rgba(DEEP, 0.92))
        arc(d, c, r0 + 1.2 * SS, 1.6 * SS, a, SECTOR_SPAN, rgba(EDGE, 0.75))
        arc(d, c, r1 - 1.2 * SS, 1.6 * SS, a, SECTOR_SPAN, rgba(EDGE, 0.55))
        # Fine radial hairlines inside each sector, and end walls.
        for j in (0.25, 0.5, 0.75):
            radial(d, c, r0 + 3 * SS, r1 - 3 * SS, a + SECTOR_SPAN * j, rgba(EDGE, 0.28), 1.0 * SS)
        radial(d, c, r0, r1, a, rgba(EDGE, 0.6), 1.2 * SS)
        radial(d, c, r0, r1, a + SECTOR_SPAN, rgba(EDGE, 0.6), 1.2 * SS)
        # A short bright index arc at the middle of the outer edge.
        arc(d, c, r1 - 1.4 * SS, 2.0 * SS, a + SECTOR_SPAN / 2 - 6, 12, rgba(BLUE, 0.8))
    # Dark gap floors: a faint continuous inner track so the gaps read as depth.
    arc(d, c, r0 - 4 * SS, 1.0 * SS, 0, 360, rgba(EDGE, 0.32))
    # A dotted track through the gaps' middle radius.
    for k in range(48):
        a = k * 7.5
        arc(d, c, (r0 + r1) / 2, 1.2 * SS, a, 1.6, rgba(BRIGHT, 0.35))
    out = over(glow(im, 2.5, 0.35), finish(im))
    save(out, "core-sectors.png")

    # One lit sector (sector 0, at the top): rotate by k*60 for sector k.
    # The light is an emitting face INSET into the same housing: the dark
    # bezel stays visible all round, a narrow edge highlight follows the
    # face contour, and the bloom is short enough never to reach the next
    # housing across a separator.
    lit = layer(); d = ImageDraw.Draw(lit)
    a = -90 - SECTOR_SPAN / 2
    b = BEZEL * SS
    ang_inset = np.degrees(b / ((r0 + r1) / 2))          # the same bezel, angularly
    f0, f1 = r0 + b, r1 - b
    arc(d, c, (f0 + f1) / 2, f1 - f0, a + ang_inset, SECTOR_SPAN - 2 * ang_inset, rgba(BLUE, 0.46))
    hi = layer(); dh = ImageDraw.Draw(hi)
    arc(dh, c, f0 + 0.6 * SS, 1.2 * SS, a + ang_inset, SECTOR_SPAN - 2 * ang_inset, rgba(BRIGHT, 0.95))
    arc(dh, c, f1 - 0.6 * SS, 1.2 * SS, a + ang_inset, SECTOR_SPAN - 2 * ang_inset, rgba(BRIGHT, 0.8))
    radial(dh, c, f0, f1, a + ang_inset, rgba(BRIGHT, 0.85), 1.2 * SS)
    radial(dh, c, f0, f1, a + SECTOR_SPAN - ang_inset, rgba(BRIGHT, 0.85), 1.2 * SS)
    out = over(over(over(glow(lit, 1.6, 0.35), finish(lit)), glow(hi, 1.2, 0.5)), finish(hi))
    save(out, "core-sector-lit.png")


# -- L4 / L5: the two mechanical rings -----------------------------------------
MAIN_ARCS = ((0, 62, 1.0), (79, 18, 0.65), (108, 50, 0.9), (170, 11, 0.55),
             (194, 72, 1.0), (279, 28, 0.7), (318, 30, 0.85))
SECOND_ARCS = ((8, 40, 0.7), (66, 96, 0.55), (186, 22, 0.8), (226, 58, 0.6), (298, 46, 0.7))
MAIN_R, MAIN_W = 0.33, 4.2            # radius as a fraction of D, stroke in canvas px
SECOND_R, SECOND_W = 0.37, 2.3


def geometry_qml():
    """The housing geometry, for QML: every emitting region JarvisCore draws
    (micro-signals, the success segment) takes its centre, radius, band,
    angles and bezel from here, so light and housing are one object."""
    def arcs(table):
        return "[" + ", ".join(f"[{a0}, {sw}]" for a0, sw, _g in table) + "]"
    text = f"""// GENERATED by dev/assets/reactor_core.py - do not edit by hand.
// The housing geometry the Reactor A rasters were drawn with (fractions of
// D, the core's diameter; angles in degrees, in each housing's own frame).
import QtQuick

QtObject {{
    readonly property real sectorInner: {SECTOR_R0}
    readonly property real sectorOuter: {SECTOR_R1}
    readonly property real sectorSpan: {SECTOR_SPAN}
    readonly property var sectorCentres: [-90, -30, 30, 90, 150, 210]
    readonly property real mainRadius: {MAIN_R}
    readonly property real mainBand: {MAIN_W / N:.5f}
    readonly property var mainArcs: {arcs(MAIN_ARCS)}
    readonly property real secondRadius: {SECOND_R}
    readonly property real secondBand: {SECOND_W / N:.5f}
    readonly property var secondArcs: {arcs(SECOND_ARCS)}
    readonly property real bezel: {BEZEL / N:.5f}
}}
"""
    QML.write_text(text, encoding="utf-8")
    print(f"  {QML.name}")


def rings():
    c = N * SS / 2
    # Main ring, 0.66 D: interrupted arcs of different lengths and weights.
    r = MAIN_R * N * SS
    im = layer(); d = ImageDraw.Draw(im)
    for a0, sw, g in MAIN_ARCS:
        arc(d, c, r, MAIN_W * SS, a0, sw, rgba(BLUE if g < 0.9 else BRIGHT, min(1.0, g + 0.15)))
        # Short end ticks, inward.
        for e in (a0, a0 + sw):
            radial(d, c, r - 6 * SS, r - 1.5 * SS, e, rgba(BLUE, 0.7 * g), 1.2 * SS)
    # A faint continuous track under the arcs.
    arc(d, c, r, 1.0 * SS, 0, 360, rgba(EDGE, 0.30))
    out = over(glow(im, 4.0, 0.85), finish(im))
    save(out, "core-ring-main.png")

    # Secondary ring, 0.74 D: thinner, dimmer, a different segment layout.
    r = SECOND_R * N * SS
    im = layer(); d = ImageDraw.Draw(im)
    for a0, sw, g in SECOND_ARCS:
        arc(d, c, r, SECOND_W * SS, a0, sw, rgba(BLUE, min(1.0, g + 0.1)))
    for k in range(18):                       # a dotted stretch between arcs
        arc(d, c, r, 1.6 * SS, 128 + k * 3.2, 1.1, rgba(BLUE, 0.55))
    arc(d, c, r, 1.0 * SS, 0, 360, rgba(EDGE, 0.22))
    out = over(glow(im, 3.5, 0.6), finish(im))
    save(out, "core-ring-secondary.png")


# -- L6: the energy sweep (a narrow highlight over the rings) ------------------
def sweep():
    im = wedge(N, 0.46 * N / 2, 0.78 * N / 2, 0, 9.0, BRIGHT, 0.6,
               rings=((0.66 * N / 2, 3.0, 0.9), (0.74 * N / 2, 2.5, 0.7), (0.60 * N / 2, 2.0, 0.4)))
    save(im, "core-sweep.png")


# -- L7: the energy point (0.05 D canvas) --------------------------------------
def dot():
    n = 32
    im = gaussian_spot(n, 7.0, BLUE, 0.55)
    im = over(im, gaussian_spot(n, 2.6, WHITE, 1.0))
    save(im, "core-dot.png")


# -- L8: fixed tick groups ------------------------------------------------------
def ticks():
    c = N * SS / 2
    im = layer(); d = ImageDraw.Draw(im)
    # Six groups of five hairline ticks just inside the main ring, offset
    # from the sector gaps so they read as a separate scale.
    r = 0.63 * N / 2 * SS
    for k in range(6):
        base = -60 + 60 * k
        for j in range(5):
            radial(d, c, r - 3.5 * SS, r + 3.5 * SS, base + (j - 2) * 3.0, rgba(BLUE, 0.55 if j == 2 else 0.38), 1.0 * SS)
    # A fine outer scale outside the secondary ring: every sixth tick longer.
    r = 0.775 * N / 2 * SS
    for k in range(72):
        long = k % 6 == 0
        radial(d, c, r, r + (7 if long else 3.5) * SS, k * 5, rgba(BLUE, 0.42 if long else 0.24), 1.0 * SS)
    save(finish(im), "core-ticks.png")


# -- event light: a soft ring that scales outward, and a restrained halo ------
def wave():
    yy, xx = np.mgrid[0:N, 0:N].astype(np.float32) + 0.5
    r = np.sqrt((xx - N / 2) ** 2 + (yy - N / 2) ** 2)
    out = np.zeros((N, N, 4), np.float32); out[..., :3] = BRIGHT
    out[..., 3] = 255 * (0.85 * np.exp(-((r - 0.40 * N) / 5.0) ** 2 / 2)
                         + 0.18 * np.exp(-((0.40 * N - r) / 26.0)) * (r < 0.40 * N))
    save(Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGBA"), "core-wave.png")
    save(gaussian_spot(N, 105, (31, 127, 208), 0.30), "core-halo.png")


if __name__ == "__main__":
    print("writing", OUT)
    bed(); chamber(); focus(); sectors(); rings(); sweep(); dot(); ticks(); wave(); geometry_qml()
