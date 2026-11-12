"""Record in the PDF itself which mail it was rendered from.

The PDF is the whole record of a rendered mail — there is no manifest or folder
beside it (until 26.11.5 there was) — so the finished PDF/A-3b carries its
provenance inside, where copying, mailing or renaming cannot separate them:

- **XMP metadata** — the mail's SHA-256 and its Message-ID, under imapArc's own
  namespace. PDF/A allows custom properties only when the file declares their
  schema, so the ``pdfaExtension`` description is written alongside; without it
  veraPDF fails.
- **The .eml itself** (optional), as a PDF/A-3 *associated file* with
  ``/AFRelationship /Source`` — the PDF then holds its own original.
- **Attachments that did not become pages** (optional), as associated files
  with ``/AFRelationship /Data``, so a Word file or an archive opens from the
  PDF reader's attachment panel without first extracting the ``.eml``.
- **Bookmarks** (optional) to the start of the mail and to each separator page.

The PDF is rewritten by pikepdf after Ghostscript, with existing streams left
as they are: PDF/A forbids a filter on the metadata stream, and a blanket
``compress_streams`` would add one.
"""

from __future__ import annotations

import hashlib
import io
import re
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pikepdf
from lxml import etree
from pikepdf import Array, Dictionary, Name, String

NAMESPACE = "https://github.com/jcmx9/imapArc/ns/provenance/1.0/"
PREFIX = "imaparc"
_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_DC = "http://purl.org/dc/elements/1.1/"
_EML_MIME = "message/rfc822"
# Everything XML 1.0 forbids in character data.
_XML_INVALID = re.compile(
    "[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)
_MIME = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*")

_PROPERTIES = (
    ("sourceSha256", "SHA-256 of the raw e-mail (.eml) this PDF was rendered from"),
    ("messageId", "Message-ID header of that e-mail"),
)

_EXTENSION_SCHEMA = f"""<rdf:Description xmlns:rdf="{_RDF}" rdf:about=""
    xmlns:pdfaExtension="http://www.aiim.org/pdfa/ns/extension/"
    xmlns:pdfaSchema="http://www.aiim.org/pdfa/ns/schema#"
    xmlns:pdfaProperty="http://www.aiim.org/pdfa/ns/property#">
<pdfaExtension:schemas><rdf:Bag><rdf:li rdf:parseType="Resource">
<pdfaSchema:schema>imapArc mail provenance</pdfaSchema:schema>
<pdfaSchema:namespaceURI>{NAMESPACE}</pdfaSchema:namespaceURI>
<pdfaSchema:prefix>{PREFIX}</pdfaSchema:prefix>
<pdfaSchema:property><rdf:Seq>{
    "".join(
        '<rdf:li rdf:parseType="Resource">'
        f"<pdfaProperty:name>{name}</pdfaProperty:name>"
        "<pdfaProperty:valueType>Text</pdfaProperty:valueType>"
        "<pdfaProperty:category>external</pdfaProperty:category>"
        f"<pdfaProperty:description>{description}</pdfaProperty:description>"
        "</rdf:li>"
        for name, description in _PROPERTIES
    )
}</rdf:Seq></pdfaSchema:property>
</rdf:li></rdf:Bag></pdfaExtension:schemas>
</rdf:Description>"""


_EMPTY_XMP = (
    f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="{_RDF}"/></x:xmpmeta>'
)


@dataclass(frozen=True)
class Provenance:
    """What a PDF records about its source mail."""

    sha256: str
    message_id: str | None = None


def stamp(
    pdf_bytes: bytes,
    raw: bytes,
    *,
    message_id: str | None,
    eml_name: str | None = None,
    mod_date: datetime | None = None,
    attachments: Sequence[tuple[str, str, bytes]] = (),
    bookmarks: Sequence[tuple[str, int]] = (),
    title: str | None = None,
    author: str | None = None,
) -> bytes:
    """Return ``pdf_bytes`` with the provenance of ``raw`` written into it.

    Args:
        pdf_bytes: A finished PDF/A-3b.
        raw: The mail's raw RFC-822 bytes.
        message_id: The mail's Message-ID, if it has one.
        eml_name: When given, ``raw`` is also embedded under this file name as
            the PDF's source; ``None`` records the hash only.
        mod_date: Modification date stored with the embedded files.
        attachments: ``(file name, MIME type, content)`` of further files to
            embed — the attachments that could not be shown as pages.
        bookmarks: ``(title, zero-based page index)`` outline entries.
        title: The document title (the mail's subject) — what a file manager
            or search shows instead of the file name.
        author: The document author (the mail's sender).
    """
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        _write_xmp(
            pdf,
            hashlib.sha256(raw).hexdigest(),
            message_id,
            title=_one_line(title),
            author=_one_line(author),
        )
        files: list[tuple[str, str, bytes, Name]] = []
        if eml_name is not None:
            files.append((eml_name, _EML_MIME, raw, Name.Source))
        files += [(name, mime, data, Name.Data) for name, mime, data in attachments]
        if files:
            _embed(pdf, files, mod_date)
        if bookmarks:
            with pdf.open_outline() as outline:
                outline.root.extend(
                    pikepdf.OutlineItem(title, page) for title, page in bookmarks
                )
        out = io.BytesIO()
        pdf.save(
            out,
            compress_streams=False,
            object_stream_mode=pikepdf.ObjectStreamMode.preserve,
        )
    return out.getvalue()


def read(source: bytes | Path) -> Provenance | None:
    """Read the provenance a PDF records, or ``None`` if it records none.

    ``source`` may be a path: pikepdf then reads only what it needs, which
    matters when a whole archive is indexed. **Never raises** on a PDF it cannot
    make sense of — password-protected, truncated, a ``/Metadata`` that is no
    stream or no XML: a profile directory may hold foreign files, and one of
    them must not abort indexing the rest. Such a file records nothing.
    """
    try:
        opened = io.BytesIO(source) if isinstance(source, bytes) else source
        with pikepdf.open(opened) as pdf:
            if "/Metadata" not in pdf.Root:
                return None
            root = etree.fromstring(pdf.Root.Metadata.read_bytes())
    except (
        pikepdf.PdfError,
        pikepdf.PasswordError,
        etree.XMLSyntaxError,
        OSError,
        TypeError,
        ValueError,
    ):
        return None
    sha = root.find(f".//{{{NAMESPACE}}}sourceSha256")
    if sha is None or not sha.text:
        return None
    mid = root.find(f".//{{{NAMESPACE}}}messageId")
    return Provenance(sha.text, mid.text if mid is not None else None)


def identity(source: bytes | Path) -> str | None:
    """The mail identity a PDF records, in the manifest's line format.

    ``<Message-ID>`` (when the mail has one) and ``sha256:<hash>``, one per line
    — so it compares with :func:`imaparc.pipeline.same_mail` like a manifest.
    """
    found = read(source)
    if found is None:
        return None
    lines = [found.message_id] if found.message_id else []
    return "\n".join([*lines, f"sha256:{found.sha256}"])


def embedded_files(source: bytes | Path) -> dict[str, tuple[str, bytes]]:
    """Every associated file: name → (``/AFRelationship`` without slash, bytes)."""
    found: dict[str, tuple[str, bytes]] = {}
    opened = io.BytesIO(source) if isinstance(source, bytes) else source
    with pikepdf.open(opened) as pdf:
        for spec in pdf.Root.get("/AF", Array()):
            relation = str(spec.get("/AFRelationship", Name.Unspecified))[1:]
            found[str(spec.UF)] = (relation, bytes(spec.EF.F.read_bytes()))
    return found


def embedded_source(source: bytes | Path) -> tuple[str, bytes] | None:
    """The ``.eml`` embedded as the PDF's source — ``(file name, bytes)`` — if any.

    Reads only that one file, not the attachments embedded beside it.
    """
    opened = io.BytesIO(source) if isinstance(source, bytes) else source
    with pikepdf.open(opened) as pdf:
        for spec in pdf.Root.get("/AF", Array()):
            if spec.get("/AFRelationship") == Name.Source:
                return str(spec.UF), bytes(spec.EF.F.read_bytes())
    return None


def _one_line(text: str | None) -> str:
    """``text`` as one line of valid XML character data (may be empty)."""
    return " ".join(_XML_INVALID.sub(" ", text or "").split())


def _write_xmp(
    pdf: pikepdf.Pdf,
    sha256: str,
    message_id: str | None,
    *,
    title: str = "",
    author: str = "",
) -> None:
    if "/Metadata" in pdf.Root:
        root = etree.fromstring(pdf.Root.Metadata.read_bytes())
    else:
        root = etree.fromstring(_EMPTY_XMP)
    rdf = root.find(f"{{{_RDF}}}RDF")
    if rdf is None:
        rdf = etree.SubElement(root, f"{{{_RDF}}}RDF", nsmap={"rdf": _RDF})
    rdf.append(etree.fromstring(_EXTENSION_SCHEMA))
    props = etree.SubElement(
        rdf, f"{{{_RDF}}}Description", nsmap={"rdf": _RDF, PREFIX: NAMESPACE}
    )
    props.set(f"{{{_RDF}}}about", "")
    etree.SubElement(props, f"{{{NAMESPACE}}}sourceSha256").text = sha256
    if message_id and (cleaned := _XML_INVALID.sub("", message_id)):
        # The sender picks the Message-ID; a control character in it is not
        # valid XML and would otherwise make the mail impossible to render.
        etree.SubElement(props, f"{{{NAMESPACE}}}messageId").text = cleaned
    # Title and author go into the XMP *and* the document info dictionary, with
    # the same value: PDF/A wants the two to agree where both are present.
    if title:
        _set_dc(rdf, "title", "Alt", title)
        pdf.docinfo["/Title"] = title
    if author:
        _set_dc(rdf, "creator", "Seq", author)
        pdf.docinfo["/Author"] = author
    packet = (
        b"<?xpacket begin='\xef\xbb\xbf' id='W5M0MpCehiHzreSzNTczkc9d'?>\n"
        + etree.tostring(root, encoding="utf-8")
        + b"\n<?xpacket end='w'?>"
    )
    pdf.Root.Metadata = pikepdf.Stream(
        pdf, packet, Type=Name.Metadata, Subtype=Name.XML
    )


def _set_dc(rdf: etree._Element, name: str, container: str, value: str) -> None:
    """Set the Dublin Core property ``dc:<name>`` to the single ``value``.

    Replaces what Ghostscript wrote (it titles every file "imapArc archive"),
    as an element *or* as an attribute on a description.
    """
    for description in rdf.findall(f"{{{_RDF}}}Description"):
        if f"{{{_DC}}}{name}" in description.attrib:
            del description.attrib[f"{{{_DC}}}{name}"]
        for old in description.findall(f"{{{_DC}}}{name}"):
            description.remove(old)
    description = etree.SubElement(
        rdf, f"{{{_RDF}}}Description", nsmap={"rdf": _RDF, "dc": _DC}
    )
    description.set(f"{{{_RDF}}}about", "")
    item = etree.SubElement(
        etree.SubElement(
            etree.SubElement(description, f"{{{_DC}}}{name}"),
            f"{{{_RDF}}}{container}",
        ),
        f"{{{_RDF}}}li",
    )
    if container == "Alt":
        item.set("{http://www.w3.org/XML/1998/namespace}lang", "x-default")
    item.text = value


def _embed(
    pdf: pikepdf.Pdf,
    files: list[tuple[str, str, bytes, Name]],
    mod_date: datetime | None,
) -> None:
    """Embed ``files`` as PDF/A-3 associated files, listed in the catalog.

    Names are made unique (two attachments may share one), and the name tree
    is sorted by its stored key bytes, as the PDF specification requires.
    """
    specs: list[tuple[str, pikepdf.Object]] = []
    taken: set[str] = set()
    for name, mime, data, relation in files:
        unique = _unique(name, taken)
        stream = pikepdf.Stream(pdf, zlib.compress(data))
        stream.Filter = Name.FlateDecode
        stream.Type = Name.EmbeddedFile
        stream.Subtype = Name("/" + _mime(mime))
        params = Dictionary(Size=len(data), CheckSum=String(hashlib.md5(data).digest()))
        if mod_date is not None:
            params.ModDate = String(_pdf_date(mod_date))
        stream.Params = params
        description = (
            "Original e-mail this PDF was rendered from"
            if relation == Name.Source
            else "Attachment of the e-mail, kept as original"
        )
        specs.append(
            (
                unique,
                pdf.make_indirect(
                    Dictionary(
                        Type=Name.Filespec,
                        F=String(unique),
                        UF=String(unique),
                        Desc=String(description),
                        AFRelationship=relation,
                        EF=Dictionary(F=stream, UF=stream),
                    )
                ),
            )
        )
    pdf.Root.AF = Array([spec for _, spec in specs])
    names = Array()
    # Ordered by the key bytes as stored (pikepdf writes PDFDocEncoding where it
    # can, UTF-16 with a BOM otherwise) — the order a reader's lookup relies on.
    for unique, spec in sorted(specs, key=lambda item: bytes(String(item[0]))):
        names.extend([String(unique), spec])
    pdf.Root.Names = Dictionary(EmbeddedFiles=Dictionary(Names=names))


def _unique(name: str, taken: set[str]) -> str:
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    candidate, counter = name, 1
    while candidate in taken:
        counter += 1
        candidate = f"{stem}-{counter}.{suffix}" if suffix else f"{stem}-{counter}"
    taken.add(candidate)
    return candidate


def _mime(mime: str) -> str:
    """A MIME type usable as a PDF name; anything odd becomes octet-stream."""
    return mime.lower() if _MIME.fullmatch(mime or "") else "application/octet-stream"


def _pdf_date(dt: datetime) -> str:
    """Format ``dt`` as a PDF date string (``D:YYYYMMDDHHmmSS+HH'mm'``)."""
    stamp_ = dt.strftime("D:%Y%m%d%H%M%S")
    offset = dt.utcoffset()
    if offset is None:
        return stamp_
    minutes = int(offset.total_seconds()) // 60
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"{stamp_}{sign}{hours:02d}'{mins:02d}'"
