"""Cut the approved mechanical status lamp (6G S1) into QML layers.

A copy of build/6g/lamp/build.py (the approved prototype's pipeline). Changed
here only: the inputs are master_v1.png + plate_v1.png and the recovered seed
masks (recover_seed.py); the layers are PNG at master resolution (scale 1),
each cropped to its own alpha box, with the baked edge shadow split into its
own layer (shadow under plate composites to the same pixels); the part map is
written to parts.json for MechanicalLamp.qml and its tests.

    python scripts/lamp_assets/build.py [build/6g/lamp] [--out DIR] [--scale S]
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from parts import CHAINS, COIL, LEVERS, OCCL, PLANETS, W  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("src", nargs="?", default=str(HERE.parents[1] / "build" / "6g" / "lamp"))
ap.add_argument("--out", default=str(HERE.parents[1] / "jarvis" / "ui" / "qml" / "assets" / "lamp"))
ap.add_argument("--scale", type=float, default=1.0)
args = ap.parse_args()
SRC, OUT, SC = Path(args.src), Path(args.out), args.scale
OUT.mkdir(parents=True, exist_ok=True)


def read(path: Path, flags=cv2.IMREAD_COLOR):
    # cv2.imread cannot open non-ASCII paths (the checkout lives under "Masaüstü").
    return cv2.imdecode(np.fromfile(str(path), np.uint8), flags)


M = read(SRC / "master_v1.png")
P = read(SRC / "plate_v1.png")
H, Wd = M.shape[:2]
yy, xx = np.mgrid[:H, :Wd]
hsv = cv2.cvtColor(M, cv2.COLOR_BGR2HSV)
brass = (hsv[..., 0] >= 8) & (hsv[..., 0] <= 30) & (hsv[..., 1] >= 55) & (hsv[..., 2] >= 70)
# ---------- tear matte (full, with flaps) ----------
poly = np.array(
    [
        (20, 490),
        (45, 420),
        (75, 350),
        (100, 280),
        (130, 190),
        (200, 130),
        (290, 90),
        (370, 40),
        (420, 25),
        (480, 40),
        (560, 60),
        (640, 80),
        (720, 75),
        (790, 95),
        (850, 140),
        (930, 180),
        (1000, 200),
        (1060, 240),
        (1120, 310),
        (1180, 380),
        (1220, 430),
        (1235, 500),
        (1225, 560),
        (1245, 585),
        (1200, 650),
        (1160, 720),
        (1145, 800),
        (1145, 880),
        (1135, 960),
        (1110, 1020),
        (1070, 1080),
        (1060, 1205),
        (990, 1170),
        (930, 1140),
        (880, 1110),
        (820, 1085),
        (760, 1100),
        (700, 1110),
        (620, 1110),
        (540, 1090),
        (470, 1065),
        (420, 1045),
        (370, 1005),
        (340, 960),
        (310, 905),
        (240, 855),
        (150, 835),
        (55, 805),
        (25, 760),
        (60, 720),
        (85, 690),
        (65, 620),
        (35, 560),
    ],
    np.int32,
)  # noqa: E231,E501
PP = np.zeros((H, Wd), np.uint8)
cv2.fillPoly(PP, [poly], 1)
Pd = cv2.dilate(PP, np.ones((41, 41), np.uint8))
small = cv2.resize(M, (Wd // 4, H // 4), interpolation=cv2.INTER_AREA)
surf = cv2.inpaint(
    small, cv2.resize(Pd * 255, (Wd // 4, H // 4), interpolation=cv2.INTER_NEAREST), 15, cv2.INPAINT_TELEA
)
surf = cv2.GaussianBlur(cv2.resize(surf, (Wd, H), interpolation=cv2.INTER_CUBIC), (31, 31), 10)
L = cv2.cvtColor(M, cv2.COLOR_BGR2LAB)[..., 0].astype(np.float32)
SL = cv2.cvtColor(surf, cv2.COLOR_BGR2LAB)[..., 0].astype(np.float32)
diff = L - SL
cav = (read(HERE / "seed" / "alpha.png", cv2.IMREAD_GRAYSCALE) > 128).astype(np.uint8)
bright = cv2.morphologyEx(((diff > 7) & (Pd > 0)).astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
m = cv2.morphologyEx(np.maximum(cav, bright), cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
n, labels, st, _ = cv2.connectedComponentsWithStats(m)
keep = np.zeros_like(m)
for i in range(1, n):
    if st[i, cv2.CC_STAT_AREA] >= 6:
        keep[labels == i] = 1
ff = keep * 255
fm = np.zeros((H + 2, Wd + 2), np.uint8)
cv2.floodFill(ff, fm, (0, 0), 128)
keep = np.where(ff == 128, 0, 1).astype(np.uint8)
a = cv2.GaussianBlur(keep.astype(np.float32), (3, 3), 0.8)
near = (cv2.dilate(keep, np.ones((81, 81), np.uint8)) > 0) & (keep == 0)
dark = cv2.GaussianBlur((np.clip(-diff / np.maximum(SL, 1) * 1.8, 0, 0.8) * near).astype(np.float32), (7, 7), 2)
A = a + dark * (1 - a)
cavE = cv2.erode(cav, np.ones((9, 9), np.uint8)).astype(np.float32)
cavE = cv2.GaussianBlur(cavE, (9, 9), 3)
# ---------- base: master with plate inside moving regions ----------
base = M.astype(np.float32).copy()


def blend_region(mask, src):
    global base
    f = cv2.GaussianBlur(mask.astype(np.float32), (7, 7), 2)[..., None]
    base = base * (1 - f) + src.astype(np.float32) * f


see = np.zeros((H, Wd), np.uint8)
masks = {}
for k, (cx, cy, r, kind) in W.items():
    d2 = (xx - cx) ** 2 + (yy - cy) ** 2
    circ = d2 <= r * r
    if kind == "see":
        if k in (1, 3):
            mk = ((brass & circ) | (d2 <= (28 if k == 1 else 14) ** 2)).astype(np.uint8)
            mk = cv2.morphologyEx(mk, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        else:
            mk = (read(HERE / "seed" / f"m{k}.png", cv2.IMREAD_GRAYSCALE) > 0).astype(np.uint8)
        see |= (d2 <= (r + 4) ** 2).astype(np.uint8)
    elif kind == "disc":
        mk = circ.astype(np.uint8)
    else:
        mk = (read(HERE / "seed" / f"m{k}.png", cv2.IMREAD_GRAYSCALE) > 0).astype(np.uint8)
        see |= ((d2 <= (r + 3) ** 2) & (d2 >= 64 * 64)).astype(np.uint8)
    masks[k] = mk
lev_plate = np.zeros((H, Wd), np.uint8)
for k, (pv, pl) in LEVERS.items():
    if k in (16, 17):
        t = np.zeros((H, Wd), np.uint8)
        cv2.fillPoly(t, [np.array(pl, np.int32)], 1)
        lev_plate |= cv2.dilate(t, np.ones((9, 9), np.uint8))
blend_region(see | lev_plate, P)
# occluders on top
occ = np.zeros((H, Wd), np.uint8)
for nm, pl in OCCL.items():
    cv2.fillPoly(occ, [np.array(pl, np.int32)], 1)
cx, cy, r, _ = W[9]
d2 = (xx - cx) ** 2 + (yy - cy) ** 2
ann = (d2 <= (r + 3) ** 2) & (d2 >= 64 * 64)
nb = ((~brass) & ann).astype(np.uint8)
nb = cv2.morphologyEx(nb, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
occ |= nb
(cx0, cy0), (cx1, cy1), _p = COIL
occ[cy0:cy1, cx0:cx1] = 0
# ---------- export ----------
layers = {}
parts = []


def save(name, bgr, alpha, x0=0, y0=0, crop=True):
    """One RGBA PNG cropped to its alpha box (a strip keeps its frame); colour zeroed where it is transparent."""
    al = np.clip(alpha * 255 + 0.5, 0, 255).astype(np.uint8)
    img = np.dstack([bgr.astype(np.uint8), al])
    if SC != 1:
        img = cv2.resize(
            img, (max(1, round(img.shape[1] * SC)), max(1, round(img.shape[0] * SC))), interpolation=cv2.INTER_AREA
        )
    ys, xs = np.where(img[..., 3] > 0) if crop else np.mgrid[: img.shape[0], : img.shape[1]]
    bx0, by0, bx1, by1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
    img = img[by0:by1, bx0:bx1].copy()
    # ponytail: 5 bits per colour channel (error <= 4/255, finer than the approved WebP q88) keeps the
    # master-resolution set under 3 MB (lossless: 4.5 MB); drop this line if the cap is ever lifted.
    img[..., :3] = img[..., :3] >> 3 << 3 | 4
    img[img[..., 3] == 0, :3] = 0
    buf = io.BytesIO()
    Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA)).save(buf, "PNG", optimize=True)
    (OUT / f"{name}.png").write_bytes(buf.getvalue())
    layers[name] = {
        "x": round(x0 * SC) + int(bx0),
        "y": round(y0 * SC) + int(by0),
        "w": int(bx1 - bx0),
        "h": int(by1 - by0),
    }


Mf = np.clip(base, 0, 255)
rim = A * (1 - cavE)
# Where the rim (drawn last) is opaque nothing under it shows: leave those pixels out below.
hidden = rim >= 254 / 255
# The prototype's base = plate with alpha A = a + dark*(1-a): the edge shadow under the matte.
save("shadow", Mf, np.where(hidden, 0, dark * (1 - a)))
save("plate", Mf, np.where(hidden, 0, a))


def cut(name, mask, cx, cy, extra=None):
    ys, xs = np.where(mask > 0)
    x0, y0, x1, y1 = xs.min() - 2, ys.min() - 2, xs.max() + 3, ys.max() + 3
    al = cv2.GaussianBlur(mask.astype(np.float32), (3, 3), 0.7)
    save(name, M[y0:y1, x0:x1], al[y0:y1, x0:x1], x0, y0)
    d = {"n": name, **layers[name], "cx": cx * SC, "cy": cy * SC}
    if extra:
        d.update(extra)
    return d


for k in W:
    cx, cy, r, kind = W[k]
    parts.append(cut(f"w{k}", masks[k], cx, cy, {"id": k, "r": r}))
for nm, (px, py, pr) in PLANETS.items():
    mk = (((xx - px) ** 2 + (yy - py) ** 2) <= pr * pr).astype(np.uint8)
    parts.append(cut(nm, mk, px, py, {"id": nm, "r": pr}))
for k, (pv, pl) in LEVERS.items():
    if k not in (16, 17):
        continue
    mk = np.zeros((H, Wd), np.uint8)
    cv2.fillPoly(mk, [np.array(pl, np.int32)], 1)
    parts.append(cut(f"l{k}", mk, pv[0], pv[1], {"id": f"l{k}"}))
# occluder + rim layers (full frame)
oc_al = cv2.GaussianBlur(occ.astype(np.float32), (3, 3), 0.7)
save("occ", M, np.where(hidden, 0, oc_al))
save("rim", M, np.clip(rim, 0, 1))
# strips (chains, coil)
strips = []
for nm, (p0, p1, hw, pitch, sg) in CHAINS.items():
    ang = math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0]))
    ln = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    R = cv2.getRotationMatrix2D(p0, ang, 1.0)
    rot = cv2.warpAffine(M, R, (Wd, H), flags=cv2.INTER_LINEAR)
    s = rot[int(p0[1] - hw) : int(p0[1] + hw), int(p0[0] - pitch) : int(p0[0] + ln + pitch)]
    al = np.full(s.shape[:2], 255, np.uint8)
    f = np.linspace(0, 1, 8)
    al[:, :8] = (f * 255).astype(np.uint8)
    al[:, -8:] = (f[::-1] * 255).astype(np.uint8)
    al[:2, :] = al[:2, :] // 2
    al[-2:, :] = al[-2:, :] // 2
    save("s_" + nm, s, al / 255, crop=False)
    strips.append(
        {
            "n": "s_" + nm,
            "w": layers["s_" + nm]["w"],
            "h": layers["s_" + nm]["h"],
            "x0": p0[0] * SC,
            "y0": p0[1] * SC,
            "ang": ang,
            "len": ln * SC,
            "hw": hw * SC,
            "pitch": pitch * SC,
            "sg": sg,
            "pad": pitch * SC,
        }
    )
(x0, y0), (x1, y1), pitch = COIL
s = M[y0:y1, int(x0 - pitch) : x1]
al = np.full(s.shape[:2], 255, np.uint8)
f = np.linspace(0, 1, 6)
al[:, :6] = (f * 255).astype(np.uint8)
al[:, -6:] = (f[::-1] * 255).astype(np.uint8)
save("s_coil", s, al / 255, crop=False)
strips.append(
    {
        "n": "s_coil",
        "w": layers["s_coil"]["w"],
        "h": layers["s_coil"]["h"],
        "x0": x0 * SC,
        "y0": (y0 + y1) / 2 * SC,
        "ang": 0,
        "len": (x1 - x0) * SC,
        "hw": (y1 - y0) / 2 * SC,
        "pitch": pitch * SC,
        "sg": 1,
        "pad": pitch * SC,
    }
)
meta = {
    "W": Wd * SC,
    "H": H * SC,
    "layers": {k: layers[k] for k in ("shadow", "plate", "occ", "rim")},
    "parts": parts,
    "strips": strips,
}
(OUT / "parts.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
total = sum(p.stat().st_size for p in OUT.glob("*.png"))
print("layers", len(layers), "parts", len(parts), "png bytes", total)
