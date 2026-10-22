"""Apply bounded text proposals inside an authorized worktree.

The model only proposes content.  The controller owns this module and its
allowlist.  Callers must hold their worktree lock while taking/applying a
snapshot.  All proposals are checked before mutation; replacements are atomic
per file, not a multi-file transaction in the face of an operating-system error.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

from .policy import _PROTECTED_PATTERNS


MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 20
MAX_SNAPSHOT_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_DEVICES = {"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"}
_DEVICES.update(f"{prefix}{number}" for prefix in ("com", "lpt")
                for number in (*"123456789", "¹", "²", "³"))
# Repository metadata is never a proposed source edit, even with a broad list.
_METADATA = (".git", ".git/**", "**/.git", "**/.git/**")


def _valid_path(value: object, *, pattern: bool = False) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise ValueError("path must be a nonempty relative POSIX path")
    if any(ord(char) < 32 for char in value) or any(char in value for char in '\\:<>"|'):
        raise ValueError(f"invalid path: {value!r}")
    if not pattern and any(char in value for char in "*?[]"):
        raise ValueError(f"wildcards are not valid edit paths: {value!r}")
    for part in value.split("/"):
        if not part or part in (".", "..") or part.endswith((".", " ")):
            raise ValueError(f"invalid path component: {value!r}")
        if not (pattern and any(char in part for char in "*?[]")):
            if part.split(".", 1)[0].rstrip(" ").casefold() in _DEVICES:
                raise ValueError(f"Windows device name is not allowed: {value!r}")
    return value


def _patterns(values: object, name: str) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise ValueError(f"{name} must be a list of path patterns")
    return tuple(_valid_path(value, pattern=True) for value in values)


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _root(root: Path) -> Path:
    if not isinstance(root, Path):
        raise ValueError("root must be a Path naming a real directory")
    if ".." in root.parts:
        raise ValueError("root must not contain parent traversal")
    absolute = root.absolute()
    for path in reversed((absolute, *absolute.parents)):
        try:
            info = path.lstat()
        except FileNotFoundError as error:
            raise ValueError("root must be an existing real directory") from error
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("root and its parents must be real directories without links")
    return absolute


def _target(root: Path, relative: str) -> Path:
    path = root
    parts = relative.split("/")
    for index, part in enumerate(parts):
        path = path / part
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if _is_link(info):
            raise ValueError(f"symlink or junction is not allowed: {relative}")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"parent is not a directory: {relative}")
        if index == len(parts) - 1 and not stat.S_ISREG(info.st_mode):
            raise ValueError(f"target is not a regular file: {relative}")
    return path


def _permitted(path: str, allowed: tuple[str, ...], protected: tuple[str, ...]) -> bool:
    return (any(fnmatchcase(path, pattern) for pattern in allowed)
            and not any(fnmatchcase(path.casefold(), pattern.casefold())
                        for pattern in (*_PROTECTED_PATTERNS, *_METADATA, *protected)))


def _protected_directory(path: str, protected: tuple[str, ...]) -> bool:
    # A pattern covering every descendant permits pruning without inspecting it.
    return any(pattern.casefold() in (path.casefold(), path.casefold() + "/**")
               or fnmatchcase(path.casefold() + "/", pattern.casefold())
               for pattern in (*_PROTECTED_PATTERNS, *_METADATA, *protected))


def _read(path: Path) -> bytes | None:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes: {path.name}")
    return data


def _text(data: bytes, relative: str) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"file is not UTF-8 text: {relative}") from error
    if "\x00" in text:
        raise ValueError(f"file is not plain text: {relative}")
    return text


def _digest(data: bytes | None) -> str:
    return "" if data is None else hashlib.sha256(data).hexdigest()


def _candidates(root: Path, allowed: tuple[str, ...], protected: tuple[str, ...]):
    """Scan only literal files or subtrees selected by allowlist prefixes."""
    bases: set[str] = set()
    literal: set[str] = set()
    for pattern in allowed:
        parts = pattern.split("/")
        magic = next((index for index, part in enumerate(parts)
                      if any(char in part for char in "*?[]")), None)
        if magic is None:
            literal.add(pattern)
        else:
            bases.add("/".join(parts[:magic]))
    for relative in sorted(literal):
        if _permitted(relative, allowed, protected):
            yield relative

    def walk(directory: Path):
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            child = directory / name
            relative = child.relative_to(root).as_posix()
            try:
                _valid_path(relative)
            except ValueError:
                continue
            if _protected_directory(relative, protected):
                continue
            info = child.lstat()
            if _is_link(info):
                # Do not follow a link even to decide whether it is a directory.
                raise ValueError(f"symlink or junction is not allowed: {relative}")
            if stat.S_ISDIR(info.st_mode):
                yield from walk(child)
            elif stat.S_ISREG(info.st_mode) and _permitted(relative, allowed, protected):
                yield relative

    # Eliminate nested bases so overlapping patterns do not rescan a subtree.
    selected: list[str] = []
    for base in sorted(bases, key=lambda item: (item.count("/"), item)):
        if any(not parent or base == parent or base.startswith(parent + "/") for parent in selected):
            continue
        selected.append(base)
        if base and _protected_directory(base, protected):
            continue
        directory = root
        for part in base.split("/") if base else ():
            directory = directory / part
            try:
                info = directory.lstat()
            except FileNotFoundError:
                break
            if _is_link(info):
                raise ValueError(f"symlink or junction is not allowed: {base}")
            if not stat.S_ISDIR(info.st_mode):
                break
        else:
            yield from walk(directory)


def snapshot_files(root: Path, allowed_paths: list[str], protected_paths: list[str]) -> list[dict]:
    """Return bounded UTF-8 source snapshots, without following filesystem links.

    Missing/nontext files are omitted.  A limit violation or matching link is an
    error rather than a silently incomplete snapshot.  Only allowlist-selected
    files/subtrees are inspected; protected subtrees are pruned.
    """
    root = _root(root)
    allowed = _patterns(allowed_paths, "allowed_paths")
    protected = _patterns(protected_paths, "protected_paths")
    result = []
    seen: dict[str, str] = {}
    total = 0
    for relative in _candidates(root, allowed, protected):
        key = relative.casefold()
        if key in seen:
            if seen[key] != relative:
                raise ValueError(f"Windows case-insensitive path collision: {relative}")
            continue
        seen[key] = relative
        target = _target(root, relative)
        data = _read(target)
        if data is None:
            continue
        try:
            content = _text(data, relative)
        except ValueError:
            continue
        total += len(data)
        if len(result) >= MAX_FILES or total > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot exceeds file count or total byte limit")
        result.append({"path": relative, "sha256": _digest(data), "content": content})
    return sorted(result, key=lambda item: item["path"])


def apply_edits(root: Path, edits: list[dict], allowed_paths: list[str],
                protected_paths: list[str]) -> list[dict]:
    """Preflight every proposal, then atomically replace each changed text file.

    New files require an empty ``expected_sha256``.  Existing files require
    their raw-byte SHA-256, unless they already contain the proposed bytes
    (idempotent replay).  Validation errors write nothing.
    """
    root = _root(root)
    allowed = _patterns(allowed_paths, "allowed_paths")
    protected = _patterns(protected_paths, "protected_paths")
    if not isinstance(edits, list) or len(edits) > MAX_FILES:
        raise ValueError(f"edits must be a list of at most {MAX_FILES} items")
    planned = []
    seen: set[str] = set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"path", "expected_sha256", "content"}:
            raise ValueError("each edit requires only path, expected_sha256 and content")
        relative = _valid_path(edit["path"])
        key = relative.casefold()
        if key in seen:
            raise ValueError(f"duplicate Windows path: {relative}")
        seen.add(key)
        if not _permitted(relative, allowed, protected):
            raise ValueError(f"path is outside the allowlist or protected: {relative}")
        expected = edit["expected_sha256"]
        if not isinstance(expected, str) or (expected and not _SHA256.fullmatch(expected)):
            raise ValueError(f"invalid expected_sha256: {relative}")
        if not isinstance(edit["content"], str) or "\x00" in edit["content"]:
            raise ValueError(f"content must be plain UTF-8 text: {relative}")
        try:
            data = edit["content"].encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError(f"content must be valid UTF-8: {relative}") from error
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"content exceeds {MAX_FILE_BYTES} bytes: {relative}")
        target = _target(root, relative)
        before = _read(target)
        if before is not None:
            _text(before, relative)
        before_sha = _digest(before)
        if before != data and before_sha != expected.lower():
            raise ValueError(f"expected_sha256 mismatch: {relative}")
        planned.append((relative, target, before, data))

    staged: list[tuple[Path, Path]] = []
    receipts = []
    try:
        for relative, target, before, data in planned:
            status = "already_applied" if before == data else "applied"
            receipts.append({"path": relative, "status": status,
                             "before_sha256": _digest(before), "sha256": _digest(data)})
            if status == "already_applied":
                continue
            # Recheck the path before creating directories or temporary files.
            _target(root, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            _target(root, relative)
            descriptor, name = tempfile.mkstemp(prefix=".codex-edit-", dir=target.parent)
            temporary = Path(name)
            staged.append((temporary, target))
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if before is not None:
                os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
        # Detect changes during staging before replacing any target.
        for relative, target, before, _ in planned:
            _target(root, relative)
            if _read(target) != before:
                raise ValueError(f"file changed during preflight: {relative}")
        for temporary, target in staged:
            _target(root, target.relative_to(root).as_posix())
            os.replace(temporary, target)
        return receipts
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
