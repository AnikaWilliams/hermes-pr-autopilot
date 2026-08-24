"""Worker-definition registration contracts for PR Autopilot."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]


def load_entrypoint():
    module_name = f"test_pr_autopilot_entrypoint_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "__init__.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the PR Autopilot entry point")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


class _Runtime:
    def __init__(self) -> None:
        self.definitions: list[tuple[str, object]] = []
        self.registered_definition_ids: list[str] = []
        self.unregistered: list[tuple[str, str]] = []
        self.fail_definition_id: str | None = None

    def register_definition(self, owner: str, definition: object) -> None:
        if getattr(definition, "definition_id", None) == self.fail_definition_id:
            raise RuntimeError("simulated definition registration failure")
        self.definitions.append((owner, definition))
        self.registered_definition_ids.append(str(getattr(definition, "definition_id")))

    def unregister_owner(self, owner: str, *, reason: str) -> None:
        self.unregistered.append((owner, reason))
        self.definitions = [item for item in self.definitions if item[0] != owner]


class _Service:
    instances: list["_Service"] = []
    fail_start = False
    fail_stop = False

    def __init__(self, _controller: object) -> None:
        self.started = False
        self.stopped = False
        self.instances.append(self)

    def start(self) -> None:
        self.started = True
        if self.fail_start:
            raise RuntimeError("simulated controller start failure")

    def stop(self) -> None:
        self.stopped = True
        if self.fail_stop:
            raise RuntimeError("simulated controller stop failure")


class PluginRegistrationTests(unittest.TestCase):
    def test_registration_migrates_missing_template_defaults_without_overwriting_operator_values(
        self,
    ) -> None:
        """An old profile config must gain defaults but retain private overrides."""
        _Service.instances.clear()
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory)
            template = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
            legacy = {
                "policy_revision": template["policy_revision"],
                "analyzer_profile": "operator-analysis-profile",
                "recovery_api_key_env": "OPERATOR_PRIVATE_RECOVERY_KEY",
                "operator_extension": {"retain": "this private value"},
            }
            config_path = data_root / "config.json"
            config_path.write_text(json.dumps(legacy), encoding="utf-8")
            context = SimpleNamespace(
                plugin_id="pr-autopilot",
                state=SimpleNamespace(data_dir=data_root),
                get_config=lambda _name, default: default,
                on_unload=lambda _callback: None,
            )
            runtime = _Runtime()
            entrypoint = load_entrypoint()

            with (
                patch("plugin_worker_runtime.get_plugin_worker_runtime", return_value=runtime),
                patch("standalone_controller.ControllerService", _Service),
            ):
                entrypoint.register(context)

            migrated = json.loads(config_path.read_text(encoding="utf-8"))
            for key, value in template.items():
                self.assertEqual(migrated.get(key), legacy.get(key, value), key)
            self.assertEqual(migrated["analyzer_profile"], "operator-analysis-profile")
            self.assertEqual(
                migrated["recovery_api_key_env"], "OPERATOR_PRIVATE_RECOVERY_KEY"
            )
            self.assertEqual(
                migrated["operator_extension"], {"retain": "this private value"}
            )

    def test_registration_rejects_a_data_root_inside_the_plugin_checkout(self) -> None:
        """Controller state must not be created in the installed plugin package."""
        entrypoint = load_entrypoint()
        unsafe_data_root = ROOT / "tests" / "unsafe-controller-data"
        context = SimpleNamespace(
            plugin_id="pr-autopilot",
            state=SimpleNamespace(data_dir=Path(tempfile.gettempdir()) / "safe-data"),
            get_config=lambda name, default: str(unsafe_data_root)
            if name == "data_root"
            else default,
            on_unload=lambda _callback: None,
        )

        with self.assertRaisesRegex(ValueError, "must not be inside the plugin checkout"):
            entrypoint.register(context)

    def test_registers_one_definition_per_stage_with_its_configured_timeout(self) -> None:
        _Service.instances.clear()
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory)
            config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
            config.update(
                {
                    "analysis_task_timeout": "17m",
                    "task_timeout": "23m",
                    "verification_task_timeout": "29m",
                }
            )
            (data_root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            unload_callbacks: list[object] = []
            context = SimpleNamespace(
                plugin_id="pr-autopilot",
                state=SimpleNamespace(data_dir=data_root),
                get_config=lambda _name, default: default,
                on_unload=unload_callbacks.append,
            )
            runtime = _Runtime()
            entrypoint = load_entrypoint()

            with (
                patch("plugin_worker_runtime.get_plugin_worker_runtime", return_value=runtime),
                patch("standalone_controller.ControllerService", _Service),
            ):
                entrypoint.register(context)

            self.assertEqual([owner for owner, _definition in runtime.definitions], ["pr-autopilot"] * 3)
            definitions = {
                definition.definition_id: definition for _owner, definition in runtime.definitions
            }
            self.assertEqual(set(definitions), {
                "pr-autopilot-analyze",
                "pr-autopilot-fix",
                "pr-autopilot-verify",
            })
            self.assertEqual(
                {
                    definition_id: definition.max_runtime_seconds
                    for definition_id, definition in definitions.items()
                },
                {
                    "pr-autopilot-analyze": 17 * 60,
                    "pr-autopilot-fix": 23 * 60,
                    "pr-autopilot-verify": 29 * 60,
                },
            )
            self.assertEqual(
                {
                    definition_id: definition.allowed_profiles
                    for definition_id, definition in definitions.items()
                },
                {
                    "pr-autopilot-analyze": (config["analyzer_profile"],),
                    "pr-autopilot-fix": (config["worker_profile"],),
                    "pr-autopilot-verify": (config["verifier_profile"],),
                },
            )
            self.assertEqual(len(unload_callbacks), 1)
            self.assertEqual(len(_Service.instances), 1)
            service = _Service.instances[0]
            self.assertTrue(service.started)

            unload_callbacks[0]()

            self.assertTrue(service.stopped)
            self.assertEqual(runtime.unregistered, [("pr-autopilot", "plugin_unloaded")])

    def test_failed_later_definition_registration_cleans_up_for_same_process_reload(self) -> None:
        _Service.instances.clear()
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory)
            config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
            (data_root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            unload_callbacks: list[object] = []
            context = SimpleNamespace(
                plugin_id="pr-autopilot",
                state=SimpleNamespace(data_dir=data_root),
                get_config=lambda _name, default: default,
                on_unload=unload_callbacks.append,
            )
            runtime = _Runtime()
            runtime.fail_definition_id = "pr-autopilot-fix"
            entrypoint = load_entrypoint()

            with (
                patch("plugin_worker_runtime.get_plugin_worker_runtime", return_value=runtime),
                patch("standalone_controller.ControllerService", _Service),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated definition"):
                    entrypoint.register(context)

                self.assertEqual(runtime.definitions, [])
                self.assertEqual(
                    runtime.unregistered,
                    [("pr-autopilot", "plugin_registration_failed")],
                )
                self.assertEqual(_Service.instances, [])
                self.assertEqual(unload_callbacks, [])

                runtime.fail_definition_id = None
                entrypoint.register(context)

            self.assertEqual(len(runtime.definitions), 3)
            self.assertEqual(len(_Service.instances), 1)
            self.assertTrue(_Service.instances[0].started)

    def test_failed_service_start_cleans_up_all_definitions_for_same_process_reload(self) -> None:
        _Service.instances.clear()
        _Service.fail_start = True
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                data_root = Path(temporary_directory)
                config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
                (data_root / "config.json").write_text(json.dumps(config), encoding="utf-8")
                unload_callbacks: list[object] = []
                context = SimpleNamespace(
                    plugin_id="pr-autopilot",
                    state=SimpleNamespace(data_dir=data_root),
                    get_config=lambda _name, default: default,
                    on_unload=unload_callbacks.append,
                )
                runtime = _Runtime()
                entrypoint = load_entrypoint()

                with (
                    patch("plugin_worker_runtime.get_plugin_worker_runtime", return_value=runtime),
                    patch("standalone_controller.ControllerService", _Service),
                ):
                    with self.assertRaisesRegex(RuntimeError, "simulated controller start"):
                        entrypoint.register(context)

                    self.assertEqual(
                        runtime.registered_definition_ids,
                        ["pr-autopilot-analyze", "pr-autopilot-fix", "pr-autopilot-verify"],
                    )
                    self.assertEqual(runtime.definitions, [])
                    self.assertEqual(
                        runtime.unregistered,
                        [("pr-autopilot", "plugin_registration_failed")],
                    )
                    self.assertEqual(unload_callbacks, [])
                    self.assertEqual(len(_Service.instances), 1)
                    self.assertTrue(_Service.instances[0].started)
                    self.assertTrue(_Service.instances[0].stopped)

                    _Service.fail_start = False
                    entrypoint.register(context)

                self.assertEqual(len(runtime.definitions), 3)
                self.assertEqual(len(unload_callbacks), 1)
                self.assertEqual(len(_Service.instances), 2)
                self.assertTrue(_Service.instances[1].started)
        finally:
            _Service.fail_start = False

    def test_unload_cleans_up_runtime_when_service_stop_fails(self) -> None:
        """Runtime ownership cleanup must not depend on a clean service shutdown."""

        _Service.instances.clear()
        _Service.fail_stop = True
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                data_root = Path(temporary_directory)
                config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
                (data_root / "config.json").write_text(json.dumps(config), encoding="utf-8")
                unload_callbacks: list[object] = []
                context = SimpleNamespace(
                    plugin_id="pr-autopilot",
                    state=SimpleNamespace(data_dir=data_root),
                    get_config=lambda _name, default: default,
                    on_unload=unload_callbacks.append,
                )
                runtime = _Runtime()
                entrypoint = load_entrypoint()

                with (
                    patch("plugin_worker_runtime.get_plugin_worker_runtime", return_value=runtime),
                    patch("standalone_controller.ControllerService", _Service),
                ):
                    entrypoint.register(context)

                    with self.assertRaisesRegex(RuntimeError, "simulated controller stop"):
                        unload_callbacks[0]()  # type: ignore[operator]

                self.assertTrue(_Service.instances[0].stopped)
                self.assertEqual(runtime.definitions, [])
                self.assertEqual(runtime.unregistered, [("pr-autopilot", "plugin_unloaded")])
        finally:
            _Service.fail_stop = False


if __name__ == "__main__":
    unittest.main()
