"""Reject credentials and private machine paths in tracked release files."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISALLOWED_RELATIVE_PATHS = {
    "config.json",
}
DISALLOWED_ROOT_DIRECTORIES = {"repos", "worktrees"}
DISALLOWED_PARTS = {".secrets"}
DISALLOWED_SUFFIXES = {".key", ".log", ".p12", ".pem", ".pfx"}
DISALLOWED_DATABASE_NAMES = (".db", ".sqlite", ".sqlite3")
REPARSE_TAG_NAME_SURROGATE = 0x20000000
PATTERNS = {
    "GitHub token": re.compile("gh" + r"[pousr]_[A-Za-z0-9]{20,}"),
    "GitHub fine-grained token": re.compile("github" + r"_pat_[A-Za-z0-9_]{20,}"),
    "OpenAI-style key": re.compile("s" + r"k-[A-Za-z0-9_-]{20,}"),
    "AWS access key": re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    "Slack token": re.compile("x" + r"(?:ox[baprs]|app)-[A-Za-z0-9-]{10,}"),
    "private-key block": re.compile(
        "BEGIN " + r"(?:ENCRYPTED |RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY(?: BLOCK)?"
    ),
    "Windows user path": re.compile(
        r"[A-Za-z]:[/\\]Users[/\\][^/\\\s]+", re.IGNORECASE
    ),
    "macOS user path": re.compile(
        "/Us" + r"ers/[A-Za-z0-9_][A-Za-z0-9_.-]*", re.IGNORECASE
    ),
    "Unix root path": re.compile("/ro" + r"ot(?:/|$)"),
    "Unix user path": re.compile("/ho" + r"me/[^/\s]+"),
}


class SymlinkReadError(Exception):
    """The scanner could not read tracked link text."""


class WorkingTreePathError(Exception):
    """The scanner could not inspect a tracked working-tree path."""


def is_disallowed_environment_file(path: Path) -> bool:
    name = path.name.casefold()
    return name == ".env" or (
        name.startswith(".env.") and name != ".env.example"
    )


def is_disallowed_database_file(path: Path) -> bool:
    name = path.name.casefold()
    return any(
        name.endswith(database_name) or f"{database_name}-" in name
        for database_name in DISALLOWED_DATABASE_NAMES
    )


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode() for item in result.stdout.split(b"\0") if item]


def indexed_file_content(relative: Path) -> bytes:
    result = subprocess.run(
        ["git", "show", f":{relative.as_posix()}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return result.stdout


def is_unsafe_reparse_point(path_status: os.stat_result) -> bool:
    reparse_tag = getattr(path_status, "st_reparse_tag", None)
    if not isinstance(reparse_tag, int) or reparse_tag == 0:
        return True
    return bool(reparse_tag & REPARSE_TAG_NAME_SURROGATE)


def first_link_or_reparse_path(relative: Path) -> tuple[Path, bool] | None:
    current = ROOT
    reparse_point_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for part in relative.parts:
        current = current / part
        try:
            path_status = os.lstat(current)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise WorkingTreePathError from error
        is_symlink = stat.S_ISLNK(path_status.st_mode)
        is_reparse_point = bool(
            getattr(path_status, "st_file_attributes", 0) & reparse_point_attribute
        )
        if is_symlink or (
            is_reparse_point and is_unsafe_reparse_point(path_status)
        ):
            return current, is_symlink
    return None


def contents_to_scan(path: Path, relative: Path) -> tuple[tuple[bytes, ...], bool]:
    indexed_content = indexed_file_content(relative)
    link_or_reparse_path = first_link_or_reparse_path(relative)
    if link_or_reparse_path:
        unsafe_path, is_symlink = link_or_reparse_path
        if unsafe_path != path or not is_symlink:
            return (indexed_content,), True
        try:
            working_tree_content = os.fsencode(os.readlink(path))
        except OSError as error:
            raise SymlinkReadError from error
        if working_tree_content == indexed_content:
            return (indexed_content,), False
        return (indexed_content, working_tree_content), False
    try:
        working_tree_content = path.read_bytes()
    except FileNotFoundError:
        return (indexed_content,), False
    if working_tree_content == indexed_content:
        return (indexed_content,), False
    return (indexed_content, working_tree_content), False


def read_scannable_text(content: bytes) -> str:
    if content.startswith(b"\xff\xfe") or content.startswith(b"\xfe\xff"):
        return content.decode("utf-16")
    return content.decode("utf-8")


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT)
        relative_posix = relative.as_posix()
        normalized_relative_posix = relative_posix.casefold()
        normalized_root_part = relative.parts[0].casefold()
        normalized_parts = {part.casefold() for part in relative.parts}
        if (
            normalized_relative_posix in DISALLOWED_RELATIVE_PATHS
            or normalized_root_part in DISALLOWED_ROOT_DIRECTORIES
            or DISALLOWED_PARTS.intersection(normalized_parts)
            or is_disallowed_environment_file(relative)
            or is_disallowed_database_file(relative)
            or relative.name.casefold() in DISALLOWED_SUFFIXES
            or relative.suffix.casefold() in DISALLOWED_SUFFIXES
        ):
            findings.append(f"disallowed tracked path: {relative_posix}")
            continue

        try:
            contents, unsafe_working_tree_path = contents_to_scan(path, relative)
        except SymlinkReadError:
            findings.append(f"unreadable tracked symlink: {relative_posix}")
            continue
        except WorkingTreePathError:
            findings.append(f"unreadable tracked path: {relative_posix}")
            continue

        for content in contents:
            try:
                text = read_scannable_text(content)
            except UnicodeDecodeError:
                continue

            for label, pattern in PATTERNS.items():
                match = pattern.search(text)
                if match:
                    line = text.count("\n", 0, match.start()) + 1
                    finding = f"{label}: {relative_posix}:{line}"
                    if finding not in findings:
                        findings.append(finding)
        if unsafe_working_tree_path:
            findings.append(f"unsafe tracked working-tree path: {relative_posix}")

    if findings:
        print("Release secret scan failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    print("Release secret scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
