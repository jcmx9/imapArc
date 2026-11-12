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
    assert sorted(p.name for p in (root / "00_Mails").iterdir()) == [f"{basename}.pdf"]
    # The PDF exists, so the .eml moved on to the archive — and left the inbox.
    archived = root / "eml-Archiv" / "00_Mails" / f"{basename}.eml"
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
    assert (root / "eml-Archiv" / "Amt" / f"{basename}.eml").is_file()
    assert not (root / "Other").exists()
    assert not (root / "00_Mails").exists()
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
        [archived] = (root / "eml-Archiv" / name).iterdir()
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
    pdf_dir = root / "00_Mails"
    before = sorted(p.name for p in pdf_dir.iterdir())

    again = _eml(tmp_path / "elsewhere" / "copy.eml")
    second = await _run([again], root)

    assert second.written == []
    assert second.skipped
    assert sorted(p.name for p in pdf_dir.iterdir()) == before
    # The identical bytes are already archived: reused, and the copy is gone.
    archived = list((root / "eml-Archiv" / "00_Mails").iterdir())
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
    archived = root / "eml-Archiv" / "00_Mails" / f"{first.written[0].basename}.eml"

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
async def test_a_mail_that_breaks_rendering_is_counted_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever a mail provokes, the run goes on and names the mail."""
    from imaparc import adhoc

    real = adhoc.render_mail

    async def render(parsed: object, **kwargs: object) -> object:
        if b"Broken" in kwargs["raw"]:  # type: ignore[operator]
            raise ValueError("something only this mail triggers")
        return await real(parsed, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(adhoc, "render_mail", render)
    inbox = tmp_path / "in"
    broken = _eml(inbox / "a.eml", subject="Broken")
    good = _eml(inbox / "b.eml", subject="Good One")

    report = await _run(collect_eml([inbox]), tmp_path / "arc", verbosity=0)

    assert report.failed == [str(broken)]
    assert broken.exists()  # never archived without its PDF
    assert len(report.written) == 1 and not good.exists()
    assert "1 FAILED to render" in report.summary()


# 1x1 transparent PNG, served to the browser in the remote-image test.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_remote_images_are_always_loaded(tmp_path: Path) -> None:
    """`eml` renders what the sender intended, so remote images are fetched.

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
        await _run([mail], tmp_path / "arc")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert "/pixel.png" in requested


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

    pdfs = list((tmp_path / "arc" / "00_Mails").iterdir())
    assert len(pdfs) == 1, f"one mail became {len(pdfs)} PDFs: {pdfs}"
    assert len(report.written) == 1
    assert len(report.skipped) == 1
    # Both copies end up archived, even the one skipped while its twin was
    # still rendering: the archiving pass runs after every render finished.
    assert list(inbox.glob("*.eml")) == []
    assert len(list((tmp_path / "arc" / "eml-Archiv" / "00_Mails").iterdir())) == 1
