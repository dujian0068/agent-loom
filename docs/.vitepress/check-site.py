#!/usr/bin/env python3
"""Validate built documentation links and assets without making network requests.

Usage: python3 docs/.vitepress/check-site.py [--base /agent-loom/] [--dist DIR]
The base must match the AGENTLOOM_DOCS_BASE used for the VitePress build.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

SITE_ORIGIN = "https://documentation.invalid"
RESOURCE_ATTRIBUTES = {"a": "href", "img": "src", "script": "src", "link": "href"}


@dataclass(frozen=True)
class Reference:
    tag: str
    attribute: str
    value: str
    line: int


class Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.references: list[Reference] = []
        self.base_hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id") is not None:
            self.ids.add(attributes["id"] or "")
        if tag == "a" and attributes.get("name") is not None:
            self.ids.add(attributes["name"] or "")
        if tag == "base" and attributes.get("href") is not None:
            self.base_hrefs.append(attributes["href"] or "")
        attribute = RESOURCE_ATTRIBUTES.get(tag)
        if attribute and attributes.get(attribute) is not None:
            self.references.append(
                Reference(tag, attribute, attributes[attribute] or "", self.getpos()[0])
            )

    handle_startendtag = handle_starttag


def parse_base(value: str) -> str:
    parsed = urlsplit(value)
    if (
        not value.startswith("/")
        or not value.endswith("/")
        or value.startswith("//")
        or parsed.query
        or parsed.fragment
        or parsed.scheme
        or parsed.netloc
        or "\\" in value
        or any(segment in {".", ".."} for segment in unquote(value).split("/"))
    ):
        raise argparse.ArgumentTypeError("base must be an absolute URL path such as / or /agent-loom/")
    return value


class SiteChecker:
    def __init__(self, root: Path, base: str) -> None:
        self.root = root.resolve()
        self.base = unquote(base)
        self.documents: dict[Path, Document] = {}
        self.failures: list[str] = []
        self.internal_links = 0
        self.assets = 0
        self.anchors = 0
        self.ignored = 0

    def label(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def fail(self, path: Path, line: int, message: str) -> None:
        self.failures.append(f"{self.label(path)}:{line}: {message}")

    def document(self, path: Path) -> Document | None:
        if path in self.documents:
            return self.documents[path]
        try:
            if not path.resolve().is_relative_to(self.root):
                self.fail(path, 1, "document symlink escapes the built site directory")
                return None
            document = Document()
            document.feed(path.read_text(encoding="utf-8-sig"))
            document.close()
        except (OSError, UnicodeError, ValueError) as error:
            self.fail(path, 1, f"cannot parse document: {error}")
            return None
        self.documents[path] = document
        return document

    def target(self, pathname: str, is_link: bool) -> tuple[Path | None, str | None]:
        decoded = unquote(pathname)
        if not decoded.startswith(self.base):
            return None, f"URL is outside configured base {self.base!r}"
        relative = decoded[len(self.base) :]
        candidate = (self.root / relative).resolve()
        if not candidate.is_relative_to(self.root):
            return None, "URL escapes the built site directory"
        candidates = [candidate]
        if is_link:
            if candidate.is_dir() or decoded.endswith("/"):
                candidates = [candidate / "index.html"]
            elif not candidate.suffix:
                # Accept both VitePress clean URLs and directory-style static hosting.
                candidates += [candidate.with_suffix(".html"), candidate / "index.html"]
        for possible in candidates:
            if possible.is_file():
                if not possible.resolve().is_relative_to(self.root):
                    return None, "resource symlink escapes the built site directory"
                return possible.resolve(), None
        return None, f"missing built file for {pathname!r}"

    def check_reference(self, page: Path, reference: Reference, page_url: str) -> None:
        value = reference.value.strip()
        location = f"{reference.tag}[{reference.attribute}]={reference.value!r}"
        try:
            parsed = urlsplit(value)
            if parsed.scheme.lower() == "file":
                self.fail(page, reference.line, f"{location}: local filesystem URL cannot be deployed")
                return
            if parsed.scheme or parsed.netloc or value.startswith("//"):
                self.ignored += 1
                return
            # Backslashes have special URL semantics in browsers; do not treat them as files.
            if "\\" in value:
                self.fail(page, reference.line, f"{location}: backslash in local URL")
                return
            resolved = urlsplit(urljoin(page_url, value))
            is_link = reference.tag == "a"
            if is_link:
                self.internal_links += 1
            else:
                self.assets += 1
            target, error = self.target(resolved.path, is_link)
        except (ValueError, OSError) as error:
            self.fail(page, reference.line, f"{location}: invalid URL ({error})")
            return
        if error or target is None:
            self.fail(page, reference.line, f"{location}: {error}")
            return
        if not is_link or not resolved.fragment:
            return
        # Text fragment directives are browser text searches, not element IDs.
        fragment = unquote(resolved.fragment.split(":~:text=", 1)[0])
        if not fragment:
            return
        if target.suffix.lower() not in {".html", ".htm", ".svg"}:
            # For example, PDF #page=2 is interpreted by its viewer rather than HTML.
            return
        self.anchors += 1
        document = self.document(target)
        if document is not None and fragment not in document.ids:
            self.fail(
                page,
                reference.line,
                f"{location}: missing anchor {fragment!r} in {self.label(target)}",
            )

    def check(self) -> int:
        if not self.root.is_dir():
            print(f"Site directory does not exist: {self.root}", file=sys.stderr)
            return 1
        pages = sorted(self.root.rglob("*.html"))
        if not pages:
            print(f"No built HTML pages found: {self.root}", file=sys.stderr)
            return 1
        for page in pages:
            document = self.document(page)
            if document is None:
                continue
            if document.base_hrefs:
                self.fail(page, 1, "unexpected <base href>; rebuild without URL-base overrides")
            page_url = SITE_ORIGIN + self.base + self.label(page)
            for reference in document.references:
                self.check_reference(page, reference, page_url)
        print(
            f"Checked {len(pages)} HTML pages, {self.internal_links} internal links, "
            f"{self.anchors} anchors and {self.assets} asset references; "
            f"ignored {self.ignored} external/special URLs."
        )
        if self.failures:
            print(f"FAILED: {len(self.failures)} error(s):", file=sys.stderr)
            for failure in sorted(self.failures):
                print(f"  {failure}", file=sys.stderr)
            return 1
        print(f"PASS: all checked targets exist (base={self.base}).")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=parse_base, default="/", help="URL base used for the build")
    parser.add_argument(
        "--dist",
        type=Path,
        default=Path(__file__).resolve().parent / "dist",
        help="built site directory (default: docs/.vitepress/dist)",
    )
    arguments = parser.parse_args()
    return SiteChecker(arguments.dist, arguments.base).check()


if __name__ == "__main__":
    raise SystemExit(main())
