"""Images in the repository carry no metadata (BYT-122).

A phone photo holds the GPS position it was taken at, the phone's model and the
time, and an edited image can hold an author and its editing history. None of
that may reach a public repo. docs/images/add_media.py strips it; this test fails
on any image in the repository that still carries EXIF (where GPS lives), XMP or
IPTC, and on formats the scanner can't read (TIFF, HEIC, camera raw …), which
should be converted first. The tool's own tests need Pillow (a dev dependency)
and, for the GIF, ffmpeg, and skip without them.
"""
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "docs" / "images" / "add_media.py"


@pytest.fixture(scope="module")
def am():
    spec = importlib.util.spec_from_file_location("add_media", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SKIP_DIRS = {".git", ".venv", "__pycache__", "recordings", "node_modules", ".pytest_cache",
             ".ruff_cache", "dist", "build"}


def _repo_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in filenames:
            yield Path(dirpath) / name


# ── the guard ──────────────────────────────────────────────────────────────
def test_no_image_in_the_repo_carries_metadata(am):
    images = [p for p in _repo_files() if p.suffix.lower() in am.IMAGE_SUFFIXES]
    assert images, "found no images to check; did docs/images move?"
    dirty = {str(p.relative_to(ROOT)): am.metadata_kinds(p) for p in images}
    dirty = {path: kinds for path, kinds in dirty.items() if kinds}
    assert not dirty, f"strip these with docs/images/add_media.py: {dirty}"


def test_only_formats_the_scanner_reads_are_committed(am):
    unscanned = [str(p.relative_to(ROOT)) for p in _repo_files()
                 if p.suffix.lower() in am.UNSCANNED_SUFFIXES]
    assert not unscanned, f"convert with docs/images/add_media.py photo: {unscanned}"


# ── the scanner ────────────────────────────────────────────────────────────
@pytest.fixture
def Image():
    return pytest.importorskip("PIL.Image")


def _phone_exif(Image):
    exif = Image.Exif()
    exif[0x0110] = "Phone 15"                        # Model
    exif[0x0112] = 6                                 # Orientation: rotate 90° clockwise to view
    gps = exif.get_ifd(0x8825)                       # GPSInfo
    gps[1], gps[2] = "N", (37.0, 46.0, 30.0)
    return exif


def test_the_scanner_finds_gps_exif_in_a_jpeg(am, Image, tmp_path):
    path = tmp_path / "phone.jpg"
    Image.new("RGB", (40, 20), "gray").save(path, exif=_phone_exif(Image))
    assert am.metadata_kinds(path) == ["EXIF"]


def test_the_scanner_finds_exif_and_xmp_in_a_png(am, Image, tmp_path):
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo()
    info.add_itxt("XML:com.adobe.xmp", "<x:xmpmeta xmlns:x='adobe:ns:meta/'/>")
    path = tmp_path / "edited.png"
    Image.new("RGB", (8, 8)).save(path, exif=_phone_exif(Image), pnginfo=info)
    assert am.metadata_kinds(path) == ["EXIF", "XMP"]


def test_the_scanner_reads_past_jpeg_fill_bytes(am, Image, tmp_path):
    """A marker may be padded with extra 0xFF bytes; EXIF behind them still counts."""
    clean = tmp_path / "clean.jpg"
    Image.new("RGB", (8, 8)).save(clean)
    data = clean.read_bytes()
    payload = b"Exif\x00\x00II*\x00\x08\x00\x00\x00\x00\x00"
    app1 = b"\xff\xff\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    padded = tmp_path / "padded.jpg"
    padded.write_bytes(data[:2] + app1 + data[2:])
    assert am.metadata_kinds(padded) == ["EXIF"]


@pytest.mark.parametrize("keyword,kind", [
    ("Raw profile type iptc", "IPTC"),
    ("Raw profile type exif", "EXIF"),
    ("Raw profile type xmp", "XMP"),
])
def test_the_scanner_finds_imagemagick_profiles_in_png_text(am, Image, tmp_path, keyword, kind):
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo()
    info.add_text(keyword, "\nprofile\n      4\n00000000\n")
    path = tmp_path / "magick.png"
    Image.new("RGB", (8, 8)).save(path, pnginfo=info)
    assert am.metadata_kinds(path) == [kind]


def test_the_scanner_passes_clean_files(am, Image, tmp_path):
    for name in ("clean.jpg", "clean.png", "clean.gif", "clean.webp"):
        Image.new("RGB", (8, 8), "white").save(tmp_path / name)
        assert am.metadata_kinds(tmp_path / name) == [], name


# ── add_media.py photo / gif ───────────────────────────────────────────────
def test_a_phone_photo_comes_out_upright_with_nothing_but_pixels(am, Image, tmp_path):
    from PIL import ImageCms
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    src = tmp_path / "IMG_0001.jpg"
    Image.new("RGB", (400, 200), (200, 40, 40)).save(src, exif=_phone_exif(Image), icc_profile=srgb)
    dest = am.photo(src, "throat-mic-photo", out_dir=tmp_path)
    assert dest.suffix == ".jpg"
    assert am.metadata_kinds(dest) == []
    with Image.open(dest) as out:
        assert out.size == (200, 400)                 # the rotation tag was applied, then dropped
        assert "exif" not in out.info and "icc_profile" not in out.info


def test_a_lifted_subject_keeps_its_transparency(am, Image, tmp_path):
    src = tmp_path / "subject.png"
    im = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    im.paste((30, 30, 30, 255), (100, 100, 200, 200))
    im.save(src)
    dest = am.photo(src, "subject", size=150, out_dir=tmp_path)
    with Image.open(dest) as out:
        assert dest.suffix == ".png" and out.mode == "RGBA" and out.size == (150, 150)
        assert out.getpixel((0, 0))[3] == 0 and out.getpixel((75, 75))[3] == 255
    assert am.metadata_kinds(dest) == []


CMYK_PROFILE = Path("/System/Library/ColorSync/Profiles/Generic CMYK Profile.icc")


@pytest.mark.skipif(not CMYK_PROFILE.exists(), reason="needs a CMYK ICC profile (macOS ships one)")
def test_a_cmyk_photo_is_converted_through_its_own_profile(am, Image, tmp_path):
    """The transform has to start from the pixels' own mode: converting CMYK to
    RGB first mismatches the profile, and the naive conversion's colors are off."""
    from PIL import ImageCms
    cyan = Image.new("CMYK", (64, 64), (255, 0, 0, 0))
    src = tmp_path / "print.jpg"
    cyan.save(src, icc_profile=CMYK_PROFILE.read_bytes())
    dest = am.photo(src, "print", out_dir=tmp_path)
    expected = ImageCms.profileToProfile(cyan, ImageCms.ImageCmsProfile(str(CMYK_PROFILE)),
                                         ImageCms.createProfile("sRGB"), outputMode="RGB").getpixel((0, 0))
    naive = cyan.convert("RGB").getpixel((0, 0))
    with Image.open(dest) as out:
        got = out.getpixel((32, 32))
    assert all(abs(g - e) <= 8 for g, e in zip(got, expected)), (got, expected)
    assert max(abs(g - n) for g, n in zip(got, naive)) > 20, (got, naive)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_a_screen_recording_becomes_a_clean_looping_gif(am, Image, tmp_path):
    src = tmp_path / "recording.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    dest = am.gif(src, "demo", start=0.5, duration=1.0, width=160, fps=5, out_dir=tmp_path)
    assert dest.read_bytes()[:6] == b"GIF89a" and am.metadata_kinds(dest) == []
    with Image.open(dest) as out:
        assert out.size[0] == 160 and out.n_frames > 1
