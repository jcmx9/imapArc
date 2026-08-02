"""Tests for `imaparc doctor` — the setup diagnosis."""

from __future__ import annotations

from pathlib import Path

import pytest

from imaparc.doctor import Check, Status, run_checks


def _by_name(checks: list[Check], name: str) -> Check:
    return next(c for c in checks if c.name == name)


def _profiles_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_reports_a_missing_tool_as_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "imaparc.doctor.shutil.which",
        lambda cmd: None if cmd == "verapdf" else f"/usr/bin/{cmd}",
    )

    checks = run_checks(env_file=_env(tmp_path, ""), profile_file=tmp_path / "n.yaml")

    assert _by_name(checks, "verapdf").status is Status.FAIL
    assert _by_name(checks, "gs").status is Status.OK
    assert "/usr/bin/gs" in _by_name(checks, "gs").detail


def test_reports_an_unparsable_profile_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("imaparc.doctor.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    broken = _profiles_yaml(tmp_path, "profiles: [ this is not valid")

    checks = run_checks(env_file=_env(tmp_path, ""), profile_file=broken)

    assert _by_name(checks, "profile.yaml").status is Status.FAIL


def test_counts_the_profiles_it_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("imaparc.doctor.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    good = _profiles_yaml(
        tmp_path,
        "profiles:\n"
        "  - name: a\n    account: x\n    output: /tmp/a\n"
        "  - name: b\n    account: x\n    output: /tmp/b\n",
    )

    checks = run_checks(env_file=_env(tmp_path, ""), profile_file=good)

    check = _by_name(checks, "profile.yaml")
    assert check.status is Status.OK
    assert "2" in check.detail


def test_missing_config_is_a_warning_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh install has no config yet — that is 'run init', not 'broken'."""
    monkeypatch.setattr("imaparc.doctor.shutil.which", lambda cmd: f"/usr/bin/{cmd}")

    checks = run_checks(
        env_file=tmp_path / "absent.env", profile_file=tmp_path / "absent.yaml"
    )

    assert _by_name(checks, "profile.yaml").status is Status.WARN
    assert _by_name(checks, ".env").status is Status.WARN


def test_incomplete_account_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("imaparc.doctor.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    env = _env(tmp_path, "IMAP_PRIVAT_HOST=imap.example.com\n")  # user/password fehlen

    checks = run_checks(env_file=env, profile_file=tmp_path / "n.yaml")

    assert _by_name(checks, ".env").status is Status.FAIL


def test_offline_skips_the_login_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("imaparc.doctor.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    env = _env(
        tmp_path,
        "IMAP_PRIVAT_HOST=imap.example.com\n"
        "IMAP_PRIVAT_USER=u\n"
        "IMAP_PRIVAT_PASSWORD=p\n",
    )

    offline = run_checks(env_file=env, profile_file=tmp_path / "n.yaml", offline=True)

    assert not [c for c in offline if c.name.startswith("login")]


def test_exit_code_reflects_the_findings() -> None:
    from imaparc.doctor import exit_code

    assert exit_code([Check("a", Status.OK, "")]) == 0
    assert exit_code([Check("a", Status.OK, ""), Check("b", Status.WARN, "")]) == 0
    assert exit_code([Check("a", Status.OK, ""), Check("b", Status.FAIL, "")]) == 1


def test_chromium_check_names_the_wrong_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure that bit us: a populated cache holding the wrong revision.

    `playwright install` deletes outdated builds, so two environments with
    different Playwright versions evict each other's browser.
    """
    from imaparc.doctor import _chromium_check

    cache = tmp_path / "Library" / "Caches" / "ms-playwright"
    (cache / "chromium-1228").mkdir(parents=True)  # some other version's build
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("imaparc.doctor.chromium_revision", lambda: "1234")

    check = _chromium_check()

    assert check.status is Status.FAIL
    assert "1234" in check.detail  # what is needed
    assert "chromium-1228" in check.detail  # what is actually there


def test_chromium_check_accepts_the_matching_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from imaparc.doctor import _chromium_check

    cache = tmp_path / "Library" / "Caches" / "ms-playwright"
    (cache / "chromium_headless_shell-1234").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("imaparc.doctor.chromium_revision", lambda: "1234")

    assert _chromium_check().status is Status.OK
