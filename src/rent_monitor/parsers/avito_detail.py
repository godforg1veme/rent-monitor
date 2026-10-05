"""Extract public text fields from an identified Avito listing page."""

from __future__ import annotations

import re
from collections import defaultdict
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

_MARKERS = {
    "item-view/item-description": "description",
    "item-view/item-params": "parameters",
    "item-view/item-date": "published_label",
    "item-view/item-id": "source_id",
    "item-view/title-info": "title",
    "seller-info/name": "seller_name",
    "seller-info/label": "seller_type_label",
    "seller-info/contact-person": "contact_person",
    "seller-info/rating": "seller_rating",
    "seller-info/reviews": "seller_reviews",
    "item-view/seller-info": "seller_info",
}
_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class _Reader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.values = defaultdict(list)
        self.links = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        field = _MARKERS.get(attrs.get("data-marker"))
        if tag == "br":
            self.handle_data("\n")
        if tag in _VOID:
            return
        active = [entry[1] for entry in self.stack if entry[1]]
        if tag == "a" and attrs.get("data-marker") == "seller-link/link":
            self.links["seller_url"] = attrs.get("href")
        if tag in {"div", "p", "li", "section"}:
            for name in active:
                self.values[name].append("\n")
        self.stack.append((tag, field, tag in {"script", "style", "noscript"}))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                for _, field, _ in self.stack[index:]:
                    if field:
                        self.values[field].append("\n")
                del self.stack[index:]
                break

    def handle_data(self, data):
        if any(entry[2] for entry in self.stack):
            return
        for _, field, _ in self.stack:
            if field:
                self.values[field].append(data)


def _clean(parts):
    text = "".join(parts).replace("\xa0", " ")
    return "\n".join(line for raw in text.splitlines() if (line := " ".join(raw.split())))


def parse_detail(html: str, source_id: str, url: str) -> dict | None:
    reader = _Reader()
    reader.feed(html)
    values = {key: _clean(parts) for key, parts in reader.values.items()}
    identity = re.search(r"\d{7,}", values.get("source_id", ""))
    if not identity or identity.group() != source_id or not values.get("description"):
        return None
    parameters = {}
    for line in values.get("parameters", "").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            if key.strip() and value.strip():
                parameters[key.strip()] = value.strip()
    values["characteristics"] = parameters
    values.pop("parameters", None)
    values["status"] = "complete"
    if reader.links.get("seller_url"):
        seller_url = urljoin(url, reader.links["seller_url"])
        parts = urlsplit(seller_url)
        if parts.scheme == "https" and parts.hostname in {"avito.ru", "www.avito.ru"}:
            values["seller_url"] = seller_url
    return values
