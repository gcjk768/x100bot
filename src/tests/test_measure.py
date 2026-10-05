import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from x100bot import measure

TH = {"highlights_clipped_pct": 1.0, "shadows_crushed_pct": 2.0, "cast_ab": 6.0, "tilt_min_degrees": 0.7,
      "tilt_min_lines": 4, "blur_laplacian_min": 0}


def horizon(angle=0.0, size=(900, 600)):
    """Sky above, ground below, with a few extra horizontal edges, rotated by angle degrees (positive = CCW)."""
    im = Image.new("RGB", size, (190, 200, 215))
    d = ImageDraw.Draw(im)
    d.rectangle([0, size[1] // 2, size[0], size[1]], fill=(70, 90, 50))
    for y in (size[1] * 0.62, size[1] * 0.74, size[1] * 0.86):
        d.line([(0, y), (size[0], y)], fill=(20, 20, 20), width=3)
    for x in (size[0] * 0.2, size[0] * 0.55, size[0] * 0.8):   # a few posts, so verticals vote too
        d.line([(x, size[1] * 0.1), (x, size[1] * 0.9)], fill=(240, 240, 240), width=4)
    d.rectangle([size[0] * 0.3, size[1] * 0.2, size[0] * 0.45, size[1] * 0.5], outline=(10, 10, 10), width=3)
    big = im.resize((size[0] * 2, size[1] * 2))
    rot = big.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=(128, 128, 128))
    return rot.crop((size[0] // 2, size[1] // 2, size[0] // 2 + size[0], size[1] // 2 + size[1]))


def test_level_horizon_is_not_called_tilted():
    m = measure.measure(horizon(0.0), TH)
    assert m["tilt"]["lines"] >= 4 and abs(m["tilt"]["tilt_degrees"]) < 0.5 and not m["tilt"]["tilted"]


def test_three_degree_tilt_detected_with_the_right_sign():
    # rotated 3 degrees counterclockwise, so levelling needs 3 degrees clockwise: a negative value
    m = measure.measure(horizon(3.0), TH)
    assert m["tilt"]["tilted"] and m["tilt"]["confidence"] in ("medium", "high")
    assert abs(m["tilt"]["tilt_degrees"] - (-3.0)) < 0.5, m["tilt"]
    m2 = measure.measure(horizon(-3.0), TH)
    assert abs(m2["tilt"]["tilt_degrees"] - 3.0) < 0.5, m2["tilt"]


def test_blown_out_image_passes_the_clipping_threshold():
    im = Image.new("RGB", (400, 300), (120, 120, 120))
    ImageDraw.Draw(im).rectangle([0, 0, 200, 300], fill=(255, 255, 250))
    m = measure.measure(im, TH)
    assert m["highlights_clipped_pct"] > 40 and m["flags"]["highlights_clipped"]
    dark = Image.new("RGB", (400, 300), (2, 2, 2))
    assert measure.measure(dark, TH)["flags"]["shadows_crushed"]


def test_warm_cast_is_labelled_warm():
    warm = Image.new("RGB", (300, 200), (200, 160, 90))
    assert measure.measure(warm, TH)["colour"]["cast"] == "warm"
    cool = Image.new("RGB", (300, 200), (100, 140, 210))
    assert measure.measure(cool, TH)["colour"]["cast"] == "cool"
    grey = Image.new("RGB", (300, 200), (128, 128, 128))
    assert measure.measure(grey, TH)["colour"]["cast"] == "neutral"


def test_blurred_copy_is_less_sharp():
    rng = np.random.default_rng(1)
    im = Image.fromarray(rng.integers(0, 255, (600, 800, 3), dtype=np.uint8))
    sharp = measure.measure(im, TH)["sharpness"]
    blurred = measure.measure(im.filter(ImageFilter.GaussianBlur(4)), TH)["sharpness"]
    assert blurred["max"] < sharp["max"] / 4
    assert sharp["sharpest_cell"] in measure.CELLS


def test_prepared_copies_carry_no_metadata(tmp_path, settings):
    src = tmp_path / "in.jpg"
    im = Image.new("RGB", (3000, 2000), (90, 120, 150))
    exif = Image.Exif()
    exif[0x0110] = "X100VI"
    exif[0x010e] = "lat 1.3 lon 103.8"
    im.save(src, "JPEG", exif=exif.tobytes())
    p = measure.prepare(src, tmp_path / "out", settings.teacher, TH)
    assert p.photo.exists() and max(Image.open(p.photo).size) == 2048
    assert not Image.open(p.photo).getexif()
    assert max(p.analysis.size) == 1024 and p.measurements["size"]["w"] == 1024


def test_overlay_draws_without_error():
    im = horizon(2.0)
    out = measure.overlay(im, {"x": 0.1, "y": 0.1, "w": 0.6, "h": 0.6}, measure.measure(im, TH)["tilt"])
    assert out.size == im.size
