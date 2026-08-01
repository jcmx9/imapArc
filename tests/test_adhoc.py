"""Tests for the profile-free ``eml`` command (ad-hoc rendering)."""

from __future__ import annotations

import errno
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from imaparc.adhoc import collect_eml, move_into_folder, run_adhoc
from imaparc.config import ToolPaths
from imaparc.exceptions import SourceError
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


# --- move_into_folder ------------------------------------------------------


def _folder(tmp_path: Path, name: str = "2026-03-23_02-18-04_mail_Test") -> Path:
    """A rendered mail's output folder, as _store() would leave it."""
    folder = tmp_path / name
    folder.mkdir()
    return folder


def test_move_renames_eml_to_the_folder_name(tmp_path: Path) -> None:
    mail = _eml(tmp_path / "Rechnung Juli.eml")
    folder = _folder(tmp_path)

    target = move_into_folder(mail, folder)

    assert target == folder / f"{folder.name}.eml"
    assert target.is_file()
    assert not mail.exists()


def test_moved_eml_is_read_only(tmp_path: Path) -> None:
    mail = _eml(tmp_path / "a.eml")
    mail.chmod(0o644)

    target = move_into_folder(mail, _folder(tmp_path))

    assert target is not None
    assert target.stat().st_mode & 0o777 == 0o400


def test_move_is_idempotent(tmp_path: Path) -> None:
    """A second run finds nothing left to move and must not fail."""
    mail = _eml(tmp_path / "a.eml")
    folder = _folder(tmp_path)
    first = move_into_folder(mail, folder)

    assert move_into_folder(mail, folder) is None
    assert first is not None
    assert first.is_file()


def test_move_does_not_clobber_a_same_named_attachment(tmp_path: Path) -> None:
    mail = _eml(tmp_path / "a.eml")
    folder = _folder(tmp_path)
    attachment = folder / f"{folder.name}.eml"
    attachment.write_bytes(b"an attachment that happens to be named like this")

    target = move_into_folder(mail, folder)

    assert target == folder / f"{folder.name}-2.eml"
    assert attachment.read_bytes().startswith(b"an attachment")


def test_move_works_across_filesystems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dragged .eml can sit in /var/folders while the target is elsewhere."""
    mail = _eml(tmp_path / "a.eml")
    folder = _folder(tmp_path)

    def _cross_device(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(os, "rename", _cross_device)

    target = move_into_folder(mail, folder)

    assert target is not None
    assert target.is_file()
    assert not mail.exists()


# --- run_adhoc (end to end) ------------------------------------------------


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_renders_into_a_folder_beside_the_eml(tmp_path: Path) -> None:
    mail = _eml(tmp_path / "Rechnung.eml", subject="Rechnung Juli")

    report = await run_adhoc([mail], name="mail", tools=ToolPaths.resolve())

    assert len(report.written) == 1
    folder = tmp_path / report.written[0].basename
    assert folder.is_dir()
    assert folder.name.endswith("_mail_Rechnung_Juli")
    assert (folder / f"{folder.name}.pdf").is_file()


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_eml_ends_up_inside_the_rendered_folder(tmp_path: Path) -> None:
    mail = _eml(tmp_path / "Rechnung.eml", subject="Rechnung Juli")

    report = await run_adhoc([mail], name="mail", tools=ToolPaths.resolve())

    folder = tmp_path / report.written[0].basename
    assert (folder / f"{folder.name}.eml").is_file()
    assert not mail.exists()
    assert list(tmp_path.glob("*.eml")) == []


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_name_option_lands_in_the_basename(tmp_path: Path) -> None:
    mail = _eml(tmp_path / "a.eml", subject="Rechnung")

    report = await run_adhoc([mail], name="hetzner", tools=ToolPaths.resolve())

    assert "_hetzner_" in report.written[0].basename


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_parent_directory_permissions_are_left_alone(tmp_path: Path) -> None:
    """The target is the user's own directory — never lock it down like an archive."""
    work = tmp_path / "desktop"
    work.mkdir(mode=0o755)
    before = work.stat().st_mode & 0o777
    mail = _eml(work / "a.eml")

    await run_adhoc([mail], name="mail", tools=ToolPaths.resolve())

    assert work.stat().st_mode & 0o777 == before


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_rerun_skips_and_writes_nothing_new(tmp_path: Path) -> None:
    mail = _eml(tmp_path / "a.eml")
    first = await run_adhoc([mail], name="mail", tools=ToolPaths.resolve())
    folder = tmp_path / first.written[0].basename
    before = sorted(p.name for p in folder.iterdir())

    second = await run_adhoc(
        collect_eml([tmp_path]), name="mail", tools=ToolPaths.resolve()
    )

    assert second.written == []
    assert sorted(p.name for p in folder.iterdir()) == before


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_interrupted_run_gets_its_eml_moved_on_the_next_run(
    tmp_path: Path,
) -> None:
    """Rendered folder present, .eml still outside — the next run repairs it."""
    mail = _eml(tmp_path / "a.eml")
    first = await run_adhoc([mail], name="mail", tools=ToolPaths.resolve())
    folder = tmp_path / first.written[0].basename
    stray = folder / f"{folder.name}.eml"
    stray.chmod(0o600)
    shutil.move(str(stray), str(tmp_path / "a.eml"))

    await run_adhoc([tmp_path / "a.eml"], name="mail", tools=ToolPaths.resolve())

    assert (folder / f"{folder.name}.eml").is_file()
    assert not (tmp_path / "a.eml").exists()


@pytest.mark.requires_chromium
@pytest.mark.requires_tools
@pytest.mark.slow
async def test_an_unreadable_mail_does_not_abort_the_run(tmp_path: Path) -> None:
    """Note: garbage bytes will NOT do here — the email parser accepts anything
    and renders it as a text mail. An unreadable file is a real failure path."""
    unreadable = _eml(tmp_path / "aa-unreadable.eml")
    unreadable.chmod(0o000)
    good = _eml(tmp_path / "zz-good.eml", subject="Good One")

    try:
        report = await run_adhoc(
            collect_eml([tmp_path]), name="mail", tools=ToolPaths.resolve()
        )
    finally:
        unreadable.chmod(0o600)

    assert [r.basename for r in report.written if "Good_One" in r.basename]
    assert not good.exists()
    assert unreadable.exists()  # left untouched, nothing was moved


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
        await run_adhoc([mail], name="mail", tools=ToolPaths.resolve())
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert "/pixel.png" in requested
