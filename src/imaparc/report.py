"""Summary report for a render run."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from imaparc.pipeline import RenderResult


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
