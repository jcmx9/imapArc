"""Tests for the `imaparc render` command (profile-driven)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from imaparc.cli import app
from tests.mail_builder import build_mail

runner = CliRunner()


@pytest.fixture(autouse=True)
def _restore_perms(tmp_path: Path) -> Iterator[None]:
    yield
    for entry in sorted(tmp_path.rglob("*"), reverse=True):
        with contextlib.suppress(OSError):
            entry.chmod(0o700)


def _profiles_yaml(tmp_path: Path, output: Path, *, name: str, pdf: bool) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(
        "profiles:\n"
        f"  - name: {name}\n"
        "    account: test\n"
        f"    output: {output}\n"
        f"    pdf: {'true' if pdf else 'false'}\n",
        encoding="utf-8",
    )
    return path


def _eml_under(
    output: Path, *mails: bytes, legacy: bool = False, group: str = "eml-input"
) -> None:
    """Mail as fetch leaves it: in eml-input/<output name>/ (or legacy eml/)."""
    eml = output / "eml" if legacy else output.parent / group / output.name
    eml.mkdir(parents=True, exist_ok=True)
    for i, raw in enumerate(mails):
        (eml / f"2026-03-23_00-00-{i:02d}_p_m{i}.eml").write_bytes(raw)


def test_render_missing_tools_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("imaparc.config.shutil.which", lambda name: None)
    output = tmp_path / "vault"
    _eml_under(output)
    profiles = _profiles_yaml(tmp_path, output, name="p", pdf=True)
    result = runner.invoke(app, ["render", "--profiles", str(profiles)])
    assert result.exit_code == 1
    assert "Error" in result.output


@pytest.mark.requires_tools  # ToolPaths.resolve() runs before the browser starts
def test_render_missing_chromium_exits_1_with_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "no-browsers"))
    output = tmp_path / "vault"
    _eml_under(output, build_mail())
    profiles = _profiles_yaml(tmp_path, output, name="p", pdf=True)

    result = runner.invoke(app, ["render", "--profiles", str(profiles)])

    assert result.exit_code == 1
    assert "playwright install chromium" in result.output


def test_render_nothing_when_no_profiles(tmp_path: Path) -> None:
    profiles = tmp_path / "profile.yaml"
    profiles.write_text("profiles: []\n", encoding="utf-8")
    result = runner.invoke(app, ["render", "--profiles", str(profiles)])
    assert result.exit_code == 0
    assert "Nothing to render" in result.output


def test_render_unknown_profile_exits_1(tmp_path: Path) -> None:
    output = tmp_path / "vault"
    profiles = _profiles_yaml(tmp_path, output, name="p", pdf=True)
    result = runner.invoke(
        app, ["render", "--profiles", str(profiles), "--profile", "nope"]
    )
    assert result.exit_code == 1
    assert "no profile named" in result.output


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
def test_render_command_end_to_end(tmp_path: Path) -> None:
    output = tmp_path / "vault"
    _eml_under(
        output,
        build_mail(subject="One", html="<p>first</p>", text="first"),
        build_mail(subject="Two", html="<p>second</p>", text="second"),
    )
    profiles = _profiles_yaml(tmp_path, output, name="acme", pdf=True)

    result = runner.invoke(app, ["render", "--profiles", str(profiles)])
    assert result.exit_code == 0, result.output
    # Each mail is one PDF directly in the output directory.
    pdfs = sorted(output.glob("*.pdf"))
    assert len(pdfs) == 2
    assert all("_acme_" in p.name for p in pdfs)
    assert sorted(p.name for p in output.iterdir()) == [p.name for p in pdfs]
    # Rendered mail leaves the eml-input for the eml-archive, names unchanged.
    assert list((tmp_path / "eml-input" / "vault").iterdir()) == []
    archived = sorted(p.name for p in (tmp_path / "eml-archive" / "vault").iterdir())
    assert archived == ["2026-03-23_00-00-00_p_m0.eml", "2026-03-23_00-00-01_p_m1.eml"]
    assert not (tmp_path / "eml-error").exists()

    # Second run is idempotent: nothing new written.
    again = runner.invoke(app, ["render", "--profiles", str(profiles)])
    assert again.exit_code == 0
    assert len(list(output.glob("*.pdf"))) == 2


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
def test_render_moves_mail_from_a_legacy_inbox_to_the_archive(tmp_path: Path) -> None:
    output = tmp_path / "vault"
    _eml_under(output, build_mail(subject="Alt"), legacy=True)
    profiles = _profiles_yaml(tmp_path, output, name="acme", pdf=True)

    result = runner.invoke(app, ["render", "--profiles", str(profiles)])

    assert result.exit_code == 0, result.output
    assert len(list(output.glob("*.pdf"))) == 1
    assert list((output / "eml").iterdir()) == []
    assert [p.name for p in (tmp_path / "eml-archive" / "vault").iterdir()] == [
        "2026-03-23_00-00-00_p_m0.eml"
    ]


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
def test_render_points_at_mail_waiting_outside_a_profile(tmp_path: Path) -> None:
    """`render` cannot know which profile loose mail belongs to — it says so."""
    output = tmp_path / "vault"
    _eml_under(output, build_mail(subject="Mine"))
    loose = tmp_path / "eml-input" / "00_Mails"
    loose.mkdir()
    (loose / "x.eml").write_bytes(build_mail(subject="Whose"))
    profiles = _profiles_yaml(tmp_path, output, name="acme", pdf=True)

    result = runner.invoke(app, ["render", "--profiles", str(profiles)])

    assert result.exit_code == 0, result.output
    assert "1 mail(s) wait in" in result.output and "imaparc eml" in result.output
    assert (loose / "x.eml").is_file()  # untouched by render


@pytest.mark.requires_tools
def test_render_never_reads_the_archive(tmp_path: Path) -> None:
    """The eml-archive is for mail that is done: a mail there without a PDF is
    not rendered behind the user's back — it has to return to the eml-input."""
    output = tmp_path / "vault"
    _eml_under(output, build_mail(subject="Archived"), group="eml-archive")
    profiles = _profiles_yaml(tmp_path, output, name="acme", pdf=True)

    result = runner.invoke(app, ["render", "--profiles", str(profiles)])

    assert result.exit_code == 0, result.output
    assert not list(output.glob("*.pdf")) if output.exists() else True
    assert len(list((tmp_path / "eml-archive" / "vault").iterdir())) == 1


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
def test_render_moves_a_mail_it_cannot_render_to_eml_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from imaparc import render_run

    real = render_run.render_mail

    async def render(parsed: object, **kwargs: object) -> object:
        if b"Broken" in kwargs["raw"]:  # type: ignore[operator]
            raise ValueError("only this mail")
        return await real(parsed, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(render_run, "render_mail", render)
    output = tmp_path / "vault"
    _eml_under(output, build_mail(subject="Broken"), build_mail(subject="Fine"))
    profiles = _profiles_yaml(tmp_path, output, name="acme", pdf=True)

    result = runner.invoke(app, ["render", "--profiles", str(profiles)])

    assert result.exit_code == 0, result.output
    assert "1 FAILED to render" in result.output
    assert len(list(output.glob("*.pdf"))) == 1
    assert list((tmp_path / "eml-input" / "vault").iterdir()) == []
    error_dir = tmp_path / "eml-error" / "vault"
    assert sorted(p.name for p in error_dir.iterdir()) == [
        "2026-03-23_00-00-00_p_m0.eml",
        "2026-03-23_00-00-00_p_m0.error.txt",
    ]
    assert (
        "ValueError: only this mail"
        in (error_dir / "2026-03-23_00-00-00_p_m0.error.txt").read_text()
    )
    assert len(list((tmp_path / "eml-archive" / "vault").iterdir())) == 1


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
def test_render_recognises_rendered_mail_under_an_older_name(tmp_path: Path) -> None:
    """Folders named by an older scheme are found by manifest, not re-rendered."""
    output = tmp_path / "vault"
    _eml_under(output, build_mail(subject="Termin am 17. November, 14 Uhr"))
    profiles = _profiles_yaml(tmp_path, output, name="acme", pdf=True)
    assert runner.invoke(app, ["render", "--profiles", str(profiles)]).exit_code == 0
    (pdf,) = output.glob("*.pdf")
    pdf.rename(pdf.with_name("2026-03-23_acme_Termin_am_17._November,_14_Uhr.pdf"))
    # The same mail arrives again (the first copy is archived by now).
    _eml_under(output, build_mail(subject="Termin am 17. November, 14 Uhr"))

    again = runner.invoke(app, ["render", "--profiles", str(profiles)])

    assert again.exit_code == 0, again.output
    assert len(list(output.glob("*.pdf"))) == 1


# --- timestamp fallback (moved here with the render orchestration) ----------


def test_received_returns_none_for_missing_file(tmp_path: Path) -> None:
    from imaparc.render_run import received_from_file

    assert received_from_file(tmp_path / "gone.eml") is None


def test_received_reads_mtime(tmp_path: Path) -> None:
    from imaparc.render_run import received_from_file

    path = tmp_path / "mail.eml"
    path.write_bytes(b"x")

    assert received_from_file(path) is not None
