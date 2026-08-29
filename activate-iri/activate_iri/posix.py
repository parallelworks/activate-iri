"""POSIX filesystem operations rendered as shell scripts, with parsers for their output.

The IRI filesystem domain is FirecREST-shaped: each operation is equivalent to one command a
user could run over SSH. Rendering them as scripts keeps the same code path for edge mode
(local login node), federation mode (SSH), and the workflow transport.
"""
from __future__ import annotations

import base64
import re
import shlex

from app.routers.filesystem import models as fs

OPS_SIZE_LIMIT = 5 * 1024 * 1024

_LS_LINE = re.compile(
    r"^(?P<perm>[-dlbcps][rwxsStT-]{9}[.+@]?)\s+(?P<links>\d+)\s+(?P<user>\S+)\s+(?P<group>\S+)\s+"
    r"(?P<size>\d+)\s+(?P<mtime>\S+)\s+(?P<name>.*)$"
)
_TYPES = {"-": "file", "d": "directory", "l": "symlink", "b": "block", "c": "character", "p": "fifo", "s": "socket"}


def q(path: str) -> str:
    return shlex.quote(path)


_PROTECTED_ROOTS = {"/", "/home", "/scratch", "/tmp", "/data", "/opt", "/usr", "/etc", "/var", "/root", "/mnt", "/lustre", "/project", "/projects", "/work"}


def guard(path: str) -> str:
    """Refuse destructive operations on relative paths, parent references, or protected roots.

    Destructive commands run as the calling user, so the kernel enforces ownership; this guard
    stops the obvious mistakes (a stray "..", a bare "/home") before they reach the shell.
    """
    if not path or not path.strip():
        raise ValueError("path is required")
    p = path.strip()
    if not p.startswith("/"):
        raise ValueError("path must be absolute")
    parts = [seg for seg in p.split("/") if seg]
    if any(seg == ".." for seg in parts) or "\n" in p or "\0" in p:
        raise ValueError("path may not contain parent references or control characters")
    normalized = "/" + "/".join(parts)
    if normalized in _PROTECTED_ROOTS or len(parts) < 3 and normalized.startswith(("/home/", "/scratch/", "/data/", "/project", "/work/")):
        raise ValueError(f"refusing to operate on protected path {normalized}")
    return p


def parse_ls_line(line: str) -> fs.File | None:
    m = _LS_LINE.match(line.rstrip())
    if not m:
        return None
    name, target = m.group("name"), None
    if m.group("perm").startswith("l") and " -> " in name:
        name, target = name.split(" -> ", 1)
    return fs.File(
        name=name, type=_TYPES.get(m.group("perm")[0], "unknown"), link_target=target, user=m.group("user"),
        group=m.group("group"), permissions=m.group("perm")[1:10], last_modified=m.group("mtime"), size=m.group("size"),
    )


def parse_ls(stdout: str) -> list[fs.File]:
    out = []
    for line in stdout.splitlines():
        if line.startswith("total ") or not line.strip() or line.endswith(":"):
            continue
        entry = parse_ls_line(line)
        if entry and entry.name not in (".", ".."):
            out.append(entry)
    return out


TIME_STYLE = "--time-style=+%Y-%m-%dT%H:%M:%S%z"


def ls_script(path: str, show_hidden=False, numeric_uid=False, recursive=False, dereference=False) -> str:
    flags = "-la" if show_hidden else "-l"
    flags += "n" if numeric_uid else ""
    flags += "R" if recursive else ""
    flags += "L" if dereference else ""
    return f"ls {flags} {TIME_STYLE} -- {q(path)}"


def stat_entry_script(path: str) -> str:
    """One-line listing of a single path, used to build File models after mutations."""
    return f"ls -ld {TIME_STYLE} -- {q(path)}"


def parse_single(stdout: str) -> fs.File | None:
    for line in stdout.splitlines():
        entry = parse_ls_line(line)
        if entry:
            entry.name = entry.name.rsplit("/", 1)[-1] or entry.name
            return entry
    return None


def head_script(path, file_bytes=None, lines=None, skip_trailing=False) -> str:
    if lines:
        return f"head -n {'-' if skip_trailing else ''}{int(lines)} -- {q(path)}"
    n = int(file_bytes or 1024)
    return f"head -c {'-' if skip_trailing else ''}{n} -- {q(path)}"


def tail_script(path, file_bytes=None, lines=None, skip_heading=False) -> str:
    if lines:
        return f"tail -n {'+' if skip_heading else ''}{int(lines) + (1 if skip_heading else 0)} -- {q(path)}"
    n = int(file_bytes or 1024)
    return f"tail -c {'+' if skip_heading else ''}{n + (1 if skip_heading else 0)} -- {q(path)}"


def view_script(path, size: int, offset: int) -> str:
    size = min(int(size or OPS_SIZE_LIMIT), OPS_SIZE_LIMIT)
    return f"tail -c +{int(offset) + 1} -- {q(path)} | head -c {size}"


def checksum_script(path) -> str:
    return f"sha256sum -- {q(path)} | cut -d' ' -f1"


def file_script(path) -> str:
    return f"file -b -- {q(path)}"


def stat_script(path, dereference=False) -> str:
    flag = "-L" if dereference else ""
    return f"stat {flag} -c '%f %i %d %h %u %g %s %X %Z %Y' -- {q(path)}"


def parse_stat(stdout: str) -> fs.FileStat:
    parts = stdout.split()
    mode_hex, ino, dev, nlink, uid, gid, size, atime, ctime, mtime = parts[:10]
    return fs.FileStat(mode=int(mode_hex, 16), ino=int(ino), dev=int(dev), nlink=int(nlink), uid=int(uid), gid=int(gid),
                       size=int(size), atime=int(atime), ctime=int(ctime), mtime=int(mtime))


def rm_script(path) -> str:
    return f"rm -rf -- {q(guard(path))}"


def mkdir_script(path, parent=False) -> str:
    return f"mkdir {'-p' if parent else ''} -- {q(path)} && {stat_entry_script(path)}"


def symlink_script(target, link_path) -> str:
    return f"ln -s -- {q(target)} {q(link_path)} && {stat_entry_script(link_path)}"


def download_script(path) -> str:
    return (f"__sz=$(stat -c %s -- {q(path)}) && [ \"$__sz\" -le {OPS_SIZE_LIMIT} ] || {{ echo 'file exceeds OPS_SIZE_LIMIT' >&2; exit 3; }}; "
            f"base64 -w0 -- {q(path)}")


def upload_script(path, content_b64: str) -> str:
    # The payload is embedded, so the script stays self-contained across every executor.
    return f"printf %s {q(content_b64)} | base64 -d > {q(path)} && {stat_entry_script(path)}"


_TAR_FLAGS = {"urn:doe-iri:compression:gzip": "z", "urn:doe-iri:compression:bzip2": "j", "urn:doe-iri:compression:xz": "J",
              "urn:doe-iri:compression:none": "", "gzip": "z", "bzip2": "j", "xz": "J", "none": ""}


def compress_script(source, target, compression: str, match_pattern=None, dereference=False) -> str:
    flag = _TAR_FLAGS.get(str(compression), "z")
    deref = "--dereference" if dereference else ""
    src = q(source)
    if match_pattern:
        return (f"cd \"$(dirname {src})\" && find \"$(basename {src})\" -regex {q(match_pattern)} -print0 "
                f"| tar {deref} -c{flag}f {q(target)} --null -T - && {stat_entry_script(target)}")
    return f"tar {deref} -c{flag}f {q(target)} -C \"$(dirname {src})\" \"$(basename {src})\" && {stat_entry_script(target)}"


def extract_script(source, target, compression: str) -> str:
    guard(target)
    flag = _TAR_FLAGS.get(str(compression), "z")
    return f"mkdir -p -- {q(target)} && tar -x{flag}f {q(source)} -C {q(target)} && {stat_entry_script(target)}"


def mv_script(source, target) -> str:
    guard(source)
    return f"mv -- {q(source)} {q(target)} && {stat_entry_script(target)}"


def cp_script(source, target, dereference=False) -> str:
    return f"cp -r {'-L' if dereference else '-P'} -- {q(source)} {q(target)} && {stat_entry_script(target)}"


def chmod_script(path, mode: str) -> str:
    if not re.fullmatch(r"[0-7]{3,4}", mode):
        raise ValueError("mode must be octal, e.g. 755")
    return f"chmod {mode} -- {q(path)} && {stat_entry_script(path)}"


def chown_script(path, owner: str = "", group: str = "") -> str:
    spec = f"{owner}:{group}" if group else owner
    return f"chown {shlex.quote(spec)} -- {q(path)} && {stat_entry_script(path)}"


def b64(content: str | bytes) -> str:
    if isinstance(content, str):
        # The framework passes upload content already base64 encoded; pass it through.
        try:
            base64.b64decode(content, validate=True)
            return content
        except Exception:  # noqa: BLE001
            return base64.b64encode(content.encode()).decode()
    return base64.b64encode(content).decode()
