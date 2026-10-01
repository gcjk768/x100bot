"""Measurements the teacher grounds its claims in: clipping, luma, colour cast, tilt and sharpness, plus the image
copies (analysis, Claude, overlay) with all metadata stripped. Pillow and OpenCV only, no network."""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:   # HEIF support is optional on a dev machine
    pass

CELLS = ["top left", "top centre", "top right", "middle left", "middle centre", "middle right",
         "bottom left", "bottom centre", "bottom right"]


def load(path: Path) -> Image.Image:
    """Open JPEG, HEIF or PNG, apply the orientation tag, and return plain sRGB with no metadata attached."""
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        im = im.convert("RGB")
    out = Image.new("RGB", im.size)
    out.paste(im)   # a fresh image carries no EXIF, ICC or XMP
    return out


def resize_long_edge(im: Image.Image, long_edge: int) -> Image.Image:
    w, h = im.size
    scale = long_edge / max(w, h)
    if scale >= 1:
        return im.copy()
    return im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def save_stripped(im: Image.Image, out: Path, quality: int = 90) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, "JPEG", quality=quality, optimize=True)   # no exif argument: nothing is written
    return out


# measurements -----------------------------------------------------------------------------------------------------

def _luma(arr: np.ndarray) -> np.ndarray:
    return 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]


def exposure(arr: np.ndarray) -> dict:
    luma = _luma(arr)
    return {
        "highlights_clipped_pct": round(float((arr >= 250).any(axis=2).mean() * 100), 2),
        "shadows_crushed_pct": round(float((luma <= 5).mean() * 100), 2),
        "luma_mean": round(float(luma.mean()), 1), "luma_median": round(float(np.median(luma)), 1),
        "luma_p5": round(float(np.percentile(luma, 5)), 1), "luma_p95": round(float(np.percentile(luma, 95)), 1),
        "contrast": round(float(luma.std()), 1),
    }


def colour(arr: np.ndarray, cast_ab: float) -> dict:
    lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB).astype(np.float32)
    luma = _luma(arr)
    mask = (luma >= 30) & (luma <= 220)
    if mask.sum() < 100:
        mask = np.ones_like(luma, dtype=bool)
    a = float(lab[..., 1][mask].mean() - 128)
    b = float(lab[..., 2][mask].mean() - 128)
    if math.hypot(a, b) < cast_ab:
        cast = "neutral"
    elif abs(b) >= abs(a):
        cast = "warm" if b > 0 else "cool"
    else:
        cast = "magenta" if a > 0 else "green"
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    s, v = hsv[..., 1], hsv[..., 2]
    return {"a_mean": round(a, 1), "b_mean": round(b, 1), "cast": cast,
            "saturation_mean": round(float(s.mean()) / 255 * 100, 1),
            "oversaturated_pct": round(float(((s >= 240) & (v >= 60)).mean() * 100), 2)}


def tilt(grey: np.ndarray, min_lines: int, min_degrees: float) -> dict:
    """Canny edges, probabilistic Hough, lines longer than 15 percent of the width within 15 degrees of horizontal or
    vertical. tilt_degrees positive means rotate counterclockwise to level."""
    h, w = grey.shape
    edges = cv2.Canny(grey, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=60, minLineLength=int(0.15 * w), maxLineGap=12)
    devs, weights = [], []
    if lines is not None:
        for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
            theta = math.degrees(math.atan2(y2 - y1, x2 - x1))   # image coordinates, y down
            theta = (theta + 180) % 180 - 90 if abs(theta) > 90 else theta
            if abs(theta) <= 15:
                dev = theta
            elif abs(abs(theta) - 90) <= 15:
                dev = theta - 90 if theta > 0 else theta + 90
            else:
                continue
            devs.append(dev)
            weights.append(math.hypot(x2 - x1, y2 - y1))
    if not devs:
        return {"tilt_degrees": 0.0, "lines": 0, "confidence": "none", "tilted": False}
    order = np.argsort(devs)
    d, wts = np.array(devs)[order], np.array(weights)[order]
    cum = np.cumsum(wts)
    med = float(d[np.searchsorted(cum, cum[-1] / 2)])
    spread = float(np.sqrt(np.average((d - med) ** 2, weights=wts)))
    n = len(devs)
    confidence = "high" if n >= 2 * min_lines and spread < 1.0 else "medium" if n >= min_lines and spread < 2.5 else "low"
    # the sign flips: a line sloping down to the right (positive theta) needs a counterclockwise turn to level
    deg = round(med, 2)
    return {"tilt_degrees": deg, "lines": n, "spread": round(spread, 2), "confidence": confidence,
            "tilted": n >= min_lines and abs(deg) >= min_degrees and confidence != "low"}


def sharpness(grey_512: np.ndarray, blur_min: float) -> dict:
    h, w = grey_512.shape
    cells = {}
    for r in range(3):
        for c in range(3):
            cell = grey_512[r * h // 3:(r + 1) * h // 3, c * w // 3:(c + 1) * w // 3]
            cells[CELLS[r * 3 + c]] = round(float(cv2.Laplacian(cell, cv2.CV_64F).var()), 1)
    best = max(cells, key=cells.get)
    return {"cells": cells, "sharpest_cell": best, "max": cells[best], "blur_suspect": cells[best] < blur_min}


def measure(im: Image.Image, thresholds: dict) -> dict:
    """All measurements on a copy with the long edge at analysis size (the caller resizes)."""
    arr = np.asarray(im)
    grey = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    grey_512 = np.asarray(resize_long_edge(Image.fromarray(grey), 512))
    out = exposure(arr)
    out["colour"] = colour(arr, thresholds["cast_ab"])
    out["tilt"] = tilt(grey, int(thresholds["tilt_min_lines"]), thresholds["tilt_min_degrees"])
    out["sharpness"] = sharpness(grey_512, thresholds["blur_laplacian_min"])
    out["flags"] = {"highlights_clipped": out["highlights_clipped_pct"] >= thresholds["highlights_clipped_pct"],
                    "shadows_crushed": out["shadows_crushed_pct"] >= thresholds["shadows_crushed_pct"],
                    "cast": out["colour"]["cast"] != "neutral", "tilted": out["tilt"]["tilted"],
                    "blur_suspect": out["sharpness"]["blur_suspect"]}
    out["size"] = {"w": im.size[0], "h": im.size[1]}
    return out


# overlay -----------------------------------------------------------------------------------------------------------

def overlay(im: Image.Image, crop: dict | None, tilt_info: dict | None) -> Image.Image:
    """Rule of thirds grid, the suggested crop rectangle when given, and a level line when the tilt is confident."""
    out = im.copy()
    d = ImageDraw.Draw(out, "RGBA")
    w, h = out.size
    lw = max(1, w // 600)
    for i in (1, 2):
        d.line([(w * i // 3, 0), (w * i // 3, h)], fill=(255, 255, 255, 140), width=lw)
        d.line([(0, h * i // 3), (w, h * i // 3)], fill=(255, 255, 255, 140), width=lw)
    if crop:
        x0, y0 = int(crop["x"] * w), int(crop["y"] * h)
        x1, y1 = int((crop["x"] + crop["w"]) * w), int((crop["y"] + crop["h"]) * h)
        d.rectangle([x0, y0, x1, y1], outline=(255, 200, 0, 230), width=lw * 3)
    if tilt_info and tilt_info.get("tilted") and tilt_info.get("confidence") in ("medium", "high"):
        ang = math.radians(tilt_info["tilt_degrees"])
        cx, cy, half = w / 2, h / 2, w * 0.4
        d.line([(cx - half * math.cos(ang), cy - half * math.sin(ang)),
                (cx + half * math.cos(ang), cy + half * math.sin(ang))], fill=(255, 80, 80, 230), width=lw * 3)
        d.line([(cx - half, cy), (cx + half, cy)], fill=(80, 255, 120, 200), width=lw * 2)
    return out


def crop_preview(im: Image.Image, crop: dict) -> Image.Image:
    w, h = im.size
    return im.crop((int(crop["x"] * w), int(crop["y"] * h), int((crop["x"] + crop["w"]) * w), int((crop["y"] + crop["h"]) * h)))


@dataclass
class Prepared:
    folder: Path
    photo: Path          # the Claude copy, photo.jpg
    analysis: Image.Image
    overlay_base: Image.Image
    measurements: dict


def prepare(path: Path, folder: Path, cfg, thresholds: dict, name: str = "photo.jpg") -> Prepared:
    """Make the three copies and the measurements for one photo. The original stays where it was saved."""
    im = load(path)
    analysis = resize_long_edge(im, cfg.analysis_long_edge_px)
    claude = resize_long_edge(im, cfg.claude_long_edge_px)
    photo = save_stripped(claude, folder / name)
    return Prepared(folder, photo, analysis, resize_long_edge(im, cfg.overlay_long_edge_px), measure(analysis, thresholds))
