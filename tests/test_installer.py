from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from codexbot.installer import (
    ALL_HOOK_EVENTS,
    CORE_HOOK_EVENTS,
    install_personal_plugin,
    validate_plugin_tree,
)


ROOT = Path(__file__).resolve().parents[1]


def test_source_plugin_passes_installer_validation() -> None:
    validate_plugin_tree(ROOT / "plugin" / "codexbot")


def test_installer_merges_marketplace_preserves_order_and_updates(tmp_path: Path) -> None:
    home = tmp_path / "home with 空格"
    runtime = home / "本地 数据" / "runtime" / "Scripts" / "python.exe"
    marketplace_path = home / ".agents" / "plugins" / "marketplace.json"
    marketplace_path.parent.mkdir(parents=True)
    original = {
        "name": "my-personal",
        "interface": {"displayName": "我的插件"},
        "plugins": [
            {
                "name": "existing",
                "source": {"source": "local", "path": "./plugins/existing"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_USE"},
                "category": "Developer Tools",
            }
        ],
    }
    marketplace_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

    result = install_personal_plugin(
        ROOT,
        home=home,
        run_codex=False,
        runtime_executable=runtime,
        permission_notifications=False,
    )

    assert result.marketplace_name == "my-personal"
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    assert marketplace["interface"]["displayName"] == "我的插件"
    assert [entry["name"] for entry in marketplace["plugins"]] == ["existing", "codexbot"]
    assert marketplace["plugins"][1]["source"] == {
        "source": "local",
        "path": "./plugins/codexbot",
    }
    assert marketplace_path.with_suffix(".json.codexbot.bak").is_file()

    manifest_dir = result.plugin_path / ".codex-plugin"
    manifest = json.loads((manifest_dir / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"].startswith("0.1.0+codex.local-")
    assert [path.name for path in manifest_dir.iterdir()] == ["plugin.json"]

    installed_hooks = json.loads(
        (result.plugin_path / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )["hooks"]
    expected_command = (
        f'py.exe -3.11 -E "{result.plugin_path / "hooks" / "entry.py"}" '
        f'--runtime "{runtime}"'
    )
    assert set(installed_hooks) == CORE_HOOK_EVENTS
    for groups in installed_hooks.values():
        handler = groups[0]["hooks"][0]
        assert handler["command"] == expected_command
        assert handler["commandWindows"] == expected_command
        assert "${PLUGIN_ROOT}" not in expected_command
        assert "%LOCALAPPDATA%" not in expected_command
        assert "powershell" not in expected_command.casefold()

    second = install_personal_plugin(
        ROOT,
        home=home,
        run_codex=False,
        runtime_executable=runtime,
        permission_notifications=False,
    )
    assert second.plugin_path == result.plugin_path
    second_manifest = json.loads(
        (second.plugin_path / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert second_manifest["version"] != manifest["version"]
    updated = json.loads(marketplace_path.read_text(encoding="utf-8"))
    assert [entry["name"] for entry in updated["plugins"]].count("codexbot") == 1


def test_installer_can_opt_in_to_permission_hooks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = install_personal_plugin(
        ROOT,
        home=home,
        run_codex=False,
        runtime_executable=home / "runtime" / "Scripts" / "python.exe",
        permission_notifications=True,
    )

    installed_hooks = json.loads(
        (result.plugin_path / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )["hooks"]
    assert set(installed_hooks) == ALL_HOOK_EVENTS


@pytest.mark.skipif(os.name != "nt", reason="CodexBot hook commands target Windows shells")
def test_materialized_hook_command_runs_from_cmd_and_powershell(tmp_path: Path) -> None:
    home = tmp_path / "shell test 空格"
    runtime = home / "runtime path" / "Scripts" / "python.exe"
    result = install_personal_plugin(
        ROOT,
        home=home,
        run_codex=False,
        runtime_executable=runtime,
        permission_notifications=False,
    )
    hooks = json.loads(
        (result.plugin_path / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )["hooks"]
    command = hooks["SessionStart"][0]["hooks"][0]["commandWindows"]

    invocations = (
        # Codex uses Command::raw_arg for cmd.exe /C and wraps the complete
        # hook string in one raw quote pair on Windows.
        f'cmd.exe /D /S /C "{command}"',
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
    )
    environment = os.environ.copy()
    environment["CODEXBOT_DATA_DIR"] = str(tmp_path / "hook data")
    for invocation in invocations:
        completed = subprocess.run(
            invocation,
            input="",
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            env=environment,
            timeout=5,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "{}"


def test_installer_repairs_missing_marketplace_interface(tmp_path: Path) -> None:
    home = tmp_path / "home"
    marketplace_path = home / ".agents" / "plugins" / "marketplace.json"
    marketplace_path.parent.mkdir(parents=True)
    marketplace_path.write_text(
        json.dumps(
            {
                "name": "personal",
                "plugins": [
                    {
                        "name": "codexbot",
                        "source": {"source": "local", "path": "./plugins/codexbot"},
                        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                        "category": "Productivity",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    install_personal_plugin(ROOT, home=home, run_codex=False)

    repaired = json.loads(marketplace_path.read_text(encoding="utf-8"))
    assert repaired["interface"] == {"displayName": "Personal"}


def test_installer_removes_files_stale_from_the_source_tree(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = install_personal_plugin(ROOT, home=home, run_codex=False)

    stale_file = result.plugin_path / "stale-from-previous-install.txt"
    stale_directory = result.plugin_path / "stale-directory"
    stale_file.write_text("stale", encoding="utf-8")
    stale_directory.mkdir()
    (stale_directory / "old.txt").write_text("stale", encoding="utf-8")

    install_personal_plugin(ROOT, home=home, run_codex=False)

    assert not stale_file.exists()
    assert not stale_directory.exists()


def test_installer_rolls_back_local_changes_when_codex_add_fails(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    first = install_personal_plugin(ROOT, home=home, run_codex=False)
    manifest_before = (first.plugin_path / ".codex-plugin" / "plugin.json").read_bytes()

    marketplace_path = first.marketplace_path
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    marketplace["plugins"][0]["category"] = "Keep this value"
    marketplace_path.write_text(json.dumps(marketplace), encoding="utf-8")
    marketplace_before = marketplace_path.read_bytes()

    preserved_file = first.plugin_path / "preserve-on-failure.txt"
    preserved_file.write_text("keep", encoding="utf-8")

    monkeypatch.setattr("codexbot.installer.find_codex_command", lambda: "codex")

    def failed_codex(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if args[2] == "list":
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps({"plugins": []}),
                stderr="",
            )
        if args[2] == "add":
            return subprocess.CompletedProcess(args=args, returncode=7, stdout="", stderr="boom")
        raise AssertionError(f"unexpected Codex command: {args}")

    monkeypatch.setattr("codexbot.installer.subprocess.run", failed_codex)

    with pytest.raises(RuntimeError, match="codex plugin add"):
        install_personal_plugin(ROOT, home=home, run_codex=True)

    assert (first.plugin_path / ".codex-plugin" / "plugin.json").read_bytes() == manifest_before
    assert preserved_file.read_text(encoding="utf-8") == "keep"
    assert marketplace_path.read_bytes() == marketplace_before
    assert not marketplace_path.with_suffix(".json.codexbot.bak").exists()


def test_installer_never_removes_preexisting_codex_plugin_after_add_failure(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    first = install_personal_plugin(ROOT, home=home, run_codex=False)
    calls: list[list[str]] = []

    monkeypatch.setattr("codexbot.installer.find_codex_command", lambda: "codex")

    def fake_codex(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[2] == "list":
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(
                    {"plugins": [{"name": "codexbot", "marketplace": "personal"}]}
                ),
                stderr="",
            )
        if args[2] == "add":
            return subprocess.CompletedProcess(args=args, returncode=9, stdout="", stderr="boom")
        raise AssertionError(f"existing plugin must not be removed: {args}")

    monkeypatch.setattr("codexbot.installer.subprocess.run", fake_codex)

    with pytest.raises(RuntimeError, match="codex plugin add"):
        install_personal_plugin(ROOT, home=home, run_codex=True)

    assert [args[2] for args in calls] == ["list", "add", "list"]
    assert (first.plugin_path / ".codex-plugin" / "plugin.json").is_file()


def test_installer_removes_only_new_codex_registration_after_add_failure(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    calls: list[list[str]] = []
    snapshots = [
        {"plugins": []},
        {"plugins": [{"id": "codexbot@personal"}]},
    ]

    monkeypatch.setattr("codexbot.installer.find_codex_command", lambda: "codex")

    def fake_codex(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[2] == "list":
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=json.dumps(snapshots.pop(0)), stderr=""
            )
        if args[2] == "add":
            return subprocess.CompletedProcess(args=args, returncode=7, stdout="", stderr="boom")
        if args[2] == "remove":
            assert args[3:5] == ["codexbot@personal", "--json"]
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")
        raise AssertionError(f"unexpected Codex command: {args}")

    monkeypatch.setattr("codexbot.installer.subprocess.run", fake_codex)

    with pytest.raises(RuntimeError, match="codex plugin add"):
        install_personal_plugin(ROOT, home=home, run_codex=True)

    assert [args[2] for args in calls] == ["list", "add", "list", "remove"]
    assert not (home / "plugins" / "codexbot").exists()
    assert not (home / ".agents" / "plugins" / "marketplace.json").exists()


def test_installer_recovers_a_partial_registration_after_add_timeout(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    calls: list[list[str]] = []
    snapshots = [
        {"plugins": []},
        {"plugins": [{"name": "codexbot", "marketplace": "personal"}]},
    ]

    monkeypatch.setattr("codexbot.installer.find_codex_command", lambda: "codex")

    def fake_codex(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[2] == "list":
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=json.dumps(snapshots.pop(0)), stderr=""
            )
        if args[2] == "add":
            raise subprocess.TimeoutExpired(args, timeout=60)
        if args[2] == "remove":
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")
        raise AssertionError(f"unexpected Codex command: {args}")

    monkeypatch.setattr("codexbot.installer.subprocess.run", fake_codex)

    with pytest.raises(RuntimeError, match="timed out"):
        install_personal_plugin(ROOT, home=home, run_codex=True)

    assert [args[2] for args in calls] == ["list", "add", "list", "remove"]


def test_installer_refuses_to_overwrite_an_unrecognized_target(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = home / "plugins" / "codexbot" / ".codex-plugin"
    target.mkdir(parents=True)
    (target / "plugin.json").write_text('{"name":"someone-else"}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        install_personal_plugin(ROOT, home=home, run_codex=False)
