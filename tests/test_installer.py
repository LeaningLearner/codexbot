from __future__ import annotations

import json
from pathlib import Path

import pytest

from codexbot.installer import install_personal_plugin, validate_plugin_tree


ROOT = Path(__file__).resolve().parents[1]


def test_source_plugin_passes_installer_validation() -> None:
    validate_plugin_tree(ROOT / "plugin" / "codexbot")


def test_installer_merges_marketplace_preserves_order_and_updates(tmp_path: Path) -> None:
    home = tmp_path / "home"
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

    result = install_personal_plugin(ROOT, home=home, run_codex=False)

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

    second = install_personal_plugin(ROOT, home=home, run_codex=False)
    assert second.plugin_path == result.plugin_path
    second_manifest = json.loads(
        (second.plugin_path / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert second_manifest["version"] != manifest["version"]
    updated = json.loads(marketplace_path.read_text(encoding="utf-8"))
    assert [entry["name"] for entry in updated["plugins"]].count("codexbot") == 1


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


def test_installer_refuses_to_overwrite_an_unrecognized_target(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = home / "plugins" / "codexbot" / ".codex-plugin"
    target.mkdir(parents=True)
    (target / "plugin.json").write_text('{"name":"someone-else"}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        install_personal_plugin(ROOT, home=home, run_codex=False)
