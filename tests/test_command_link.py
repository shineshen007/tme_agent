"""The per-user ``multiomics`` launcher and its managed PATH entry."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from native_dist import command_link


def fake_python(root: Path) -> Path:
    path = root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def install(root: Path, bin_dir: Path, **kwargs):
    fake_python(root)
    return command_link.install(root, bin_dir=bin_dir, **kwargs)


def test_launcher_is_generated_once_and_reports_status(tmp_path):
    root = tmp_path / "install"
    bin_dir = tmp_path / "bin"
    first = install(root, bin_dir, add_path=False)
    launcher = bin_dir / ("multiomics.cmd" if os.name == "nt" else "multiomics")
    assert Path(first["launcher"]) == launcher
    assert launcher.is_file()
    text = launcher.read_text(encoding="utf-8")
    assert command_link.MARKER in text
    assert "multiomics_tui" in text
    assert "PYTHONPATH" in text
    assert str(root) in text
    if os.name != "nt":
        assert stat.S_IMODE(launcher.stat().st_mode) & 0o111

    second = install(root, bin_dir, add_path=False)
    assert launcher.read_text(encoding="utf-8") == text
    assert second["path_requested"] is False
    state = command_link.status(root, bin_dir=bin_dir, home=tmp_path / "home",
                                environ={"PATH": ""})
    assert state["exists"] is True
    assert state["managed"] is True
    assert state["path_requested"] is False


def test_foreign_file_is_never_overwritten(tmp_path):
    root = tmp_path / "install"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / ("multiomics.cmd" if os.name == "nt" else "multiomics")
    launcher.write_text("echo foreign\n", encoding="utf-8")
    fake_python(root)
    with pytest.raises(command_link.LauncherConflict):
        command_link.install(root, bin_dir=bin_dir, add_path=False)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell launcher")
def test_posix_launcher_forwards_default_root_before_user_arguments(tmp_path):
    root = tmp_path / "install"
    bin_dir = tmp_path / "bin"
    python = fake_python(root)
    python.write_text(
        '#!/bin/sh\n: > "$RECORD"\nfor arg in "$@"; do printf "%s\\n" "$arg" >> "$RECORD"; done\n',
        encoding="utf-8")
    python.chmod(0o755)
    command_link.install(root, bin_dir=bin_dir, add_path=False)
    record = tmp_path / "argv.txt"
    launcher = bin_dir / "multiomics"
    result = os.spawnve(os.P_WAIT, str(launcher), [str(launcher), "--version"],
                        {**os.environ, "RECORD": str(record)})
    assert result == 0
    argv = record.read_text(encoding="utf-8").splitlines()
    assert argv[:2] == ["-P", "-m"]
    assert argv[2:5] == ["multiomics_tui", "--project-root", str(root)]
    assert argv[5:] == ["--version"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX profile block")
def test_managed_path_block_is_idempotent_and_removable(tmp_path):
    root = tmp_path / "install"
    home = tmp_path / "home"
    home.mkdir()
    profile = home / ".zshrc"
    profile.write_text("export EDITOR=vim\n", encoding="utf-8")
    bin_dir = home / ".local/bin"
    first = install(root, bin_dir, add_path=True, home=home, shell="/bin/zsh",
                    environ={"PATH": "/usr/bin"})
    assert first["path_changed"] is True
    assert first["restart_required"] is True
    text = profile.read_text(encoding="utf-8")
    assert "export EDITOR=vim" in text
    assert text.count(command_link.BEGIN) == 1
    assert str(bin_dir) in text

    second = install(root, bin_dir, add_path=True, home=home, shell="/bin/zsh",
                     environ={"PATH": "/usr/bin"})
    assert second["path_changed"] is False
    assert profile.read_text(encoding="utf-8").count(command_link.BEGIN) == 1

    result = command_link.uninstall(root=root, bin_dir=bin_dir, home=home)
    assert result["removed"] is True
    assert result["path_removed"] is True
    remaining = profile.read_text(encoding="utf-8")
    assert "export EDITOR=vim" in remaining
    assert command_link.BEGIN not in remaining
    assert not (bin_dir / "multiomics").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX profile block")
def test_no_path_leaves_profile_untouched(tmp_path):
    root = tmp_path / "install"
    home = tmp_path / "home"
    home.mkdir()
    profile = home / ".zshrc"
    profile.write_text("export EDITOR=vim\n", encoding="utf-8")
    install(root, home / ".local/bin", add_path=False, home=home, shell="/bin/zsh")
    assert profile.read_text(encoding="utf-8") == "export EDITOR=vim\n"


def test_previous_path_preference_round_trip(tmp_path):
    root = tmp_path / "install"
    bin_dir = tmp_path / "bin"
    install(root, bin_dir, add_path=False)
    assert command_link.previous_path_preference(root) is False
    install(root, bin_dir, add_path=True, home=tmp_path / "home", shell="/bin/zsh",
            environ={"PATH": str(bin_dir)})
    assert command_link.previous_path_preference(root) is True


def test_shell_profile_selection(tmp_path):
    home = tmp_path
    assert command_link.shell_profile(home, "/bin/zsh")[0] == home / ".zshrc"
    assert command_link.shell_profile(home, "/usr/local/bin/fish")[0] == home / ".config/fish/config.fish"
    bash = command_link.shell_profile(home, "/bin/bash")[0]
    assert bash == home / (".bash_profile" if sys.platform == "darwin" else ".bashrc")
    assert command_link.shell_profile(home, "/bin/sh")[0] == home / ".profile"


def test_windows_launcher_shape(tmp_path):
    text = command_link._windows_launcher(tmp_path / "a b", tmp_path / "py.exe")
    assert "rem tme_agent launcher" in text
    assert "set \"PYTHONPATH=" in text
    assert "--project-root" in text
    assert "%*" in text


@pytest.mark.skipif(os.name != "nt", reason="Windows PATH helper")
def test_windows_path_value_keeps_existing_entries(tmp_path):
    value = command_link._windows_path_value(tmp_path / "bin", r"C:\Tools;C:\Other")
    assert value.endswith(str(tmp_path / "bin"))
    assert r"C:\Tools" in value and r"C:\Other" in value


def test_cli_install_status_and_uninstall(tmp_path, capsys):
    root = tmp_path / "install"
    bin_dir = tmp_path / "bin"
    fake_python(root)
    assert command_link.main([
        "install", "--project-root", str(root), "--bin-dir", str(bin_dir), "--no-path",
    ]) == 0
    assert "multiomics" in capsys.readouterr().out
    assert command_link.previous_path_preference(root) is False
    assert command_link.main([
        "status", "--project-root", str(root), "--bin-dir", str(bin_dir),
    ]) == 0
    assert '"managed": true' in capsys.readouterr().out
    assert command_link.main([
        "uninstall", "--project-root", str(root), "--bin-dir", str(bin_dir),
    ]) == 0
    assert command_link.previous_path_preference(root) is None
    assert not (bin_dir / ("multiomics.cmd" if os.name == "nt" else "multiomics")).exists()
