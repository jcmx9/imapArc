"""Convert a PDF to PDF/A-3b via Ghostscript.

The argument set and the two non-obvious findings here were verified by a spike
(``spike/spike_pdfa.py``): ``--permit-file-read`` must precede ``-dSAFER`` or gs
10.x refuses the ICC profile, and the ``PDFA_def.ps`` is generated at runtime so
the packaged Homebrew copy (which points at a placeholder) is never touched.

``build_gs_args`` is pure and unit-tested; ``to_pdfa3b`` runs the process.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from imaparc.exceptions import PdfaConversionError

logger = logging.getLogger(__name__)

# Written into the work directory at runtime; ``{icc}`` is the ICC path.
_PDFA_DEF_TEMPLATE = """%!
% PDF/A definition file, generated at runtime by imapArc.
[ /Title (imapArc archive) /DOCINFO pdfmark

[ /_objdef {{icc_PDFA}} /type /stream /OBJ pdfmark
[ {{icc_PDFA}} << /N 3 >> /PUT pdfmark
[ {{icc_PDFA}} ({icc}) (r) file /PUT pdfmark

[ /_objdef {{OutputIntent_PDFA}} /type /dict /OBJ pdfmark
[ {{OutputIntent_PDFA}} <<
    /Type /OutputIntent
    /S /GTS_PDFA1
    /DestOutputProfile {{icc_PDFA}}
    /OutputConditionIdentifier (sRGB IEC61966-2.1)
    /Info (sRGB IEC61966-2.1)
>> /PUT pdfmark
[ {{Catalog}} << /OutputIntents [ {{OutputIntent_PDFA}} ] >> /PUT pdfmark
"""


def build_gs_args(
    gs: Path, src: Path, dst: Path, pdfa_def: Path, icc: Path
) -> list[str]:
    """Build the Ghostscript argv for a PDF/A-3b conversion.

    ``--permit-file-read`` for the ICC profile must come first, before
    ``-dSAFER``; ``-dAutoRotatePages=/None`` is essential or gs rotates
    landscape image pages and destroys the orientation set upstream.
    """
    return [
        str(gs),
        f"--permit-file-read={icc}",
        "-dPDFA=3",
        "-dBATCH",
        "-dNOPAUSE",
        "-dSAFER",
        "-dPDFACompatibilityPolicy=1",
        "-sColorConversionStrategy=RGB",
        "-dProcessColorModel=/DeviceRGB",
        "-dAutoRotatePages=/None",
        "-sDEVICE=pdfwrite",
        f"-sOutputFile={dst}",
        str(pdfa_def),
        str(src),
    ]


def write_pdfa_def(work_dir: Path, icc: Path) -> Path:
    """Write the runtime PDFA_def.ps referencing ``icc``; return its path."""
    pdfa_def = work_dir / "PDFA_def.ps"
    pdfa_def.write_text(_PDFA_DEF_TEMPLATE.format(icc=icc), encoding="utf-8")
    return pdfa_def


def to_pdfa3b(src: Path, dst: Path, *, gs: Path, icc: Path, work_dir: Path) -> None:
    """Convert ``src`` to PDF/A-3b at ``dst`` using Ghostscript.

    Args:
        src: Source PDF.
        dst: Destination path for the PDF/A-3b output.
        gs: Resolved Ghostscript executable.
        icc: sRGB ICC profile to embed as the output intent.
        work_dir: A writable scratch directory (the generated PDFA_def.ps and a
            local ICC copy live here, so ``-dSAFER`` can read them).

    Raises:
        PdfaConversionError: If the ICC profile is missing or gs fails.
    """
    if not icc.exists():
        raise PdfaConversionError(f"ICC profile not found: {icc}")
    # Copy the ICC next to the inputs so -dSAFER can read it even without the
    # permit flag resolving system paths; the permit flag names this copy.
    icc_local = (work_dir / "srgb.icc").resolve()
    shutil.copyfile(icc, icc_local)
    pdfa_def = write_pdfa_def(work_dir, icc_local)
    args = build_gs_args(gs, src, dst, pdfa_def, icc_local)
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise PdfaConversionError(
            f"Ghostscript failed ({result.returncode}): {result.stderr.strip()}"
        )
    if not dst.exists():
        raise PdfaConversionError("Ghostscript produced no output file")
