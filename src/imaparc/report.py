"""Summary report for a render run, and the PDF/A validation that feeds it."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from rich.progress import Progress

from imaparc.config import ToolPaths
from imaparc.pdf.validate import ValidationError, run_verapdf, run_verapdf_batch
from imaparc.pipeline import RenderResult

logger = logging.getLogger(__name__)

# PDFs validated per veraPDF process. One JVM start per batch; kept well below
# the OS argument-length limit even with long archive paths.
_VERAPDF_BATCH = 100


@dataclass
class RunReport:
    """Accumulates per-mail results and PDF/A validation findings."""

    results: list[RenderResult] = field(default_factory=list)
    non_compliant: list[str] = field(default_factory=list)

    def add(self, result: RenderResult) -> None:
        self.results.append(result)

    @property
    def written(self) -> list[RenderResult]:
        return [r for r in self.results if r.written]

    @property
    def skipped(self) -> list[RenderResult]:
        return [r for r in self.results if r.skipped]

    @property
    def incomplete(self) -> list[RenderResult]:
        """Written mails with at least one attachment shown as an info page."""
        return [r for r in self.written if not r.complete]

    @property
    def html_fallback(self) -> list[RenderResult]:
        return [r for r in self.written if not r.html_rendered]

    def summary(self) -> str:
        """Return a short human-readable summary."""
        lines = [f"{len(self.written)} written, {len(self.skipped)} skipped"]
        if self.incomplete:
            lines.append(
                f"{len(self.incomplete)} with attachments not embedded as pages "
                "(shown as info pages, originals kept)"
            )
        if self.html_fallback:
            lines.append(
                f"{len(self.html_fallback)} fell back to the text version "
                "(HTML rendering failed)"
            )
        if self.non_compliant:
            lines.append(
                f"{len(self.non_compliant)} not PDF/A-3b compliant (kept anyway):"
            )
            for path in self.non_compliant[:10]:
                lines.append(f"  - {Path(path).name}")
            if len(self.non_compliant) > 10:
                lines.append(f"  … and {len(self.non_compliant) - 10} more")
            mailonly = [p for p in self.non_compliant if p.endswith("_mailonly.pdf")]
            if mailonly:
                # The combined PDF may carry an exotic attachment that resists
                # conversion — that is accepted. A non-conformant *mail-only*
                # rendition is imapArc's own output and should never happen.
                lines.append(
                    f"  ⚠ {len(mailonly)} of these are mail-only renditions "
                    "(imapArc's own output) — this is an anomaly, please report."
                )
        return "\n".join(lines)


def validate_pdfa(report: RunReport, tools: ToolPaths, progress: Progress) -> None:
    """Validate the written PDFs with veraPDF in batches (one JVM per batch).

    Per-file validation starts a fresh JVM each time, which is unusably slow at
    scale (hundreds of mails → hundreds of JVM starts). Instead each veraPDF
    process validates a whole chunk of files. Paths are deduplicated first — an
    attachment-less mail's combined and mail-only PDFs are the same file.
    """
    seen: set[Path] = set()
    paths: list[Path] = []
    for result in report.written:
        for pdf in (result.combined_pdf, result.mail_only_pdf):
            if pdf is not None and pdf not in seen and pdf.exists():
                seen.add(pdf)
                paths.append(pdf)
    if not paths:
        return

    task = progress.add_task("Validating PDF/A", total=len(paths))
    for start in range(0, len(paths), _VERAPDF_BATCH):
        chunk = paths[start : start + _VERAPDF_BATCH]
        try:
            results = run_verapdf_batch(tools.verapdf, chunk)
        except ValidationError as exc:
            # Batch alignment failed — fall back to per-file for this chunk so a
            # single odd PDF cannot mislabel its neighbours.
            logger.warning("veraPDF batch failed (%s); validating individually", exc)
            results = [run_verapdf(tools.verapdf, pdf) for pdf in chunk]
        for pdf, res in zip(chunk, results, strict=True):
            if not res.compliant:
                report.non_compliant.append(str(pdf))
        progress.advance(task, len(chunk))
