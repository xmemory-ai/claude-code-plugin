#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "beautifulsoup4>=4.12,<5",
#     "lxml>=5,<7",
#     "markdownify>=0.13,<2",
# ]
# ///
"""Documentation ingestion for the xmemory ingest-docs skill.

The agent running the skill never reads a corpus into its own context. This script does the
bulk work instead: it finds the pages a source offers, normalises them to Markdown, splits them
into section-sized chunks that carry their page, section and source URL, and writes those chunks
to xmemory instances through ``xmemcli`` - resumably, so a rerun skips what already landed.

Python 3.9+. Markdown sources need nothing beyond the standard library; HTML pages are parsed by
the locked dependencies declared at the top of this file, which ``uv run`` installs on first use.
Every call to xmemory goes through ``xmemcli``, so the credential stays with the CLI and never
passes through this script.

    uv run ingest.py discover SOURCE... --run DIR [--include TEXT]... [--exclude TEXT]...
    uv run ingest.py prepare --run DIR [--max-chars N] [--refresh]
    uv run ingest.py sample --run DIR --questions FILE [--per-question N]
    uv run ingest.py write --run DIR --instance ID [--instance ID]... (--chunks IDS | --all) [--sync]
    uv run ingest.py status --run DIR
    uv run ingest.py ask --run DIR --instance ID [--instance ID]... --questions FILE

Each command prints one JSON document on stdout; progress goes to stderr.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import functools
import hashlib
import html
import http.client
import json
import math
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_CHARS_DEFAULT = 6000
# A single code block is never split below this multiple of --max-chars.
HARD_MAX_FACTOR = 3
# Windows caps a whole command line at 32,767 characters, and a chunk travels as one argument.
WINDOWS_ARG_LIMIT = 30000
# How a failed write that may nevertheless have been stored is reported, logged and counted.
MAY_HAVE_LANDED = "it may have landed - check whether it is stored, then --mark-stored or --resend-unknown"
# Pause before asking again for a finished write's report, times the attempt.
REPORT_RETRY_SECONDS = 5
USER_AGENT = "xmemory-ingest-docs/1.0 (+https://xmemory.ai)"
FETCH_TIMEOUT = 30
FETCH_CONCURRENCY = 8
MAX_FETCH_BYTES = 50 * 1024 * 1024
MAX_CHILD_SITEMAPS = 100

TEXT_EXTS = {".md", ".mdx", ".markdown", ".txt"}
# Formats that carry documentation but need converting to Markdown first; discovery reports them.
DOCUMENT_EXTS = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".odt", ".rtf", ".epub", ".rst", ".adoc", ".asciidoc",
    ".tex", ".org", ".ipynb", ".json", ".yaml", ".yml", ".xlsx", ".csv",
}
# Characters that render as nothing and would only split words and headings.
INVISIBLE = re.compile("[\u200b\u2060\ufeff]")
HTML_EXTS = {".html", ".htm"}
SKIP_EXTS = {
    ".pdf", ".zip", ".gz", ".tar", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".mjs", ".map", ".woff", ".woff2", ".ttf", ".mp4", ".mp3", ".json", ".yaml",
    ".yml", ".csv", ".docx", ".pptx", ".xlsx",
}
SKIP_EXTS |= DOCUMENT_EXTS

# xmemcli exit codes (see `xmemcli help`).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_HTTP = 3
EXIT_AUTH = 4
EXIT_TIMEOUT = 5
EXIT_QUOTA = 6
EXIT_STILL_PROCESSING = 8
LOCAL_TIMEOUT = 124
LOCAL_SPAWN_ERROR = 126

TERMINAL_WRITE_STATES = {"completed", "failed", "not_found"}
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class UsageError(Exception):
    """A problem the caller has to fix; reported as {"error": ...} with exit 2."""


def emit(doc: Any) -> None:
    json.dump(doc, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def short_hash(text: str, length: int = 12) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


# --------------------------------------------------------------------------------------------
# The run directory
# --------------------------------------------------------------------------------------------


class Run:
    """Everything one ingestion keeps on disk. Safe to delete; nothing else reads it."""

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.config = self.root / "run.json"
        self.pages = self.root / "pages.jsonl"
        self.skipped_file = self.root / "skipped.jsonl"
        self.page_dir = self.root / "pages"
        self.chunk_dir = self.root / "chunks"
        self.manifest = self.root / "manifest.jsonl"
        self.state = self.root / "state.jsonl"
        self.pilot_dir = self.root / "pilot"
        self.answers = self.root / "answers.jsonl"

    def ensure(self) -> None:
        for d in (self.root, self.page_dir, self.chunk_dir, self.pilot_dir):
            d.mkdir(parents=True, exist_ok=True)

    def load_config(self) -> dict[str, Any]:
        if not self.config.exists():
            return {}
        return json.loads(self.config.read_text(encoding="utf-8"))

    def save_config(self, config: dict[str, Any]) -> None:
        write_atomic(self.config, json.dumps(config, indent=2, ensure_ascii=False) + "\n")

    def load_manifest(self) -> list[dict[str, Any]]:
        if not self.manifest.exists():
            raise UsageError(f"no manifest in {self.root}; run `prepare` first")
        return read_jsonl(self.manifest)


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # A line cut short when a run was stopped mid-append; every complete line before it stands.
                continue
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    write_atomic(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


# --------------------------------------------------------------------------------------------
# URLs and fetching
# --------------------------------------------------------------------------------------------


def is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def canonical_key(location: str) -> str:
    """One key per page, whichever of its renderings (HTML, .md, index.html.md) was linked."""
    if not is_url(location):
        return os.path.normpath(location)
    parts = urllib.parse.urlsplit(location)
    path = parts.path
    for suffix in ("/index.html.md", "/index.md", "/index.html", ".html.md", ".md", ".mdx", ".html"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    path = path.rstrip("/") or "/"
    # The query stays: on some sites (a wiki's index.php?title=...) it is what names the page.
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def extension(location: str) -> str:
    path = urllib.parse.urlsplit(location).path if is_url(location) else location
    return os.path.splitext(path)[1].lower()


@dataclass
class Response:
    status: int
    content_type: str
    text: str
    url: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def media_type(self) -> str:
        return self.content_type.split(";")[0].strip().lower()


def looks_like_html(text: str) -> bool:
    head = text.lstrip()[:300].lower()
    return head.startswith(("<!doctype html", "<html", "<head")) or "<body" in head


def http_get(url: str, attempts: int = 3) -> Response:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/markdown, text/plain;q=0.9, text/html;q=0.8, */*;q=0.1",
    }
    last_error = ""
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as resp:
                raw = resp.read(MAX_FETCH_BYTES + 1)
                if len(raw) > MAX_FETCH_BYTES:
                    return Response(0, "", "", url, f"larger than {MAX_FETCH_BYTES} bytes")
                charset = resp.headers.get_content_charset() or "utf-8"
                try:
                    text = raw.decode(charset, errors="replace")
                except LookupError:
                    text = raw.decode("utf-8", errors="replace")
                return Response(resp.status, resp.headers.get("Content-Type", ""), text, resp.geturl())
        except ValueError as exc:
            return Response(0, "", "", url, f"not a fetchable URL: {exc}")
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = int(retry_after) if retry_after and retry_after.isdigit() else 2 ** (attempt + 1)
                time.sleep(min(delay, 60))
                continue
            return Response(exc.code, "", "", url, last_error)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError) as exc:
            last_error = str(getattr(exc, "reason", exc)) or exc.__class__.__name__
            if attempt + 1 < attempts:
                time.sleep(2 ** (attempt + 1))
                continue
    return Response(0, "", "", url, last_error or "request failed")


def is_markdown_response(resp: Response) -> bool:
    if not resp.ok or not resp.text.strip() or looks_like_html(resp.text):
        return False
    return resp.media_type in ("text/markdown", "text/x-markdown", "text/plain", "")


class Fetcher:
    """Fetches a page as Markdown, preferring the site's own Markdown rendering.

    Many documentation sites serve every page as Markdown at a sibling URL (``page.md``, or
    ``page/index.html.md`` per the llms.txt convention). The first page on a host finds which
    one works; later pages go straight to it. Sites without one are converted from HTML.
    """

    VARIANTS = ("llmstxt", "md", "dir_index")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._variant: dict[str, str] = {}
        self._misses: dict[str, int] = {}

    @staticmethod
    def variant_url(url: str, variant: str) -> str:
        parts = urllib.parse.urlsplit(url)
        path = parts.path or "/"
        if variant == "llmstxt":
            new = path + "index.html.md" if path.endswith("/") else path + ".md"
        elif variant == "md":
            stripped = path.rstrip("/")
            new = stripped + ".md" if stripped else "/index.md"
        else:
            new = path.rstrip("/") + "/index.html.md"
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, new, "", ""))

    def fetch(self, url: str) -> tuple[str, str]:
        """Return (markdown, html_title) or raise RuntimeError with the reason."""
        if extension(url) in TEXT_EXTS:
            resp = http_get(url)
            if not resp.ok:
                raise RuntimeError(resp.error or f"HTTP {resp.status}")
            if looks_like_html(resp.text):
                return html_to_markdown(resp.text)
            return resp.text, ""
        host = urllib.parse.urlsplit(url).netloc.lower()
        with self._lock:
            known = self._variant.get(host)
            misses = self._misses.get(host, 0)
        if urllib.parse.urlsplit(url).query:
            candidates: tuple[str, ...] = ()  # the query may select the page; a sibling .md would not
        elif known:
            candidates = (known,)
        elif misses < 3:
            candidates = self.VARIANTS
        else:
            candidates = ()
        tried = set()
        for variant in candidates:
            candidate = self.variant_url(url, variant)
            if candidate in tried:
                continue
            tried.add(candidate)
            resp = http_get(candidate, attempts=2)
            if is_markdown_response(resp):
                with self._lock:
                    self._variant.setdefault(host, variant)
                return resp.text, ""
        if not known and candidates:
            with self._lock:
                self._misses[host] = self._misses.get(host, 0) + 1
        resp = http_get(url)
        if not resp.ok:
            raise RuntimeError(resp.error or f"HTTP {resp.status}")
        if is_markdown_response(resp):
            return resp.text, ""
        if resp.media_type not in ("text/html", "application/xhtml+xml", "") and not looks_like_html(resp.text):
            raise RuntimeError(f"unsupported content type {resp.media_type or 'unknown'}")
        return html_to_markdown(resp.text)


# --------------------------------------------------------------------------------------------
# HTML to Markdown
# --------------------------------------------------------------------------------------------

# Page chrome, dropped wherever it sits. A <header> stays when the main content is found: inside
# <main> or <article> it usually holds the page title.
CHROME_TAGS = [
    "script", "style", "noscript", "svg", "template", "iframe", "button", "form", "select", "canvas",
    "dialog", "nav", "aside", "footer",
]
CHROME_ROLES = ["navigation", "search", "banner", "contentinfo", "complementary"]


def code_language(element: Any) -> str:
    """The language a <pre> block declares on itself or its <code>, as `language-x` or `lang-x`."""
    for node in (element, element.find("code")):
        for name in (node.get("class") or []) if node is not None else []:
            match = re.match(r"(?:language|lang)-([\w+#.-]+)$", name)
            if match:
                return match.group(1)
    return ""


def tidy_prose(markdown: str) -> str:
    """Clean up converted prose - permalink marks, invisible characters, trailing spaces, runs of
    blank lines - and leave fenced code exactly as rendered, where any of them can be content."""
    out: list[str] = []
    fence: str | None = None
    blank = 0
    for line in markdown.split("\n"):
        if fence is not None:
            out.append(line)
            if re.match(r"^\s*" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$", line):
                fence = None
            continue
        opener = FENCE_OPEN.match(line)
        if opener:
            fence = opener.group(1)
            out.append(line.rstrip())
            blank = 0
            continue
        # Doc generators end headings and API signatures with a permalink mark.
        line = re.sub(r"[ \t]*\u00b6[ \t]*$", "", INVISIBLE.sub("", line)).rstrip(" \t")
        blank = blank + 1 if not line else 0
        if blank < 2:
            out.append(line)
    return "\n".join(out).strip()


def html_to_markdown(document: str) -> tuple[str, str]:
    """Convert an HTML page to Markdown, keeping the main content: (markdown, <title>).

    Parsing is left to lxml through BeautifulSoup, which closes elements the way browsers do (HTML
    lets many end tags be left out), and rendering to markdownify. Both are the script's locked
    dependencies, installed by `uv run`; a corpus that is all Markdown never needs them.
    """
    try:
        from bs4 import BeautifulSoup
        from markdownify import MarkdownConverter
    except ImportError as exc:
        raise RuntimeError("HTML pages need the script's parser libraries: run the script with `uv run`") from exc
    soup = BeautifulSoup(document, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for element in soup.find_all(CHROME_TAGS) + soup.find_all(attrs={"role": CHROME_ROLES}):
        element.decompose()
    main = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"})
    if main is None or len(main.get_text(strip=True)) < 200:
        # No main region, or one too thin to be the content: take the whole page without its header.
        main = soup.body or soup
        for element in main.find_all("header"):
            element.decompose()
    for anchor in main.find_all("a", class_="headerlink"):
        anchor.decompose()
    class Converter(MarkdownConverter):  # type: ignore[misc, valid-type]
        """markdownify, with `|` escaped inside table cells so `string | null` stays one cell."""

        def convert_td(self, el: Any, text: str, *args: Any, **kwargs: Any) -> str:
            return super().convert_td(el, text.replace("|", "\\|"), *args, **kwargs)

        def convert_th(self, el: Any, text: str, *args: Any, **kwargs: Any) -> str:
            return super().convert_th(el, text.replace("|", "\\|"), *args, **kwargs)

        def convert_pre(self, el: Any, text: str, *args: Any, **kwargs: Any) -> str:
            # A sample that itself contains a ``` line would close a three-backtick fence early,
            # so the fence is made longer than any backtick run inside the code.
            rendered = super().convert_pre(el, text, *args, **kwargs)
            longest = max((len(run) for run in re.findall(r"`{3,}", text)), default=0)
            if not longest:
                return rendered
            fence = "`" * (longest + 1)
            match = re.match(r"(\s*)```([^\n]*\n)(.*\n)```(\s*)$", rendered, re.S)
            return f"{match.group(1)}{fence}{match.group(2)}{match.group(3)}{fence}{match.group(4)}" if match else rendered

    converter = Converter(
        # Link targets are mostly site-relative paths and permalink anchors: noise to extraction,
        # and tokens on every write. Their text stays; images carry nothing to extract.
        strip=["a", "img"],
        heading_style="ATX",
        bullets="-",
        escape_underscores=False,
        escape_asterisks=False,
        escape_misc=False,
        code_language_callback=code_language,
    )
    markdown = tidy_prose(converter.convert_soup(main))
    if not markdown:
        raise RuntimeError("no readable text in the HTML (a JavaScript-rendered page?)")
    return markdown + "\n", title


# --------------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------------

LLMS_FULL_MARKER = re.compile(r"^# (?P<title>[^\n]+)\n(?:Source|URL|Canonical page): *(?P<url>\S+)[ \t]*$", re.M)
LLMS_LINK = re.compile(r"^\s*[-*+]\s*\[(?P<title>[^\]]+)\]\((?P<url>[^)\s]+)\)")
SITEMAP_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S | re.I)


def page_group(location: str, root: str | None = None) -> str:
    if is_url(location):
        segments = [s for s in urllib.parse.urlsplit(location).path.split("/") if s]
        if not segments:
            return "(root)"
        first = segments[0]
        return re.sub(r"\.(md|mdx|html?|txt)$", "", first) if len(segments) == 1 else first
    if root:
        rel = os.path.relpath(location, root)
        head = rel.split(os.sep)[0]
        return head if os.sep in rel else "(root)"
    return "(root)"


def looks_like_api_spec(path: str) -> bool:
    """Whether a JSON or YAML file is an OpenAPI / Swagger document rather than site config.

    A local file is judged by its opening lines. A linked one is not fetched while discovering, so
    only its file name can say so: `openapi3.json`, `swagger.yaml`, `api-docs.json`. The names of
    those judged otherwise are still reported, so a spec under another name is not lost silently.
    """
    if is_url(path):
        location = urllib.parse.urlsplit(path).path.lower()
        name = location.rsplit("/", 1)[-1]
        return "/.well-known/" not in location and re.search(
            r"(?:^|[._-])(?:openapi|swagger|apispec|oas|api-?docs|apis?)\d*(?:[._-]|$)", name
        ) is not None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    # `openapi: 3.1.0`, `"swagger": "2.0"`, minified or not: the key followed by its version.
    return re.search(r"""\b(openapi|swagger)["']?\s*:\s*["']?[23]\.""", head, re.I) is not None


def skippable(location: str) -> bool:
    ext = extension(location)
    name = location.rstrip("/").rsplit("/", 1)[-1].lower()
    return ext in SKIP_EXTS or name in ("llms.txt", "llms-full.txt")


class Discovery:
    def __init__(self, run: Run) -> None:
        self.run = run
        self.pages: list[dict[str, Any]] = []
        self.keys: set[str] = set()
        self.notes: list[dict[str, Any]] = []
        self.skipped = 0
        self.skipped_types: dict[str, int] = {}
        self.skipped_examples: dict[str, list[str]] = {}
        # Every skipped location, for the run's `skipped.jsonl`: the output shows only examples.
        self.skipped_all: list[dict[str, str]] = []
        self.skipped_keys: set[str] = set()

    def skip(self, location: str) -> None:
        key = canonical_key(location)
        if key in self.skipped_keys:
            return
        self.skipped_keys.add(key)
        self.skipped += 1
        kind = extension(location) or "(no extension)"
        if kind in (".json", ".yaml", ".yml") and not looks_like_api_spec(location):
            kind += " (not an API spec)"
        self.skipped_all.append({"location": location, "kind": kind})
        self.skipped_types[kind] = self.skipped_types.get(kind, 0) + 1
        examples = self.skipped_examples.setdefault(kind, [])
        if len(examples) < 3:
            examples.append(location)

    def add(self, url: str, title: str = "", group: str = "", body: str | None = None, local: bool = False) -> None:
        key = canonical_key(url)
        if key in self.keys:
            return
        if body is None and skippable(url):
            if not url.rstrip("/").lower().endswith(("llms.txt", "llms-full.txt")):
                self.skip(url)
            return
        self.keys.add(key)
        page: dict[str, Any] = {"url": url, "key": key, "title": title.strip(), "group": group or page_group(url)}
        if local:
            page["local"] = True
        if body is not None:
            body_file = f"pages/{short_hash(key)}.md"
            (self.run.root / body_file).write_text(body, encoding="utf-8")
            page["body_file"] = body_file
        self.pages.append(page)

    # -- index formats -------------------------------------------------------------------

    def from_llms_full(self, text: str, origin: str) -> bool:
        markers = list(LLMS_FULL_MARKER.finditer(text))
        if len(markers) < 2:
            return False
        for i, marker in enumerate(markers):
            end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
            body = f"# {marker.group('title')}\n\n" + text[marker.end() : end].strip() + "\n"
            url = marker.group("url")
            self.add(url, marker.group("title"), page_group(url), body=body)
        self.notes.append({"source": origin, "kind": "llms-full", "pages": len(markers)})
        return True

    def from_llms_index(self, text: str, origin: str) -> int:
        group = ""
        count = 0
        for line in text.splitlines():
            if line.startswith("## "):
                group = line[3:].strip()
                continue
            match = LLMS_LINK.match(line)
            if not match:
                continue
            link = match.group("url")
            if is_url(origin):
                url = urllib.parse.urljoin(origin, link)
                if not is_url(url):
                    # A published index must not pick files on the reader's machine.
                    self.skip(url)
                    continue
            elif is_url(link) or os.path.isabs(link):
                url = link
            else:
                url = os.path.join(os.path.dirname(origin), link)
            before = len(self.pages)
            self.add(url, match.group("title"), group or page_group(url), local=not is_url(url))
            count += len(self.pages) - before
        self.notes.append({"source": origin, "kind": "llms.txt", "pages": count})
        return count

    def from_sitemap(self, text: str, origin: str, depth: int = 0) -> int:
        locations = [html.unescape(u) for u in SITEMAP_LOC.findall(text)]
        count = 0
        if "<sitemapindex" in text.lower() and depth == 0:
            for child in [loc for loc in locations if is_url(loc)][:MAX_CHILD_SITEMAPS]:
                resp = http_get(child)
                if resp.ok:
                    count += self.from_sitemap(resp.text, child, depth + 1)
            if len(locations) > MAX_CHILD_SITEMAPS:
                self.notes.append({"source": origin, "warning": f"only the first {MAX_CHILD_SITEMAPS} child sitemaps were read"})
            return count
        for url in locations:
            if not is_url(url):
                self.skip(url)
                continue
            before = len(self.pages)
            self.add(url)
            count += len(self.pages) - before
        if depth == 0:
            self.notes.append({"source": origin, "kind": "sitemap", "pages": count})
        return count

    # -- sources -------------------------------------------------------------------------

    def source(self, value: str) -> None:
        if value.startswith("@"):
            listing = Path(value[1:])
            if not listing.exists():
                raise UsageError(f"list file not found: {listing}")
            for line in listing.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    self.source(line)
            return
        if is_url(value):
            self.url_source(value)
        else:
            self.path_source(Path(value))

    def url_source(self, url: str) -> None:
        path = urllib.parse.urlsplit(url).path
        name = path.rstrip("/").rsplit("/", 1)[-1].lower()
        if path in ("", "/"):
            self.site_root(url)
            return
        if name.endswith("llms-full.txt") or name == "llms.txt" or name.endswith(".xml") or "sitemap" in name:
            resp = http_get(url)
            if not resp.ok:
                raise UsageError(f"could not fetch {url}: {resp.error or resp.status}")
            self.index_text(resp.text, url, name)
            return
        before = len(self.pages)
        self.add(url)
        if len(self.pages) > before:
            self.notes.append({"source": url, "kind": "page", "pages": 1})

    def index_text(self, text: str, origin: str, name: str) -> None:
        if name.endswith("llms-full.txt"):
            if not self.from_llms_full(text, origin):
                title = first_heading(text) or name
                self.add(origin, title, "(root)", body=text)
                self.notes.append(
                    {"source": origin, "kind": "llms-full", "pages": 1, "note": "no page markers; treated as one page"}
                )
        elif name == "llms.txt":
            self.from_llms_index(text, origin)
        else:
            self.from_sitemap(text, origin)

    def site_root(self, url: str) -> None:
        base = url if url.endswith("/") else url + "/"
        full = http_get(urllib.parse.urljoin(base, "llms-full.txt"))
        if full.ok and not looks_like_html(full.text) and self.from_llms_full(full.text, full.url):
            return
        index = http_get(urllib.parse.urljoin(base, "llms.txt"))
        if index.ok and not looks_like_html(index.text) and self.from_llms_index(index.text, index.url):
            return
        sitemap = http_get(urllib.parse.urljoin(base, "sitemap.xml"))
        if sitemap.ok and "<loc>" in sitemap.text and self.from_sitemap(sitemap.text, sitemap.url):
            return
        self.add(url, group="(root)")
        self.notes.append({
            "source": url,
            "kind": "page",
            "pages": 1,
            "note": "no llms-full.txt, llms.txt or sitemap.xml at this root; pass page URLs or a list file instead",
        })

    def is_run_dir(self, directory: str) -> bool:
        """A run directory - this one, or another run's - whose pages and chunks are output, not docs."""
        real = os.path.realpath(directory)
        own = os.path.realpath(self.run.root)
        if real == own or real.startswith(own + os.sep):
            return True
        return os.path.exists(os.path.join(directory, "run.json")) and os.path.exists(os.path.join(directory, "pages.jsonl"))

    def path_source(self, path: Path) -> None:
        if not path.exists():
            raise UsageError(f"path not found: {path}")
        if path.is_dir():
            count = 0
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = sorted(
                    d for d in dirnames
                    if not d.startswith(".") and d != "node_modules" and not self.is_run_dir(os.path.join(dirpath, d))
                )
                for filename in sorted(filenames):
                    if filename.startswith("."):
                        continue
                    file_path = os.path.join(dirpath, filename)
                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in TEXT_EXTS and ext not in HTML_EXTS:
                        self.skip(file_path)
                        continue
                    before = len(self.pages)
                    self.add(file_path, "", page_group(file_path, str(path)), local=True)
                    count += len(self.pages) - before
            self.notes.append({"source": str(path), "kind": "directory", "pages": count})
            return
        name = path.name.lower()
        if name.endswith("llms-full.txt") or name == "llms.txt" or path.suffix.lower() == ".xml":
            self.index_text(path.read_text(encoding="utf-8", errors="replace"), str(path), name)
            return
        self.add(str(path), "", "(root)", local=True)
        self.notes.append({"source": str(path), "kind": "file", "pages": 1})


def first_heading(text: str) -> str:
    for block in parse_blocks(text):
        if block.kind == "heading":
            return block.title
    return ""


def matches(page: dict[str, Any], needles: list[str]) -> bool:
    haystack = " ".join((page.get("url", ""), page.get("group", ""), page.get("title", ""))).lower()
    return any(n.lower() in haystack for n in needles)


def apply_scope(pages: list[dict[str, Any]], include: list[str], exclude: list[str]) -> None:
    for page in pages:
        keep = matches(page, include) if include else True
        if exclude and matches(page, exclude):
            keep = False
        page["in_scope"] = keep


def group_summary(pages: list[dict[str, Any]], limit: int = 60) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for page in pages:
        entry = groups.setdefault(page["group"], {"group": page["group"], "pages": 0, "in_scope": 0, "examples": []})
        entry["pages"] += 1
        entry["in_scope"] += 1 if page.get("in_scope", True) else 0
        if len(entry["examples"]) < 3:
            entry["examples"].append(page.get("title") or page["url"])
    summary = list(groups.values())
    if len(summary) > limit:
        rest = summary[limit:]
        summary = summary[:limit] + [{
            "group": f"... {len(rest)} more groups",
            "pages": sum(g["pages"] for g in rest),
            "in_scope": sum(g["in_scope"] for g in rest),
            "examples": [],
        }]
    return summary


def cmd_discover(args: argparse.Namespace) -> int:
    run = Run(args.run)
    run.ensure()
    discovery = Discovery(run)
    for value in args.sources:
        discovery.source(value)
    # The scope is part of the run: a rerun on the same sources keeps it unless it is replaced
    # (--include / --exclude) or cleared (--reset-scope), so refreshing never widens it silently.
    config = run.load_config()
    saved_include = [] if args.reset_scope else config.get("include", [])
    saved_exclude = [] if args.reset_scope else config.get("exclude", [])
    include = args.include if args.include is not None else saved_include
    exclude = args.exclude if args.exclude is not None else saved_exclude
    apply_scope(discovery.pages, include, exclude)
    write_jsonl(run.pages, discovery.pages)
    write_jsonl(run.skipped_file, discovery.skipped_all)
    config.update({"sources": args.sources, "include": include, "exclude": exclude, "discovered_at": now_iso()})
    run.save_config(config)
    in_scope = [p for p in discovery.pages if p["in_scope"]]
    listed = 60
    emit({
        "run": str(run.root),
        "pages": len(discovery.pages),
        "in_scope": len(in_scope),
        "scope": {"include": include, "exclude": exclude},
        # Enough to check the scope against what the user meant; `pages_file` holds every page.
        "in_scope_pages": [f"{p['title']} | {p['url']}" if p.get("title") else p["url"] for p in in_scope[:listed]],
        **({"in_scope_not_listed": len(in_scope) - listed} if len(in_scope) > listed else {}),
        "skipped_documents": {k: v for k, v in sorted(discovery.skipped_types.items()) if k in DOCUMENT_EXTS},
        "skipped_examples": {k: v for k, v in sorted(discovery.skipped_examples.items()) if k in DOCUMENT_EXTS},
        "skipped_other": sum(v for k, v in discovery.skipped_types.items() if k not in DOCUMENT_EXTS),
        # JSON and YAML judged to be configuration, by name, so a spec named otherwise can be spotted.
        "skipped_config_examples": {
            k: v for k, v in sorted(discovery.skipped_examples.items()) if k.endswith("(not an API spec)")
        },
        "sources": discovery.notes,
        "groups": group_summary(discovery.pages),
        "pages_file": str(run.pages),
        "skipped_file": str(run.skipped_file),
    })
    return 0


# --------------------------------------------------------------------------------------------
# Markdown structure and chunking
# --------------------------------------------------------------------------------------------

FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})")
# A closing run of #s only counts after whitespace, so "## Using C#" keeps its "#".
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
TABLE_LINE = re.compile(r"^\s*\|")
TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
# MDX module lines: `import X from "pkg"`, `import "pkg"`, `export const ...`. The quotes keep an
# ordinary sentence that starts with "import ... from" out of it.
MDX_IMPORT = re.compile(
    r"""^(import\s+[\w{}*,\s]+\s+from\s+['"][^'"]+['"];?|import\s+['"][^'"]+['"];?|export\s+(const|let|default|function)\s.*)$"""
)


@dataclass
class Block:
    kind: str  # heading | fence | table | text
    text: str
    level: int = 0
    title: str = ""


def clean_heading(raw: str) -> str:
    text = re.sub(r"\{#[^}]*\}\s*$", "", re.sub(r"\s*\u00b6\s*$", "", INVISIBLE.sub("", raw)))
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("`", "").replace("\\", "")
    return re.sub(r"\s+", " ", text).strip()


def parse_blocks(markdown: str) -> list[Block]:
    """Split Markdown into headings, fenced code, tables and text runs, fence-aware."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[Block] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            text = "\n".join(paragraph).strip("\n")
            if text.strip():
                blocks.append(Block("text", text))
            paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        fence = FENCE_OPEN.match(line)
        if fence:
            flush()
            marker = fence.group(1)
            closing = re.compile(r"^\s*" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*$")
            j = i + 1
            while j < len(lines) and not closing.match(lines[j]):
                j += 1
            blocks.append(Block("fence", "\n".join(lines[i : j + 1])))
            i = j + 1
            continue
        heading = HEADING.match(line)
        if heading:
            flush()
            blocks.append(Block("heading", line.strip(), len(heading.group(1)), clean_heading(heading.group(2))))
            i += 1
            continue
        if "|" in line and i + 1 < len(lines) and "|" in lines[i + 1] and TABLE_SEPARATOR.match(lines[i + 1]):
            flush()
            j = i
            while j < len(lines) and lines[j].strip() and "|" in lines[j]:
                j += 1
            blocks.append(Block("table", "\n".join(lines[i:j])))
            i = j
            continue
        if TABLE_LINE.match(line):
            flush()
            j = i
            while j < len(lines) and TABLE_LINE.match(lines[j]):
                j += 1
            blocks.append(Block("table", "\n".join(lines[i:j])))
            i = j
            continue
        if not line.strip():
            flush()
        else:
            paragraph.append(line)
        i += 1
    flush()
    return blocks


def strip_front_matter(markdown: str) -> tuple[str, str]:
    text = markdown.lstrip("﻿")
    if not text.startswith("---\n"):
        return text, ""
    end = text.find("\n---\n", 4)
    if end == -1 or end > 20000:
        return text, ""
    front = text[4:end]
    title = ""
    match = re.search(r"^title:\s*(.+)$", front, re.M)
    if match:
        title = match.group(1).strip().strip("'\"")
    return text[end + 5 :], title


def normalise(markdown: str) -> tuple[str, str]:
    """Drop front matter and MDX import/export lines; return (markdown, front-matter title)."""
    # Line endings first: a CRLF page must chunk the same as its LF copy, or a rerun that reads
    # the cached copy would see every chunk of it as changed.
    text, title = strip_front_matter(markdown.replace("\r\n", "\n").replace("\r", "\n"))
    kept = []
    fence: str | None = None
    blank = 0
    for line in text.split("\n"):
        opener = FENCE_OPEN.match(line)
        if fence is None and opener:
            fence = opener.group(1)
        elif fence is not None and re.match(r"^\s*" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$", line):
            fence = None
        elif fence is not None:
            kept.append(line)  # code is content: its blank lines and imports stay
            continue
        elif MDX_IMPORT.match(line.strip()):
            continue
        # Runs of blank lines in prose collapse to one.
        blank = blank + 1 if not line.strip() else 0
        if blank < 2:
            kept.append(line)
    return "\n".join(kept).strip() + "\n", title


def join_blocks(blocks: list[Block]) -> str:
    return "\n\n".join(b.text for b in blocks)


def blocks_length(blocks: list[Block]) -> int:
    return sum(len(b.text) for b in blocks) + 2 * max(len(blocks) - 1, 0)


@dataclass
class Unit:
    path: list[str]
    blocks: list[Block]
    part: tuple[int, int] | None = None

    @property
    def length(self) -> int:
        return blocks_length(self.blocks)


def split_on(blocks: list[Block], level: int) -> list[tuple[Block | None, list[Block]]]:
    """Group blocks at headings of `level` or shallower: [(heading or None, blocks...)]."""
    groups: list[tuple[Block | None, list[Block]]] = []
    current: list[Block] = []
    head: Block | None = None
    for block in blocks:
        if block.kind == "heading" and block.level <= level:
            if current:
                groups.append((head, current))
            head, current = block, [block]
        else:
            current.append(block)
    if current:
        groups.append((head, current))
    return groups


def slices(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def expand_oversize(blocks: list[Block], max_chars: int) -> list[Block]:
    """Break single blocks that cannot fit.

    A table is cut between rows with its header repeated. A code block stays whole up to
    HARD_MAX_FACTOR x max_chars and is cut between lines beyond that. A single row or line longer
    than that is cut mid-line: the last resort that keeps every write a sendable size.
    """
    hard_max = max_chars * HARD_MAX_FACTOR
    out: list[Block] = []
    for block in blocks:
        if len(block.text) <= max_chars:
            out.append(block)
        elif block.kind == "table":
            lines = block.text.split("\n")
            header = lines[:2] if len(lines) > 1 and TABLE_SEPARATOR.match(lines[1]) else lines[:1]
            body: list[str] = []
            for row in lines[len(header):]:
                body.extend(slices(row, max_chars) if len(row) > max_chars else [row])
            piece: list[str] = []
            for row in body:
                if piece and len("\n".join(header + piece + [row])) > max_chars:
                    out.append(Block("table", "\n".join(header + piece)))
                    piece = []
                piece.append(row)
            if piece or not body:
                out.append(Block("table", "\n".join(header + piece)))
        elif block.kind == "fence":
            if len(block.text) <= hard_max:
                out.append(block)
                continue
            lines = block.text.split("\n")
            opener, closer = lines[0], lines[-1] if len(lines) > 1 else lines[0]
            inner: list[str] = []
            for code_line in lines[1:-1] if len(lines) > 2 else []:
                inner.extend(slices(code_line, hard_max // 2) if len(code_line) > hard_max // 2 else [code_line])
            piece = []
            for code_line in inner:
                if piece and len("\n".join([opener] + piece + [code_line, closer])) > hard_max:
                    out.append(Block("fence", "\n".join([opener] + piece + [closer])))
                    piece = []
                piece.append(code_line)
            out.append(Block("fence", "\n".join([opener] + piece + [closer])))
        elif block.kind == "text":
            out.extend(Block("text", t) for t in split_text(block.text, max_chars))
        else:
            out.append(block)
    return out


def split_text(text: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    units = text.split("\n")
    if len(units) == 1:
        units = re.split(r"(?<=[.!?])\s+", text)
    for unit in units:
        while len(unit) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(unit[:max_chars])
            unit = unit[max_chars:]
        candidate = f"{current}\n{unit}" if current else unit
        if len(candidate) > max_chars and current:
            pieces.append(current)
            current = unit
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def pack_blocks(blocks: list[Block], max_chars: int) -> list[list[Block]]:
    parts: list[list[Block]] = []
    current: list[Block] = []
    for block in expand_oversize(blocks, max_chars):
        only_headings = all(b.kind == "heading" for b in current)
        if current and not only_headings and blocks_length(current + [block]) > max_chars:
            carry: list[Block] = []
            # A heading never ends a part: it moves on with the text it introduces.
            while len(current) > 1 and current[-1].kind == "heading":
                carry.insert(0, current.pop())
            parts.append(current)
            current = carry
        current.append(block)
    if current:
        parts.append(current)
    return parts


def page_units(blocks: list[Block], max_chars: int) -> list[Unit]:
    levels = sorted({b.level for b in blocks if b.kind == "heading"})
    if not levels:
        return split_unit(Unit([], blocks), max_chars)
    top = levels[0]
    below = [lv for lv in levels if lv > top]
    units: list[Unit] = []
    for head, section in split_on(blocks, top):
        path = [head.title] if head is not None else []
        unit = Unit(path, section)
        if unit.length <= max_chars or not below:
            units.extend(split_unit(unit, max_chars))
            continue
        for sub_head, sub_blocks in split_on(section, below[0]):
            sub_path = path + [sub_head.title] if sub_head is not None and sub_head is not head else path
            units.extend(split_unit(Unit(sub_path, sub_blocks), max_chars))
    # A section that is only its heading (its content all sits in subsections) rides along with
    # the next unit rather than becoming a chunk of its own.
    merged: list[Unit] = []
    pending: list[Block] = []
    for unit in units:
        if all(b.kind == "heading" for b in unit.blocks):
            pending.extend(unit.blocks)
            continue
        if pending:
            unit = Unit(unit.path, pending + unit.blocks, unit.part)
            pending = []
        merged.append(unit)
    if pending and merged:
        last = merged[-1]
        merged[-1] = Unit(last.path, last.blocks + pending, last.part)
    elif pending:
        merged.append(Unit([], pending))
    return merged


def split_unit(unit: Unit, max_chars: int) -> list[Unit]:
    if unit.length <= max_chars:
        return [unit]
    parts = pack_blocks(unit.blocks, max_chars)
    if len(parts) == 1:
        return [Unit(unit.path, parts[0])]
    return [Unit(unit.path, part, (i + 1, len(parts))) for i, part in enumerate(parts)]


def section_label(units: list[Unit]) -> str:
    paths = [u.path for u in units if u.path]
    if not paths:
        label = ""
    else:
        prefix = paths[0]
        for p in paths[1:]:
            common = 0
            while common < min(len(prefix), len(p)) and prefix[common] == p[common]:
                common += 1
            prefix = prefix[:common]
        rest: list[str] = []
        for p in paths:
            tail = p[len(prefix):]
            if tail and tail[0] not in rest:
                rest.append(tail[0])
        label = " > ".join(prefix)
        if rest:
            listed = "; ".join(rest[:4]) + ("; ..." if len(rest) > 4 else "")
            label = f"{label} > {listed}" if label else listed
    if len(units) == 1 and units[0].part is not None:
        k, n = units[0].part
        label = f"{label} (part {k} of {n})".strip()
    return label


def ends_a_group(unit: Unit) -> bool:
    """Whether packing always closes the chunk after this unit.

    The answer depends only on the unit's own heading path, never on sizes, so the boundaries it
    places stay where they are when a section grows or shrinks. Packing purely by size would let
    one edited section shift every chunk after it on the page, and each shifted chunk would be
    extracted again. With these fixed boundaries an edit rewrites the chunk holding the section,
    and at most the few sections between two boundaries when the edit makes that chunk overflow.
    """
    key = " > ".join(unit.path)
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:8], 16) % 3 == 0


def pack_units(units: list[Unit], max_chars: int) -> list[list[Unit]]:
    """Join consecutive small units of one page while they fit; split parts stay alone."""
    groups: list[list[Unit]] = []
    current: list[Unit] = []
    size = 0
    for unit in units:
        if unit.part is not None:
            if current:
                groups.append(current)
            groups.append([unit])
            current, size = [], 0
            continue
        added = unit.length + (2 if current else 0)
        if current and size + added > max_chars:
            groups.append(current)
            current, size = [], 0
            added = unit.length
        current.append(unit)
        size += added
        if ends_a_group(unit):
            groups.append(current)
            current, size = [], 0
    if current:
        groups.append(current)
    return groups


def chunk_page(markdown: str, title_hint: str, max_chars: int) -> tuple[str, list[tuple[str, str]]]:
    """Return the page title and its chunks as (section label, body)."""
    text, front_title = normalise(markdown)
    blocks = parse_blocks(text)
    title = ""
    headings = [i for i, b in enumerate(blocks) if b.kind == "heading"]
    h1s = [i for i in headings if blocks[i].level == 1]
    if headings and len(h1s) == 1 and headings[0] == h1s[0]:
        title = blocks[h1s[0]].title
        del blocks[h1s[0]]
    title = title or title_hint or front_title
    if not blocks:
        return title, []
    if blocks_length(blocks) <= max_chars:
        return title, [("", join_blocks(blocks))]
    chunks = []
    for group in pack_units(page_units(blocks, max_chars), max_chars):
        body = join_blocks([b for unit in group for b in unit.blocks])
        chunks.append((section_label(group), body))
    return title, chunks


def render_chunk(title: str, section: str, source: str, body: str) -> str:
    head = f"Page: {title}" + (f" > Section: {section}" if section else "")
    return f"{head}\nSource: {source}\n\n{body.strip()}\n"


# --------------------------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------------------------


def fallback_title(location: str) -> str:
    if is_url(location):
        segments = [s for s in urllib.parse.urlsplit(location).path.split("/") if s]
        name = segments[-1] if segments else urllib.parse.urlsplit(location).netloc
    else:
        name = os.path.basename(location)
    name = re.sub(r"(\.html)?\.(md|mdx|markdown|txt|html?)$", "", name)
    return name.replace("-", " ").replace("_", " ").strip() or location


def load_page_body(run: Run, page: dict[str, Any], fetcher: Fetcher, refresh: bool) -> tuple[str, str]:
    """(markdown, html title) for one page, from the run cache, the disk, or the web."""
    if page.get("body_file"):
        return (run.root / page["body_file"]).read_text(encoding="utf-8"), ""
    location = page["url"]
    if page.get("local"):
        raw = Path(location).read_text(encoding="utf-8", errors="replace")
        if extension(location) in HTML_EXTS or looks_like_html(raw):
            return html_to_markdown(raw)
        return raw, ""
    cache = run.page_dir / f"{short_hash(page['key'])}.md"
    meta = run.page_dir / f"{short_hash(page['key'])}.title"
    if cache.exists() and not refresh:
        return cache.read_text(encoding="utf-8"), meta.read_text(encoding="utf-8") if meta.exists() else ""
    markdown, html_title = fetcher.fetch(location)
    cache.write_text(markdown, encoding="utf-8")
    meta.write_text(html_title, encoding="utf-8")
    return markdown, html_title


def cmd_prepare(args: argparse.Namespace) -> int:
    run = Run(args.run)
    run.ensure()
    # A running `write` reads the chunk files this rewrites, so the two never overlap.
    with RunLock(run):
        return prepare_locked(run, args)


def prepare_locked(run: Run, args: argparse.Namespace) -> int:
    if not run.pages.exists():
        raise UsageError(f"no pages in {run.root}; run `discover` first")
    pages = read_jsonl(run.pages)
    config = run.load_config()
    if args.include is not None or args.exclude is not None:
        include = args.include if args.include is not None else config.get("include", [])
        exclude = args.exclude if args.exclude is not None else config.get("exclude", [])
        apply_scope(pages, include, exclude)
        write_jsonl(run.pages, pages)
        config.update({"include": include, "exclude": exclude})
    max_chars = args.max_chars or config.get("max_chars") or MAX_CHARS_DEFAULT
    if max_chars < 500:
        raise UsageError("--max-chars below 500 leaves no room for a section")
    config["max_chars"] = max_chars
    scoped = [p for p in pages if p.get("in_scope", True)]
    fetcher = Fetcher()
    bodies: dict[str, tuple[str, str]] = {}
    failures: list[dict[str, str]] = []
    done = 0

    def load(page: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, str] | None, str]:
        try:
            return page, load_page_body(run, page, fetcher, args.refresh), ""
        except Exception as exc:  # one unreadable page must not cost the rest of the corpus
            return page, None, f"{exc.__class__.__name__}: {exc}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.fetch_concurrency) as pool:
        for page, body, error in pool.map(load, scoped):
            done += 1
            if body is None:
                failures.append({"url": page["url"], "error": error})
            else:
                bodies[page["key"]] = body
            if done % 25 == 0 or done == len(scoped):
                progress(f"[prepare] {done}/{len(scoped)} pages loaded, {len(failures)} failed")

    manifest: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    too_long: list[str] = []
    empty_pages = 0
    for page in scoped:
        if page["key"] not in bodies:
            continue
        markdown, html_title = bodies[page["key"]]
        title, sections = chunk_page(markdown, page.get("title") or html_title, max_chars)
        title = title or html_title or fallback_title(page["url"])
        if not sections:
            empty_pages += 1
            continue
        for section, body in sections:
            chunk_id = short_hash(f"{page['key']}\n{section}")
            suffix = 2
            while chunk_id in used_ids:
                chunk_id = short_hash(f"{page['key']}\n{section}\n{suffix}")
                suffix += 1
            used_ids.add(chunk_id)
            text = render_chunk(title, section, page["url"], body)
            # Sent as one argument, a chunk must leave room on a Windows command line for the rest of it.
            if len(subprocess.list2cmdline(["--", text])) > WINDOWS_ARG_LIMIT - 1000:
                too_long.append(chunk_id)
            (run.chunk_dir / f"{chunk_id}.md").write_text(text, encoding="utf-8")
            manifest.append({
                "id": chunk_id,
                "page": page["key"],
                "url": page["url"],
                "group": page.get("group", ""),
                "title": title,
                "section": section,
                "chars": len(text),
                "hash": content_hash(text),
                "file": f"chunks/{chunk_id}.md",
                "oversize": len(text) > max_chars + 500,
            })
    for stale in run.chunk_dir.glob("*.md"):
        if stale.stem not in used_ids:
            stale.unlink()
    write_jsonl(run.manifest, manifest)
    config["prepared_at"] = now_iso()
    run.save_config(config)
    sizes = sorted(row["chars"] for row in manifest)
    emit({
        "run": str(run.root),
        "pages_in_scope": len(scoped),
        "pages_loaded": len(bodies),
        "pages_failed": failures[:50],
        "pages_failed_count": len(failures),
        "pages_without_text": empty_pages,
        "chunks": len(manifest),
        "chars_total": sum(sizes),
        "chunk_chars": {
            "min": sizes[0] if sizes else 0,
            "median": sizes[len(sizes) // 2] if sizes else 0,
            "max": sizes[-1] if sizes else 0,
        },
        "oversize_chunks": [row["id"] for row in manifest if row["oversize"]][:20],
        "too_long_for_windows": too_long[:20],
        "max_chars": max_chars,
        "manifest": str(run.manifest),
    })
    return 0


# --------------------------------------------------------------------------------------------
# sample
# --------------------------------------------------------------------------------------------

WORD = re.compile(r"[a-z0-9][a-z0-9_\-./]*[a-z0-9]|[a-z0-9]")
STOPWORDS = set(
    "the and for are but not you all any can had her was one our out has him his how its may new "
    "now old see two who did get let say she too use what when where which while with would could "
    "should there their them then than this that these those from into over under about after "
    "before does done doing have having here just more most other some such only own same very "
    "will your yours been being each few much many also why whom whose ever both either neither "
    "page section source".split()
)


def terms(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if len(w) > 2 and w not in STOPWORDS]


def read_questions(path: str) -> list[str]:
    file_path = Path(path)
    if not file_path.exists():
        raise UsageError(f"questions file not found: {path}")
    questions = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        questions.append(re.sub(r"^(\d+[.)]|[-*+])\s+", "", line))
    if not questions:
        raise UsageError(f"no questions in {path}")
    return questions


def rank_chunks(run: Run, manifest: list[dict[str, Any]], questions: list[str]) -> list[list[tuple[float, int]]]:
    """BM25 ranking of every chunk for every question: per question, [(score, index)] best first."""
    docs = []
    for row in manifest:
        text = (run.root / row["file"]).read_text(encoding="utf-8")
        counts: dict[str, int] = {}
        for word in terms(text):
            counts[word] = counts.get(word, 0) + 1
        docs.append((counts, sum(counts.values())))
    total = len(docs)
    average = (sum(length for _, length in docs) / total) if total else 1.0
    frequency: dict[str, int] = {}
    for counts, _ in docs:
        for word in counts:
            frequency[word] = frequency.get(word, 0) + 1
    rankings = []
    for question in questions:
        wanted = set(terms(question))
        scored = []
        for index, (counts, length) in enumerate(docs):
            score = 0.0
            for word in wanted:
                tf = counts.get(word, 0)
                if not tf:
                    continue
                idf = math.log(1 + (total - frequency[word] + 0.5) / (frequency[word] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / (average or 1.0)))
            if score > 0:
                scored.append((score, index))
        scored.sort(key=lambda item: (-item[0], item[1]))
        rankings.append(scored)
    return rankings


def cmd_sample(args: argparse.Namespace) -> int:
    run = Run(args.run)
    manifest = run.load_manifest()
    questions = read_questions(args.questions)
    rankings = rank_chunks(run, manifest, questions)
    picked: set[int] = set()
    picks = []
    unmatched = []
    for question, ranking in zip(questions, rankings):
        taken = 0
        for score, index in ranking:
            if taken >= args.per_question:
                break
            if index in picked and not args.allow_repeats:
                continue
            picked.add(index)
            taken += 1
            row = manifest[index]
            picks.append({
                "question": question,
                "chunk": row["id"],
                "title": row["title"],
                "section": row["section"],
                "url": row["url"],
                "chars": row["chars"],
                "score": round(score, 2),
                "file": str(run.root / row["file"]),
            })
        if not taken:
            unmatched.append(question)
    emit({"picks": picks, "unmatched_questions": unmatched, "chunk_ids": ",".join(p["chunk"] for p in picks)})
    return 0


# --------------------------------------------------------------------------------------------
# xmemcli
# --------------------------------------------------------------------------------------------


@dataclass
class CliResult:
    code: int
    doc: Any
    stderr: str

    @property
    def error(self) -> str:
        if isinstance(self.doc, dict) and self.doc.get("error"):
            return str(self.doc["error"])
        return (self.stderr or "").strip().splitlines()[-1] if (self.stderr or "").strip() else f"exit {self.code}"

    @property
    def http_status(self) -> int | None:
        if isinstance(self.doc, dict):
            status = self.doc.get("status") or self.doc.get("http_status")
            if isinstance(status, int):
                return status
        return None


class XmemCli:
    def __init__(self, binary: str) -> None:
        resolved = shutil.which(binary) or (binary if os.path.exists(binary) else None)
        if resolved is None:
            raise UsageError(
                f"`{binary}` not found; install it with `uv tool install xmemcli` and sign in with `xmemcli auth login`"
            )
        self.binary = resolved

    def run(self, args: list[str], timeout: float) -> CliResult:
        # xmemcli is Python too: UTF-8 mode keeps non-ASCII text intact through the pipe on
        # Windows, where the default code page would mangle or refuse it.
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        # Its own process group, so Ctrl-C stops this script from sending more without also killing
        # a write already on its way: that one finishes and is logged, rather than lost and resent.
        isolation: dict[str, Any] = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        )
        try:
            proc = subprocess.run(
                [self.binary, "--json", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
                **isolation,
            )
        except subprocess.TimeoutExpired:
            return CliResult(LOCAL_TIMEOUT, None, f"xmemcli did not answer within {int(timeout)}s")
        except OSError as exc:
            # E2BIG and its Windows equivalent: the chunk is too long to pass as an argument.
            return CliResult(LOCAL_SPAWN_ERROR, {"error": f"could not start xmemcli: {exc}"}, "")
        doc: Any = None
        out = proc.stdout.strip()
        if out:
            try:
                doc = json.loads(out)
            except json.JSONDecodeError:
                doc = {"raw": out[-2000:]}
        return CliResult(proc.returncode, doc, proc.stderr)


def retry_delay(result: CliResult, attempt: int) -> float:
    match = re.search(r"retry[_ -]after[\":= ]+(\d+)", json.dumps(result.doc) if result.doc else result.stderr, re.I)
    if match:
        return min(float(match.group(1)), 600.0) + random.uniform(0, 2)
    return min(5.0 * (3 ** attempt), 180.0) + random.uniform(0, 3)


def retryable(result: CliResult) -> bool:
    """Whether sending the same write again right away is safe: only when the server refused it.

    A 429 means the server did not take the write. When anything else interrupts a write after the
    request left, it may already be stored, and sending it again could store it twice; those are
    reported as `unknown` instead.
    """
    return result.code == EXIT_HTTP and result.http_status == 429


def rejected(result: CliResult) -> bool:
    """Whether a failed write was certainly not stored: never sent, or refused by the server.

    Only these are safe to send again on a later run. Any other outcome the script cannot confirm,
    such as an interrupted connection, may already be stored, so it defaults to `unknown` rather
    than to a resend: an unconfirmed outcome becomes a question for the user, never a duplicate.
    """
    if result.code == LOCAL_SPAWN_ERROR:
        return True
    status = result.http_status if result.code == EXIT_HTTP else None
    return status is not None and 400 <= status < 500 and status != 408


# --------------------------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------------------------


@dataclass
class Task:
    chunk: dict[str, Any]
    instance: str
    resume_write_id: str | None = None


@dataclass
class Outcome:
    task: Task
    status: str  # completed | failed | pending | stopped | skipped
    write_id: str | None = None
    error: str = ""
    pilot: dict[str, Any] | None = None


class Writer:
    def __init__(self, run: Run, cli: XmemCli, args: argparse.Namespace) -> None:
        self.run = run
        self.cli = cli
        self.sync = args.sync
        self.force = args.force
        self.status_timeout = args.status_timeout
        self.stop = threading.Event()
        self.stop_reason = ""
        self.lock = threading.Lock()
        self.pk_cache: dict[str, dict[str, list[str]]] = {}

    def log(self, task: Task, status: str, write_id: str | None = None, error: str = "") -> None:
        row = {
            "at": now_iso(),
            "instance": task.instance,
            "chunk": task.chunk["id"],
            "hash": task.chunk["hash"],
            "status": status,
        }
        if write_id:
            row["write_id"] = write_id
        if error:
            row["error"] = error[:2000]
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with self.lock:
            with self.run.state.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    def halt(self, reason: str) -> None:
        with self.lock:
            if not self.stop_reason:
                self.stop_reason = reason
        self.stop.set()

    def fatal(self, result: CliResult) -> bool:
        if result.code == EXIT_QUOTA:
            self.halt(f"quota exhausted: {result.error}")
            return True
        if result.code == EXIT_AUTH:
            self.halt(f"xmemcli is not signed in or the key was refused: {result.error}")
            return True
        if result.code == EXIT_USAGE:
            self.halt(
                f"xmemcli refused the command or could not start ({result.error}); check that it is 1.5.1 or "
                "newer and that the network is up, then rerun the same command"
            )
            return True
        return False

    def wait(self, task: Task, write_id: str) -> tuple[str, str]:
        """Poll one async write to a terminal state: (completed|failed|pending|stopped, error)."""
        deadline = time.monotonic() + self.status_timeout
        errors = 0
        while True:
            if self.stop.is_set():
                # Already sent: the log keeps its write_id, and the next run checks it instead of
                # sending it again.
                return "pending", "the run stopped before this write finished; the next run checks it"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "pending", "still in flight when the status wait ran out"
            poll = min(60, max(5, int(remaining)))
            asked = time.monotonic()
            result = self.cli.run(
                ["--instance-id", task.instance, "write-status", write_id, "--timeout", str(poll)],
                timeout=poll + 60,
            )
            if self.fatal(result):
                return "stopped", result.error
            doc = result.doc if isinstance(result.doc, dict) else {}
            state = doc.get("write_status")
            if state == "completed":
                return "completed", ""
            if state == "failed":
                return "failed", str(doc.get("error_detail") or doc.get("error") or state)
            if state == "not_found":
                return "not_found", "this write's status is not available"
            # A write still in flight when --timeout runs out comes back with its current state
            # and exit 5: that is an answer, so poll again. Only a poll that says nothing counts
            # against the write.
            if result.code in (EXIT_OK, EXIT_TIMEOUT) and isinstance(state, str) and state not in ("error", "unknown"):
                errors = 0
                if time.monotonic() - asked < 2:
                    self.stop.wait(2)  # a CLI that answers at once must not turn this into a busy loop
                continue
            errors += 1
            if errors >= 5:
                return "pending", f"write-status gave no answer after 5 attempts: {doc.get('error') or result.error}"
            self.stop.wait(min(5 * errors, 30))

    def not_sent(self, task: Task, attempts: int) -> str:
        """Stop before the write could land: every attempt so far was refused (quota, sign-in, 429)."""
        if attempts:
            self.log(task, "not_sent")
        return "stopped"

    def submit(self, task: Task, text: str) -> tuple[CliResult | None, str]:
        """Queue one write, retrying what is safe to retry. (result, error) - result None on failure.

        Pilot writes are queued too: the write id is logged before any waiting starts, so a write
        still processing when the wait ends is followed by its id, never left unknown and sent again.
        """
        base = ["--verbose", "--instance-id", task.instance, "write", "--no-wait"]
        if os.name == "nt" and len(subprocess.list2cmdline(base + ["--", text])) > WINDOWS_ARG_LIMIT:
            return None, (
                "too long for the Windows command line; shorten that section in the source and run `prepare` "
                "again (a lower --max-chars re-chunks every page, and `write --all` would then send them all again)"
            )
        for attempt in range(5):
            if self.stop.is_set():
                return None, self.not_sent(task, attempt)
            if attempt == 0:
                # Logged before the write leaves: if the run is killed before its answer is logged,
                # the next run reads this row as `unknown` instead of sending the chunk again.
                self.log(task, "sending")
            result = self.cli.run(base + ["--", text], timeout=90)
            # Exit 8: accepted and still being processed; the answer carries the id to follow.
            if result.code in (EXIT_OK, EXIT_STILL_PROCESSING):
                return result, ""
            if self.fatal(result):
                return None, self.not_sent(task, attempt + 1)
            if retryable(result) and attempt < 4:
                delay = retry_delay(result, attempt)
                progress(f"[write] {task.chunk['id']} -> {task.instance[:8]}: {result.error}; retrying in {int(delay)}s")
                if self.stop.wait(delay):
                    return None, self.not_sent(task, attempt + 1)
                continue
            if not rejected(result):
                status = result.http_status
                if result.code in (EXIT_HTTP, LOCAL_TIMEOUT) and (status is None or status >= 500):
                    # No answer, or a server error: the following writes would likely get the same
                    # result, each left unknown. Stop, so only those already in flight are.
                    self.halt(f"xmemory could not be reached or did not answer ({result.error}); "
                              "rerun the same command once it is back")
                return None, f"{MAY_HAVE_LANDED}: {result.error}"
            return None, result.error
        return None, "gave up after 5 attempts"

    def perform(self, task: Task) -> Outcome:
        if self.stop.is_set():
            return Outcome(task, "stopped")
        if task.resume_write_id:
            write_id = task.resume_write_id
            state, error = self.wait(task, write_id)
            if state in ("pending", "stopped"):
                self.log(task, "pending" if state == "pending" else "queued", write_id, error)
                return Outcome(task, state, write_id, error)
            # Under --force the earlier copy is only waited for, so two never run at once; the
            # chunk is then sent again below, and the report is the new write's.
            if state == "completed":
                self.log(task, "completed", write_id)
                if not self.force:
                    pilot = self.report(task, functools.partial(self.completed_write, task, write_id)) if self.sync else None
                    return Outcome(task, "completed", write_id, pilot=pilot)
            elif state == "not_found":
                if not self.force:
                    # A write's status can be looked up for a limited time, so one sent long ago
                    # whose status is gone has most likely landed. Sending it again would be a
                    # second copy; --force sends it anyway.
                    error = ("this write is too old to look up; it most likely landed "
                             "(check whether it is stored, then --mark-stored or --resend-unknown)")
                    self.log(task, "unknown", write_id, error)
                    return Outcome(task, "unknown", write_id, error)
            else:
                self.log(task, "failed", write_id, error)
        # The text sent is what the log records, so a chunk rewritten mid-run is not mistaken for sent.
        text = (self.run.root / task.chunk["file"]).read_text(encoding="utf-8")
        task.chunk = dict(task.chunk, hash=content_hash(text))
        last_error = ""
        for round_ in range(2):
            result, error = self.submit(task, text)
            if result is None:
                if error == "stopped":
                    return Outcome(task, "stopped")
                if error.startswith(MAY_HAVE_LANDED):
                    self.log(task, "unknown", error=error)
                    return Outcome(task, "unknown", error=error)
                last_error = error
                break
            doc = result.doc if isinstance(result.doc, dict) else {}
            write_id = doc.get("write_id")
            if not write_id:
                match = UUID_RE.search(json.dumps(doc))
                write_id = match.group(0) if match else None
            if not write_id:
                # Accepted, but without an id to follow it by: it may well land, so it is not resent.
                error = f"{MAY_HAVE_LANDED}: no write_id in the xmemcli answer"
                self.log(task, "unknown", error=error)
                return Outcome(task, "unknown", error=error)
            self.log(task, "queued", write_id)
            state, error = self.wait(task, write_id)
            if state == "completed":
                self.log(task, "completed", write_id)
                pilot = self.report(task, functools.partial(self.completed_write, task, write_id)) if self.sync else None
                return Outcome(task, "completed", write_id, pilot=pilot)
            if state == "pending":
                self.log(task, "pending", write_id, error)
                return Outcome(task, "pending", write_id, error)
            if state == "stopped":
                return Outcome(task, "stopped", write_id)
            self.log(task, "failed", write_id, error)
            last_error = error
            if round_ == 0:
                progress(f"[write] {task.chunk['id']} -> {task.instance[:8]} failed ({error}); retrying once")
        self.log(task, "failed", error=last_error)
        return Outcome(task, "failed", error=last_error)

    # -- pilot ---------------------------------------------------------------------------

    def primary_keys(self, instance: str) -> dict[str, list[str]]:
        with self.lock:
            if instance in self.pk_cache:
                return self.pk_cache[instance]
        keys: dict[str, list[str]] = {}
        result = self.cli.run(["schema", "get", instance], timeout=60)
        schema: Any = result.doc if isinstance(result.doc, dict) else {}
        schema = schema.get("data_schema", schema)
        objects = schema.get("objects") if isinstance(schema, dict) else None
        for name, obj in (objects.items() if isinstance(objects, dict) else []):
            if isinstance(obj, dict) and isinstance(obj.get("primary_key"), list):
                keys[name] = [str(k) for k in obj["primary_key"]]
        with self.lock:
            self.pk_cache[instance] = keys
        return keys

    def tokens_used(self, trace_id: str) -> tuple[float | None, str]:
        """The xmemory tokens one write used, from its trace: (tokens, error).

        Only the token count is read from the trace; what the write stored comes from its own
        `changes`. A trace may not be available right away, so an empty one is asked for again
        before it is reported as missing.
        """
        error = ""
        for attempt in range(8):
            if attempt:
                time.sleep(min(2 * attempt, 10))
            result = self.cli.run(["trace", "get", trace_id, "--trace-type", "write"], timeout=120)
            if result.code != EXIT_OK or not isinstance(result.doc, dict):
                error = result.error
                if result.code in (EXIT_USAGE, EXIT_AUTH):
                    break
                continue
            tokens = result.doc.get("xmemory_tokens_used")
            if result.doc.get("found") is not False and isinstance(tokens, (int, float)):
                return float(tokens), ""
            error = "the write's trace is not available yet"
        return None, error

    def completed_write(self, task: Task, write_id: str) -> dict[str, Any]:
        """A finished write's own report: its status, which carries its `changes` and trace.

        An answer that is not that report raises, so the pilot record says the report is missing
        rather than showing an empty extraction, which would read as a description too narrow.
        """
        error = ""
        for attempt in range(3):
            result = self.cli.run(
                ["--verbose", "--instance-id", task.instance, "write-status", write_id, "--timeout", "10"], timeout=70
            )
            doc = result.doc if isinstance(result.doc, dict) else {}
            if result.code == EXIT_OK and doc.get("write_status") == "completed":
                return doc
            error = str(doc.get("error") or result.error or f"write status {doc.get('write_status')}")
            if attempt < 2:
                self.stop.wait(REPORT_RETRY_SECONDS * (attempt + 1))
        raise RuntimeError(f"could not read the report of write {write_id} ({error}); the write itself landed")

    def report(self, task: Task, write_doc: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """The pilot record of a write that landed.

        Building it reads the write's report and saves a file; if that fails, the write still
        landed and stays logged as completed - the error goes into the record instead.
        """
        try:
            return self.pilot_record(task, write_doc())
        except Exception as exc:
            return {
                "chunk": task.chunk["id"],
                "instance": task.instance,
                "section": " > ".join(x for x in (task.chunk["title"], task.chunk["section"]) if x),
                "url": task.chunk["url"],
                "objects": [],
                "warnings": [],
                "report_error": f"{exc.__class__.__name__}: {exc}",
            }

    def pilot_record(self, task: Task, write_doc: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {
            "chunk": task.chunk["id"],
            "instance": task.instance,
            "section": " > ".join(x for x in (task.chunk["title"], task.chunk["section"]) if x),
            "url": task.chunk["url"],
            "console_url": write_doc.get("console_url"),
            # What the write changed, not all it extracted: a record extracted exactly as already
            # stored is not listed.
            "changed_records": stored_tags(write_doc.get("changes")),
        }
        record.update(summarise_changes(write_doc.get("changes"), self.primary_keys(task.instance)))
        trace_id = write_doc.get("trace_id")
        if trace_id:
            record["tokens"], error = self.tokens_used(str(trace_id))
            if error:
                record["tokens_error"] = error
        # One file per write, not per chunk: a rewrite must not erase what an earlier round created.
        write_id = str(write_doc.get("write_id") or "")
        record["write_id"] = write_id or None
        suffix = f"__{write_id[:8]}" if write_id else ""
        path = self.run.pilot_dir / f"{task.chunk['id']}__{task.instance[:8]}{suffix}.json"
        write_atomic(path, json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        record["file"] = str(path)
        return record


def scalar(value: Any) -> Any:
    """A `{"string_value": "x"}`-style field value as the plain value."""
    if isinstance(value, dict) and len(value) == 1 and next(iter(value)).endswith("_value"):
        return next(iter(value.values()))
    return value


# A change entry's identifier names a record by its key, `code='a', region='b'`. A record whose
# key came back empty is named by its position instead, `#1`, as a record of a keyless type is.
KEY_PART = re.compile(r"""(\w+)=('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|[^,\s]+)""")


def missing_key_fields(identifier: str, keys: list[str]) -> list[str]:
    """The key fields a record of a keyed type came out without, judged from its identifier."""
    identifier = identifier.strip()
    if not keys or not identifier:
        return []  # a keyless type, or a record the server could not name: nothing to judge
    if identifier.startswith("#"):
        return list(keys)
    present = dict(KEY_PART.findall(identifier))
    return [k for k in keys if present.get(k, "None") in ("None", "''", '""', "null")]


# Server notes that bear on the key decision, and what each means for the pilot.
KEY_NOTES = {
    "merged_field_conflicts": "the server merged records whose values disagree; the key may be too coarse",
}


def stored_tags(changes: Any) -> list[str]:
    """`Type(key)` tags of the records a write created or updated, from its `changes` block."""
    tags = []
    for bucket in ("created", "updated"):
        value = changes.get(bucket) if isinstance(changes, dict) else None
        entries = value.get("objects") if isinstance(value, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and entry.get("name"):
                identifier = entry.get("identifier")
                tags.append(f"{entry['name']}({identifier})" if identifier else str(entry["name"]))
    return tags


def summarise_changes(changes: Any, primary_keys: dict[str, list[str]]) -> dict[str, Any]:
    """What one pilot write did, from the `changes` block the write returns (xmemcli 1.5.1+).

    `created` and `updated` hold records as `{name, identifier, fields}`; an updated field carries
    `old_value` and `new_value`. Any other bucket (advisory notes, skipped records) is counted and
    kept as it came.
    """
    created: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    relations: list[Any] = []
    deleted = 0
    notes: dict[str, Any] = {}
    for bucket, value in (changes.items() if isinstance(changes, dict) else []):
        members = value if isinstance(value, dict) else {"objects": value} if isinstance(value, list) else {}
        for member, entries in members.items():
            entries = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
            if bucket == "created" and member == "objects":
                created.extend(entries)
            elif bucket == "created" and member == "relations":
                relations.extend(entries)
            elif bucket == "updated" and member == "objects":
                updated.extend(entries)
            elif bucket == "deleted":
                # Relations a write re-points are unlinked on the way; only records count here.
                deleted += len(entries) if member == "objects" else 0
            elif entries:
                notes[bucket] = notes.get(bucket, []) + entries
    objects = [
        {
            "type": entry.get("name"),
            "key": entry.get("identifier") or "",
            "fields": {f.get("name"): scalar(f.get("value")) for f in entry.get("fields") or [] if isinstance(f, dict)},
        }
        for entry in created
    ]
    overwrote: list[dict[str, Any]] = []
    # Fields an earlier write left empty and this one filled: no warning, but shown, because a
    # value appearing on an existing record is often what a schema fix was for.
    filled: list[dict[str, Any]] = []
    for entry in updated:
        for field in entry.get("fields") or []:
            if not isinstance(field, dict):
                continue
            change = {
                "type": entry.get("name"),
                "record": entry.get("identifier") or "",
                "field": field.get("name"),
                "old": scalar(field.get("old_value")),
                "new": scalar(field.get("new_value")),
            }
            if "old_value" not in field:
                # An empty value is reported as null; one not reported at all may have held
                # something, so it counts as an overwrite.
                if change["new"] not in (None, ""):
                    overwrote.append({**change, "old_reported": False})
            elif change["old"] in (None, ""):
                if change["new"] not in (None, ""):
                    filled.append(change)
            elif change["old"] != change["new"]:
                overwrote.append(change)
    warnings: list[str] = []
    keyless: dict[str, set[str]] = {}
    for entry in created + updated:
        missing = missing_key_fields(str(entry.get("identifier") or ""), primary_keys.get(str(entry.get("name")), []))
        if missing:
            keyless.setdefault(str(entry.get("name")), set()).update(missing)
    for type_name, missing in sorted(keyless.items()):
        warnings.append(
            f"{type_name} came out without its key field(s) {', '.join(sorted(missing))}: "
            "it would be matched with every other record stored without them"
        )
    for bucket, meaning in KEY_NOTES.items():
        if notes.get(bucket):
            warnings.append(f"{bucket} ({len(notes[bucket])}): {meaning}; see the pilot file")
    if overwrote:
        fields = sorted({f"{c['type']}.{c['field']}" for c in overwrote})
        warnings.append(
            f"overwrote {len(overwrote)} value(s) of records written earlier ({', '.join(fields)}); "
            "fine when it is the same thing described again, a key that is too coarse when it is not"
        )
    if deleted:
        warnings.append(f"deleted {deleted} record(s): unexpected when loading documentation, check which description led to it")
    return {
        "objects": objects,
        "relations": relations,
        "updated": updated,
        "overwrote_existing": overwrote,
        "filled_existing": filled,
        "server_notes": notes,
        "warnings": warnings,
    }


def state_by_instance(run: Run) -> dict[str, dict[str, dict[str, Any]]]:
    """Last log row per (instance, chunk id): the version of each chunk the instance last received.

    Keyed by chunk, not by text: a section changed and then changed back must be written again,
    because the instance now holds what the version in between said.
    """
    latest: dict[str, dict[str, dict[str, Any]]] = {}
    # A `sending` row is logged before a write leaves; a run killed at that moment leaves it as the
    # last row, and it reads as `unknown`. A send that certainly did not land puts the chunk back
    # as it was before it.
    before_sending: dict[tuple[str, str], tuple[dict[str, Any] | None, dict[str, Any] | None]] = {}
    # The version each instance is known to hold, while no other version may have landed since: a
    # failed rewrite of it does not undo that.
    stored: dict[tuple[str, str], dict[str, Any]] = {}
    for row in read_jsonl(run.state):
        rows = latest.setdefault(row["instance"], {})
        key = (row["instance"], row["chunk"])
        if row["status"] == "sending":
            before_sending[key] = (rows.get(row["chunk"]), stored.get(key))
            if stored.get(key, {}).get("hash") != row["hash"]:
                stored.pop(key, None)  # another version is on its way: what is held may change
            rows[row["chunk"]] = row
            continue
        if key in before_sending:
            earlier, earlier_stored = before_sending.pop(key)
            if row["status"] in ("failed", "not_sent"):
                if earlier is None:
                    rows.pop(row["chunk"], None)
                else:
                    rows[row["chunk"]] = earlier
                if earlier_stored is not None:
                    stored[key] = earlier_stored
        if row["status"] == "not_sent":
            continue
        earlier = rows.get(row["chunk"])
        if row["status"] == "failed" and earlier is not None:
            if earlier["hash"] != row["hash"]:
                continue  # a different version that failed was never stored; the earlier one still is
            if stored.get(key, {}).get("hash") == row["hash"]:
                rows[row["chunk"]] = stored[key]  # this version was stored before the rewrite failed
                continue
        if row["status"] == "completed":
            stored[key] = row
        elif stored.get(key, {}).get("hash") != row["hash"]:
            stored.pop(key, None)  # a different version may have landed
        rows[row["chunk"]] = row
    return latest


def select_chunks(manifest: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.all:
        return manifest
    wanted = [c.strip() for c in args.chunks.split(",") if c.strip()]
    by_id = {row["id"]: row for row in manifest}
    unknown = [c for c in wanted if c not in by_id]
    if unknown:
        raise UsageError(f"unknown chunk ids: {', '.join(unknown)}")
    return [by_id[c] for c in wanted]


def cmd_write(args: argparse.Namespace) -> int:
    run = Run(args.run)
    run.ensure()
    instances = list(dict.fromkeys(args.instance))
    for instance in instances:
        if not UUID_RE.fullmatch(instance.lower()):
            raise UsageError(f"not an instance id: {instance}")
    # The manifest and the log are read under the lock, so no other `write` or `prepare` can change
    # them between reading what is due and sending it.
    if args.mark_stored:
        if not args.chunks:
            raise UsageError("--mark-stored needs --chunks: name the chunks a check found stored")
        if len(instances) != 1:
            raise UsageError("--mark-stored takes one --instance: the one where the check found the chunks")
        with RunLock(run):
            return mark_stored(run, instances, args)  # sends nothing, so it needs no xmemcli
    cli = XmemCli(args.xmemcli)
    with RunLock(run):
        return write_locked(run, cli, instances, args)


def mark_stored(run: Run, instances: list[str], args: argparse.Namespace) -> int:
    """Record `unknown` chunks as written, after a check found what they say in the instance.

    Only chunks whose current version is `unknown` are touched, and nothing is sent: this closes
    the question so a later `--resend-unknown` does not store them a second time.
    """
    latest = state_by_instance(run)
    marked, left = [], []
    rows = []
    for chunk in select_chunks(run.load_manifest(), args):
        for instance in instances:
            row = latest.get(instance, {}).get(chunk["id"])
            status = row["status"] if row and row["hash"] == chunk["hash"] else None
            entry = {"chunk": chunk["id"], "instance": instance}
            if status in ("unknown", "sending"):
                rows.append({"at": now_iso(), **entry, "hash": chunk["hash"], "status": "completed",
                             "note": "marked stored after a check"})
                marked.append(entry)
            else:
                left.append({**entry, "status": status or "not_written"})
    with run.state.open("a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    emit({"run": str(run.root), "marked_stored": marked, "not_unknown": left})
    return 0


def write_locked(run: Run, cli: XmemCli, instances: list[str], args: argparse.Namespace) -> int:
    manifest = run.load_manifest()
    chunks = select_chunks(manifest, args)
    latest = state_by_instance(run)
    tasks: list[Task] = []
    buckets = ("completed", "already_written", "failed", "pending", "unknown", "not_attempted")
    counts = {i: dict.fromkeys(buckets, 0) for i in instances}
    held: list[dict[str, Any]] = []  # unknown from earlier runs: listed, so each can be checked
    for chunk in chunks:
        for instance in instances:
            row = latest.get(instance, {}).get(chunk["id"])
            status = row["status"] if row and row["hash"] == chunk["hash"] else None
            if status == "sending":
                status = "unknown"  # the run stopped while this write was leaving: it may have landed
            if status == "completed" and not args.force:
                counts[instance]["already_written"] += 1
                continue
            if status == "unknown" and not (args.force or args.resend_unknown):
                counts[instance]["unknown"] += 1
                reason = (row or {}).get("error") or f"{MAY_HAVE_LANDED}: the run stopped while this write was being sent"
                held.append({"chunk": chunk["id"], "instance": instance, "error": reason})
                continue
            # This same text still in flight is collected first, even under --force: a second
            # copy sent while the first is being processed would land beside it, its id lost.
            resume = row.get("write_id") if row and status in ("queued", "pending") else None
            tasks.append(Task(chunk, instance, resume))
    writer = Writer(run, cli, args)
    total = len(tasks)
    progress(f"[write] {total} writes to send ({'pilot' if args.sync else 'bulk'}, {args.concurrency} at a time)")
    started = time.monotonic()

    def perform(task: Task) -> Outcome:
        try:
            return writer.perform(task)
        except Exception as exc:  # one broken chunk must not cost the report for the rest
            error = f"{exc.__class__.__name__}: {exc}"
            try:
                writer.log(task, "failed", error=error)
            except OSError:
                pass
            return Outcome(task, "failed", error=error)

    interrupted = False
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency))
    futures = [pool.submit(perform, task) for task in tasks]
    try:
        done = 0
        for future in concurrent.futures.as_completed(futures):
            outcome = future.result()
            done += 1
            if outcome.status != "stopped":
                elapsed = time.monotonic() - started
                progress(
                    f"[write] {done}/{total} {outcome.status:9} {outcome.task.chunk['id']} -> {outcome.task.instance[:8]}"
                    + (f" ({outcome.error[:160]})" if outcome.error else "")
                    + f"  [{int(elapsed)}s]"
                )
    except KeyboardInterrupt:
        interrupted = True
        writer.halt("interrupted; rerun the same command to resume")
        for future in futures:
            future.cancel()
        progress("[write] interrupted: sending nothing more; writes already on their way finish and are logged")
    finally:
        pool.shutdown(wait=True)
    outcomes = [f.result() for f in futures if f.done() and not f.cancelled()]
    failures = []
    unknown: list[dict[str, Any]] = []
    pilot = []
    for task in (t for t, f in zip(tasks, futures) if f.cancelled()):
        counts[task.instance]["not_attempted"] += 1
    for outcome in outcomes:
        bucket = counts[outcome.task.instance]
        key = outcome.status if outcome.status in ("completed", "failed", "pending", "unknown") else "not_attempted"
        bucket[key] += 1
        if outcome.status == "failed":
            failures.append({"chunk": outcome.task.chunk["id"], "instance": outcome.task.instance, "error": outcome.error})
        if outcome.status == "unknown":
            unknown.append({"chunk": outcome.task.chunk["id"], "instance": outcome.task.instance, "error": outcome.error})
        if outcome.pilot is not None:
            pilot.append(outcome.pilot)
    report: dict[str, Any] = {
        "run": str(run.root),
        "mode": "pilot" if args.sync else "bulk",
        "chunks_selected": len(chunks),
        "instances": counts,
        "stopped": writer.stop_reason or None,
        "failures": failures[:50],
        "failures_count": len(failures),
        # This run's unknown writes, then those held back from earlier runs, each with its reason.
        "unknown": (unknown + held)[:50],
        "seconds": int(time.monotonic() - started),
        "log": str(run.state),
    }
    if args.sync:
        pilot.sort(key=lambda r: (r["chunk"], r["instance"]))
        report["pilot"] = [compact_pilot(r) for r in pilot]
        report.update(estimate(run, manifest, pilot, instances))
    emit(report)
    if interrupted:
        return 130
    if writer.stop_reason:
        return 3
    return 1 if failures or unknown else 0


class RunLock:
    """One `write` or `prepare` per run directory at a time: two writers would send the same chunks
    twice, and a `prepare` would rewrite the chunks a `write` is sending."""

    def __init__(self, run: Run) -> None:
        self.path = run.root / "run.lock"

    def __enter__(self) -> RunLock:
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._stale():
                    self.path.unlink()
                    continue
                raise UsageError(self._held_message()) from None
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "host": socket.gethostname(), "since": now_iso()}, handle)
            return self
        raise UsageError(self._held_message())

    def __exit__(self, *exc_info: object) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    def _info(self) -> dict[str, Any]:
        try:
            info = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return info if isinstance(info, dict) else {}

    def _stale(self) -> bool:
        """True only when the holder is provably gone: same host, POSIX, no such process."""
        info = self._info()
        if os.name == "nt" or info.get("host") != socket.gethostname():
            return False  # on Windows os.kill(pid, 0) would terminate the process, not probe it
        try:
            os.kill(int(info["pid"]), 0)
        except ProcessLookupError:
            return True
        except (KeyError, TypeError, ValueError, OSError):
            return False
        return False

    def _held_message(self) -> str:
        info = self._info()
        return (
            f"another `write` or `prepare` is using {self.path.parent} (pid {info.get('pid', '?')} on {info.get('host', '?')} "
            f"since {info.get('since', '?')}); wait for it to finish, or delete {self.path} if it is no longer running"
        )


def compact_pilot(record: dict[str, Any]) -> dict[str, Any]:
    objects = record.get("objects") or []
    rendered = []
    for obj in objects[:15]:
        fields = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)[:120]}" for k, v in (obj.get("fields") or {}).items())
        # `#N` numbers records within one write only; shown, two chunks' `#1` would read as one record.
        key = f"[{obj['key']}]" if obj.get("key") and not str(obj["key"]).startswith("#") else ""
        rendered.append(f"{obj.get('type')}{key}({fields})")
    for change in (record.get("overwrote_existing") or [])[:10]:
        old = "(not reported)" if change.get("old_reported") is False else json.dumps(change["old"], ensure_ascii=False)[:80]
        new = json.dumps(change["new"], ensure_ascii=False)[:80]
        rendered.append(f"{change['type']}[{change['record']}].{change['field']}: {old} -> {new}")
    for change in (record.get("filled_existing") or [])[:10]:
        new = json.dumps(change["new"], ensure_ascii=False)[:80]
        rendered.append(f"{change['type']}[{change['record']}].{change['field']}: (empty) -> {new}")
    return {
        "chunk": record["chunk"],
        "instance": record["instance"],
        "section": record["section"],
        "objects": rendered,
        "more_objects": max(0, len(objects) - 15),
        "updated_records": len(record.get("updated") or []),
        "changed_records": [re.sub(r"\(#\d+\)$", "", str(tag)) for tag in record.get("changed_records") or []],
        "relations": len(record.get("relations") or []),
        "server_notes": {bucket: len(entries) for bucket, entries in (record.get("server_notes") or {}).items()},
        "warnings": record.get("warnings") or [],
        **({"report_error": record["report_error"]} if record.get("report_error") else {}),
        "tokens": record.get("tokens"),
        "console_url": record.get("console_url"),
        "file": record.get("file"),
    }


def estimate(run: Run, manifest: list[dict[str, Any]], pilot: list[dict[str, Any]], instances: list[str]) -> dict[str, Any]:
    """Project the bulk write's token use from what the pilot writes cost per character."""
    chars = {row["id"]: row["chars"] for row in manifest}
    per_instance: dict[str, Any] = {}
    total_chars = sum(chars.values())
    for instance in instances:
        rows = [r for r in pilot if r["instance"] == instance and isinstance(r.get("tokens"), (int, float))]
        spent = sum(r["tokens"] for r in rows)
        sampled = sum(chars.get(r["chunk"], 0) for r in rows)
        if not rows or not sampled:
            continue
        per_instance[instance] = {
            "based_on_chunks": len(rows),
            "pilot_tokens": round(spent, 1),
            "tokens_per_1000_chars": round(1000 * spent / sampled, 1),
            "projected_tokens_all_chunks": int(spent / sampled * total_chars),
        }
    return {"token_estimate": per_instance} if per_instance else {}


# --------------------------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    run = Run(args.run)
    manifest = run.load_manifest()
    latest = state_by_instance(run)
    report = {}
    for instance, rows in latest.items():
        states = {"completed": 0, "failed": 0, "pending": 0, "unknown": 0, "outdated": 0, "not_written": 0}
        for chunk in manifest:
            row = rows.get(chunk["id"])
            if row is None:
                states["not_written"] += 1
            elif row["hash"] != chunk["hash"]:
                states["outdated"] += 1  # an earlier version of this chunk was written
            else:
                status = {"queued": "pending", "sending": "unknown"}.get(row["status"], row["status"])
                states[status] = states.get(status, 0) + 1
        report[instance] = states
    emit({"run": str(run.root), "chunks": len(manifest), "instances": report})
    return 0


# --------------------------------------------------------------------------------------------
# ask
# --------------------------------------------------------------------------------------------


def records_shown(answer: dict[str, Any], limit: int = 15, budget: int = 3000) -> dict[str, Any]:
    """An xresponse answer as the records it selected, each with its id and plain field values.

    The whole answer stays in answers.jsonl. The report keeps it valid JSON rather than cutting it
    mid-record, and small enough that a round of questions does not flood the agent's context:
    at most `limit` records within about `budget` characters, values shortened, the rest counted.
    """
    objects = answer["objects"]
    records: list[dict[str, Any]] = []
    used = 0
    for obj in objects[:limit]:
        fields = {}
        for field in obj.get("fields") or []:
            value = scalar(field.get("value"))
            if isinstance(value, str) and len(value) > 200:
                value = value[:200] + " ..."
            fields[field.get("name")] = value
        # Fields sit apart from the object type: a schema may well have a field named `type`.
        record = {"type": obj.get("name"), "id": obj.get("identifier"), "fields": fields}
        size = len(json.dumps(record, ensure_ascii=False))
        if records and used + size > budget:
            break
        records.append(record)
        used += size
    shown: dict[str, Any] = {"records": records}
    if len(objects) > len(records):
        shown["more_records"] = len(objects) - len(records)
    if answer.get("relations"):
        shown["relations"] = len(answer["relations"])
    return shown


def cmd_ask(args: argparse.Namespace) -> int:
    run = Run(args.run)
    run.ensure()
    questions = read_questions(args.questions)
    instances = list(dict.fromkeys(args.instance))
    cli = XmemCli(args.xmemcli)
    jobs = [(n, q, i) for n, q in enumerate(questions, 1) for i in instances]
    stop = threading.Event()

    def ask(job: tuple[int, str, str]) -> dict[str, Any]:
        number, question, instance = job
        entry: dict[str, Any] = {"n": number, "question": question, "instance": instance}
        if stop.is_set():
            entry["error"] = "not asked: an earlier read stopped the run"
            return entry
        for attempt in range(3):
            result = cli.run(
                ["--verbose", "--instance-id", instance, "read", "--read-mode", args.read_mode, "--", question],
                timeout=330,
            )
            if result.code == EXIT_OK and isinstance(result.doc, dict):
                doc = result.doc
                if "reader_result" in doc and doc["reader_result"] is None:
                    entry["answer"] = None
                    entry["note"] = "the schema cannot hold what was asked"
                elif args.read_mode == "single":
                    entry["answer"] = doc.get("answer", {k: v for k, v in doc.items() if k not in ("trace_id", "console_url")})
                else:
                    entry["answer"] = {k: v for k, v in doc.items() if k not in ("trace_id", "console_url")}
                if doc.get("unanswered_sub_queries"):
                    entry["unanswered_parts"] = doc["unanswered_sub_queries"]
                entry["console_url"] = doc.get("console_url")
                return entry
            if result.code in (EXIT_QUOTA, EXIT_AUTH, EXIT_USAGE):
                stop.set()
                entry["error"] = result.error
                return entry
            if retryable(result) and attempt < 2:
                time.sleep(retry_delay(result, attempt))
                continue
            entry["error"] = result.error
            return entry
        return entry

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        answers = list(pool.map(ask, jobs))
    # Appended, not replaced: a later run on one or two questions must not lose the full round.
    asked_at = now_iso()
    with run.answers.open("a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps({**entry, "asked_at": asked_at}, ensure_ascii=False) + "\n" for entry in answers))
    shown = []
    for entry in answers:
        item = dict(entry)
        if isinstance(item.get("answer"), dict) and isinstance(item["answer"].get("objects"), list):
            item["answer"] = records_shown(item["answer"])
        elif isinstance(item.get("answer"), str) and len(item["answer"]) > 1200:
            item["answer"] = item["answer"][:1200] + " ..."
        elif item.get("answer") is not None and not isinstance(item.get("answer"), str):
            text = json.dumps(item["answer"], ensure_ascii=False)
            item["answer"] = text if len(text) <= 1200 else text[:1200] + " ..."
        shown.append(item)
    emit({"answers": shown, "file": str(run.answers)})
    return 0


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingest.py",
        description="Ingest a documentation corpus into xmemory instances, outside the agent's context.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def with_run(p: argparse.ArgumentParser) -> None:
        p.add_argument("--run", required=True, help="Run directory: pages, chunks, manifest and the write log")

    def with_cli(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--xmemcli",
            default=os.environ.get("XMEMORY_INGEST_XMEMCLI", "xmemcli"),
            help="xmemcli executable (default: xmemcli on PATH)",
        )

    p = sub.add_parser("discover", help="List the pages a source offers")
    p.add_argument("sources", nargs="+", help="Directory, file, URL, site root, llms.txt, llms-full.txt, sitemap, or @list-file")
    with_run(p)
    p.add_argument(
        "--include", action="append", default=None,
        help="Keep pages whose URL, group or title contains TEXT (replaces the run's saved includes)",
    )
    p.add_argument(
        "--exclude", action="append", default=None,
        help="Drop pages whose URL, group or title contains TEXT (replaces the run's saved excludes)",
    )
    p.add_argument("--reset-scope", action="store_true", help="Forget the run's saved scope")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("prepare", help="Fetch, normalise and chunk the pages in scope")
    with_run(p)
    p.add_argument("--include", action="append", default=None, help="Replace the scope's include filters")
    p.add_argument("--exclude", action="append", default=None, help="Replace the scope's exclude filters")
    p.add_argument("--max-chars", type=int, default=None, help=f"Chunk size ceiling in characters (default {MAX_CHARS_DEFAULT})")
    p.add_argument("--refresh", action="store_true", help="Fetch web pages again instead of using the run's copies")
    p.add_argument("--fetch-concurrency", type=int, default=FETCH_CONCURRENCY)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("sample", help="Pick the chunks that best match a list of questions")
    with_run(p)
    p.add_argument("--questions", required=True, help="File with one question per line")
    p.add_argument("--per-question", type=int, default=1)
    p.add_argument("--allow-repeats", action="store_true", help="Let one chunk answer several questions")
    p.set_defaults(func=cmd_sample)

    p = sub.add_parser("write", help="Write chunks to instances through xmemcli")
    with_run(p)
    with_cli(p)
    p.add_argument("--instance", action="append", required=True, help="Instance id (repeat for several)")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--chunks", help="Comma-separated chunk ids")
    which.add_argument("--all", action="store_true", help="Every chunk in the manifest")
    p.add_argument("--sync", action="store_true", help="Wait for each write and report what it stored (the pilot)")
    p.add_argument("--force", action="store_true", help="Write again even where this exact chunk already landed")
    p.add_argument(
        "--resend-unknown", action="store_true",
        help="Send again the chunks whose earlier write may or may not have landed (status unknown)",
    )
    p.add_argument(
        "--mark-stored", action="store_true",
        help="With --chunks and one --instance: record unknown chunks as written there, once a check found "
             "them stored; sends nothing",
    )
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--status-timeout", type=int, default=900, help="Seconds to wait for one write")
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("status", help="What has been written, per instance")
    with_run(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("ask", help="Run questions through xmemcli read")
    with_run(p)
    with_cli(p)
    p.add_argument("--instance", action="append", required=True)
    p.add_argument("--questions", required=True)
    p.add_argument("--read-mode", choices=("single", "xresponse", "raw"), default="xresponse")
    p.add_argument("--concurrency", type=int, default=4)
    p.set_defaults(func=cmd_ask)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Page titles and answers are any language; a Windows pipe defaults to a legacy code page
    # that cannot encode them and would lose the whole report to one character.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except UsageError as exc:
        emit({"error": str(exc)})
        return 2
    except KeyboardInterrupt:
        emit({"error": "interrupted; rerun the same command to resume"})
        return 130
    except Exception as exc:
        traceback.print_exc()
        emit({"error": f"{exc.__class__.__name__}: {exc}", "note": "the run directory keeps everything done so far"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
