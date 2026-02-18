from __future__ import annotations

from argparse import Namespace

import mobius.__main__ as cli


def test_raw_repo_path_from_origin_url_parses_github_formats() -> None:
    assert (
        cli._raw_repo_path_from_origin_url("https://github.com/zigamilek/mobius.git")
        == "zigamilek/mobius"
    )
    assert (
        cli._raw_repo_path_from_origin_url("git@github.com:zigamilek/mobius.git")
        == "zigamilek/mobius"
    )
    assert cli._raw_repo_path_from_origin_url("https://example.com/repo.git") is None


def test_parser_accepts_update_command_flags() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(
        ["update", "--dry-run", "--repo-ref", "v0.5.0", "--raw-repo-path", "foo/bar"]
    )
    assert args.command == "update"
    assert args.dry_run is True
    assert args.repo_ref == "v0.5.0"
    assert args.raw_repo_path == "foo/bar"


def test_update_command_dry_run_does_not_execute_subprocess(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli, "_resolve_update_sources", lambda **_: ("foo/bar", "https://github.com/foo/bar.git", "main"))
    if hasattr(cli.os, "geteuid"):
        monkeypatch.setattr(cli.os, "geteuid", lambda: 0)

    called = {"count": 0}

    def _fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        called["count"] += 1
        raise AssertionError("subprocess.run should not be called in dry-run mode")

    monkeypatch.setattr(cli.subprocess, "run", _fake_run)

    rc = cli._cmd_update(
        Namespace(
            raw_repo_path=None,
            repo_url=None,
            repo_ref=None,
            dry_run=True,
        )
    )
    output = capsys.readouterr().out
    assert rc == 0
    assert called["count"] == 0
    assert "Mobius Update" in output
    assert "Dry run only. No update executed." in output


def test_update_command_requires_root_when_supported(monkeypatch, capsys) -> None:
    if not hasattr(cli.os, "geteuid"):
        return
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    rc = cli._cmd_update(
        Namespace(
            raw_repo_path=None,
            repo_url=None,
            repo_ref=None,
            dry_run=True,
        )
    )
    output = capsys.readouterr().out
    assert rc == 1
    assert "should be run as root" in output
