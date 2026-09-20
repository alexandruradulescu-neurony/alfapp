"""Keep embedded evidence-report photos out of the editor's HTTP round trip.

Evidence reports store photos as base64 data URIs inline in
`DisputeDocument.content_html` (`<img src="data:image/...;base64,...">`).
The in-place WYSIWYG editor used to inline every photo straight into the
browser (an iframe `srcdoc`) and post the WHOLE document back on every save
-- photos included. A report with a handful of embedded photos meant
2.5-10MB POST bodies, 84-97% of it image bytes that were never actually
edited, and production started rejecting them outright (HTTP 400
RequestDataTooBig, 2026-09-20).

These helpers let the editor talk about a photo by its POSITION ("photo 0",
"photo 1", ...) instead of by its content, so the bytes never have to travel
through the browser a second time:

- `externalize_images` (used on GET): replace each inline data URI with a
  tiny per-index URL, so the HTML handed to the browser's iframe has no
  image bytes in it at all.
- `extract_image_data_uris`: pull the ordered list of data URIs back out of
  a stored `content_html` value. This is what the per-index image view
  serves from, and what `reinline_images` re-inlines from.
- `reinline_images` (used on POST): the browser posts back `<img src="...">`
  tags referencing those per-index URLs (for photos the manager kept), plus
  whatever text was edited. Swap each referenced URL back for the real data
  URI -- read from the CURRENTLY STORED content_html, never from the
  request -- before the HTML is persisted or handed to the PDF renderer
  (which cannot fetch a relative URL).
- `parse_data_uri`: decode a single `data:<mime>;base64,<payload>` value.

Regex-based on purpose, not a full HTML parser -- this mirrors
`strip_active_html` in apps/payments/frontend_views.py, which takes the same
approach for the same reason: these are small, self-generated HTML
documents (report-service output, then manager edits), not arbitrary
third-party markup.
"""

import base64
import re
from typing import Callable, List, Optional, Tuple

# A single <img ...> tag. Non-greedy up to the first '>' -- content_html here
# is self-generated (report service + manager edits), never arbitrary markup
# with a literal '>' hiding inside an attribute value.
_IMG_TAG_RE = re.compile(r'<img\b[^>]*>', re.IGNORECASE)

# A src="..." or src='...' attribute anywhere inside a tag (order-independent).
_SRC_ATTR_RE = re.compile(r'''\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)')''', re.IGNORECASE)

# The exact shape document_service embeds a photo as.
_DATA_IMAGE_RE = re.compile(r'^data:image/[^;,\s]+;base64,[A-Za-z0-9+/=]*$', re.IGNORECASE)


def _src_value(img_tag: str) -> Optional[str]:
    """The `src` attribute's value from a single `<img ...>` tag, or None."""
    m = _SRC_ATTR_RE.search(img_tag)
    if not m:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)


def _set_src_value(img_tag: str, new_value: str) -> str:
    """`img_tag` with its `src` attribute's value replaced, preserving
    whichever quote style the tag already used."""
    def _replace(match):
        quote = '"' if match.group(1) is not None else "'"
        return f'src={quote}{new_value}{quote}'
    return _SRC_ATTR_RE.sub(_replace, img_tag, count=1)


def extract_image_data_uris(html: str) -> List[str]:
    """The `src` values of every `<img>` whose src is a
    `data:image/...;base64,...` URI, in document order."""
    out = []
    for tag in _IMG_TAG_RE.findall(html or ''):
        src = _src_value(tag)
        if src and _DATA_IMAGE_RE.match(src.strip()):
            out.append(src)
    return out


def externalize_images(html: str, url_for_index: Callable[[int], str]) -> str:
    """Replace each inline `data:image` src with `url_for_index(i)`, where
    `i` is the 0-based position among the data-image `<img>` tags. Every
    other byte of `html` -- other attributes, other tags, surrounding text
    -- is left exactly as it was."""
    counter = {'next': 0}

    def _replace_tag(match):
        tag = match.group(0)
        src = _src_value(tag)
        if not src or not _DATA_IMAGE_RE.match(src.strip()):
            return tag
        index = counter['next']
        counter['next'] += 1
        return _set_src_value(tag, url_for_index(index))

    return _IMG_TAG_RE.sub(_replace_tag, html or '')


def reinline_images(posted_html: str, stored_html: str,
                     index_from_url: Callable[[str], Optional[int]]) -> str:
    """Replace each `<img src="...">` in `posted_html` whose src resolves
    (via `index_from_url`) to a photo index with the actual `data:image` URI
    at that position in `stored_html` (the row as currently saved -- never
    trust image bytes from the request, there aren't any to trust there).

    `index_from_url` takes a raw src value and returns the 0-based index for
    a recognised per-document placeholder URL (relative, or with an
    absolute `https://host` prefix), or None for anything else. A `data:`
    URI is always left alone unconditionally (without even being offered to
    `index_from_url`) so an already-inline image -- e.g. freshly pasted,
    never externalised -- survives untouched. An index with nothing at that
    position in `stored_html` (out of range) is likewise left as-is.
    """
    stored_data_uris = extract_image_data_uris(stored_html)

    def _replace_tag(match):
        tag = match.group(0)
        src = _src_value(tag)
        if not src or src.strip().lower().startswith('data:'):
            return tag
        index = index_from_url(src.strip())
        if index is None or index < 0 or index >= len(stored_data_uris):
            return tag
        return _set_src_value(tag, stored_data_uris[index])

    return _IMG_TAG_RE.sub(_replace_tag, posted_html or '')


def parse_data_uri(uri: str) -> Tuple[str, bytes]:
    """Decode a `data:<mime>;base64,<payload>` URI into `(mime, bytes)`.
    Raises ValueError for anything that isn't exactly that shape, or whose
    payload isn't valid base64."""
    if not isinstance(uri, str) or not uri.startswith('data:'):
        raise ValueError('not a data URI')
    header, comma, payload = uri[len('data:'):].partition(',')
    if not comma:
        raise ValueError('data URI has no comma separator')
    if not header.lower().endswith(';base64'):
        raise ValueError('data URI is not base64-encoded')
    mime = header[:-len(';base64')]
    if not mime:
        raise ValueError('data URI has no mime type')
    try:
        data = base64.b64decode(payload, validate=True)
    except Exception as e:
        raise ValueError(f'invalid base64 payload: {e}')
    return mime, data
