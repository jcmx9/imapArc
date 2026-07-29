"""Normalise a PDF attachment via qpdf before appending it 1:1.

qpdf decrypts empty-password PDFs and repairs minor damage. It reads and writes
files (its stdin/stdout streaming does not round-trip reliably here), so a
throwaway temp directory holds the in/out pair. Encrypted (real password) and
corrupt inputs are told apart from qpdf's message so the caller can report the
right reason.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from imaparc.exceptions import ImapArcError

# qpdf exit codes: 0 success, 3 success-with-warnings, 2 errors.
_OK_RETURNCODES = (0, 3)


class PdfEncryptedError(ImapArcError):
    """The PDF is encrypted with a non-empty password."""


class PdfCorruptError(ImapArcError):
    """The PDF is too damaged for qpdf to recover."""


def normalize_pdf(data: bytes, *, qpdf: Path, work_dir: Path) -> bytes:
    """Return a decrypted, normalised copy of the PDF bytes.

    Args:
        data: The raw PDF attachment.
        qpdf: Resolved qpdf executable.
        work_dir: A writable scratch directory; a temp subdir is created in it.

    Raises:
        PdfEncryptedError: The PDF needs a password we do not have.
        PdfCorruptError: qpdf could not recover the file.
    """
    with tempfile.TemporaryDirectory(dir=work_dir) as td:
        src = Path(td) / "in.pdf"
        dst = Path(td) / "out.pdf"
        src.write_bytes(data)
        result = subprocess.run(
            [str(qpdf), "--decrypt", "--object-streams=preserve", str(src), str(dst)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode in _OK_RETURNCODES and dst.exists():
            return dst.read_bytes()
        message = result.stderr.strip()
        if "password" in message.lower() or "encrypt" in message.lower():
            raise PdfEncryptedError(message)
        raise PdfCorruptError(message)
