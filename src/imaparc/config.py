"""Runtime configuration model and external-tool resolution."""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from imaparc import naming
from imaparc.exceptions import ToolNotFoundError

# Names of the external tools resolved on PATH unless explicitly overridden.
REQUIRED_TOOLS: tuple[str, ...] = ("gs", "qpdf", "verapdf")

# Candidate sRGB ICC profiles, tried in this order. One of them is embedded as
# the PDF/A output intent; any sRGB profile serves, and systems ship theirs in
# different places.
ICC_CANDIDATES: tuple[Path, ...] = (
    # macOS
    Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc"),
    # Debian/Ubuntu: icc-profiles-free
    Path("/usr/share/color/icc/sRGB.icc"),
    # colord (most Linux desktops)
    Path("/usr/share/color/icc/colord/sRGB.icc"),
    # shipped with Ghostscript
    Path("/usr/share/color/icc/ghostscript/srgb.icc"),
)


def default_icc_profile() -> Path:
    """The first sRGB ICC profile present on this system.

    Falls back to the first candidate when none exists, so the failure surfaces
    downstream as a ``PdfaConversionError`` naming a concrete path.
    """
    for candidate in ICC_CANDIDATES:
        if candidate.exists():
            return candidate
    return ICC_CANDIDATES[0]


class ToolPaths(BaseModel):
    """Resolved absolute paths to the external command-line tools."""

    gs: Path
    qpdf: Path
    verapdf: Path

    @classmethod
    def resolve(cls, overrides: dict[str, str] | None = None) -> ToolPaths:
        """Locate the tools on PATH, honouring per-tool overrides.

        Raises:
            ToolNotFoundError: If a required tool cannot be found.
        """
        overrides = overrides or {}
        found: dict[str, Path] = {}
        missing: list[str] = []
        for tool in REQUIRED_TOOLS:
            override = overrides.get(tool)
            resolved = override or shutil.which(tool)
            if resolved is None:
                missing.append(tool)
                continue
            found[tool] = Path(resolved)
        if missing:
            raise ToolNotFoundError(
                "Missing required tool(s): "
                + ", ".join(missing)
                + ". Install them or pass an explicit path."
            )
        return cls(**found)


class RunConfig(BaseModel):
    """Everything one convert run needs to know."""

    tools: ToolPaths
    icc_profile: Path = Field(default_factory=default_icc_profile)
    verbosity: int = 1
    jobs: int = Field(default=4, ge=1)
    gs_jobs: int = Field(default=2, ge=1)
    validate_pdfa: bool = True
    overwrite: bool = False
    # Remote images: default off (stripped); opt-in fetches them (see renderer).
    allow_remote: bool = False
    # Per-attachment safety limits.
    max_attachment_bytes: int = Field(default=400 * 1024 * 1024, ge=0)
    attachment_timeout_s: float = Field(default=120.0, gt=0)
    render_timeout_ms: int = Field(default=30_000, gt=0)
    # Filename / subfolder templates.
    filename_pattern: str = naming.DEFAULT_PATTERN
    date_format: str = naming.DEFAULT_DATE_FORMAT
