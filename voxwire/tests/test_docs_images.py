"""Images in docs/ carry no metadata (BYT-122).

A phone photo holds the GPS position it was taken at, the phone's model and the
time, and an edited image can hold an author and its editing history. None of
that may reach a public repo. docs/images/add_media.py strips it; this test fails
on any image in docs/ that still carries EXIF (where GPS lives), XMP or IPTC, and
on HEIC files, which should be converted first. The tool's own tests need Pillow
(and ffmpeg for the GIF) and skip without them.
"""
import importlib.util
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


def _docs_files():
    return [p for p in (ROOT / "docs").rglob("*") if p.is_file() and "__pycache__" not in p.parts]


# ── the guard ──────────────────────────────────────────────────────────────
def test_no_image_in_docs_carries_metadata(am):
    images = [p for p in _docs_files() if p.suffix.lower() in am.IMAGE_SUFFIXES]
    assert images, "found no images to check; did docs/ move?"
    dirty = {str(p.relative_to(ROOT)): am.metadata_kinds(p) for p in images}
    dirty = {path: kinds for path, kinds in dirty.items() if kinds}
    assert not dirty, f"strip these with docs/images/add_media.py: {dirty}"


def test_no_heic_in_docs():
    heic = [str(p.relative_to(ROOT)) for p in _docs_files() if p.suffix.lower() in (".heic", ".heif")]
    assert not heic, f"convert with docs/images/add_media.py photo: {heic}"


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


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_a_screen_recording_becomes_a_clean_looping_gif(am, Image, tmp_path):
    src = tmp_path / "recording.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    dest = am.gif(src, "demo", start=0.5, duration=1.0, width=160, fps=5, out_dir=tmp_path)
    assert dest.read_bytes()[:6] == b"GIF89a" and am.metadata_kinds(dest) == []
    with Image.open(dest) as out:
        assert out.size[0] == 160 and out.n_frames > 1
