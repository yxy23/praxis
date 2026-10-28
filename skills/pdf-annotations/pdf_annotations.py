#!/usr/bin/env python3
"""Recover PDF annotations, including handwriting, for reading.

Typed comments come back as text. Handwritten ink cannot be extracted as text,
so the annotated pages are rendered as images, with sharper crops around each
cluster of ink, for the agent to read visually.

    pdf_annotations.py summary <pdf>
    pdf_annotations.py render  <pdf> [--out DIR] [--pages 2,5-7] [--all-pages]

`render` writes the images plus `annotations.json` and `annotations.md` to the
output folder and prints the Markdown index: read every image it lists.
Requires pypdf and poppler's pdftoppm.
"""

import argparse
import colorsys
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    import pypdf
except ImportError:  # pragma: no cover - environment guard
    pypdf = None

OVERVIEW_DPI = 110
ZOOM_DPI = 220
CLUSTER_GAP = 36  # points: ink boxes closer than this vertically share a crop
CROP_PAD = 18  # points around each ink cluster
MAX_CROP_HEIGHT = 330  # points: taller clusters are split into bands
MARKUP_SUBTYPES = {"/Highlight", "/Underline", "/StrikeOut", "/Squiggly"}
SKIP_SUBTYPES = {"/Popup", "/Link", "/Widget"}


def fail(message):
    print(f"pdf_annotations: {message}", file=sys.stderr)
    sys.exit(2)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def text_of(value):
    if value is None:
        return ""
    return str(value).replace("\r", "\n").strip()


def page_box(page):
    box = page.mediabox
    return float(box.left), float(box.bottom), float(box.right), float(box.top)


def annotation_rect(annot):
    rect = annot.get("/Rect")
    if not rect or len(rect) != 4:
        return None
    x1, y1, x2, y2 = (float(v) for v in rect)
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


# Hue bands in degrees. Highlight colours vary in lightness between apps, so
# the name follows hue; saturation and value only separate the greys.
HUES = [(15, "red"), (40, "orange"), (70, "yellow"), (165, "green"),
        (255, "blue"), (290, "purple"), (345, "pink"), (360, "red")]


def colour_name(value):
    try:
        rgb = [float(v) for v in value]
    except (TypeError, ValueError):
        return ""
    if len(rgb) != 3:
        return ""
    hue, saturation, brightness = colorsys.rgb_to_hsv(*rgb)
    if brightness < 0.2:
        return "black"
    if saturation < 0.15:
        return "grey"
    degrees = hue * 360
    return next(name for limit, name in HUES if degrees <= limit)


def quads(annot):
    points = annot.get("/QuadPoints")
    if not points:
        rect = annotation_rect(annot)
        return [rect] if rect else []
    values = [float(v) for v in points]
    boxes = []
    for i in range(0, len(values) - 7, 8):
        xs, ys = values[i:i + 8:2], values[i + 1:i + 8:2]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    return boxes


def text_chunks(page):
    """Text runs with their baseline origin in page space."""
    chunks = []

    def visit(text, cm, tm, *_):
        if not text.strip():
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        chunks.append((x, y, text))

    try:
        page.extract_text(visitor_text=visit)
    except Exception:  # pragma: no cover - malformed content streams
        return []
    return chunks


def covered_text(chunks, boxes):
    """Approximate the marked text: runs whose baseline sits inside a quad."""
    hits = []
    for x, y, text in chunks:
        for x1, y1, x2, y2 in boxes:
            if y1 - 2 <= y <= y2 and x1 - 2 <= x <= x2:
                hits.append(text.strip())
                break
    return " ".join(hits)


def read_pdf(path):
    if pypdf is None:
        fail("pypdf is not installed for this python3; install it or use the "
             "Praxis CLI python (~/.config/praxis/cli-python/bin/python).")
    reader = pypdf.PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as error:  # pragma: no cover - depends on file
            fail(f"cannot open encrypted PDF: {error}")
    return reader


def collect(path):
    """Return file identity and per-page annotation records."""
    reader = read_pdf(path)
    stat = path.stat()
    pages = []
    for index, page in enumerate(reader.pages):
        raw = page.get("/Annots")
        if not raw:
            continue
        records = []
        chunks = None
        for ref in raw:
            annot = ref.get_object()
            subtype = str(annot.get("/Subtype", ""))
            if subtype in SKIP_SUBTYPES:
                continue
            marked = ""
            if subtype in MARKUP_SUBTYPES:
                if chunks is None:
                    chunks = text_chunks(page)
                marked = covered_text(chunks, quads(annot))
            records.append({
                "type": subtype.lstrip("/"),
                "text": text_of(annot.get("/Contents")),
                "colour": colour_name(annot.get("/C")),
                "marked_text": marked,
                "author": text_of(annot.get("/T")),
                "modified": text_of(annot.get("/M")),
                "rect": annotation_rect(annot),
            })
        if records:
            pages.append({
                "page": index + 1,
                "box": page_box(page),
                "rotation": int(page.get("/Rotate", 0) or 0),
                "annotations": records,
            })
    return {
        "file": str(path),
        "sha256": sha256(path),
        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        .isoformat(timespec="seconds"),
        "page_count": len(reader.pages),
        "pages": pages,
    }


def ink_clusters(page):
    """Group ink boxes into vertical bands, each a full-width crop region."""
    left, bottom, right, top = page["box"]
    boxes = sorted(
        (a["rect"] for a in page["annotations"]
         if a["type"] == "Ink" and a["rect"]),
        key=lambda r: -r[3],
    )
    bands = []
    for _, y1, _, y2 in boxes:
        if bands and y2 >= bands[-1][0] - CLUSTER_GAP:
            bands[-1][0] = min(bands[-1][0], y1)
        else:
            bands.append([y1, y2])
    regions = []
    for low, high in bands:
        low = max(bottom, low - CROP_PAD)
        high = min(top, high + CROP_PAD)
        while high - low > MAX_CROP_HEIGHT:
            regions.append((high - MAX_CROP_HEIGHT, high))
            high -= MAX_CROP_HEIGHT - CROP_PAD
        regions.append((low, high))
    return regions


def parse_pages(spec, count):
    selected = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = (int(v) for v in part.split("-", 1))
            selected.update(range(start, end + 1))
        else:
            selected.add(int(part))
    return sorted(p for p in selected if 1 <= p <= count)


def pdftoppm(pdf, page, dpi, target, crop=None):
    command = ["pdftoppm", "-r", str(dpi), "-f", str(page), "-l", str(page),
               "-png", "-singlefile"]
    if crop:
        command += ["-x", str(crop[0]), "-y", str(crop[1]),
                    "-W", str(crop[2]), "-H", str(crop[3])]
    command += [str(pdf), str(target)]
    subprocess.run(command, check=True, capture_output=True)
    return Path(f"{target}.png")


def render(data, pdf, out, pages):
    if not shutil.which("pdftoppm"):
        fail("pdftoppm not found; install poppler (brew install poppler).")
    out.mkdir(parents=True, exist_ok=True)
    by_page = {p["page"]: p for p in data["pages"]}
    images = []
    for number in pages:
        overview = pdftoppm(pdf, number, OVERVIEW_DPI, out / f"page-{number:03d}")
        images.append({"page": number, "kind": "page", "path": str(overview)})
        page = by_page.get(number)
        if not page or page["rotation"] % 360:
            continue  # rotated pages: read the overview, crops would misalign
        left, bottom, right, top = page["box"]
        scale = ZOOM_DPI / 72
        for n, (low, high) in enumerate(ink_clusters(page), start=1):
            crop = (0, round((top - high) * scale),
                    round((right - left) * scale), round((high - low) * scale))
            path = pdftoppm(pdf, number, ZOOM_DPI,
                            out / f"page-{number:03d}-ink-{n:02d}", crop)
            images.append({"page": number, "kind": "ink-zoom", "path": str(path)})
    return images


def markdown(data, images):
    lines = [f"# Annotations: {Path(data['file']).name}", "",
             f"- File: `{data['file']}`",
             f"- Modified: {data['modified']}",
             f"- SHA-256: `{data['sha256']}`",
             f"- Pages: {data['page_count']}; annotated: "
             + (", ".join(str(p["page"]) for p in data["pages"]) or "none"), ""]
    if not data["pages"]:
        lines.append("No annotations found in this PDF.")
    for page in data["pages"]:
        counts = Counter(a["type"] for a in page["annotations"])
        summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        lines += [f"## PDF page {page['page']}", "", f"Annotations: {summary}"]
        for a in page["annotations"]:
            label = f"{a['type']} ({a['colour']})" if a["colour"] else a["type"]
            if a["marked_text"]:
                lines.append(f"- {label} on (approximate): \"{a['marked_text']}\"")
            if a["text"]:
                lines.append(f"- {label} comment: \"{a['text']}\"")
        if counts.get("Ink"):
            lines.append("- Handwriting present: read this page's images.")
        lines.append("")
    if images:
        lines += ["## Images to read", ""]
        lines += [f"- p. {i['page']} {i['kind']}: `{i['path']}`" for i in images]
    return "\n".join(lines) + "\n"


def default_out(data):
    base = Path(os.environ.get("TMPDIR", tempfile.gettempdir()))
    return base / "pdf-annotations" / data["sha256"][:12]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("summary", help="list annotations and typed comments")
    s.add_argument("pdf")
    s.add_argument("--json", action="store_true")
    r = sub.add_parser("render", help="render annotated pages for reading")
    r.add_argument("pdf")
    r.add_argument("--out", help="output folder (default: temp folder)")
    r.add_argument("--pages", help="page list such as 2,5-7")
    r.add_argument("--all-pages", action="store_true")
    args = parser.parse_args(argv)

    pdf = Path(args.pdf).expanduser().resolve()
    if not pdf.is_file():
        fail(f"no such file: {pdf}")
    data = collect(pdf)

    if args.command == "summary":
        print(json.dumps(data, indent=2) if args.json else markdown(data, []),
              end="")
        return 0

    if args.pages:
        pages = parse_pages(args.pages, data["page_count"])
    elif args.all_pages:
        pages = list(range(1, data["page_count"] + 1))
    else:
        pages = [p["page"] for p in data["pages"]]
    out = Path(args.out).expanduser() if args.out else default_out(data)
    images = render(data, pdf, out, pages)
    report = markdown(data, images)
    (out / "annotations.json").write_text(
        json.dumps({**data, "images": images}, indent=2))
    (out / "annotations.md").write_text(report)
    print(report, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
