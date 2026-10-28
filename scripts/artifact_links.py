

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import urlsplit

LINK_DIR = Path.home() / ".local/share/praxis/artifact-links"

def markdown_label(title: str) -> str:
    if not title.strip() or re.search(r"[\x00-\x1f\x7f-\x9f]", title):
        raise ValueError("Label must be non-empty and on one line")
    return re.sub(r"([\\`*_{}\[\]()<>!])", r"\\\1", title)

def artifact_link(filename: str, label: str | None = None) -> str:

    if not filename or re.search(r"[\x00-\x1f\x7f-\x9f<>]", filename):
        raise ValueError("Expected a local filename without control characters or angle brackets")
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", filename):
        raise ValueError("Pass the filesystem path, not a URL")
    path = Path(filename).expanduser().absolute()
    try:
        if not path.is_file():
            raise ValueError(f"Not an existing regular file: {path}; use its literal spelling, not %20 encoding")
        with path.open("rb") as source:
            source.read(1)
    except OSError as exc:
        raise ValueError(f"Artifact is not readable: {path}: {exc}") from exc
    title = path.name if label is None else label
    title = markdown_label(title)
    try:
        target = delivery_path(path)
    except OSError as exc:
        raise ValueError(f"Could not prepare artifact link: {exc}") from exc
    return f"[{title}](<{target}>)"

class _HTMLReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []
        self.base = None
        self.styles = []
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        if tag == 'style': self.in_style = True
        for name, value in attrs:
            if not value:
                continue
            if tag == "base" and name == "href":
                self.base = value
            elif name in {"src", "href", "poster", "data", "action"}:
                self.urls.append(value)
            elif name == "srcset":
                                                                       
                self.urls.append("relative-image-set")
            elif name == 'style':
                self.styles.append(value)

    def handle_endtag(self, tag):
        if tag == 'style': self.in_style = False

    def handle_data(self, data):
        if self.in_style: self.styles.append(data)

def _check_html_base(path: Path, *, body: str | None = None) -> None:

    text = path.read_text(encoding="utf-8") if body is None else body
    refs = _HTMLReferences()
    refs.feed(text)
    if refs.base and urlsplit(refs.base).scheme in {"file", "http", "https"}:
        return
    refs.urls.extend(re.findall(r"url\(\s*['\"]?([^'\")\s]+)", '\n'.join(refs.styles), re.I))
    if refs.base or any(url.strip() and not url.strip().startswith(("#", "//"))
                        and not urlsplit(url.strip()).scheme for url in refs.urls):
        raise ValueError("Cannot alias HTML with relative assets or links; make it self-contained "
                         "or set its canonical absolute base before delivery")

def delivery_path(path: Path) -> Path:

    path = path.absolute()
    if path.suffix.lower() not in {".html", ".htm"} or not re.search(r"[^\w/ .~@+-]", str(path)):
        return path
    if not path.is_file():
        raise ValueError(f"Artifact no longer exists: {path}")
    _check_html_base(path)
                                                                              
    name = hashlib.sha256(str(path).encode("utf-8")).hexdigest() + path.suffix.lower()
    LINK_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    link = LINK_DIR / name
    try:
        link.symlink_to(path)
    except FileExistsError:
                                                                                
        if not link.is_symlink() or link.readlink() != path:
            raise ValueError(f"Artifact link path is occupied by something else: {link}")
    if not link.is_file() or link.resolve() != path.resolve():
        raise ValueError(f"Could not verify artifact link: {link}")
    return link
