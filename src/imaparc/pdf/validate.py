"""Validate PDF/A conformance with veraPDF.

Conformance is reported, never silently assumed. ``parse_verapdf_json`` is pure
and unit-tested; ``run_verapdf`` runs the process. veraPDF's JVM startup is
costly, so callers should validate in a single batch at the end of a run rather
than once per mail.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from imaparc.exceptions import ImapArcError

logger = logging.getLogger(__name__)


class ValidationError(ImapArcError):
    """veraPDF output could not be parsed."""


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of a PDF/A validation."""

    compliant: bool
    passed_rules: int = 0
    failed_rules: int = 0


def _result_from_job(job: dict[str, object]) -> ValidationResult:
    """Build a :class:`ValidationResult` from one veraPDF job entry.

    A job without a ``validationResult`` (e.g. veraPDF could not process the
    file) is treated as non-compliant rather than silently passing.
    """
    result = job.get("validationResult")
    if isinstance(result, list):
        result = result[0] if result else None
    if not isinstance(result, dict):
        return ValidationResult(compliant=False)
    details = result.get("details", {})
    return ValidationResult(
        compliant=bool(result.get("compliant", False)),
        passed_rules=int(details.get("passedRules", 0)),
        failed_rules=int(details.get("failedRules", 0)),
    )


def parse_verapdf_json(stdout: str) -> ValidationResult:
    """Parse single-file veraPDF ``--format json`` output.

    Raises:
        ValidationError: If the JSON is missing the expected structure.
    """
    try:
        job = json.loads(stdout)["report"]["jobs"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise ValidationError(f"could not parse veraPDF output: {exc}") from exc
    return _result_from_job(job)


def parse_verapdf_jobs(stdout: str) -> list[ValidationResult]:
    """Parse a batch veraPDF run into one result per job, in input order.

    Raises:
        ValidationError: If the JSON is missing the expected structure.
    """
    try:
        jobs = json.loads(stdout)["report"]["jobs"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValidationError(f"could not parse veraPDF output: {exc}") from exc
    return [_result_from_job(job) for job in jobs]


def run_verapdf(
    verapdf: Path, pdf_path: Path, *, flavour: str = "3b"
) -> ValidationResult:
    """Validate a single PDF file against the given PDF/A flavour (default 3b)."""
    result = subprocess.run(
        [str(verapdf), "--format", "json", "--flavour", flavour, str(pdf_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return parse_verapdf_json(result.stdout)


def run_verapdf_batch(
    verapdf: Path, pdf_paths: list[Path], *, flavour: str = "3b"
) -> list[ValidationResult]:
    """Validate many PDFs in a single veraPDF process (one JVM start).

    veraPDF preserves argument order in its ``jobs`` array, so results align to
    ``pdf_paths`` positionally. Returns one result per input path.

    Raises:
        ValidationError: If the output cannot be parsed, or the number of jobs
            does not match the number of inputs (alignment would be unsafe).
    """
    if not pdf_paths:
        return []
    proc = subprocess.run(
        [
            str(verapdf),
            "--format",
            "json",
            "--flavour",
            flavour,
            *(str(p) for p in pdf_paths),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    results = parse_verapdf_jobs(proc.stdout)
    if len(results) != len(pdf_paths):
        raise ValidationError(
            f"veraPDF returned {len(results)} results for {len(pdf_paths)} inputs"
        )
    return results
