"""exiftool readout of Fujifilm settings from an original JPEG or HEIF, and the closest library recipe.
Tag names follow exiftool's Fujifilm maker note group (MakerNotes) and standard EXIF; they are confirmed against a
real X100VI file during the build (see tests/fixtures/exif/)."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# exiftool -G1 keys -> our field names. Several candidates per field, first present wins.
TAGS = {
    "model": ["IFD0:Model"],
    "focal_length": ["ExifIFD:FocalLength"],
    "aperture": ["ExifIFD:FNumber"],
    "shutter": ["ExifIFD:ExposureTime"],
    "iso": ["ExifIFD:ISO"],
    "exposure_compensation": ["ExifIFD:ExposureCompensation"],
    "exposure_mode": ["ExifIFD:ExposureProgram"],
    "metering": ["ExifIFD:MeteringMode"],
    "focus_mode": ["Fujifilm:FocusMode"],
    "af_area": ["Fujifilm:AFMode", "Fujifilm:AFAreaMode"],
    "flash": ["ExifIFD:Flash", "Fujifilm:FlashMode"],
    "shutter_type": ["Fujifilm:ShutterType"],
    "image_size": ["File:ImageSize", "Composite:ImageSize"],
    "film_simulation": ["Fujifilm:FilmMode"],
    "saturation": ["Fujifilm:Saturation"],           # Fujifilm stores monochrome modes here too
    "grain": ["Fujifilm:GrainEffectRoughness", "Fujifilm:GrainEffect"],
    "grain_size": ["Fujifilm:GrainEffectSize"],
    "color_chrome_effect": ["Fujifilm:ColorChromeEffect"],
    "color_chrome_fx_blue": ["Fujifilm:ColorChromeFXBlue"],
    "white_balance": ["Fujifilm:WhiteBalance"],
    "wb_fine_tune": ["Fujifilm:WhiteBalanceFineTune"],
    "dynamic_range": ["Fujifilm:DynamicRangeSetting", "Fujifilm:DevelopmentDynamicRange"],
    "d_range_priority": ["Fujifilm:DRangePriority"],
    "highlight": ["Fujifilm:HighlightTone"],
    "shadow": ["Fujifilm:ShadowTone"],
    "color": ["Fujifilm:Saturation"],
    "sharpness": ["Fujifilm:Sharpness"],
    "noise_reduction": ["Fujifilm:NoiseReduction"],
    "clarity": ["Fujifilm:Clarity"],
}
GPS_RE = re.compile(r"gps|location|latitude|longitude|altitude", re.I)


def available() -> bool:
    return shutil.which("exiftool") is not None


def read_raw(path: Path) -> dict:
    """exiftool -j -G1 on one file, with every GPS or location tag removed before anything else sees it."""
    out = subprocess.run(["exiftool", "-j", "-G1", "-n", str(path)], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"exiftool failed: {out.stderr[:200]}")
    data = json.loads(out.stdout)[0]
    return {k: v for k, v in data.items() if not GPS_RE.search(k)}


def map_tags(raw: dict) -> dict:
    """Our field names from exiftool's. Unknown fields are simply absent."""
    found = {}
    for field, keys in TAGS.items():
        for k in keys:
            if k in raw and raw[k] not in ("", None):
                found[field] = raw[k]
                break
    if "shutter" in found and isinstance(found["shutter"], (int, float)) and found["shutter"] > 0:
        found["shutter_text"] = f"1/{round(1 / found['shutter'])}" if found["shutter"] < 1 else f"{found['shutter']:g} s"
    return found


def read(path: Path) -> dict:
    return map_tags(read_raw(path))


def _num_tone(v):
    """Fujifilm stores tone values as integers in units of 1/8 or 1/16 on some bodies; -n gives the raw number.
    The mapping is confirmed with the real file; until then the raw value is shown."""
    return v


def to_recipe_like(fields: dict) -> dict:
    """A settings dict in the library's shape for the distance comparison, with what the tags give."""
    return {k: fields.get(k) for k in ("film_simulation", "grain", "grain_size", "color_chrome_effect",
                                       "color_chrome_fx_blue", "white_balance", "dynamic_range", "highlight",
                                       "shadow", "color", "sharpness", "noise_reduction", "clarity")}


def closest_recipe(conn, shot: dict) -> tuple[dict | None, float]:
    """The library recipe whose settings are nearest, by a simple distance over the shared settings."""
    best, best_d = None, float("inf")
    for r in conn.execute("SELECT * FROM recipes"):
        rs = json.loads(r["adapted_settings_json"] or r["settings_json"])
        d = 0.0
        if shot.get("film_simulation") and str(rs.get("film_simulation", "")).upper() not in str(shot["film_simulation"]).upper():
            d += 5
        for k, rk in (("highlight", "highlight"), ("shadow", "shadow"), ("color", "color"), ("sharpness", "sharpness"),
                      ("noise_reduction", "high_iso_nr"), ("clarity", "clarity")):
            a, b = shot.get(k), rs.get(rk)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                d += abs(a - b)
        for k, rk in (("grain", "grain_roughness"), ("color_chrome_effect", "color_chrome_effect"),
                      ("color_chrome_fx_blue", "color_chrome_fx_blue"), ("dynamic_range", "dynamic_range")):
            a, b = shot.get(k), rs.get(rk)
            if a is not None and b is not None and str(a).upper().replace("%", "") not in str(b).upper().replace("%", ""):
                d += 1
        if d < best_d:
            best, best_d = dict(r), d
    return best, best_d
