from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from desktop.python.entrypoint import _run_python_compatibility_mode
from desktop.python.runtime import (
    refresh_official_plugin_sources,
    seed_bundled_plugins,
    seed_user_config,
)
from desktop.scripts import official_plugin_lock as plugin_lock_module
from desktop.scripts import stage_defaults as defaults_module
from desktop.scripts.create_update_manifest import create_manifest
from desktop.scripts.publish_r2_release import (
    IMMUTABLE_CACHE_CONTROL,
    LATEST_CACHE_CONTROL,
    R2Publisher,
    publish_release,
)
from desktop.scripts.verify_bundle import (
    verify_bundled_plugins,
    verify_defaults,
    verify_updater_signature,
    verify_windows_gui_executable,
    write_checksum,
)
from src.extension_host.source_config import PluginSourceConfig

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_desktop_backend_bundles_anthropic_provider() -> None:
    requirements = (REPO_ROOT / "requirements.lock").read_text(encoding="utf-8")
    spec = (
        REPO_ROOT / "desktop" / "pyinstaller" / "determinflow-backend.spec"
    ).read_text(encoding="utf-8")

    assert "anthropic==0.121.0" in requirements
    assert "langchain-anthropic==1.4.1" in requirements
    assert '    "anthropic",' in spec
    assert '    "httpx",' in spec
    assert '    "langchain-anthropic",' in spec
    assert 'collect_submodules("anthropic")' in spec
    assert 'collect_submodules("langchain_anthropic")' in spec
    assert 'hiddenimports += ["src.workflow.executor_worker"]' in spec


def test_tauri_bundle_is_a_per_user_nsis_installer() -> None:
    config = json.loads(
        (REPO_ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )

    bundle = config["bundle"]
    nsis = bundle["windows"]["nsis"]

    assert config["productName"] == "DeterminFlow"
    assert bundle["targets"] == ["nsis"]
    assert bundle["createUpdaterArtifacts"] is True
    assert bundle["icon"] == ["icons/icon.ico", "icons/icon.png"]
    assert nsis["installMode"] == "currentUser"
    assert nsis["installerIcon"] == "icons/icon.ico"
    assert nsis["installerHooks"] == "./windows/installer-hooks.nsh"
    assert nsis["uninstallerIcon"] == "icons/icon.ico"
    assert nsis["headerImage"] == "images/nsis-header.bmp"
    assert nsis["sidebarImage"] == "images/nsis-sidebar.bmp"
    assert nsis["uninstallerHeaderImage"] == "images/nsis-header.bmp"
    assert (
        bundle["windows"]["webviewInstallMode"]["type"]
        == "downloadBootstrapper"
    )
    updater = config["plugins"]["updater"]
    assert updater["endpoints"] == [
        "https://github.com/xuweibing233/DeterminFlow/releases/latest/download/latest.json"
    ]
    assert len(updater["pubkey"]) > 100
    assert b"minisign public key" in base64.b64decode(
        updater["pubkey"], validate=True
    )

    icons_dir = REPO_ROOT / "desktop" / "src-tauri" / "icons"
    assert (icons_dir / "icon.ico").stat().st_size > 1024
    assert (icons_dir / "icon.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    images_dir = REPO_ROOT / "desktop" / "src-tauri" / "images"
    for name, dimensions in {
        "nsis-header.bmp": (150, 57),
        "nsis-sidebar.bmp": (164, 314),
    }.items():
        image = (images_dir / name).read_bytes()
        assert image[:2] == b"BM"
        assert int.from_bytes(image[18:22], "little") == dimensions[0]
        assert int.from_bytes(image[22:26], "little") == dimensions[1]


def test_desktop_update_capability_only_trusts_the_bundled_loopback_ui() -> None:
    capability = json.loads(
        (REPO_ROOT / "desktop" / "src-tauri" / "capabilities" / "desktop-update.json")
        .read_text(encoding="utf-8")
    )

    assert capability["windows"] == ["main"]
    assert capability["remote"]["urls"] == ["http://127.0.0.1:*/*"]
    assert capability["permissions"] == [
        "allow-check-update-sources",
        "allow-prepare-for-update",
        "core:app:allow-version",
        "updater:default",
        "process:allow-restart",
    ]


def test_desktop_onboarding_capability_allows_only_the_bundled_loopback_ui() -> None:
    build_script = (REPO_ROOT / "desktop" / "src-tauri" / "build.rs").read_text(
        encoding="utf-8"
    )
    capability = json.loads(
        (
            REPO_ROOT
            / "desktop"
            / "src-tauri"
            / "capabilities"
            / "desktop-onboarding.json"
        ).read_text(encoding="utf-8")
    )

    assert '"get_desktop_onboarding_status"' in build_script
    assert '"set_desktop_onboarding_status"' in build_script
    assert capability["windows"] == ["main"]
    assert capability["remote"]["urls"] == ["http://127.0.0.1:*/*"]
    assert capability["permissions"] == [
        "allow-get-desktop-onboarding-status",
        "allow-set-desktop-onboarding-status",
    ]


def test_desktop_file_drop_capability_only_trusts_the_bundled_loopback_ui() -> None:
    capability = json.loads(
        (REPO_ROOT / "desktop" / "src-tauri" / "capabilities" / "desktop-file-drop.json")
        .read_text(encoding="utf-8")
    )

    assert capability["windows"] == ["main"]
    assert capability["remote"]["urls"] == ["http://127.0.0.1:*/*"]
    assert capability["permissions"] == ["core:event:default"]


def test_tauri_release_shell_uses_the_windows_gui_subsystem() -> None:
    main_source = (REPO_ROOT / "desktop" / "src-tauri" / "src" / "main.rs").read_text(
        encoding="utf-8"
    )

    assert (
        '#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]'
        in main_source
    )


def test_desktop_external_links_open_in_the_system_browser() -> None:
    main_source = (REPO_ROOT / "desktop" / "src-tauri" / "src" / "main.rs").read_text(
        encoding="utf-8"
    )
    cargo = (REPO_ROOT / "desktop" / "src-tauri" / "Cargo.toml").read_text(
        encoding="utf-8"
    )

    assert 'tauri-plugin-opener = "2.5.4"' in cargo
    assert ".on_new_window(" in main_source
    assert "is_allowed_external_url(&url)" in main_source
    assert "tauri_plugin_opener::open_url(" in main_source
    assert "tauri::webview::NewWindowResponse::Deny" in main_source


def test_desktop_lifecycle_cleans_up_the_backend_before_every_exit() -> None:
    main_source = (REPO_ROOT / "desktop" / "src-tauri" / "src" / "main.rs").read_text(
        encoding="utf-8"
    )
    updater_context = (
        REPO_ROOT / "web" / "src" / "desktop-updater" / "context.tsx"
    ).read_text(encoding="utf-8")
    hooks = (
        REPO_ROOT
        / "desktop"
        / "src-tauri"
        / "windows"
        / "installer-hooks.nsh"
    ).read_text(encoding="utf-8")
    updater_source = (
        REPO_ROOT / "desktop" / "src-tauri" / "src" / "updater.rs"
    ).read_text(encoding="utf-8")

    single_instance = main_source.index("tauri_plugin_single_instance::init")
    process_plugin = main_source.index("tauri_plugin_process::init")
    updater_plugin = main_source.index("tauri_plugin_updater::Builder::new")
    assert single_instance < process_plugin < updater_plugin
    assert "fn prepare_for_update" in main_source
    assert "prepare_for_update," in main_source
    assert "updater::check_update_sources," in main_source
    assert "releases/latest/download/latest.json" in updater_source
    assert (
        "https://github.com/xuweibing233/DeterminFlow/releases/latest/download/latest.json"
        in updater_source
    )
    assert (
        "https://api.github.com/repos/xuweibing233/DeterminFlow/releases/latest"
        in updater_source
    )
    assert "join3(r2, github, gitee)" in updater_source
    assert "primary: UpdateSource::Gitee" in updater_source
    assert "fallback: Some(UpdateSource::Github)" in updater_source
    assert "fallback_rid" in updater_source
    assert 'invoke<UpdateMetadata | null>("check_update_sources")' in updater_context
    assert "downloadUpdateWithFallback(" in updater_context
    assert "fallbackRid" in updater_context
    assert 'matches!(event, RunEvent::Exit)' in main_source
    assert main_source.count(".stop();") >= 2
    download = updater_context.index("await candidate.download(")
    prepare = updater_context.index('await invoke("prepare_for_update")')
    install = updater_context.index("await downloadedResource.install()")
    assert download < prepare < install
    assert "downloadAndInstall" not in updater_context
    assert "NSIS_HOOK_PREINSTALL" in hooks
    assert "NSIS_HOOK_PREUNINSTALL" in hooks
    assert "/IM determinflow-backend.exe" in hooks


def test_desktop_workflow_builds_candidates_and_publishes_tags() -> None:
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "desktop-windows.yml"
    ).read_text(encoding="utf-8")

    assert "runs-on: windows-2025" in workflow
    assert "matrix.flavor" in workflow
    assert "--flavor ${{ matrix.flavor }}" in workflow
    assert "desktop/scripts/smoke_backend.py" in workflow
    assert "Test Windows Workflow Executor process pool" in workflow
    assert "tests/test_workflow_executor_pool_scenarios.py" in workflow
    assert "--timeout 180" in workflow
    assert "desktop/scripts/smoke_installer.ps1" in workflow
    assert '-Flavor "${{ matrix.flavor }}"' in workflow
    assert "--expected-flavor ${{ matrix.flavor }}" in workflow
    assert "refresh_official_plugin_lock.py --check" in workflow
    assert "github.ref_type != 'tag'" in workflow
    assert "--desktop-executable" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "actions/download-artifact@v4" in workflow
    assert "TAURI_SIGNING_PRIVATE_KEY" in workflow
    assert "--updater-signature" in workflow
    assert "tauri-action" not in workflow
    assert "softprops/action-gh-release" not in workflow
    assert "gh release create" in workflow.lower()
    assert "contents: write" in workflow
    assert "release-assets/latest.json" in workflow
    assert "desktop/scripts/publish_r2_release.py" in workflow
    assert "R2_DISTRIBUTION_ENABLED" in workflow

    installer_smoke = (
        REPO_ROOT / "desktop" / "scripts" / "smoke_installer.ps1"
    ).read_text(encoding="utf-8")
    assert "CloseMainWindow" in installer_smoke
    assert "Second launch created duplicate Controllers" in installer_smoke
    assert "Assert-WorkflowExecutorPool" in installer_smoke
    assert "Get-InstalledWorkflowExecutors" in installer_smoke
    assert "NSIS reinstall with a stale backend" in installer_smoke


def test_stage_defaults_uses_sanitized_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_read(_repo_root: Path, relative_path: str) -> object:
        if relative_path.endswith("models_config.example.json"):
            return {"providers": {"safe": {"api_key": "${SAFE_API_KEY}"}}}
        return {"source": relative_path}

    monkeypatch.setattr(defaults_module, "_read_git_json", fake_read)
    output = tmp_path / "defaults"
    defaults_module.stage_defaults(tmp_path, output)

    assert json.loads((output / "extensions.json").read_text())["enabled"] == []
    assert json.loads((output / "mcp_servers.json").read_text()) == {
        "mcpServers": {}
    }
    plugin_source = json.loads((output / "plugin-sources.json").read_text())
    assert plugin_source["official_sources"][0]["url"].startswith("https://github.com/")
    assert plugin_source["official_sources"][0]["mirrors"] == [
        "https://gitee.com/alikon/DeterminFlow-Plugins.git"
    ]
    assert plugin_source["official_sources"][0]["ref"] == "main"
    assert plugin_source["official_sources"][0]["registry"] == {
        "url": "https://downloads.determinflow.com/plugins/v1",
        "public_key": "C4oDxekhIr8Czlx0zpkRx46k26KK3d1T3HIZGsIxIr0=",
    }
    assert (output / "models_config.json").read_text() == (
        output / "models_config.example.json"
    ).read_text()
    verify_defaults(output)


def test_plaintext_api_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="明文凭据"):
        defaults_module._validate_no_plaintext_secrets(
            {"provider": {"api_key": "not-an-env-reference"}}
        )


def test_seed_user_config_preserves_existing_files(tmp_path: Path) -> None:
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    (defaults / "settings.json").write_text('{"source": "default"}', encoding="utf-8")
    (defaults / "models_config.json").write_text(
        '{"providers": {}}', encoding="utf-8"
    )
    user_root = tmp_path / "user"
    existing = user_root / "config" / "settings.json"
    existing.parent.mkdir(parents=True)
    existing.write_text('{"source": "user"}', encoding="utf-8")

    created = seed_user_config(user_root, defaults)

    assert existing.read_text(encoding="utf-8") == '{"source": "user"}'
    assert user_root / "config" / "models_config.json" in created


def test_desktop_refreshes_only_core_owned_official_plugin_sources(
    tmp_path: Path,
) -> None:
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    (defaults / "plugin-sources.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "official_sources": [
                    {
                        "id": "determinflow-official",
                        "url": "https://github.com/alikon-art/DeterminFlow-Plugins.git",
                        "ref": "main",
                    }
                ],
                "custom_sources": [],
            }
        ),
        encoding="utf-8",
    )
    user_root = tmp_path / "user"
    user_config = user_root / "config" / "plugin-sources.json"
    user_config.parent.mkdir(parents=True)
    custom = {"id": "custom", "url": "https://example.com/plugins.git"}
    user_config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "official_sources": [
                    {
                        "id": "determinflow-official",
                        "url": "https://github.com/alikon-art/DeterminFlow-Plugins.git",
                        "ref": "v0.2.1",
                    }
                ],
                "custom_sources": [custom],
            }
        ),
        encoding="utf-8",
    )

    assert refresh_official_plugin_sources(user_root, defaults) is True
    refreshed = json.loads(user_config.read_text(encoding="utf-8"))
    assert refreshed["official_sources"][0]["ref"] == "main"
    assert refreshed["custom_sources"] == [custom]
    assert refresh_official_plugin_sources(user_root, defaults) is False


def test_full_snapshot_merges_and_enables_plugins_only_once(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    checkout = snapshot / "checkouts" / "bishu-novel" / "commit-digest"
    checkout.mkdir(parents=True)
    (checkout / "extension.toml").write_text("[extension]\n", encoding="utf-8")
    record = {
        "source": "https://github.com/alikon-art/DeterminFlow-Plugins.git",
        "source_kind": "git",
        "trust": "official",
        "subdirectory": "plugins/bishu-novel",
        "resource_prefix": "bishu-novel",
        "resource_prefix_override": None,
        "active_revision": {
            "commit": "a" * 40,
            "content_sha256": "b" * 64,
            "checkout": "checkouts/bishu-novel/commit-digest",
            "requested_ref": "main",
        },
        "history": [],
        "pending_action": None,
    }
    (snapshot / "plugins.lock.json").write_text(
        json.dumps({"schema_version": 1, "plugins": {"bishu-novel": record}}),
        encoding="utf-8",
    )
    (snapshot / "release-plugins.json").write_text(
        json.dumps({"schema_version": 1, "plugins": {"bishu-novel": {}}}),
        encoding="utf-8",
    )
    user_root = tmp_path / "user"
    config = user_root / "config" / "extensions.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"enabled": ["existing"], "strict_startup": False}),
        encoding="utf-8",
    )

    assert seed_bundled_plugins(user_root, snapshot) == ["bishu-novel"]
    assert seed_bundled_plugins(user_root, snapshot) == []
    enabled = json.loads(config.read_text(encoding="utf-8"))["enabled"]
    assert enabled == ["existing", "bishu-novel"]
    installed = json.loads(
        (user_root / "data" / "plugins" / "plugins.lock.json").read_text(
            encoding="utf-8"
        )
    )
    assert installed["plugins"]["bishu-novel"] == record


def test_full_snapshot_preserves_existing_plugin_record(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    bundled_record = {"active_revision": {"checkout": "invalid"}}
    (snapshot / "plugins.lock.json").write_text(
        json.dumps(
            {"schema_version": 1, "plugins": {"bishu-novel": bundled_record}}
        ),
        encoding="utf-8",
    )
    (snapshot / "release-plugins.json").write_text(
        json.dumps({"schema_version": 1, "plugins": {"bishu-novel": {}}}),
        encoding="utf-8",
    )
    user_root = tmp_path / "user"
    config = user_root / "config" / "extensions.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"enabled": [], "strict_startup": False}), encoding="utf-8"
    )
    existing_record = {"source": "user-managed"}
    lock = user_root / "data" / "plugins" / "plugins.lock.json"
    lock.parent.mkdir(parents=True)
    lock.write_text(
        json.dumps(
            {"schema_version": 1, "plugins": {"bishu-novel": existing_record}}
        ),
        encoding="utf-8",
    )

    assert seed_bundled_plugins(user_root, snapshot) == []
    stored = json.loads(lock.read_text(encoding="utf-8"))
    assert stored["plugins"]["bishu-novel"] == existing_record
    assert json.loads(config.read_text(encoding="utf-8"))["enabled"] == [
        "bishu-novel"
    ]


def test_bundled_plugin_verifier_rejects_empty_snapshot(tmp_path: Path) -> None:
    (tmp_path / "release-plugins.json").write_text(
        json.dumps({"schema_version": 1, "plugins": {}}), encoding="utf-8"
    )
    (tmp_path / "plugins.lock.json").write_text(
        json.dumps({"schema_version": 1, "plugins": {}}), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="不能为空"):
        verify_bundled_plugins(tmp_path)


def _write_official_plugin_lock_fixture(
    repo_root: Path,
    *,
    desktop_version: str = "1.0.10",
) -> dict:
    tauri = repo_root / "desktop" / "src-tauri" / "tauri.conf.json"
    tauri.parent.mkdir(parents=True, exist_ok=True)
    tauri.write_text(json.dumps({"version": "1.0.10"}), encoding="utf-8")
    lock = {
        "schema_version": 1,
        "desktop_version": desktop_version,
        "source": {
            "id": "determinflow-official",
            "url": "https://github.com/alikon-art/DeterminFlow-Plugins.git",
            "ref": "main",
            "commit": "a" * 40,
        },
        "plugins": [
            {
                "id": "bishu-novel",
                "version": "0.2.2",
                "subdirectory": "plugins/bishu-novel",
            },
            {
                "id": "public-api",
                "version": "0.1.33",
                "subdirectory": "plugins/public-api",
            },
        ],
    }
    lock_path = repo_root / plugin_lock_module.LOCK_RELATIVE_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    return lock


def test_full_plugin_lock_is_bound_to_desktop_version(tmp_path: Path) -> None:
    expected = _write_official_plugin_lock_fixture(tmp_path)

    assert plugin_lock_module.load_official_plugin_lock(tmp_path) == expected

    _write_official_plugin_lock_fixture(tmp_path, desktop_version="1.0.9")
    with pytest.raises(RuntimeError, match="桌面版本与"):
        plugin_lock_module.load_official_plugin_lock(tmp_path)


def test_full_plugin_catalog_must_match_the_exact_build_lock(
    tmp_path: Path,
) -> None:
    lock = _write_official_plugin_lock_fixture(tmp_path)
    source = PluginSourceConfig(
        id="determinflow-official",
        name="DeterminFlow Official Plugins",
        url=lock["source"]["url"],
        ref="main",
        mirrors=("https://gitee.com/alikon/DeterminFlow-Plugins.git",),
    )
    pinned = plugin_lock_module.pin_official_sources((source,), lock)

    assert pinned[0].ref == "a" * 40
    assert pinned[0].url == source.url
    catalog = {
        "sources": [
            {
                "id": source.id,
                "name": source.name,
                "error": "",
                "ref": "a" * 40,
                "resolved_commit": "a" * 40,
            }
        ],
        "plugins": [
            {
                **plugin,
                "source_id": source.id,
                "source": source.url,
                "ref": "a" * 40,
                "resolved_commit": "a" * 40,
            }
            for plugin in lock["plugins"]
        ],
    }
    entries = plugin_lock_module.validate_locked_catalog(catalog, lock)
    assert [entry["id"] for entry in entries] == ["bishu-novel", "public-api"]

    catalog["plugins"][1]["version"] = "0.1.34"
    with pytest.raises(RuntimeError, match="条目与构建锁不一致"):
        plugin_lock_module.validate_locked_catalog(catalog, lock)

    catalog["plugins"][1]["version"] = "0.1.33"
    catalog["plugins"][1]["ref"] = "main"
    with pytest.raises(RuntimeError, match="未锁定到构建锁 Commit"):
        plugin_lock_module.validate_locked_catalog(catalog, lock)


def test_full_plugin_lock_refresh_captures_latest_public_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_official_plugin_lock_fixture(tmp_path)
    source_file = tmp_path / "config" / "plugin-sources.json"
    source_file.parent.mkdir()
    source_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "official_sources": [
                    {
                        "id": "determinflow-official",
                        "name": "DeterminFlow Official Plugins",
                        "url": "https://github.com/alikon-art/DeterminFlow-Plugins.git",
                        "ref": "main",
                    }
                ],
                "custom_sources": [],
            }
        ),
        encoding="utf-8",
    )
    commit = "b" * 40

    def fake_catalog(sources: tuple[PluginSourceConfig, ...]) -> dict:
        source = sources[0]
        return {
            "sources": [
                {
                    "id": source.id,
                    "name": source.name,
                    "error": "",
                    "resolved_commit": commit,
                }
            ],
            "plugins": [
                {
                    "id": "public-api",
                    "version": "0.1.33",
                    "subdirectory": "plugins/public-api",
                    "source_id": source.id,
                    "resolved_commit": commit,
                }
            ],
        }

    monkeypatch.setattr(plugin_lock_module, "fetch_plugin_catalog", fake_catalog)

    refreshed = plugin_lock_module.resolve_latest_official_plugin_lock(
        tmp_path, source_file
    )

    assert refreshed["desktop_version"] == "1.0.10"
    assert refreshed["source"]["commit"] == commit
    assert refreshed["plugins"] == [
        {
            "id": "public-api",
            "version": "0.1.33",
            "subdirectory": "plugins/public-api",
        }
    ]


def test_desktop_versions_are_consistent() -> None:
    tauri = json.loads(
        (REPO_ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )
    package = json.loads(
        (REPO_ROOT / "desktop" / "package.json").read_text(encoding="utf-8")
    )
    cargo = (REPO_ROOT / "desktop" / "src-tauri" / "Cargo.toml").read_text(
        encoding="utf-8"
    )

    assert tauri["version"] == "1.0.10"
    assert package["version"] == tauri["version"]
    assert f'version = "{tauri["version"]}"' in cargo


def test_frozen_backend_can_execute_python_workflow_scripts(tmp_path: Path) -> None:
    output = tmp_path / "result.txt"
    script = tmp_path / "workflow.py"
    script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text('ok', encoding='utf-8')\n",
        encoding="utf-8",
    )

    assert _run_python_compatibility_mode([str(script), str(output)]) is True
    assert output.read_text(encoding="utf-8") == "ok"


def test_runtime_config_consumers_follow_redirected_config_dir(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name, payload in {
        "compression_config.json": {"general": {"enabled": True}},
        "mcp_servers.json": {"mcpServers": {}},
        "user_injection_config.json": {"sections": []},
    }.items():
        (config_dir / name).write_text(json.dumps(payload), encoding="utf-8")

    environment = os.environ.copy()
    environment["DETERMINFLOW_CONFIG_DIR"] = str(config_dir)
    code = (
        "from pathlib import Path\n"
        "from src.compression.config import CompressionConfigManager\n"
        "from src.mcp.client import MCPClient\n"
        "from src.web.api_routes import USER_INJECTION_CONFIG_FILE\n"
        f"expected = Path({str(config_dir)!r}).resolve()\n"
        "assert Path(CompressionConfigManager._DEFAULT_CONFIG_PATH).parent == expected\n"
        "assert MCPClient()._resolve_config_path() == expected / 'mcp_servers.json'\n"
        "assert USER_INJECTION_CONFIG_FILE == expected / 'user_injection_config.json'\n"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=environment,
        check=True,
    )


def test_checksum_file_is_portable_across_line_endings(tmp_path: Path) -> None:
    installer = tmp_path / "DeterminFlow-setup.exe"
    installer.write_bytes(b"installer")

    checksum = write_checksum(installer)

    assert checksum.read_bytes().endswith(b"\n")
    assert b"\r\n" not in checksum.read_bytes()


def test_updater_signature_and_static_manifest(tmp_path: Path) -> None:
    installer = tmp_path / "DeterminFlow 1.1.0-setup.exe"
    installer.write_bytes(b"installer")
    signature = installer.with_suffix(installer.suffix + ".sig")
    signature.write_text(
        base64.b64encode(b"signature" * 16).decode("ascii"), encoding="utf-8"
    )

    verify_updater_signature(signature)
    manifest = create_manifest(
        version="1.1.0",
        installer=installer,
        signature=signature,
        base_url="https://github.com/alikon-art/DeterminFlow/releases/download/v1.1.0",
        notes="桌面更新",
        pub_date="2026-08-04T00:00:00Z",
    )

    assert manifest["version"] == "1.1.0"
    platform = manifest["platforms"]["windows-x86_64"]
    assert platform["url"].endswith("/DeterminFlow%201.1.0-setup.exe")
    assert platform["signature"] == signature.read_text(encoding="utf-8")


def test_r2_publisher_rejects_changed_immutable_objects(tmp_path: Path) -> None:
    asset = tmp_path / "asset.exe"
    asset.write_bytes(b"new")

    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {"ContentLength": 3, "Metadata": {"sha256": "different"}}
            ),
            stderr="",
        )

    publisher = R2Publisher(
        bucket="downloads",
        endpoint_url="https://example.r2.cloudflarestorage.com",
        public_base_url="https://downloads.determinflow.com",
        runner=runner,
    )

    with pytest.raises(RuntimeError, match="immutable R2 object"):
        publisher.publish_immutable(asset, "desktop/releases/v1.2.3/asset.exe")


def test_r2_release_publishes_latest_only_after_verified_assets(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    installer = assets / "DeterminFlow_1.2.3_x64-setup.exe"
    installer.write_bytes(b"installer")
    installer.with_suffix(".exe.sig").write_text("signed", encoding="utf-8")
    (assets / "DeterminFlow_1.2.3_x64-full-setup.exe").write_bytes(b"full")
    notes = tmp_path / "notes.md"
    notes.write_text("R2 release", encoding="utf-8")
    calls: list[tuple[str, str, str]] = []

    class RecordingPublisher:
        public_base_url = "https://downloads.determinflow.com"

        def publish_immutable(self, path: Path, key: str) -> None:
            calls.append(("immutable", key, path.read_text(errors="ignore")))

        def publish_latest(self, path: Path, key: str) -> None:
            calls.append(("latest", key, path.read_text(encoding="utf-8")))

    publish_release(
        assets_dir=assets,
        version="1.2.3",
        notes_file=notes,
        pub_date="2026-08-29T00:00:00Z",
        publisher=RecordingPublisher(),  # type: ignore[arg-type]
    )

    assert calls[-1][0:2] == ("latest", "desktop/stable/latest.json")
    assert all(call[0] == "immutable" for call in calls[:-1])
    assert json.loads(calls[-1][2])["platforms"]["windows-x86_64"][
        "url"
    ].startswith("https://downloads.determinflow.com/desktop/releases/v1.2.3/")
    assert IMMUTABLE_CACHE_CONTROL.endswith("immutable")
    assert LATEST_CACHE_CONTROL.startswith("no-cache")


def test_windows_desktop_executable_must_use_the_gui_subsystem(tmp_path: Path) -> None:
    def write_pe(path: Path, subsystem: int) -> None:
        image = bytearray(256)
        image[:2] = b"MZ"
        image[0x3C:0x40] = (64).to_bytes(4, "little")
        image[64:68] = b"PE\x00\x00"
        optional_header = 64 + 24
        image[optional_header : optional_header + 2] = (0x20B).to_bytes(2, "little")
        image[optional_header + 68 : optional_header + 70] = subsystem.to_bytes(
            2, "little"
        )
        path.write_bytes(image)

    executable = tmp_path / "determinflow-desktop.exe"
    write_pe(executable, subsystem=2)
    verify_windows_gui_executable(executable)

    write_pe(executable, subsystem=3)
    with pytest.raises(RuntimeError, match="GUI Subsystem"):
        verify_windows_gui_executable(executable)
