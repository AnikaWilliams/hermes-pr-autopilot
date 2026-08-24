"""Hermes plugin entry point for the standalone PR Autopilot."""

from __future__ import annotations

import logging
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any


_LOG = logging.getLogger("pr_autopilot.plugin")


def _duration_seconds(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([smhd])", value.strip().lower())
    if match is None:
        raise ValueError("worker timeout must use a positive s, m, h, or d duration")
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    seconds = int(match.group(1)) * multiplier
    if not 1 <= seconds <= 86400:
        raise ValueError("worker timeout must be between one second and one day")
    return seconds


def _initialize_config(package_root: Path, data_root: Path) -> Path:
    """Create a private profile-scoped config from the public safe template."""

    template = package_root / "config.example.json"
    if not template.is_file():
        raise RuntimeError("PR Autopilot configuration template is missing")
    try:
        template_config = json.loads(template.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError("PR Autopilot configuration template is invalid") from error
    if not isinstance(template_config, dict):
        raise RuntimeError("PR Autopilot configuration template must be an object")
    data_root.mkdir(parents=True, exist_ok=True)
    config_path = data_root / "config.json"
    if config_path.exists():
        if config_path.is_symlink():
            raise RuntimeError("PR Autopilot configuration must not be a symbolic link")
        try:
            existing_config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Keep an invalid operator file unchanged. Config.load reports the
            # precise validation failure rather than a migration overwriting it.
            return config_path
        if not isinstance(existing_config, dict):
            return config_path
        if all(key in existing_config for key in template_config):
            return config_path

        # Add only missing public defaults. Existing values, unknown values,
        # and operator-selected secret references stay exactly as supplied.
        merged_config = dict(template_config)
        merged_config.update(existing_config)
        payload = (json.dumps(merged_config, indent=2) + "\n").encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.",
            suffix=".migration",
            dir=str(data_root),
        )
        try:
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, config_path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
        return config_path
    payload = template.read_bytes()
    try:
        descriptor = os.open(
            config_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        return config_path
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
    return config_path


def _is_plugin_doctor() -> bool:
    """Return true inside Hermes' isolated, network-blocked validation home."""

    home = Path(os.environ.get("HERMES_HOME", ""))
    return home.name.startswith("hermes-plugin-doctor-")


def register(context: Any) -> None:
    """Register fixed worker policy and one supervised controller loop."""

    package_root = Path(__file__).resolve().parent
    configured_data_root = context.get_config("data_root", str(context.state.data_dir))
    if not isinstance(configured_data_root, str) or not configured_data_root.strip():
        raise ValueError("PR Autopilot data root must be a non-empty absolute path")
    data_root = Path(configured_data_root).expanduser()
    if not data_root.is_absolute():
        raise ValueError("PR Autopilot data root must be an absolute path")
    data_root = data_root.resolve()
    try:
        data_root.relative_to(package_root)
    except ValueError:
        pass
    else:
        raise ValueError("PR Autopilot data root must not be inside the plugin checkout")
    config_path = _initialize_config(package_root, data_root)
    worker_entry = package_root / "worker_entry.py"
    if not worker_entry.is_file():
        raise RuntimeError("PR Autopilot worker entry is missing")
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    if _is_plugin_doctor():
        _LOG.info("PR Autopilot registration validated without starting the controller")
        return

    from plugin_worker_runtime import WorkerDefinition, get_plugin_worker_runtime
    from pr_autopilot import Config
    from standalone_controller import ControllerService, StandaloneController, StandaloneTaskClient

    config = Config.load(config_path)
    config.worktree_root.mkdir(parents=True, exist_ok=True)
    stage_settings = (
        ("analyze", "pr-autopilot-analyze", config.analyzer_profile, config.analysis_task_timeout),
        ("fix", "pr-autopilot-fix", config.worker_profile, config.task_timeout),
        ("verify", "pr-autopilot-verify", config.verifier_profile, config.verification_task_timeout),
    )
    # Build all definitions before registration. A malformed later-stage
    # timeout must not leave an earlier stage registered.
    stage_definitions = tuple(
        (
            role,
            WorkerDefinition(
                definition_id=definition_id,
                argv=(
                    sys.executable,
                    str(worker_entry),
                    "--database",
                    str(config.state_path),
                    "--task-id",
                    "{work_item_ref}",
                    "--workspace",
                    "{workspace}",
                ),
                allowed_profiles=(profile,),
                allowed_models=("default",),
                allowed_reasoning=("none",),
                workspace_root=config.worktree_root,
                max_runtime_seconds=_duration_seconds(timeout),
                output_limit_bytes=1_048_576,
                cancellation_grace_seconds=10,
            ),
        )
        for role, definition_id, profile, timeout in stage_settings
    )
    runtime = get_plugin_worker_runtime()
    definition_ids: dict[str, str] = {}
    service: Any | None = None

    def shutdown() -> None:
        stop_error: BaseException | None = None
        try:
            if service is not None:
                service.stop()
        except BaseException as error:
            # The host must still see the controller stop error. The runtime
            # owner is released in the finally path below.
            stop_error = error
            _LOG.exception("PR Autopilot controller stop failed during unload")
            raise
        finally:
            try:
                runtime.unregister_owner(context.plugin_id, reason="plugin_unloaded")
            except BaseException:
                if stop_error is None:
                    raise
                # Do not replace the controller stop failure with cleanup
                # noise. The process restart fence remains fail-closed.
                _LOG.exception(
                    "PR Autopilot worker cleanup failed after controller stop error"
                )

    try:
        for role, definition in stage_definitions:
            runtime.register_definition(context.plugin_id, definition)
            definition_ids[role] = definition.definition_id
        task_client = StandaloneTaskClient(
            config,
            runtime=runtime,
            owner=context.plugin_id,
            definition_ids=definition_ids,
        )
        controller = StandaloneController(config, task_client=task_client)
        service = ControllerService(controller)
        service.start()
        # Do not publish the unload callback until the controller is live.
        # If startup fails, rollback removes every registered definition and a
        # later registration in this process can start from a clean state.
        context.on_unload(shutdown)
    except BaseException:
        if service is not None:
            try:
                service.stop()
            except Exception:
                _LOG.exception("PR Autopilot controller cleanup failed after registration error")
        try:
            runtime.unregister_owner(context.plugin_id, reason="plugin_registration_failed")
        except Exception:
            _LOG.exception("PR Autopilot worker cleanup failed after registration error")
        raise
    _LOG.info("PR Autopilot standalone controller registered")
