from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PublicPackageTests(unittest.TestCase):
    def test_unified_plugin_layout_is_installable(self) -> None:
        required = (
            ROOT / "plugin.yaml",
            ROOT / "__init__.py",
            ROOT / "desktop" / "plugin.js",
            ROOT / "dashboard" / "manifest.json",
            ROOT / "dashboard" / "plugin_api.py",
            ROOT / "worker_entry.py",
            ROOT / "plugin_worker_runtime.py",
        )

        self.assertEqual([str(path) for path in required if not path.is_file()], [])
        self.assertFalse((ROOT / "plugin" / "plugin.yaml").exists())

    def test_default_configuration_contains_no_machine_specific_recovery_target(self) -> None:
        config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))

        self.assertEqual(config["recovery_endpoint_hosts"], [])
        self.assertEqual(config["recovery_model_id"], "disabled")
        self.assertEqual(config["recovery_api_key_env"], "PR_AUTOPILOT_RECOVERY_API_KEY")
        self.assertEqual(config["recovery_api_key_file"], ".secrets/recovery-api-key")

    def test_plugin_uses_profile_scoped_data_and_bundled_worker_runtime(self) -> None:
        entrypoint = (ROOT / "__init__.py").read_text(encoding="utf-8")
        dashboard_api = (ROOT / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")

        self.assertIn("context.state.data_dir", entrypoint)
        self.assertIn("from plugin_worker_runtime import", entrypoint)
        self.assertNotIn("hermes_cli.plugin_worker_runtime", entrypoint)
        self.assertIn('"plugin-data" / "pr-autopilot"', dashboard_api)

    def test_ci_cache_uses_the_public_test_dependency_manifest(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        self.assertIn("cache: pip", workflow)
        self.assertIn("cache-dependency-path: requirements-dev.txt", workflow)

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell is required")
    def test_validate_only_checks_github_without_writing_profiles_or_plugin_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            hermes_home = temporary_root / "hermes"
            hermes_home.mkdir()
            command_root = temporary_root / "commands"
            command_root.mkdir()
            log_path = temporary_root / "commands.log"
            for command in ("gh", "git", "hermes"):
                (command_root / f"{command}.cmd").write_text(
                    "@echo off\r\n"
                    "echo %~n0 %*>> \"%INSTALL_TEST_LOG%\"\r\n"
                    "exit /b 0\r\n",
                    encoding="utf-8",
                )
            environment = os.environ.copy()
            environment["INSTALL_TEST_LOG"] = str(log_path)
            environment["PATH"] = f"{command_root}{os.pathsep}{environment['PATH']}"

            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts" / "install.ps1"),
                    "-SourceProfile",
                    "default",
                    "-HermesHome",
                    str(hermes_home),
                    "-ValidateOnly",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                calls,
                [
                    "gh auth status --hostname github.com --active",
                    "gh api --hostname github.com repos/AnikaWilliams/hermes-pr-autopilot --jq .full_name",
                ],
            )
            self.assertEqual(list(hermes_home.iterdir()), [])

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell is required")
    def test_backend_activation_requires_a_later_manual_enable(self) -> None:
        """The installer must install disabled and reject one-step backend activation."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hermes_home = root / "hermes"
            hermes_home.mkdir()
            commands = root / "commands"
            commands.mkdir()
            log_path = root / "commands.log"
            for command in ("gh", "git"):
                (commands / f"{command}.cmd").write_text(
                    "@echo off\r\n"
                    "echo %~n0 %*>> \"%INSTALL_TEST_LOG%\"\r\n"
                    "exit /b 0\r\n",
                    encoding="utf-8",
                )
            (commands / "hermes.cmd").write_text(
                "@echo off\r\n"
                "echo hermes %*>> \"%INSTALL_TEST_LOG%\"\r\n"
                "if /i \"%~1 %~2\"==\"profile create\" "
                "mkdir \"%HERMES_HOME%\\profiles\\%~3\" >nul 2>nul\r\n"
                "exit /b 0\r\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["INSTALL_TEST_LOG"] = str(log_path)
            environment["PATH"] = f"{commands}{os.pathsep}{environment['PATH']}"
            normal = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts" / "install.ps1"),
                    "-SourceProfile",
                    "default",
                    "-HermesHome",
                    str(hermes_home),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(normal.returncode, 0, normal.stderr)
            plugin_calls = [
                call
                for call in log_path.read_text(encoding="utf-8").splitlines()
                if call.startswith("hermes plugins install ")
            ]
            self.assertEqual(
                plugin_calls,
                ["hermes plugins install AnikaWilliams/hermes-pr-autopilot --no-enable"],
            )
            self.assertFalse(any(" --enable" in call for call in plugin_calls))
            self.assertIn(
                "Verify that any existing controller is paused before you manually enable",
                normal.stdout,
            )
            self.assertNotIn("first run", normal.stdout.lower())

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hermes_home = root / "hermes"
            hermes_home.mkdir()
            commands = root / "commands"
            commands.mkdir()
            log_path = root / "commands.log"
            for command in ("gh", "git", "hermes"):
                (commands / f"{command}.cmd").write_text(
                    "@echo off\r\n"
                    "echo %~n0 %*>> \"%INSTALL_TEST_LOG%\"\r\n"
                    "exit /b 0\r\n",
                    encoding="utf-8",
                )
            environment = os.environ.copy()
            environment["INSTALL_TEST_LOG"] = str(log_path)
            environment["PATH"] = f"{commands}{os.pathsep}{environment['PATH']}"
            enabled = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts" / "install.ps1"),
                    "-SourceProfile",
                    "default",
                    "-HermesHome",
                    str(hermes_home),
                    "-EnableBackend",
                    "-ValidateOnly",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertNotEqual(enabled.returncode, 0)
            self.assertIn("-EnableBackend is not supported", enabled.stdout + enabled.stderr)
            self.assertFalse(log_path.exists())

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell is required")
    def test_declined_profile_action_does_not_offer_or_create_a_skill_only_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            hermes_home = temporary_root / "hermes"
            hermes_home.mkdir()
            command_root = temporary_root / "commands"
            command_root.mkdir()
            log_path = temporary_root / "commands.log"
            for command in ("gh", "git", "hermes"):
                (command_root / f"{command}.cmd").write_text(
                    "@echo off\r\n"
                    "echo %~n0 %*>> \"%INSTALL_TEST_LOG%\"\r\n"
                    "exit /b 0\r\n",
                    encoding="utf-8",
                )
            environment = os.environ.copy()
            environment["INSTALL_TEST_LOG"] = str(log_path)
            environment["PATH"] = f"{command_root}{os.pathsep}{environment['PATH']}"

            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts" / "install.ps1"),
                    "-SourceProfile",
                    "default",
                    "-HermesHome",
                    str(hermes_home),
                    "-WhatIf",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Install skill '", result.stdout)
            self.assertNotIn("Install the Hermes plugin backend", result.stdout)
            self.assertIn("setup is incomplete", result.stdout.lower())
            self.assertFalse((hermes_home / "profiles").exists())
            self.assertEqual(
                log_path.read_text(encoding="utf-8").splitlines(),
                [
                    "gh auth status --hostname github.com --active",
                    "gh api --hostname github.com repos/AnikaWilliams/hermes-pr-autopilot --jq .full_name",
                ],
            )

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell is required")
    def test_source_profile_validation_allows_dots_but_rejects_unsafe_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            hermes_home = temporary_root / "hermes"
            (hermes_home / "profiles" / "Work.prod").mkdir(parents=True)
            command_root = temporary_root / "commands"
            command_root.mkdir()
            log_path = temporary_root / "commands.log"
            for command in ("gh", "git", "hermes"):
                (command_root / f"{command}.cmd").write_text(
                    "@echo off\r\n"
                    "echo %~n0 %*>> \"%INSTALL_TEST_LOG%\"\r\n"
                    "exit /b 0\r\n",
                    encoding="utf-8",
                )
            environment = os.environ.copy()
            environment["INSTALL_TEST_LOG"] = str(log_path)
            environment["PATH"] = f"{command_root}{os.pathsep}{environment['PATH']}"

            def validate(profile: str) -> subprocess.CompletedProcess[str]:
                if log_path.exists():
                    log_path.unlink()
                return subprocess.run(
                    [
                        "powershell.exe",
                        "-NoLogo",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(ROOT / "scripts" / "install.ps1"),
                        "-SourceProfile",
                        profile,
                        "-HermesHome",
                        str(hermes_home),
                        "-ValidateOnly",
                    ],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

            result = validate("Work.prod")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(log_path.exists())

            for invalid_profile in (".unsafe", "work/prod", "a" * 65):
                result = validate(invalid_profile)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(log_path.exists())

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell is required")
    def test_reused_worker_profiles_require_exact_bundled_skills(self) -> None:
        workers = (
            ("prtriage", "pr-autopilot-analyzer"),
            ("prfix", "pr-autopilot-fixer"),
            ("prverify", "pr-autopilot-verifier"),
        )

        def prepare_profiles(root: Path) -> tuple[Path, Path, Path]:
            hermes_home = root / "hermes"
            hermes_home.mkdir()
            for profile, skill in workers:
                target = hermes_home / "profiles" / profile / "skills" / skill / "SKILL.md"
                target.parent.mkdir(parents=True)
                target.write_bytes((ROOT / "skills" / skill / "SKILL.md").read_bytes())
            commands = root / "commands"
            commands.mkdir()
            log_path = root / "commands.log"
            for command in ("gh", "git", "hermes"):
                (commands / f"{command}.cmd").write_text(
                    "@echo off\r\n"
                    "echo %~n0 %*>> \"%INSTALL_TEST_LOG%\"\r\n"
                    "exit /b 0\r\n",
                    encoding="utf-8",
                )
            return hermes_home, commands, log_path

        def run_install(
            hermes_home: Path,
            commands: Path,
            log_path: Path,
            *,
            validate_only: bool = True,
            what_if: bool = False,
        ) -> subprocess.CompletedProcess[str]:
            environment = os.environ.copy()
            environment["INSTALL_TEST_LOG"] = str(log_path)
            environment["PATH"] = f"{commands}{os.pathsep}{environment['PATH']}"
            arguments = [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "install.ps1"),
                "-SourceProfile",
                "default",
                "-HermesHome",
                str(hermes_home),
                "-ReuseExistingWorkerProfiles",
            ]
            if validate_only:
                arguments.append("-ValidateOnly")
            if what_if:
                arguments.append("-WhatIf")
            return subprocess.run(
                arguments,
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hermes_home, commands, log_path = prepare_profiles(root)
            result = run_install(hermes_home, commands, log_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                log_path.read_text(encoding="utf-8").splitlines(),
                [
                    "gh auth status --hostname github.com --active",
                    "gh api --hostname github.com repos/AnikaWilliams/hermes-pr-autopilot --jq .full_name",
                ],
            )

        for failure in ("missing", "different"):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                root = Path(temporary_directory)
                hermes_home, commands, log_path = prepare_profiles(root)
                target = (
                    hermes_home
                    / "profiles"
                    / "prfix"
                    / "skills"
                    / "pr-autopilot-fixer"
                    / "SKILL.md"
                )
                if failure == "missing":
                    target.unlink()
                else:
                    target.write_text("modified bundled skill\n", encoding="utf-8")

                result = run_install(hermes_home, commands, log_path)

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(log_path.exists())
                if failure == "missing":
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(
                        target.read_text(encoding="utf-8"), "modified bundled skill\n"
                    )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hermes_home, commands, log_path = prepare_profiles(root)
            target = (
                hermes_home
                / "profiles"
                / "prfix"
                / "skills"
                / "pr-autopilot-fixer"
                / "SKILL.md"
            )
            target.unlink()

            result = run_install(
                hermes_home,
                commands,
                log_path,
                validate_only=False,
                what_if=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Install skill 'pr-autopilot-fixer'", result.stdout)
            self.assertIn("setup is incomplete", result.stdout.lower())
            self.assertFalse(target.exists())
            self.assertEqual(
                log_path.read_text(encoding="utf-8").splitlines(),
                [
                    "gh auth status --hostname github.com --active",
                    "gh api --hostname github.com repos/AnikaWilliams/hermes-pr-autopilot --jq .full_name",
                ],
            )


if __name__ == "__main__":
    unittest.main()
