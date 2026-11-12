"""Record in the PDF itself which mail it was rendered from.

The ``.imaparc-manifest`` beside a PDF names its mail, but only while the two
stay together. A PDF copied out of its folder, mailed on or renamed loses that
link. So the finished PDF/A-3b carries its provenance inside:

- **XMP metadata** — the mail's SHA-256 (the same value as the manifest's
  ``sha256:`` line) and its Message-ID, under imapArc's own namespace. PDF/A
  allows custom properties only when the file declares their schema, so the
  ``pdfaExtension`` description is written alongside; without it veraPDF fails.
- **The .eml itself** (optional), as a PDF/A-3 *associated file* with
  ``/AFRelationship /Source`` — the PDF then holds its own original.

The PDF is rewritten by pikepdf after Ghostscript, with existing streams left
as they are: PDF/A forbids a filter on the metadata stream, and a blanket
``compress_streams`` would add one.
"""

from __future__ import annotations

import hashlib
import io
import zlib
from dataclasses import dataclass
from datetime import datetime

import pikepdf
from lxml import etree
from pikepdf import Array, Dictionary, Name, String

NAMESPACE = "https://github.com/jcmx9/imapArc/ns/provenance/1.0/"
PREFIX = "imaparc"
_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

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
) -> bytes:
    """Return ``pdf_bytes`` with the provenance of ``raw`` written into it.

    Args:
        pdf_bytes: A finished PDF/A-3b.
        raw: The mail's raw RFC-822 bytes.
        message_id: The mail's Message-ID, if it has one.
        eml_name: When given, ``raw`` is also embedded under this file name as
            the PDF's source; ``None`` records the hash only.
        mod_date: Modification date stored with the embedded file.
    """
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        _write_xmp(pdf, hashlib.sha256(raw).hexdigest(), message_id)
        if eml_name is not None:
            _embed_source(pdf, raw, eml_name, mod_date)
        out = io.BytesIO()
        pdf.save(
            out,
            compress_streams=False,
            object_stream_mode=pikepdf.ObjectStreamMode.preserve,
        )
    return out.getvalue()


def read(pdf_bytes: bytes) -> Provenance | None:
    """Read the provenance a PDF records, or ``None`` if it records none."""
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        if "/Metadata" not in pdf.Root:
            return None
        root = etree.fromstring(pdf.Root.Metadata.read_bytes())
    sha = root.find(f".//{{{NAMESPACE}}}sourceSha256")
    if sha is None or not sha.text:
        return None
    mid = root.find(f".//{{{NAMESPACE}}}messageId")
    return Provenance(sha.text, mid.text if mid is not None else None)


def embedded_source(pdf_bytes: bytes) -> bytes | None:
    """Return the .eml embedded as the PDF's source, if any."""
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        for spec in pdf.Root.get("/AF", Array()):
            if spec.get("/AFRelationship") == Name.Source:
                return bytes(spec.EF.F.read_bytes())
    return None


def _write_xmp(pdf: pikepdf.Pdf, sha256: str, message_id: str | None) -> None:
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
    if message_id:
        etree.SubElement(props, f"{{{NAMESPACE}}}messageId").text = message_id
    packet = (
        b"<?xpacket begin='\xef\xbb\xbf' id='W5M0MpCehiHzreSzNTczkc9d'?>\n"
        + etree.tostring(root, encoding="utf-8")
        + b"\n<?xpacket end='w'?>"
    )
    pdf.Root.Metadata = pikepdf.Stream(
        pdf, packet, Type=Name.Metadata, Subtype=Name.XML
    )


def _embed_source(
    pdf: pikepdf.Pdf, raw: bytes, name: str, mod_date: datetime | None
) -> None:
    stream = pikepdf.Stream(pdf, zlib.compress(raw))
    stream.Filter = Name.FlateDecode
    stream.Type = Name.EmbeddedFile
    stream.Subtype = Name("/message/rfc822")
    params = Dictionary(Size=len(raw), CheckSum=String(hashlib.md5(raw).digest()))
    if mod_date is not None:
        params.ModDate = String(_pdf_date(mod_date))
    stream.Params = params
    spec = pdf.make_indirect(
        Dictionary(
            Type=Name.Filespec,
            F=String(name),
            UF=String(name),
            Desc=String("Original e-mail this PDF was rendered from"),
            AFRelationship=Name.Source,
            EF=Dictionary(F=stream, UF=stream),
        )
    )
    pdf.Root.AF = Array([spec])
    pdf.Root.Names = Dictionary(
        EmbeddedFiles=Dictionary(Names=Array([String(name), spec]))
    )


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
