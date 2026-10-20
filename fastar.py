"""Fast, streaming extraction of tar archives.

Archives are read front to back exactly once, from a file or any readable binary stream, and
only the header fields that extraction needs are parsed. ``extract`` can strip a leading
directory that is only known from the archive's contents: the parent of the shallowest member
with a given name. Files are created with their final permissions.
"""

import contextlib
import errno
import gzip
import os
import shutil
import stat
import struct
import sys
from typing import IO, Dict, Iterable, Iterator, List, Optional, Tuple, Union

__version__ = "0.1.0"
__all__ = ["Error", "Member", "TarReader", "extract"]

REGTYPE = b"0"
AREGTYPE = b"\0"
LNKTYPE = b"1"
SYMTYPE = b"2"
DIRTYPE = b"5"
CONTTYPE = b"7"
_PAX_HEADER = b"x"
_SOLARIS_PAX_HEADER = b"X"
_PAX_GLOBAL_HEADER = b"g"
_GNU_LONGNAME = b"L"
_GNU_LONGLINK = b"K"
_EXTENDED = (
    _PAX_HEADER,
    _SOLARIS_PAX_HEADER,
    _PAX_GLOBAL_HEADER,
    _GNU_LONGNAME,
    _GNU_LONGLINK,
)
_DATA_TYPES = (REGTYPE, AREGTYPE, CONTTYPE)

_WINDOWS = sys.platform == "win32"
_BLOCK = 512
_EOF_BLOCK = bytes(_BLOCK)
_CHUNK = 64 * 1024
#: Pax and GNU long name headers are read into memory: limit their size
_MAX_EXTENDED = 1024 * 1024


class Error(ValueError):
    """The archive is invalid, or cannot be extracted safely."""


class Member:
    """A member of a tar archive, with only the fields that extraction needs."""

    __slots__ = ("linkname", "mode", "name", "size", "type")

    def __init__(
        self, name: str, type: bytes, mode: int, size: int, linkname: str
    ) -> None:
        self.name = name
        self.type = type
        self.mode = mode
        self.size = size
        self.linkname = linkname

    def isreg(self) -> bool:
        return self.type in _DATA_TYPES

    def isdir(self) -> bool:
        return self.type == DIRTYPE

    def issym(self) -> bool:
        return self.type == SYMTYPE

    def islnk(self) -> bool:
        return self.type == LNKTYPE

    def __repr__(self) -> str:
        return f"Member({self.name!r}, type={self.type!r}, mode={oct(self.mode)}, size={self.size})"


def _str(field: bytes) -> str:
    return field.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")


def _int(field: bytes) -> int:
    """A numeric header field: NUL or space terminated octal, or base-256 (GNU)."""
    if field[0] == 0o200:
        return int.from_bytes(field[1:], "big")
    elif field[0] == 0o377:
        raise Error("Archive contains a negative number")
    digits = field.split(b"\0", 1)[0].strip()
    try:
        return int(digits, 8) if digits else 0
    except ValueError:
        raise Error(f"Archive contains an invalid number {digits!r}") from None


def _checksum_ok(header: bytes) -> bool:
    expected = _int(header[148:156])
    if expected == sum(header[:148]) + sum(header[156:]) + 256:
        return True
    # Some old tar implementations summed signed chars
    signed = struct.unpack("148b8x356b", header)
    return expected == sum(signed) + 256


def _pax_records(data: bytes) -> Dict[str, str]:
    """Parse the ``<length> <key>=<value>\\n`` records of a pax extended header."""
    records = {}
    pos = 0
    while pos < len(data) and data[pos] != 0:
        try:
            space = data.index(b" ", pos)
            end = pos + int(data[pos:space])
        except ValueError:
            raise Error("Archive contains an invalid pax header") from None
        if end <= space or end > len(data) or data[end - 1] != 0x0A:
            raise Error("Archive contains an invalid pax header")
        key, _, value = data[space + 1 : end - 1].partition(b"=")
        records[key.decode("utf-8", "surrogateescape")] = value.decode(
            "utf-8", "surrogateescape"
        )
        pos = end
    return records


def _fill(stream: IO[bytes], size: int) -> bytes:
    """Read size bytes, or fewer at the end of the stream."""
    data = stream.read(size)
    while 0 < len(data) < size:
        more = stream.read(size - len(data))
        if not more:
            break
        data += more
    return data


class TarReader:
    """Reads the members of a tar archive front to back from a binary stream, without seeking.

    Parses ustar headers, pax extended headers and GNU long names, and verifies header
    checksums. Iterate for members, and read the data of the current regular file with
    ``chunks()``; data that is not read is skipped."""

    def __init__(self, stream: IO[bytes]) -> None:
        self.stream = stream
        #: Unread data of the current member
        self.remaining = 0

    def _read(self, size: int) -> bytes:
        data = _fill(self.stream, size)
        if len(data) < size:
            raise Error("Archive is truncated")
        return data

    def _header(self) -> Optional[bytes]:
        header = _fill(self.stream, _BLOCK)
        if header in (b"", _EOF_BLOCK):
            return None
        if len(header) < _BLOCK:
            raise Error("Archive is truncated")
        if not _checksum_ok(header):
            raise Error("Archive contains a header with an invalid checksum")
        return header

    def chunks(self, size: int = _CHUNK) -> Iterator[bytes]:
        """Yield the unread data of the current member."""
        while self.remaining:
            chunk = self._read(min(size, self.remaining))
            self.remaining -= len(chunk)
            yield chunk

    def __iter__(self) -> Iterator[Member]:
        global_pax: Dict[str, str] = {}
        padding = 0
        while True:
            for _ in self.chunks():
                pass
            self._read(padding)

            pax: Dict[str, str] = {}
            while True:
                header = self._header()
                if header is None:
                    return
                type = header[156:157]
                if type not in _EXTENDED:
                    break
                size = _int(header[124:136])
                if size > _MAX_EXTENDED:
                    raise Error(f"Archive contains an extended header of {size} bytes")
                data = self._read(size)
                self._read(-size % _BLOCK)
                if type == _PAX_GLOBAL_HEADER:
                    global_pax.update(_pax_records(data))
                elif type in (_PAX_HEADER, _SOLARIS_PAX_HEADER):
                    pax.update(_pax_records(data))
                else:
                    pax["path" if type == _GNU_LONGNAME else "linkpath"] = _str(data)
            pax = {**global_pax, **pax}

            name = _str(header[0:100])
            if header[257:263] == b"ustar\0":
                prefix = _str(header[345:500])
                if prefix:
                    name = f"{prefix}/{name}"
            if type == AREGTYPE and name.endswith("/"):
                type = DIRTYPE
            if type == b"S" or any(key.startswith("GNU.sparse.") for key in pax):
                raise Error(
                    f"Archive contains a sparse file {name}, which is not supported"
                )

            name = pax.get("path", name)
            if type == DIRTYPE:
                name = name.rstrip("/") or name
            member = Member(
                name,
                type,
                _int(header[100:108]),
                int(pax["size"]) if "size" in pax else _int(header[124:136]),
                pax.get("linkpath", _str(header[157:257])),
            )

            # Like tarfile, only regular files have data
            self.remaining = member.size if type in _DATA_TYPES else 0
            padding = -self.remaining % _BLOCK
            yield member


class _Prepend:
    """A stream that first returns ``head`` and then reads from ``stream``."""

    def __init__(self, head: bytes, stream: IO[bytes]) -> None:
        self.head = head
        self.stream = stream

    def read(self, size: int = -1) -> bytes:
        if not self.head:
            return self.stream.read(size)
        if size < 0:
            data, self.head = self.head + self.stream.read(), b""
        else:
            data, self.head = self.head[:size], self.head[size:]
            if len(data) < size:
                data += self.stream.read(size - len(data))
        return data


@contextlib.contextmanager
def _open_archive(
    src: Union[str, "os.PathLike[str]", IO[bytes]],
) -> Iterator[IO[bytes]]:
    """Open a path or a binary file object as a stream of uncompressed tar data, which is
    detected from the first bytes. Use as a context manager; file objects are not closed."""
    file = open(src, "rb") if isinstance(src, (str, os.PathLike)) else None  # noqa: SIM115
    try:
        stream = file or src
        magic = _fill(stream, 2)  # type: ignore[arg-type]
        head = _Prepend(magic, stream)  # type: ignore[arg-type]
        yield gzip.GzipFile(fileobj=head, mode="rb") if magic == b"\x1f\x8b" else head  # type: ignore
    finally:
        if file is not None:
            file.close()


def _common(a: List[str], b: List[str]) -> int:
    """The number of leading names a and b have in common."""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _parts(name: str) -> List[str]:
    """Split a member name into path components, rejecting names that escape the destination."""
    parts = [p for p in name.split("/") if p and p != "."]
    if (
        name.startswith("/")
        or ".." in parts
        or _WINDOWS
        and any("\\" in p or ":" in p for p in parts)
    ):
        raise Error(f"Archive contains unsafe path {name}")
    return parts


def _resolve(parts: List[str], linkname: str) -> Optional[List[str]]:
    """The member a relative symlink at parts points to, or None if it points outside."""
    if linkname.startswith("/"):
        return None
    target = parts[:-1]
    for part in linkname.split("/"):
        if part == "..":
            if not target:
                return None
            target = target[:-1]
        elif part and part != ".":
            target = target + [part]
    return target


#: Whether entries are created relative to open dir file descriptors. Otherwise, as on Windows,
#: they are created by (extended-length) path.
_DIR_FD = {os.open, os.mkdir, os.symlink, os.link, os.rename} <= os.supports_dir_fd
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)
# os.open makes file descriptors non-inheritable, so O_CLOEXEC is implied
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _O_NOFOLLOW
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY

#: An open dir: a file descriptor, or a path where dir file descriptors are not supported
Dir = Union[int, str]


def _at(parent: Dir, name: str) -> Tuple[str, Optional[int]]:
    """The path and dir_fd arguments for name in parent."""
    if isinstance(parent, int):
        return name, parent
    return os.path.join(parent, name), None


def _open_dir(parent: Dir, name: str) -> Dir:
    """Open the dir name in parent, without following symlinks."""
    if isinstance(parent, int):
        try:
            return os.open(name, _DIR_FLAGS, dir_fd=parent)
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.ENOTDIR):
                raise Error(
                    f"Archive writes through {name}, which is not a directory"
                ) from e
            raise
    path = os.path.join(parent, name)
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        raise Error(f"Archive writes through {name}, which is not a directory")
    return path


def _close(d: Dir) -> None:
    if isinstance(d, int):
        os.close(d)


class _Extractor:
    """Extracts members in a single pass, each to its full archive name in a temporary dir.

    Entries are created relative to open dir file descriptors, so paths are never longer than
    a single name, and nothing is written through a symlink, even one swapped in concurrently.
    The open dirs are a stack of the ancestors of the current member, which for the depth-first
    order of tar archives changes little from one member to the next.

    The archive name of the final root is only known after the last member. Validation only
    needs ``root``, the deepest dir containing every member that is not a dir leading up to
    it. State is bounded by the depth of the archive."""

    def __init__(
        self,
        dest: str,
        root_marker: Optional[str],
        file_mode: int,
        dir_mode: int,
        gid: Optional[int],
    ) -> None:
        self.file_mode = file_mode
        self.dir_mode = dir_mode
        self.gid = gid
        self.root: List[str] = []
        self.descending = True
        self.marker_name = root_marker
        #: The shallowest member with the marker name
        self.marker: Optional[List[str]] = None
        #: Symlinks that could not be created, to be copies of targets extracted later
        self.links: List[Tuple[List[str], List[str]]] = []
        #: Open dirs below tmp, and their names
        self.names: List[str] = []
        self.dirs: List[Dir] = []

        dest = os.path.abspath(dest)
        if _WINDOWS and not dest.startswith("\\\\?\\"):
            # an extended-length path, which may exceed MAX_PATH
            dest = "\\\\?\\" + dest
        self.dest: Dir = os.open(dest, _DIR_FLAGS) if _DIR_FD else dest
        while True:
            tmp_name = f".fastar-{os.urandom(4).hex()}"
            path, dir_fd = _at(self.dest, tmp_name)
            try:
                os.mkdir(path, 0o700, dir_fd=dir_fd)
                break
            except FileExistsError:
                pass
        self.tmp_path = os.path.join(dest, tmp_name)
        self.tmp = _open_dir(self.dest, tmp_name)

        # Find out once what the filesystem does to new entries: mode bits the umask or default
        # ACLs strip, special bits it does not set, and the group they get.
        path, dir_fd = _at(self.tmp, "probe")
        os.close(os.open(path, _FILE_FLAGS, file_mode, dir_fd=dir_fd))
        s = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        os.unlink(path, dir_fd=dir_fd)
        self.chown = gid is not None and s.st_gid != gid
        self.chmod_files = not _WINDOWS and stat.S_IMODE(s.st_mode) != file_mode
        os.mkdir(path, dir_mode, dir_fd=dir_fd)
        s = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        os.rmdir(path, dir_fd=dir_fd)
        self.chmod_dirs = not _WINDOWS and stat.S_IMODE(s.st_mode) != dir_mode

    def close(self) -> None:
        for d in (*self.dirs, self.tmp, self.dest):
            _close(d)
        # rmtree walks with dir file descriptors where supported, so depth is no problem
        shutil.rmtree(self.tmp_path, ignore_errors=True)

    def _attrs(self, fd: Dir, mode: int, chmod: bool) -> None:
        """Set the group and mode of an open file or dir, which never follows a symlink."""
        # chown before chmod, since chown may clear set-user-ID and set-group-ID bits
        if self.chown:
            os.fchown(fd, -1, self.gid)  # type: ignore
        if chmod:
            os.fchmod(fd, mode)  # type: ignore

    def _dir(self, parts: List[str]) -> Dir:
        """Open tmp/parts, creating missing dirs, and keep its ancestors open."""
        common = _common(self.names, parts)
        for d in self.dirs[common:]:
            _close(d)
        del self.names[common:], self.dirs[common:]
        for name in parts[common:]:
            parent = self.dirs[-1] if self.dirs else self.tmp
            try:
                d = _open_dir(parent, name)
            except FileNotFoundError:
                path, dir_fd = _at(parent, name)
                os.mkdir(path, self.dir_mode, dir_fd=dir_fd)
                d = _open_dir(parent, name)
                self._attrs(d, self.dir_mode, self.chmod_dirs)
            self.names.append(name)
            self.dirs.append(d)
        return self.dirs[-1] if self.dirs else self.tmp

    def _file(self, parent: Dir, name: str, mode: int, chunks: Iterable[bytes]) -> None:
        path, dir_fd = _at(parent, name)
        fd = os.open(path, _FILE_FLAGS, mode, dir_fd=dir_fd)
        try:
            for data in chunks:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
            self._attrs(fd, mode, self.chmod_files)
        finally:
            os.close(fd)

    def _track(self, member: Member, parts: List[str]) -> None:
        """Update root, which validation needs, and marker."""
        if parts[-1:] == [self.marker_name] and (
            self.marker is None or len(parts) < len(self.marker)
        ):
            self.marker = parts
        common = _common(parts, self.root)
        if common == len(parts):
            if not member.isdir():
                raise Error(
                    f"Archive contains non-directory {member.name} where a directory is"
                )
        elif common < len(self.root):
            self.root = self.root[:common]
            self.descending = False
        elif self.descending:
            # Nothing but dirs so far: descend to the deepest dir containing this member.
            self.root = parts if member.isdir() else parts[:-1]
            self.descending = member.isdir()

    def add(self, member: Member, reader: TarReader) -> None:
        parts = _parts(member.name)
        self._track(member, parts)
        if not parts:
            return
        if member.isdir():
            self._dir(parts)
            return
        parent = self._dir(parts[:-1])
        path, dir_fd = _at(parent, parts[-1])
        if member.isreg():
            # Like chmod +X: only executable if the member is executable by its owner
            mode = self.file_mode if member.mode & 0o100 else self.file_mode & ~0o111
            self._file(parent, parts[-1], mode, reader.chunks())
        elif member.issym():
            try:
                os.symlink(member.linkname, path, dir_fd=dir_fd)
            except FileExistsError:
                raise
            except OSError:
                # Like tarfile, copy the target instead where symlinks are not supported.
                target = _resolve(parts, member.linkname)
                if target is not None and not self._copy(target, parts):
                    self.links.append((target, parts))
                return
            if self.chown:
                os.chown(path, -1, self.gid, dir_fd=dir_fd, follow_symlinks=False)  # type: ignore
        elif member.islnk():
            if not self._copy(_parts(member.linkname), parts, link=True):
                raise Error(
                    f"Archive hardlinks {member.linkname}, which is not extracted"
                )
        else:
            raise Error(
                f"Archive contains {member.name} of unsupported type {member.type!r}"
            )

    def _open_chain(self, parts: List[str]) -> Dir:
        """Open tmp/parts without creating anything, independently of the stack."""
        d = os.dup(self.tmp) if isinstance(self.tmp, int) else self.tmp
        for name in parts:
            try:
                child = _open_dir(d, name)
            finally:
                _close(d)
            d = child
        return d

    def _copy(self, source: List[str], dest: List[str], link: bool = False) -> bool:
        """Hardlink or copy an extracted entry to dest, and return whether it was extracted."""
        try:
            source_parent = self._open_chain(source[:-1])
        except (FileNotFoundError, Error):
            return False
        try:
            path, dir_fd = _at(source_parent, source[-1])
            try:
                s = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if stat.S_ISDIR(s.st_mode):
                self._dir(dest)
                return True
            parent = self._dir(dest[:-1])
            if link:
                dest_path, dest_fd = _at(parent, dest[-1])
                try:
                    os.link(
                        path,
                        dest_path,
                        src_dir_fd=dir_fd,
                        dst_dir_fd=dest_fd,
                        follow_symlinks=False,
                    )
                    return True
                except FileExistsError:
                    raise
                except OSError:
                    pass  # like tarfile, copy where hardlinks are not supported
            if not stat.S_ISREG(s.st_mode):
                return False
            src = os.open(path, os.O_RDONLY | _O_BINARY, dir_fd=dir_fd)
            try:
                chunks = iter(lambda: os.read(src, _CHUNK), b"")
                self._file(parent, dest[-1], stat.S_IMODE(s.st_mode), chunks)
            finally:
                os.close(src)
            return True
        finally:
            _close(source_parent)

    def finish(self) -> str:
        """Move the contents of the final root into dest, and return its archive name."""
        root: List[str] = []
        if self.marker_name is not None:
            if self.marker is None:
                raise Error(f"Archive contains no member named {self.marker_name}")
            root = self.marker[:-1]
        if self.root[: len(root)] != root:
            raise Error(f"Archive contains members outside of {'/'.join(root) or '.'}")

        for source, dest in self.links:
            self._copy(source, dest)

        src = self._dir(root)
        for name in os.listdir(src):
            path, dir_fd = _at(src, name)
            new_path, new_dir_fd = _at(self.dest, name)
            os.rename(path, new_path, src_dir_fd=dir_fd, dst_dir_fd=new_dir_fd)
        return "/".join(root)


def extract(
    src: Union[str, "os.PathLike[str]", IO[bytes]],
    dest: str,
    *,
    root_marker: Optional[str] = None,
    file_mode: int = 0o755,
    dir_mode: int = 0o755,
    gid: Optional[int] = None,
) -> str:
    """Extract the tar archive ``src``, a path or a binary file object that is read front to
    back once, into ``dest``, which is created if it does not exist and should be empty.

    With ``root_marker``, only the contents of the parent of the shallowest member with that
    name are extracted, and the archive name of that parent is returned. Other members must be
    in it, except for the dirs leading up to it. Without, everything is extracted and ``""`` is
    returned. No path longer than a single name is passed to the operating system, and nothing
    is written through a symlink.

    Files are created with ``file_mode``, without executable bits if the member is not
    executable by its owner, and dirs with ``dir_mode``; with ``gid``, both get that group.
    Timestamps and ownership in the archive are not restored. Members must be regular files,
    dirs, symlinks or hardlinks, and cannot be absolute or contain ``..``. Where links cannot
    be created, their targets are copied."""
    if gid is not None and _WINDOWS:
        raise ValueError("gid is not supported on Windows")
    os.makedirs(dest, exist_ok=True)
    extractor = _Extractor(dest, root_marker, file_mode, dir_mode, gid)
    try:
        with _open_archive(src) as stream:
            reader = TarReader(stream)
            for member in reader:
                extractor.add(member, reader)
        return extractor.finish()
    finally:
        extractor.close()
