"""Regression checks for the release secret scanner."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "secret_scan.py"
SPEC = importlib.util.spec_from_file_location("secret_scan", MODULE_PATH)
assert SPEC and SPEC.loader
secret_scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(secret_scan)


class SecretScanTests(unittest.TestCase):
    def scan_fixture(self, relative_path: str, content: str) -> tuple[int, str]:
        return self.scan_bytes_fixture(relative_path, content.encode("utf-8"))

    def scan_bytes_fixture(self, relative_path: str, content: bytes) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            fixture = temporary_root / relative_path
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_bytes(content)
            output = io.StringIO()

            with (
                patch.object(secret_scan, "ROOT", temporary_root),
                patch.object(secret_scan, "tracked_files", return_value=[fixture]),
                patch.object(secret_scan, "indexed_file_content", return_value=content),
                contextlib.redirect_stdout(output),
            ):
                exit_code = secret_scan.main()

        return exit_code, output.getvalue()

    def test_rejects_fine_grained_github_pat(self) -> None:
        token = "github" + "_pat_" + ("A" * 82)
        exit_code, output = self.scan_fixture("fixture.txt", token)

        self.assertEqual(exit_code, 1)
        self.assertIn("GitHub fine-grained token: fixture.txt:1", output)

    def test_rejects_classic_github_pat(self) -> None:
        token = "gh" + "p_" + ("A" * 36)
        exit_code, output = self.scan_fixture("fixture.txt", token)

        self.assertEqual(exit_code, 1)
        self.assertIn("GitHub token: fixture.txt:1", output)

    def test_rejects_temporary_aws_access_key_id(self) -> None:
        exit_code, output = self.scan_fixture("fixture.txt", "ASIA" + ("A" * 16))

        self.assertEqual(exit_code, 1)
        self.assertIn("AWS access key: fixture.txt:1", output)

    def test_rejects_dynamically_assembled_slack_app_token(self) -> None:
        token = "xa" + "pp-" + "A1b2-C3d4-E5"
        exit_code, output = self.scan_fixture("fixture.txt", token)

        self.assertEqual(exit_code, 1)
        self.assertIn("Slack token: fixture.txt:1", output)

    def test_scans_the_staged_blob_not_a_safe_working_tree_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            subprocess.run(["git", "init", "--quiet", str(temporary_root)], check=True)
            fixture = temporary_root / "fixture.txt"
            fixture.write_text("xa" + "pp-" + "A1b2-C3d4-E5", encoding="utf-8")
            subprocess.run(["git", "-C", str(temporary_root), "add", "fixture.txt"], check=True)
            fixture.write_text("SAFE_VALUE=true", encoding="utf-8")
            output = io.StringIO()

            with (
                patch.object(secret_scan, "ROOT", temporary_root),
                contextlib.redirect_stdout(output),
            ):
                exit_code = secret_scan.main()

        self.assertEqual(exit_code, 1)
        self.assertIn("Slack token: fixture.txt:1", output.getvalue())

    def test_scans_a_working_tree_secret_not_present_in_the_staged_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            subprocess.run(["git", "init", "--quiet", str(temporary_root)], check=True)
            fixture = temporary_root / "fixture.txt"
            fixture.write_text("SAFE_VALUE=true", encoding="utf-8")
            subprocess.run(["git", "-C", str(temporary_root), "add", "fixture.txt"], check=True)
            fixture.write_text("xa" + "pp-" + "A1b2-C3d4-E5", encoding="utf-8")
            output = io.StringIO()

            with (
                patch.object(secret_scan, "ROOT", temporary_root),
                contextlib.redirect_stdout(output),
            ):
                exit_code = secret_scan.main()

        self.assertEqual(exit_code, 1)
        self.assertIn("Slack token: fixture.txt:1", output.getvalue())

    def test_scans_an_alternate_history_index_alongside_the_working_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            subprocess.run(["git", "init", "--quiet", str(temporary_root)], check=True)
            fixture = temporary_root / "fixture.txt"
            fixture.write_text("xa" + "pp-" + "A1b2-C3d4-E5", encoding="utf-8")
            subprocess.run(["git", "-C", str(temporary_root), "add", "fixture.txt"], check=True)
            alternate_index = temporary_root / "history.index"
            alternate_index.write_bytes((temporary_root / ".git" / "index").read_bytes())
            fixture.write_text("SAFE_VALUE=true", encoding="utf-8")
            subprocess.run(["git", "-C", str(temporary_root), "add", "fixture.txt"], check=True)
            output = io.StringIO()

            with (
                patch.object(secret_scan, "ROOT", temporary_root),
                patch.dict("os.environ", {"GIT_INDEX_FILE": str(alternate_index)}),
                contextlib.redirect_stdout(output),
            ):
                exit_code = secret_scan.main()

        self.assertEqual(exit_code, 1)
        self.assertIn("Slack token: fixture.txt:1", output.getvalue())

    def test_rejects_secret_in_utf16_bom_file(self) -> None:
        token = "github" + "_pat_" + ("A" * 82)
        content = b"\xff\xfe" + token.encode("utf-16-le")
        exit_code, output = self.scan_bytes_fixture("fixture.txt", content)

        self.assertEqual(exit_code, 1)
        self.assertIn("GitHub fine-grained token: fixture.txt:1", output)

    def test_skips_a_tracked_binary_blob(self) -> None:
        exit_code, output = self.scan_bytes_fixture("fixture.bin", b"\x80\x81\x82\x00")

        self.assertEqual(exit_code, 0)
        self.assertIn("Release secret scan passed.", output)

    def test_rejects_environment_file_variants_except_example(self) -> None:
        exit_code, output = self.scan_fixture(".env.production", "SAFE_VALUE=true")

        self.assertEqual(exit_code, 1)
        self.assertIn("disallowed tracked path: .env.production", output)

        exit_code, output = self.scan_fixture(".env.example", "SAFE_VALUE=true")

        self.assertEqual(exit_code, 0)
        self.assertIn("Release secret scan passed.", output)

    def test_rejects_only_root_machine_config_path(self) -> None:
        exit_code, output = self.scan_fixture("config.json", "machine-specific=true")

        self.assertEqual(exit_code, 1)
        self.assertIn("disallowed tracked path: config.json", output)

        exit_code, output = self.scan_fixture("dashboard/config.json", "public-dashboard=true")

        self.assertEqual(exit_code, 0)
        self.assertIn("Release secret scan passed.", output)

    def test_rejects_machine_paths_case_insensitively_and_retains_spelling(self) -> None:
        for relative_path in ("CONFIG.JSON", "nested/.Secrets/token", ".ENV", ".ENV.Local"):
            with self.subTest(relative_path=relative_path):
                exit_code, output = self.scan_fixture(relative_path, "SAFE_VALUE=true")

                self.assertEqual(exit_code, 1)
                self.assertIn(f"disallowed tracked path: {relative_path}", output)

        exit_code, output = self.scan_fixture(".ENV.Example", "SAFE_VALUE=true")

        self.assertEqual(exit_code, 0)
        self.assertIn("Release secret scan passed.", output)

    def test_rejects_encrypted_private_key_header(self) -> None:
        header = "BEGIN " + "ENCRYPTED PRIVATE KEY"
        exit_code, output = self.scan_fixture("fixture.txt", header)

        self.assertEqual(exit_code, 1)
        self.assertIn("private-key block: fixture.txt:1", output)

    def test_rejects_openpgp_private_key_block(self) -> None:
        header = "BEGIN " + "PGP PRIVATE KEY BLOCK"
        exit_code, output = self.scan_fixture("fixture.asc", header)

        self.assertEqual(exit_code, 1)
        self.assertIn("private-key block: fixture.asc:1", output)

    def test_rejects_private_key_file_suffixes_before_binary_decoding(self) -> None:
        for relative_path in (".key", ".PEM", "server.key", "Keys/CLIENT.PEM"):
            with self.subTest(relative_path=relative_path):
                exit_code, output = self.scan_bytes_fixture(
                    relative_path, b"\x80\x81\x82\x00"
                )

                self.assertEqual(exit_code, 1)
                self.assertIn(f"disallowed tracked path: {relative_path}", output)

    def test_rejects_pkcs12_file_suffixes_before_binary_decoding(self) -> None:
        for relative_path in (".PFX", ".p12", "certs/CLIENT.P12", "keys/client.pfx"):
            with self.subTest(relative_path=relative_path):
                exit_code, output = self.scan_bytes_fixture(
                    relative_path, b"\x80\x81\x82\x00"
                )

                self.assertEqual(exit_code, 1)
                self.assertIn(f"disallowed tracked path: {relative_path}", output)

    def test_rejects_tracked_runtime_database_paths_before_binary_decoding(self) -> None:
        for relative_path in (
            "state.DB",
            "runtime/state.db-WAL",
            "worker.SQLITE",
            "runtime/worker.sqlite-SHM",
            "cache/STATE.SQLITE3",
            "cache/state.sqlite3-JOURNAL",
        ):
            with self.subTest(relative_path=relative_path):
                exit_code, output = self.scan_bytes_fixture(
                    relative_path, b"\x80\x81\x82\x00"
                )

                self.assertEqual(exit_code, 1)
                self.assertIn(f"disallowed tracked path: {relative_path}", output)

    def test_rejects_nested_case_insensitive_log_paths_before_binary_decoding(self) -> None:
        exit_code, output = self.scan_bytes_fixture("state/Worker.LOG", b"\x80\x81\x82\x00")

        self.assertEqual(exit_code, 1)
        self.assertIn("disallowed tracked path: state/Worker.LOG", output)

        exit_code, output = self.scan_bytes_fixture("state/Worker.log.bak", b"\x80\x81\x82\x00")

        self.assertEqual(exit_code, 0)
        self.assertIn("Release secret scan passed.", output)

    def test_rejects_root_runtime_cache_directories_before_binary_decoding(self) -> None:
        for relative_path in ("Repos/cache.bin", "WORKTREES/example/worker.bin"):
            with self.subTest(relative_path=relative_path):
                exit_code, output = self.scan_bytes_fixture(
                    relative_path, b"\x80\x81\x82\x00"
                )

                self.assertEqual(exit_code, 1)
                self.assertIn(f"disallowed tracked path: {relative_path}", output)

        exit_code, output = self.scan_bytes_fixture(
            "nested/repos/fixture.bin", b"\x80\x81\x82\x00"
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("Release secret scan passed.", output)

    def test_rejects_windows_user_paths_case_insensitively(self) -> None:
        machine_paths = (
            "C:" + "\\" + "users" + "\\" + "Alice" + "\\" + "private.txt",
            "c:" + "/" + "USERS" + "/" + "Alice" + "/" + "private.txt",
        )
        for machine_path in machine_paths:
            with self.subTest(machine_path=machine_path):
                exit_code, output = self.scan_fixture("fixture.txt", machine_path)

                self.assertEqual(exit_code, 1)
                self.assertIn("Windows user path: fixture.txt:1", output)

    def test_scans_a_tracked_symlink_from_its_index_text_without_reading_the_target(self) -> None:
        """A release scan must not follow a tracked link outside the repository."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            outside = temporary_root.parent / "outside-secret-target.bin"
            outside.write_bytes(b"\x80\x81\x82\x00")
            link = temporary_root / "outside-link"
            link.write_text(str(outside), encoding="utf-8")
            output = io.StringIO()

            with (
                patch.object(secret_scan, "ROOT", temporary_root),
                patch.object(secret_scan, "tracked_files", return_value=[link]),
                patch.object(secret_scan, "indexed_file_content", return_value=b"outside-target"),
                patch.object(
                    secret_scan, "first_link_or_reparse_path", return_value=(link, True)
                ),
                patch.object(secret_scan.os, "readlink", return_value="outside-target"),
                patch.object(Path, "read_bytes", side_effect=AssertionError("followed symlink")),
                contextlib.redirect_stdout(output),
            ):
                exit_code = secret_scan.main()

        self.assertEqual(exit_code, 0)
        self.assertIn("Release secret scan passed.", output.getvalue())

    def test_rejects_a_secret_in_tracked_symlink_index_text(self) -> None:
        """Link text is tracked content and must receive the normal secret scan."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            link = temporary_root / "secret-link"
            token = "gh" + "p_" + ("A" * 36)
            link.write_text(token, encoding="utf-8")
            output = io.StringIO()

            with (
                patch.object(secret_scan, "ROOT", temporary_root),
                patch.object(secret_scan, "tracked_files", return_value=[link]),
                patch.object(secret_scan, "indexed_file_content", return_value=token.encode("utf-8")),
                patch.object(
                    secret_scan, "first_link_or_reparse_path", return_value=(link, True)
                ),
                patch.object(secret_scan.os, "readlink", return_value=token),
                patch.object(Path, "read_bytes", side_effect=AssertionError("followed symlink")),
                contextlib.redirect_stdout(output),
            ):
                exit_code = secret_scan.main()

        self.assertEqual(exit_code, 1)
        self.assertIn("GitHub token: secret-link:1", output.getvalue())

    def test_scans_changed_tracked_symlink_text_without_dereferencing_its_target(self) -> None:
        """Changed link text is tracked content and must not read the target file."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            link = temporary_root / "changed-link"
            link.write_text("safe-index-link", encoding="utf-8")
            output = io.StringIO()

            with (
                patch.object(secret_scan, "ROOT", temporary_root),
                patch.object(secret_scan, "tracked_files", return_value=[link]),
                patch.object(secret_scan, "indexed_file_content", return_value=b"safe-index-link"),
                patch.object(
                    secret_scan, "first_link_or_reparse_path", return_value=(link, True)
                ),
                patch.object(
                    secret_scan.os,
                    "readlink",
                    return_value="/ro" + "ot/.config/hermes",
                ),
                patch.object(Path, "read_bytes", side_effect=AssertionError("followed symlink")),
                contextlib.redirect_stdout(output),
            ):
                exit_code = secret_scan.main()

        self.assertEqual(exit_code, 1)
        self.assertIn("Unix root path: changed-link:1", output.getvalue())

    def test_unsafe_symlinked_parent_scans_index_without_reading_the_target(self) -> None:
        """A tracked path under a link must fail closed without following it."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            relative = Path("linked-parent") / "fixture.txt"
            tracked = temporary_root / relative
            token = ("gh" + "p_" + ("A" * 36)).encode("utf-8")
            output = io.StringIO()

            with (
                patch.object(secret_scan, "ROOT", temporary_root),
                patch.object(secret_scan, "tracked_files", return_value=[tracked]),
                patch.object(secret_scan, "indexed_file_content", return_value=token),
                patch.object(
                    secret_scan,
                    "first_link_or_reparse_path",
                    return_value=(relative.parent, False),
                ) as first_link,
                patch.object(Path, "read_bytes", side_effect=AssertionError("followed linked parent")),
                contextlib.redirect_stdout(output),
            ):
                exit_code = secret_scan.main()

        self.assertEqual(exit_code, 1)
        first_link.assert_called_once_with(relative)
        self.assertIn("GitHub token: linked-parent/fixture.txt:1", output.getvalue())
        self.assertIn("unsafe tracked working-tree path: linked-parent/fixture.txt", output.getvalue())

    def test_only_name_surrogate_windows_reparse_points_are_unsafe(self) -> None:
        """A cloud placeholder is not a link, but a name-surrogate point is unsafe."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            relative = Path("placeholder.bin")
            name_surrogate = SimpleNamespace(
                st_mode=0,
                st_file_attributes=0x400,
                st_reparse_tag=0xA000000C,
            )
            cloud_placeholder = SimpleNamespace(
                st_mode=0,
                st_file_attributes=0x400,
                st_reparse_tag=0x9000001A,
            )
            with (
                patch.object(secret_scan, "ROOT", temporary_root),
                patch.object(secret_scan.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, create=True),
                patch.object(secret_scan.os, "lstat", return_value=name_surrogate),
            ):
                self.assertEqual(
                    secret_scan.first_link_or_reparse_path(relative),
                    (temporary_root / relative, False),
                )
                self.assertTrue(secret_scan.is_unsafe_reparse_point(name_surrogate))

            with (
                patch.object(secret_scan, "ROOT", temporary_root),
                patch.object(secret_scan.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, create=True),
                patch.object(secret_scan.os, "lstat", return_value=cloud_placeholder),
            ):
                self.assertIsNone(secret_scan.first_link_or_reparse_path(relative))
                self.assertFalse(secret_scan.is_unsafe_reparse_point(cloud_placeholder))

    def test_rejects_unix_root_home_paths_but_not_a_prefix_match(self) -> None:
        private_path = "/ro" + "ot/.config/hermes"
        exit_code, output = self.scan_fixture("fixture.txt", private_path)

        self.assertEqual(exit_code, 1)
        self.assertIn("Unix root path: fixture.txt:1", output)

        prefix_path = "/ro" + "oted/.config/hermes"
        exit_code, output = self.scan_fixture("fixture.txt", prefix_path)

        self.assertEqual(exit_code, 0)
        self.assertIn("Release secret scan passed.", output)

    def test_rejects_macos_user_paths_case_insensitively(self) -> None:
        machine_path = "/uSe" + "Rs/Alice/private.txt"

        exit_code, output = self.scan_fixture("fixture.txt", machine_path)

        self.assertEqual(exit_code, 1)
        self.assertIn("macOS user path: fixture.txt:1", output)

        placeholder = "/Us" + "ers/<name>/project"
        exit_code, output = self.scan_fixture("fixture.txt", placeholder)

        self.assertEqual(exit_code, 0)
        self.assertIn("Release secret scan passed.", output)


if __name__ == "__main__":
    unittest.main()
