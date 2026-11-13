"""Tests for the ``eml`` command (file mode into the profile archive)."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from imaparc.adhoc import collect_eml, run_adhoc
from imaparc.config import ToolPaths
from imaparc.exceptions import SourceError
from imaparc.profiles import Match, Profile
from imaparc.report import RunReport
from tests.mail_builder import build_mail


def _eml(path: Path, subject: str = "Test") -> Path:
    """Write a synthetic .eml at ``path``; return it resolved.

    ``collect_eml`` resolves its inputs, and on macOS ``tmp_path`` reaches the
    same file through a symlink (``/var`` → ``/private/var``), so comparisons
    only line up if the expected path is resolved too.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_mail(subject=subject))
    return path.resolve()


def test_single_file_argument(tmp_path: Path) -> None:
    mail = _eml(tmp_path / "a.eml")
    assert collect_eml([mail]) == [mail]


def test_directory_collects_its_eml_sorted(tmp_path: Path) -> None:
    _eml(tmp_path / "b.eml")
    _eml(tmp_path / "a.eml")
    assert [p.name for p in collect_eml([tmp_path])] == ["a.eml", "b.eml"]


def test_directory_ignores_other_files(tmp_path: Path) -> None:
    _eml(tmp_path / "real.eml")
    (tmp_path / "notes.txt").write_bytes(b"junk")
    (tmp_path / ".DS_Store").write_bytes(b"junk")
    assert [p.name for p in collect_eml([tmp_path])] == ["real.eml"]


def test_directory_is_not_recursive(tmp_path: Path) -> None:
    """A rendered mail's .eml lives one level down — it must not be re-collected."""
    _eml(tmp_path / "top.eml")
    _eml(tmp_path / "2026-03-23_02-18-04_mail_Test" / "nested.eml")
    assert [p.name for p in collect_eml([tmp_path])] == ["top.eml"]


def test_no_arguments_uses_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _eml(tmp_path / "a.eml")
    monkeypatch.chdir(tmp_path)
    assert [p.name for p in collect_eml([])] == ["a.eml"]


def test_argument_order_is_preserved(tmp_path: Path) -> None:
    second = _eml(tmp_path / "b.eml")
    first = _eml(tmp_path / "a.eml")
    assert collect_eml([second, first]) == [second, first]


def test_duplicates_are_removed(tmp_path: Path) -> None:
    mail = _eml(tmp_path / "a.eml")
    assert collect_eml([mail, tmp_path, mail]) == [mail]


def test_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="not found"):
        collect_eml([tmp_path / "nope.eml"])


def test_non_eml_file_argument_raises(tmp_path: Path) -> None:
    other = tmp_path / "scan.pdf"
    other.write_bytes(b"%PDF-1.4")
    with pytest.raises(SourceError, match=r"not an \.eml"):
        collect_eml([other])


# --- run_adhoc (end to end) ------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_perms(tmp_path: Path) -> Iterator[None]:
    yield
    for entry in sorted(tmp_path.rglob("*"), reverse=True):
        with contextlib.suppress(OSError):
            entry.chmod(0o700)


def _profile(name: str, root: Path, address: str) -> Profile:
    return Profile(
        name=name,
        account="a",
        output=root / name,
        match=Match(addresses=[address]),
    )


async def _run(
    files: list[Path],
    root: Path,
    profiles: list[Profile] | None = None,
    name: str = "mail",
    **kwargs: int,
) -> RunReport:
    return await run_adhoc(
        files,
        profiles=profiles or [],
        root=root,
        name=name,
        tools=ToolPaths.resolve(),
        **kwargs,
    )


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_unmatched_mail_is_filed_into_00_mails(tmp_path: Path) -> None:
    mail = _eml(tmp_path / "in" / "Rechnung.eml", subject="Rechnung Juli")
    root = tmp_path / "arc"

    report = await _run([mail], root)

    basename = report.written[0].basename
    assert basename.endswith("_mail_Rechnung_Juli")
    assert sorted(p.name for p in (root / "imaparc-pdf" / "00_Mails").iterdir()) == [
        f"{basename}.pdf"
    ]
    # The PDF exists, so the .eml moved on to the archive — and left the inbox.
    archived = root / "eml-archive" / "00_Mails" / f"{basename}.eml"
    assert archived.read_bytes() == build_mail(subject="Rechnung Juli")
    assert not mail.exists()


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_the_profile_is_derived_from_the_headers(tmp_path: Path) -> None:
    root = tmp_path / "arc"
    mail = tmp_path / "in" / "a.eml"
    mail.parent.mkdir()
    mail.write_bytes(build_mail(from_="amt@stadt.de", subject="Termin"))
    profiles = [
        _profile("Amt", root, "amt@stadt.de"),
        _profile("Other", root, "x@y.de"),
    ]

    report = await _run([mail], root, profiles)

    basename = report.written[0].basename
    assert "_Amt_" in basename
    assert (root / "Amt" / f"{basename}.pdf").is_file()
    assert (root / "eml-archive" / "Amt" / f"{basename}.eml").is_file()
    assert not (root / "Other").exists()
    assert not (root / "imaparc-pdf" / "00_Mails").exists()
    assert not mail.exists()


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_a_mail_matching_two_profiles_is_filed_into_both(tmp_path: Path) -> None:
    root = tmp_path / "arc"
    mail = tmp_path / "in" / "a.eml"
    mail.parent.mkdir()
    raw = build_mail(from_="amt@stadt.de", to="anwalt@kanzlei.de", subject="Akte")
    mail.write_bytes(raw)
    profiles = [
        _profile("Amt", root, "amt@stadt.de"),
        _profile("Anwalt", root, "anwalt@kanzlei.de"),
    ]

    report = await _run([mail], root, profiles)

    assert len(report.written) == 2
    for name in ("Amt", "Anwalt"):
        [archived] = (root / "eml-archive" / name).iterdir()
        assert archived.read_bytes() == raw
        assert (root / name / f"{archived.stem}.pdf").is_file()
    assert not mail.exists()


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_name_option_names_unmatched_mail(tmp_path: Path) -> None:
    mail = _eml(tmp_path / "in" / "a.eml", subject="Rechnung")

    report = await _run([mail], tmp_path / "arc", name="hetzner")

    assert "_hetzner_" in report.written[0].basename


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_source_directory_permissions_are_left_alone(tmp_path: Path) -> None:
    """The source is the user's own directory — never lock it down."""
    work = tmp_path / "desktop"
    work.mkdir(mode=0o755)
    before = work.stat().st_mode & 0o777
    mail = _eml(work / "a.eml")

    await _run([mail], tmp_path / "arc")

    assert work.stat().st_mode & 0o777 == before


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_a_second_copy_is_archived_without_a_second_folder(
    tmp_path: Path,
) -> None:
    root = tmp_path / "arc"
    first = await _run([_eml(tmp_path / "in" / "a.eml")], root)
    pdf_dir = root / "imaparc-pdf" / "00_Mails"
    before = sorted(p.name for p in pdf_dir.iterdir())

    again = _eml(tmp_path / "elsewhere" / "copy.eml")
    second = await _run([again], root)

    assert second.written == []
    assert second.skipped
    assert sorted(p.name for p in pdf_dir.iterdir()) == before
    # The identical bytes are already archived: reused, and the copy is gone.
    archived = list((root / "eml-archive" / "00_Mails").iterdir())
    assert [p.stem for p in archived] == [first.written[0].basename]
    assert not again.exists()


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_a_mail_rendered_from_the_archive_is_not_deleted(
    tmp_path: Path,
) -> None:
    """Archiving a file onto itself must not count it as its own duplicate."""
    root = tmp_path / "arc"
    first = await _run([_eml(tmp_path / "in" / "a.eml")], root)
    archived = root / "eml-archive" / "00_Mails" / f"{first.written[0].basename}.eml"

    await _run([archived], root)

    assert archived.is_file()


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_an_unreadable_mail_does_not_abort_the_run(tmp_path: Path) -> None:
    """Note: garbage bytes will NOT do here — the email parser accepts anything
    and renders it as a text mail. An unreadable file is a real failure path."""
    inbox = tmp_path / "in"
    unreadable = _eml(inbox / "aa-unreadable.eml")
    unreadable.chmod(0o000)
    good = _eml(inbox / "zz-good.eml", subject="Good One")

    try:
        report = await _run(collect_eml([inbox]), tmp_path / "arc")
    finally:
        unreadable.chmod(0o600)

    assert [r.basename for r in report.written if "Good_One" in r.basename]
    assert not good.exists()
    assert unreadable.exists()  # left untouched, nothing was moved


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_a_mail_that_breaks_rendering_goes_to_eml_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No PDF, the .eml in eml-error with the reason beside it, the run goes on."""
    from imaparc import adhoc

    real = adhoc.render_mail

    async def render(parsed: object, **kwargs: object) -> object:
        if b"Broken" in kwargs["raw"]:  # type: ignore[operator]
            raise ValueError("something only this mail triggers")
        return await real(parsed, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(adhoc, "render_mail", render)
    inbox, root = tmp_path / "in", tmp_path / "arc"
    broken = _eml(inbox / "a.eml", subject="Broken")
    good = _eml(inbox / "b.eml", subject="Good One")

    report = await _run(collect_eml([inbox]), root, verbosity=0)

    assert [path for path, _ in report.failed] == [str(broken)]
    assert "something only this mail triggers" in report.failed[0][1]
    # The source is emptied either way: one mail archived, one in eml-error.
    assert list(inbox.iterdir()) == []
    error_dir = root / "eml-error" / "00_Mails"
    assert (error_dir / "a.eml").read_bytes() == build_mail(subject="Broken")
    note = (error_dir / "a.error.txt").read_text()
    assert "ValueError: something only this mail triggers" in note
    assert "no PDF was written" in note
    assert len(report.written) == 1 and not good.exists()
    assert len(list((root / "imaparc-pdf" / "00_Mails").glob("*.pdf"))) == 1
    assert "1 FAILED to render" in report.summary()


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_a_failed_mail_can_be_tried_again_from_eml_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing the error directory renders it; the note goes with the success."""
    from imaparc import adhoc

    real = adhoc.render_mail
    fail = True

    async def render(parsed: object, **kwargs: object) -> object:
        if fail:
            raise ValueError("first time only")
        return await real(parsed, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(adhoc, "render_mail", render)
    root = tmp_path / "arc"
    await _run([_eml(tmp_path / "in" / "a.eml")], root, verbosity=0)
    error_dir = root / "eml-error" / "00_Mails"
    assert (error_dir / "a.error.txt").is_file()

    fail = False
    report = await _run(collect_eml([error_dir]), root, verbosity=0)

    assert len(report.written) == 1 and report.failed == []
    assert list(error_dir.iterdir()) == []  # the .eml and its note are gone
    assert len(list((root / "eml-archive" / "00_Mails").glob("*.eml"))) == 1


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_a_dead_remote_image_means_no_pdf(tmp_path: Path) -> None:
    """With remote images on, an image that does not load is not left out
    silently: the mail fails, and the note names the resource."""
    import socket

    with socket.socket() as probe:  # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    mail = tmp_path / "in" / "a.eml"
    mail.parent.mkdir()
    mail.write_bytes(
        build_mail(
            subject="Newsletter",
            html=(
                "<html><body><table><tr><td>Angebot</td></tr></table>"
                f'<img src="http://127.0.0.1:{port}/gone.png"></body></html>'
            ),
        )
    )
    root = tmp_path / "arc"

    report = await _run([mail], root, verbosity=0)

    assert report.written == []
    assert not (root / "imaparc-pdf" / "00_Mails").exists() or not list(
        (root / "imaparc-pdf" / "00_Mails").iterdir()
    )
    note = (root / "eml-error" / "00_Mails" / "a.error.txt").read_text()
    assert "remote resources did not load" in note
    assert f"http://127.0.0.1:{port}/gone.png" in note

    # The same mail renders once remote images are off — that is the choice
    # to archive it without them.
    again = await _run(
        collect_eml([root / "eml-error" / "00_Mails"]), root, allow_remote=False
    )
    assert len(again.written) == 1


# 1x1 transparent PNG, served to the browser in the remote-image test.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
@pytest.mark.parametrize("allow_remote", [True, False])
async def test_remote_images_follow_the_switch(
    tmp_path: Path, allow_remote: bool
) -> None:
    """By default `eml` renders what the sender intended: remote images load.

    With them switched off (`--no-remote-images`) nothing may reach the server —
    that is the whole point when filing old mail in bulk.


    Would fail if run_adhoc built its RunConfig with allow_remote=False: the
    browser's network lockdown blocks the request and nothing reaches the server.
    """
    requested: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(_PNG)))
            self.end_headers()
            self.wfile.write(_PNG)

        def log_message(self, *args: object) -> None:
            pass  # keep the test output clean

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        mail = tmp_path / "remote.eml"
        mail.write_bytes(
            build_mail(
                subject="Remote Image",
                html=(
                    "<html><body><p>hi</p>"
                    f'<img src="http://127.0.0.1:{port}/pixel.png">'
                    "</body></html>"
                ),
            )
        )
        await _run([mail], tmp_path / "arc", allow_remote=allow_remote)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert ("/pixel.png" in requested) is allow_remote


# --- gs_jobs actually caps Ghostscript --------------------------------------


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_ghostscript_runs_are_capped_below_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ghostscript is the memory-hungry step, so it is capped separately.

    Fails without the semaphore: the conversions then run at `jobs` concurrency
    and peak memory scales with render parallelism instead of with gs_jobs.
    """
    import threading
    import time

    from imaparc import pipeline

    real_merge = pipeline._merge_to_pdfa
    live = 0
    peak = 0
    lock = threading.Lock()

    def _counting_merge(pages: object, stem: object, config: object) -> bytes:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)  # hold the slot so siblings would pile up
        try:
            return real_merge(pages, stem, config)  # type: ignore[arg-type]
        finally:
            with lock:
                live -= 1

    monkeypatch.setattr(pipeline, "_merge_to_pdfa", _counting_merge)

    for i in range(6):
        _eml(tmp_path / f"m{i}.eml", subject=f"Mail {i}")

    await _run(
        collect_eml([tmp_path]), tmp_path / "arc", jobs=6, gs_jobs=2, verbosity=0
    )

    assert peak <= 2, f"{peak} Ghostscript runs at once, gs_jobs was 2"


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_two_copies_of_one_mail_produce_one_folder(tmp_path: Path) -> None:
    """Two files, one Message-ID — the archive must hold a single folder.

    Happens without any user error: Gmail lists a message in All Mail *and* in
    its label folder (two UIDs), and a mail uploaded back to the server returns
    with a fresh UID.
    """
    inbox = tmp_path / "in"
    inbox.mkdir()
    body = build_mail(subject="Az 4711", message_id="<the-same@example.com>")
    (inbox / "copy-a.eml").write_bytes(body)
    (inbox / "copy-b.eml").write_bytes(body)

    report = await _run(collect_eml([inbox]), tmp_path / "arc", verbosity=0)

    pdfs = list((tmp_path / "arc" / "imaparc-pdf" / "00_Mails").iterdir())
    assert len(pdfs) == 1, f"one mail became {len(pdfs)} PDFs: {pdfs}"
    assert len(report.written) == 1
    assert len(report.skipped) == 1
    # Both copies end up archived, even the one skipped while its twin was
    # still rendering: the archiving pass runs after every render finished.
    assert list(inbox.glob("*.eml")) == []
    assert len(list((tmp_path / "arc" / "eml-archive" / "00_Mails").iterdir())) == 1
