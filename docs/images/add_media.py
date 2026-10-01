# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow"]
# ///
"""Bring a photo or a screen recording into docs/images/ without its metadata.

    uv run docs/images/add_media.py photo ~/Downloads/IMG_1234.HEIC throat-mic-photo
    uv run docs/images/add_media.py gif ~/Desktop/demo.mov demo --start 1.5 --duration 9

photo  Applies the camera's rotation, converts to sRGB, scales to 1200 px on the
       long side, and saves a JPEG, or a PNG when the image has transparency
       (a subject lifted from its background on an iPhone). Nothing else from
       the original is kept: a phone photo carries the GPS position it was taken
       at, the phone's model and the time, and none of that may reach a public
       repo. HEIC goes through macOS `sips` first.
gif    Cuts a screen recording into a looping GIF for the README with ffmpeg:
       12 fps, 960 px wide, a palette built for the clip.

voxwire/tests/test_docs_images.py fails on any image in docs/ that still carries
EXIF (where GPS lives), XMP or IPTC metadata.
"""
from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".apng", ".gif", ".webp"}
# Formats that carry camera metadata but that metadata_kinds can't read. They
# must not be committed at all: convert them with `add_media.py photo` first.
UNSCANNED_SUFFIXES = {".tif", ".tiff", ".heic", ".heif", ".avif", ".jxl", ".psd", ".dng",
                      ".raw", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf"}
# ImageMagick keeps a profile in a PNG text chunk under one of these keywords.
_RAW_PROFILES = {b"raw profile type exif": "EXIF", b"raw profile type app1": "EXIF",
                 b"raw profile type xmp": "XMP", b"raw profile type iptc": "IPTC",
                 b"raw profile type 8bim": "IPTC"}


def metadata_kinds(path: Path) -> list[str]:
    """The kinds of camera or editing metadata in a JPEG, PNG, GIF or WebP file:
    "EXIF" (which holds GPS), "XMP", "IPTC". Empty means clean. Reads the file's
    structure rather than searching its bytes, so image data can't fake a hit."""
    data = Path(path).read_bytes()
    kinds: set[str] = set()
    if data[:2] == b"\xff\xd8":                               # JPEG: marker segments
        i = 2
        while i + 1 < len(data) and data[i] == 0xFF:
            while i + 1 < len(data) and data[i + 1] == 0xFF:  # fill bytes may pad a marker
                i += 1
            if i + 4 > len(data):
                break
            marker = data[i + 1]
            if marker == 0xDA:                                # image data starts: no more metadata
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:      # markers without a length
                i += 2
                continue
            seg = data[i + 4:i + 2 + int.from_bytes(data[i + 2:i + 4], "big")]
            if marker == 0xE1 and seg.startswith(b"Exif\x00"):
                kinds.add("EXIF")
            elif marker == 0xE1 and seg.startswith(b"http://ns.adobe.com/xap/1.0/"):
                kinds.add("XMP")
            elif marker == 0xED and seg.startswith(b"Photoshop 3.0"):
                kinds.add("IPTC")
            i += 4 + len(seg)
    elif data[:8] == b"\x89PNG\r\n\x1a\n":                    # PNG: chunks
        i = 8
        while i + 8 <= len(data):
            length, kind = int.from_bytes(data[i:i + 4], "big"), data[i + 4:i + 8]
            body = data[i + 8:i + 8 + length]
            if kind == b"eXIf":
                kinds.add("EXIF")
            elif kind in (b"iTXt", b"tEXt", b"zTXt"):
                keyword = body.split(b"\0", 1)[0]
                if keyword == b"XML:com.adobe.xmp":
                    kinds.add("XMP")
                elif keyword.lower() in _RAW_PROFILES:
                    kinds.add(_RAW_PROFILES[keyword.lower()])
            elif kind == b"IEND":
                break
            i += 12 + length
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        if b"XMP DataXMP" in data:                            # the XMP application extension
            kinds.add("XMP")
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":       # WebP: RIFF chunks
        i = 12
        while i + 8 <= len(data):
            kind, length = data[i:i + 4], int.from_bytes(data[i + 4:i + 8], "little")
            if kind == b"EXIF":
                kinds.add("EXIF")
            elif kind == b"XMP ":
                kinds.add("XMP")
            i += 8 + length + (length & 1)
    return sorted(kinds)


def _to_srgb(im):
    """Convert to sRGB using the photo's own color profile (iPhones shoot Display
    P3), so colors survive the profile being dropped with the rest."""
    from PIL import ImageCms
    icc = im.info.get("icc_profile")
    if not icc:
        return im
    try:
        mode = "RGBA" if "A" in im.getbands() else "RGB"
        return ImageCms.profileToProfile(im.convert(mode), ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                         ImageCms.createProfile("sRGB"), outputMode=mode)
    except (ImageCms.PyCMSError, OSError, ValueError):
        return im


def photo(src: Path, name: str, *, size: int = 1200, out_dir: Path = HERE) -> Path:
    from PIL import Image, ImageOps

    src = Path(src)
    with tempfile.TemporaryDirectory() as tmp:
        if src.suffix.lower() in (".heic", ".heif"):
            if not shutil.which("sips"):
                raise SystemExit("HEIC needs macOS `sips`; export the photo as JPEG instead")
            jpeg = Path(tmp) / "photo.jpg"            # sips keeps the rotation tag for exif_transpose
            subprocess.run(["sips", "-s", "format", "jpeg", str(src), "--out", str(jpeg)],
                           check=True, capture_output=True)
            src = jpeg
        with Image.open(src) as original:
            im = ImageOps.exif_transpose(original)    # turn it upright before the tags go
            im = _to_srgb(im)
            im.thumbnail((size, size), Image.Resampling.LANCZOS)
            alpha = "A" in im.getbands() or "transparency" in im.info
            dest = Path(out_dir) / f"{name}.{'png' if alpha else 'jpg'}"
            # A fresh image from the pixels alone: no exif=, icc_profile= or text
            # chunks are passed on, so the file holds pixels and nothing else.
            clean = Image.new("RGBA" if alpha else "RGB", im.size)
            clean.paste(im.convert(clean.mode))
            if alpha:
                clean.save(dest, optimize=True)
            else:
                clean.save(dest, quality=85, optimize=True, progressive=True)
    left = metadata_kinds(dest)
    if left:
        dest.unlink()
        raise SystemExit(f"metadata survived ({', '.join(left)}); nothing was written")
    print(f"✓ {dest.relative_to(Path.cwd()) if dest.is_relative_to(Path.cwd()) else dest} "
          f"({clean.width}×{clean.height}, {dest.stat().st_size // 1024} KB, no metadata)")
    return dest


def gif(src: Path, name: str, *, start: float = 0.0, duration: float | None = None,
        width: int = 960, fps: int = 12, out_dir: Path = HERE) -> Path:
    if not shutil.which("ffmpeg"):
        raise SystemExit("this needs ffmpeg (brew install ffmpeg)")
    dest = Path(out_dir) / f"{name}.gif"
    trim = ["-ss", str(start)] + (["-t", str(duration)] if duration else [])
    frames = f"fps={fps},scale={width}:-1:flags=lanczos"
    with tempfile.TemporaryDirectory() as tmp:
        palette = Path(tmp) / "palette.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", *trim, "-i", str(src),
                        "-vf", f"{frames},palettegen=stats_mode=diff", str(palette)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", *trim, "-i", str(src), "-i", str(palette),
                        "-lavfi", f"{frames}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:"
                        "diff_mode=rectangle", "-loop", "0", str(dest)], check=True)
    mb = dest.stat().st_size / 1e6
    print(f"✓ {dest} ({mb:.1f} MB)")
    if mb > 8:
        print("  ! over 8 MB, slow for a README: trim it (--start/--duration) or lower --width or --fps")
    return dest


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    ph = sub.add_parser("photo", help="a photo, upright, sRGB, resized, with no metadata")
    ph.add_argument("src", type=Path)
    ph.add_argument("name", help="file name without extension, e.g. throat-mic-photo")
    ph.add_argument("--size", type=int, default=1200, help="long side in px (default 1200)")
    gi = sub.add_parser("gif", help="a screen recording as a looping README GIF")
    gi.add_argument("src", type=Path)
    gi.add_argument("name", help="file name without extension, e.g. demo")
    gi.add_argument("--start", type=float, default=0.0, help="seconds to skip")
    gi.add_argument("--duration", type=float, help="seconds to keep")
    gi.add_argument("--width", type=int, default=960)
    gi.add_argument("--fps", type=int, default=12)
    args = p.parse_args(argv)
    if args.cmd == "photo":
        photo(args.src, args.name, size=args.size)
    else:
        gif(args.src, args.name, start=args.start, duration=args.duration,
            width=args.width, fps=args.fps)


if __name__ == "__main__":
    main()
