"""Recover the lost seed masks of the approved lamp from its prototype bundle.

build/6g/lamp/build.py read intermediates that were never kept: the tear
cavity (alpha.png) and the hand-cut masks of wheels 2, 5, 6, 7 and 9
(proto/m<k>.png). The approved prototype (mekanik_lamba.html) still carries
every layer at 0.64x, so this one-off step scales those alphas back to master
pixels and writes them to seed/, which build.py reads. Run once; the seed is
committed, so build.py alone reproduces the assets.

    python scripts/lamp_assets/recover_seed.py [build/6g/lamp]
"""
from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
SC = 0.64          # the prototype's scale
N = 1254           # master pixels


def bundle(src: Path) -> dict:
    html = (src / "mekanik_lamba.html").read_text(encoding="utf-8")
    return json.loads(re.search(r"const B=(\{.*?\});\n", html).group(1))


def alpha(data_url: str) -> np.ndarray:
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)[..., 3].astype(np.float32) / 255


def save(path: Path, img: np.ndarray) -> None:
    # cv2.imwrite cannot open non-ASCII paths (the checkout lives under "Masaüstü").
    path.write_bytes(cv2.imencode(".png", img)[1].tobytes())


def full(a: np.ndarray) -> np.ndarray:
    return cv2.resize(a, (N, N), interpolation=cv2.INTER_LINEAR)


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parents[1] / "build" / "6g" / "lamp"
    b = bundle(src)
    out = HERE / "seed"
    out.mkdir(exist_ok=True)
    # rim = A * (1 - blur(erode(cav, 9))), so where the matte is solid the rim's gap is the eroded cavity.
    base, rim = full(alpha(b["assets"]["base"])), full(alpha(b["assets"]["rim"]))
    eroded = ((base > 0.5) & (rim < 0.5 * base)).astype(np.uint8)
    cav = cv2.dilate(eroded, np.ones((9, 9), np.uint8))
    save(out / "alpha.png", cav * 255)
    parts = {p["n"]: p for p in b["meta"]["parts"]}
    for k in (2, 5, 6, 7, 9):
        p = parts[f"w{k}"]
        x0, y0 = round(p["x"] / SC), round(p["y"] / SC)
        w, h = round(p["w"] / SC), round(p["h"] / SC)
        a = cv2.resize(alpha(b["assets"][f"w{k}"]), (w, h), interpolation=cv2.INTER_LINEAR)
        m = np.zeros((N, N), np.uint8)
        m[y0:y0 + h, x0:x0 + w] = (a > 0.5).astype(np.uint8) * 255
        save(out / f"m{k}.png", m)
    print("seed written to", out)


if __name__ == "__main__":
    main()
