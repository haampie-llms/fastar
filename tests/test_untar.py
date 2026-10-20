import collections
import errno
import gzip
import io
import os
import random
import shutil
import stat
import subprocess
import sys
import tarfile

import pytest

import untar

FORMATS = [tarfile.USTAR_FORMAT, tarfile.GNU_FORMAT, tarfile.PAX_FORMAT]
needs_symlinks = pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")


def make_tar(members, format=tarfile.PAX_FORMAT, compress=False) -> bytes:
    """Build an archive from (name, kind, payload, mode) members, kind being one of 'd', 'f',
    's' (payload is the target) and 'h' (payload is the member it links to)."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=format) as tar:
        for name, kind, payload, mode in members:
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.type = {
                "d": tarfile.DIRTYPE,
                "f": tarfile.REGTYPE,
                "s": tarfile.SYMTYPE,
            }.get(kind, tarfile.LNKTYPE)
            if kind == "f":
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            else:
                info.linkname = payload or ""
                tar.addfile(info)
    data = out.getvalue()
    return gzip.compress(data) if compress else data


def snapshot(root):
    """The tree below root: types, normalized modes, contents, link targets, hardlinks."""
    out = {}
    inodes = collections.defaultdict(set)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            s = os.lstat(path)
            if stat.S_ISLNK(s.st_mode):
                out[rel] = ("s", os.readlink(path))
            elif stat.S_ISDIR(s.st_mode):
                out[rel] = ("d",)
            else:
                with open(path, "rb") as f:
                    out[rel] = ("f", bool(s.st_mode & 0o100), f.read())
                inodes[(s.st_dev, s.st_ino)].add(rel)
    out["<hardlinks>"] = sorted(
        sorted(group) for group in inodes.values() if len(group) > 1
    )
    return out


def tarfile_extract(data: bytes, dest) -> None:
    """The oracle: tarfile, trusting the archive."""
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extraction_filter = lambda member, path: member
        tar.extractall(dest)


SAMPLE = [
    ("pkg", "d", None, 0o755),
    ("pkg/bin", "d", None, 0o755),
    ("pkg/bin/app", "f", b"#!/bin/sh\n", 0o755),
    ("pkg/bin/link", "s", "app", 0o777),
    ("pkg/bin/abs", "s", "/usr/bin/env", 0o777),
    ("pkg/lib", "d", None, 0o755),
    ("pkg/lib/libfoo.so.1", "f", os.urandom(200_000), 0o644),
    ("pkg/lib/libfoo.so", "s", "libfoo.so.1", 0o777),
    ("pkg/lib/libfoo.a", "h", "pkg/lib/libfoo.so.1", 0o644),
    ("pkg/share/empty", "f", b"", 0o600),
    ("pkg/" + "x" * 150 + "/" + "y" * 120, "f", b"long name", 0o644),
    ("pkg/ünïcødé", "f", b"utf-8", 0o644),
]


@needs_symlinks
@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("compress", [False, True])
def test_same_tree_as_tarfile(tmp_path, format, compress):
    members = (
        SAMPLE
        if format != tarfile.USTAR_FORMAT
        else [m for m in SAMPLE if "x" * 150 not in m[0]]
    )
    data = make_tar(members, format, compress)
    tarfile_extract(data, tmp_path / "expected")
    assert untar.extract(io.BytesIO(data), str(tmp_path / "actual")) == ""
    assert snapshot(tmp_path / "actual") == snapshot(tmp_path / "expected")


@needs_symlinks
@pytest.mark.skipif(shutil.which("tar") is None, reason="needs tar")
@pytest.mark.parametrize("format", ["gnu", "posix", "ustar", "v7", "oldgnu"])
def test_archives_from_tar(tmp_path, format):
    src = tmp_path / "src"
    for name, kind, payload, mode in SAMPLE:
        if format in ("ustar", "v7") and len(name) > 99:
            continue
        path = src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "d":
            path.mkdir(exist_ok=True)
        elif kind == "f":
            path.write_bytes(payload)
            path.chmod(mode)
        elif kind == "s":
            path.symlink_to(payload)
        else:
            os.link(src / payload, path)
    archive = tmp_path / "a.tar"
    result = subprocess.run(
        ["tar", f"--format={format}", "-cf", str(archive), "-C", str(src), "pkg"],
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"tar cannot write {format}: {result.stderr.decode()}")
    data = archive.read_bytes()
    tarfile_extract(data, tmp_path / "expected")
    untar.extract(str(archive), str(tmp_path / "actual"))
    assert snapshot(tmp_path / "actual") == snapshot(tmp_path / "expected")


def package_like(prefix, top_files=False, bundle=False, parents=True):
    """Members of a package archive: the parents of its prefix, then depth-first with files before dirs, and a manifest last."""
    parts = prefix.split("/")
    members = (
        [("/".join(parts[:i]), "d", None, 0o755) for i in range(1, len(parts))]
        if parents
        else []
    )
    members.append((prefix, "d", None, 0o755))
    if top_files:
        members.append((f"{prefix}/README", "f", b"readme", 0o644))
    members += [
        (f"{prefix}/.meta", "d", None, 0o755),
        (f"{prefix}/.meta/spec.json", "f", b"{}", 0o644),
    ]
    if not bundle:
        members += [
            (f"{prefix}/bin", "d", None, 0o755),
            (f"{prefix}/bin/app", "f", b"app", 0o755),
            (f"{prefix}/lib", "d", None, 0o755),
            (f"{prefix}/lib/libfoo.so", "f", b"foo", 0o644),
        ]
    members.append((f"{prefix}/.meta/manifest", "f", b"{}", 0o644))
    return members


def deep(base: str, length: int) -> str:
    while len(base) < length:
        base += "/" + "x" * 100
    return base


@pytest.mark.parametrize("top_files", [False, True])
@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("parents", [False, True])
def test_root_marker(tmp_path, top_files, bundle, parents):
    prefix = "opt/store/linux/pkg-1.0-abcdef"
    data = make_tar(package_like(prefix, top_files, bundle, parents))
    assert untar.extract(io.BytesIO(data), str(tmp_path), root_marker=".meta") == prefix
    expected = (
        {".meta"}
        | ({"README"} if top_files else set())
        | (set() if bundle else {"bin", "lib"})
    )
    assert set(os.listdir(tmp_path)) == expected
    assert (tmp_path / ".meta" / "manifest").read_bytes() == b"{}"


def test_root_marker_long_paths(tmp_path):
    """Neither the archive's names nor the destination path limit each other."""
    path_max = (
        os.pathconf(str(tmp_path), "PC_PATH_MAX") if hasattr(os, "pathconf") else 260
    )
    prefix = deep("opt", int(path_max * 0.9)) + "/pkg"
    dest = deep(str(tmp_path), int(path_max * 0.7))
    os.makedirs(dest)
    data = make_tar(package_like(prefix), compress=True)
    assert untar.extract(io.BytesIO(data), dest, root_marker=".meta") == prefix
    assert sorted(os.listdir(dest)) == [".meta", "bin", "lib"]


def test_root_marker_shallowest(tmp_path):
    """A nested marker earlier in the archive does not decide the root."""
    members = [
        ("a", "d", None, 0o755),
        ("a/+early", "d", None, 0o755),
        ("a/+early/.meta", "d", None, 0o755),
        ("a/.meta", "d", None, 0o755),
        ("a/.meta/x", "f", b"x", 0o644),
    ]
    assert (
        untar.extract(io.BytesIO(make_tar(members)), str(tmp_path), root_marker=".meta")
        == "a"
    )
    assert sorted(os.listdir(tmp_path)) == ["+early", ".meta"]
    assert os.listdir(tmp_path / "+early") == [".meta"]


@pytest.mark.parametrize(
    "members,error",
    [
        ([("a/.meta", "d", None, 0o755), ("b/file", "f", b"", 0o644)], "outside of a"),
        ([("a/file", "f", b"", 0o644)], "no member named .meta"),
        ([("a/.meta", "d", None, 0o755), ("a", "f", b"", 0o644)], "non-directory"),
    ],
)
def test_root_marker_invalid(tmp_path, members, error):
    with pytest.raises(untar.Error, match=error):
        untar.extract(io.BytesIO(make_tar(members)), str(tmp_path), root_marker=".meta")
    assert os.listdir(tmp_path) == []


def test_member_order_does_not_matter(tmp_path):
    members = package_like("opt/store/pkg", top_files=True)
    for seed in range(50):
        rng = random.Random(seed)
        shuffled = members[:]
        rng.shuffle(shuffled)
        dest = tmp_path / str(seed)
        assert (
            untar.extract(
                io.BytesIO(make_tar(shuffled)), str(dest), root_marker=".meta"
            )
            == "opt/store/pkg"
        )
        assert sorted(os.listdir(dest)) == [".meta", "README", "bin", "lib"]


# Security


@pytest.mark.parametrize(
    "name", ["/etc/passwd", "../passwd", "a/../../passwd", "a/../../../passwd"]
)
def test_unsafe_names(tmp_path, name):
    dest = tmp_path / "dest"
    with pytest.raises(untar.Error, match="unsafe path"):
        untar.extract(io.BytesIO(make_tar([(name, "f", b"x", 0o644)])), str(dest))
    assert os.listdir(tmp_path) == ["dest"] and os.listdir(dest) == []


@needs_symlinks
@pytest.mark.parametrize("target", ["/tmp", "..", "sub"])
def test_no_writes_through_symlinks(tmp_path, target):
    """A member below a symlink is not written through it, wherever it points."""
    dest = tmp_path / "dest"
    members = [
        ("sub", "d", None, 0o755),
        ("link", "s", target, 0o777),
        ("link/file", "f", b"x", 0o644),
    ]
    with pytest.raises(untar.Error, match="not a directory"):
        untar.extract(io.BytesIO(make_tar(members)), str(dest))
    assert os.listdir(tmp_path) == ["dest"] and os.listdir(dest) == []


@needs_symlinks
def test_no_writes_through_hardlinked_symlink(tmp_path):
    """A hardlink to a symlink pointing outside links the symlink, and nothing writes into it."""
    outside = tmp_path / "outside"
    outside.write_text("original")
    members = [
        ("link", "s", str(outside), 0o777),
        ("hard", "h", "link", 0o777),
        ("hard", "f", b"overwritten", 0o644),
    ]
    with pytest.raises(FileExistsError):
        untar.extract(io.BytesIO(make_tar(members)), str(tmp_path / "dest"))
    assert outside.read_text() == "original"


@needs_symlinks
def test_no_hardlinks_through_symlinks(tmp_path):
    """A hardlink target behind a symlinked dir is refused, and nothing outside is linked."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret")
    members = [("dir", "s", str(outside), 0o777), ("hard", "h", "dir/secret", 0o644)]
    with pytest.raises(untar.Error, match="not extracted"):
        untar.extract(io.BytesIO(make_tar(members)), str(tmp_path / "dest"))
    assert (outside / "secret").stat().st_nlink == 1


@pytest.mark.skipif(not untar._DIR_FD, reason="needs dir file descriptors")
def test_hardlinks_reuse_open_dirs(tmp_path, monkeypatch):
    """A hardlink only opens the dirs of its target that are not open already."""
    opens = []
    real_open = os.open
    monkeypatch.setattr(
        os, "open", lambda *a, **k: opens.append(a[0]) or real_open(*a, **k)
    )
    deep = "/".join("abcdefgh")
    files = [(f"{deep}/x/f{i}", "f", b"", 0o644) for i in range(10)]
    links = [(f"{deep}/x/h{i}", "h", f"{deep}/x/f{i}", 0o644) for i in range(10)] + [
        (f"{deep}/y/h{i}", "h", f"{deep}/x/f{i}", 0o644) for i in range(10)
    ]
    untar.extract(io.BytesIO(make_tar(files)), str(tmp_path / "files"))
    without_links = len(opens)
    opens.clear()
    untar.extract(io.BytesIO(make_tar(files + links)), str(tmp_path / "links"))
    # Links in x open nothing, links in y open x, and y is opened, made and opened again
    assert len(opens) - without_links == 10 + 2


class FdCounter:
    """Tracks the file descriptors that os.open opens and os.close closes."""

    def __init__(self, monkeypatch):
        self.open = set()
        self.peak = 0
        self.real_open, self.real_close = os.open, os.close
        monkeypatch.setattr(os, "open", self._open)
        monkeypatch.setattr(os, "close", self._close)

    def _open(self, *args, **kwargs):
        fd = self.real_open(*args, **kwargs)
        self.open.add(fd)
        self.peak = max(self.peak, len(self.open))
        return fd

    def _close(self, fd):
        self.open.discard(fd)
        self.real_close(fd)


@pytest.mark.skipif(not untar._DIR_FD, reason="needs dir file descriptors")
def test_open_dirs_are_bounded(tmp_path, monkeypatch):
    """Only the deepest dirs on the stack stay open, and shallower ones are reopened."""
    monkeypatch.setattr(untar, "_MAX_OPEN_DIRS", 3)
    deep = "/".join("abcdefghij")
    members = [
        (f"{deep}/x/f", "f", b"f", 0o644),
        ("a/b/g", "f", b"g", 0o644),  # above the open dirs
        (f"{deep}/x/h", "h", f"{deep}/x/f", 0o644),
        (f"{deep}/y", "d", None, 0o755),
        ("a/b/c/l", "s", "d/e", 0o777),
        (f"{deep}/y/h", "h", "a/b/g", 0o644),
        ("a/b/c/d/e/z", "f", b"z", 0o644),
    ]
    fds = FdCounter(monkeypatch)
    untar.extract(io.BytesIO(make_tar(members)), str(tmp_path / "actual"))
    monkeypatch.undo()
    tarfile_extract(make_tar(members), tmp_path / "expected")
    assert snapshot(tmp_path / "actual") == snapshot(tmp_path / "expected")
    # dest, tmp, the open dirs, and a dir or file being opened from the last of them
    assert fds.peak <= 2 + 3 + 2
    assert not fds.open


@pytest.mark.skipif(not untar._DIR_FD, reason="needs dir file descriptors")
@pytest.mark.parametrize("valid", [True, False])
def test_tmp_is_removed(tmp_path, monkeypatch, valid):
    """tmp is removed through the open dirs, also below a prefix deeper than they are."""
    monkeypatch.setattr(untar, "_MAX_OPEN_DIRS", 3)
    prefix = "/".join("abcdefghijkl")
    members = [
        (f"{prefix}/.spack", "d", None, 0o755),
        (f"{prefix}/x/y/f", "f", b"f", 0o644),
        (f"{prefix}/x/y/l", "s", "/", 0o777),
    ]
    if not valid:
        members.append(("../escape", "f", b"", 0o644))
    dest = tmp_path / "dest"
    fds = FdCounter(monkeypatch)
    if valid:
        root = untar.extract(
            io.BytesIO(make_tar(members)), str(dest), root_marker=".spack"
        )
        assert root == prefix
    else:
        with pytest.raises(untar.Error, match="unsafe path"):
            untar.extract(
                io.BytesIO(make_tar(members)), str(dest), root_marker=".spack"
            )
    assert sorted(os.listdir(dest)) == ([".spack", "x"] if valid else [])
    assert fds.peak <= 2 + 3 + 2
    assert not fds.open


@pytest.mark.skipif(not untar._DIR_FD, reason="needs dir file descriptors")
def test_dest_dir_fd(tmp_path):
    """dest can be an open dir, which is extracted into wherever it moved, and stays open."""
    (tmp_path / "a").mkdir()
    fd = os.open(str(tmp_path / "a"), os.O_RDONLY)
    try:
        (tmp_path / "a").rename(tmp_path / "b")
        untar.extract(io.BytesIO(make_tar(SAMPLE)), fd)
        os.fstat(fd)
    finally:
        os.close(fd)
    tarfile_extract(make_tar(SAMPLE), tmp_path / "expected")
    assert snapshot(tmp_path / "b") == snapshot(tmp_path / "expected")


def test_no_overwrites(tmp_path):
    members = [("file", "f", b"first", 0o644), ("file", "f", b"second", 0o644)]
    with pytest.raises(FileExistsError):
        untar.extract(io.BytesIO(make_tar(members)), str(tmp_path / "dest"))


@pytest.mark.parametrize("linkname", ["/etc/passwd", "../outside"])
def test_unsafe_hardlinks(tmp_path, linkname):
    with pytest.raises(untar.Error, match="unsafe path"):
        untar.extract(
            io.BytesIO(make_tar([("hard", "h", linkname, 0o644)])), str(tmp_path / "d")
        )


@pytest.mark.parametrize("type", [tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_special_files(tmp_path, type):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        info = tarfile.TarInfo("dev")
        info.type = type
        tar.addfile(info)
    with pytest.raises(untar.Error, match="unsupported type"):
        untar.extract(io.BytesIO(out.getvalue()), str(tmp_path / "d"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_special_modes_are_not_restored(tmp_path):
    data = make_tar([("suid", "f", b"x", 0o4777), ("dir", "d", None, 0o3777)])
    untar.extract(io.BytesIO(data), str(tmp_path))
    assert stat.S_IMODE(os.stat(tmp_path / "suid").st_mode) == 0o755
    assert stat.S_IMODE(os.stat(tmp_path / "dir").st_mode) == 0o755


def test_large_extended_header(tmp_path):
    data = make_tar([("x" * (2 * 1024 * 1024), "f", b"", 0o644)], tarfile.GNU_FORMAT)
    with pytest.raises(untar.Error, match="extended header"):
        untar.extract(io.BytesIO(data), str(tmp_path))


def test_invalid_checksum(tmp_path):
    data = bytearray(make_tar([("file", "f", b"x", 0o644)], tarfile.USTAR_FORMAT))
    data[0] ^= 1
    with pytest.raises(untar.Error, match="checksum"):
        untar.extract(io.BytesIO(bytes(data)), str(tmp_path))


def test_truncated(tmp_path):
    data = make_tar([("file", "f", os.urandom(5000), 0o644)])
    with pytest.raises(untar.Error, match="truncated"):
        untar.extract(io.BytesIO(data[:2000]), str(tmp_path))


@needs_symlinks
def test_fuzz(tmp_path):
    """Corrupt archives fail with an error or extract something, but never write outside."""
    data = make_tar(SAMPLE, compress=False)
    rng = random.Random(0)
    outcomes = collections.Counter()
    for i in range(300):
        corrupt = bytearray(data)
        for _ in range(rng.randint(1, 8)):
            corrupt[rng.randrange(len(corrupt))] = rng.randrange(256)
        dest = tmp_path / "work" / str(i)
        try:
            untar.extract(io.BytesIO(bytes(corrupt)), str(dest))
            outcomes["ok"] += 1
        except (untar.Error, OSError, UnicodeError) as e:
            outcomes[type(e).__name__] += 1
        assert os.listdir(tmp_path) == ["work"]
        assert all(not name.startswith(".untar-") for name in os.listdir(dest))
    assert outcomes["Error"] > 0


# Streaming


class ReadOnly:
    """A stream that can only be read, like a pipe or an HTTP response."""

    def __init__(self, data: bytes, max_read: int = 1000) -> None:
        self.data = io.BytesIO(data)
        self.max_read = max_read

    def read(self, size=-1):
        return self.data.read(self.max_read if size < 0 else min(size, self.max_read))


@needs_symlinks
@pytest.mark.parametrize("compress", [False, True])
def test_unseekable_stream_with_short_reads(tmp_path, compress):
    data = make_tar(SAMPLE, compress=compress)
    untar.extract(ReadOnly(data), str(tmp_path / "actual"))
    tarfile_extract(make_tar(SAMPLE), tmp_path / "expected")
    assert snapshot(tmp_path / "actual") == snapshot(tmp_path / "expected")


def test_decompresses_once(tmp_path, monkeypatch):
    def rewind(self):
        raise AssertionError("gzip stream rewound")

    monkeypatch.setattr(gzip._GzipReader, "_rewind", rewind)
    path = tmp_path / "a.tar.gz"
    path.write_bytes(make_tar(SAMPLE[:7], compress=True))
    untar.extract(str(path), str(tmp_path / "dest"))


def test_reader():
    data = make_tar(
        [
            ("a", "d", None, 0o755),
            ("a/f", "f", b"hello" * 1000, 0o644),
            ("a/g", "f", b"skipped", 0o644),
        ]
    )
    reader = untar.TarReader(io.BytesIO(data))
    members = []
    for member in reader:
        members.append((member.name, member.isdir(), member.isreg()))
        if member.name == "a/f":
            assert b"".join(reader.chunks(7)) == b"hello" * 1000
    assert members == [("a", True, False), ("a/f", False, True), ("a/g", False, True)]


# Modes and groups


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
@pytest.mark.parametrize("umask", [0o022, 0o077, 0o000])
@pytest.mark.parametrize(
    "file_mode,dir_mode", [(0o755, 0o755), (0o750, 0o2750), (0o700, 0o700)]
)
def test_modes(tmp_path, umask, file_mode, dir_mode):
    old = os.umask(umask)
    try:
        untar.extract(
            io.BytesIO(make_tar(SAMPLE)),
            str(tmp_path),
            file_mode=file_mode,
            dir_mode=dir_mode,
        )
    finally:
        os.umask(old)
    for dirpath, dirnames, filenames in os.walk(tmp_path / "pkg"):
        for name in dirnames:
            assert (
                stat.S_IMODE(os.lstat(os.path.join(dirpath, name)).st_mode) == dir_mode
            )
        for name in filenames:
            s = os.lstat(os.path.join(dirpath, name))
            if stat.S_ISREG(s.st_mode):
                expected = file_mode if name == "app" else file_mode & ~0o111
                assert stat.S_IMODE(s.st_mode) == expected, name


@pytest.mark.skipif(
    not hasattr(os, "getgroups") or len(os.getgroups()) < 2, reason="needs 2 groups"
)
def test_gid(tmp_path):
    gid = next(g for g in os.getgroups() if g != os.getegid())
    untar.extract(io.BytesIO(make_tar(SAMPLE)), str(tmp_path), gid=gid, dir_mode=0o2755)
    for dirpath, dirnames, filenames in os.walk(tmp_path / "pkg"):
        for name in dirnames + filenames:
            assert os.lstat(os.path.join(dirpath, name)).st_gid == gid, name


# Where links are not supported


@pytest.mark.parametrize("target_first", [False, True])
def test_links_are_copied_where_unsupported(tmp_path, monkeypatch, target_first):
    def fail(*args, **kwargs):
        raise OSError(errno.EPERM, "links are not supported")

    monkeypatch.setattr(os, "symlink", fail)
    monkeypatch.setattr(os, "link", fail)
    target = [("lib/libfoo.so.1", "f", b"foo", 0o644)]
    links = [
        ("lib/libfoo.so", "s", "libfoo.so.1", 0o777),
        ("lib/sub", "s", "../lib", 0o777),
    ]
    members = [("lib", "d", None, 0o755)] + (
        target + links if target_first else links + target
    )
    members.append(("lib/libfoo.a", "h", "lib/libfoo.so.1", 0o644))
    untar.extract(io.BytesIO(make_tar(members)), str(tmp_path))
    for name in ("libfoo.so", "libfoo.so.1", "libfoo.a"):
        path = tmp_path / "lib" / name
        assert not path.is_symlink() and path.read_bytes() == b"foo"
    assert (tmp_path / "lib" / "sub").is_dir()


def test_solaris_extended_header(tmp_path):
    """Solaris tar writes pax headers with type X instead of x."""
    data = bytearray(make_tar([("x" * 120, "f", b"long", 0o644)], tarfile.PAX_FORMAT))
    assert data[156:157] == tarfile.XHDTYPE
    data[156:157] = b"X"
    data[148:156] = b"%06o\0 " % (sum(data[:148]) + sum(data[156:512]) + 256)
    untar.extract(io.BytesIO(bytes(data)), str(tmp_path))
    assert (tmp_path / ("x" * 120)).read_bytes() == b"long"


@needs_symlinks
@pytest.mark.parametrize(
    "members",
    [
        [("link", "s", ".", 0o777), ("link", "s", "..", 0o777)],
        [("link", "s", ".", 0o777), ("link", "d", None, 0o755)],
        [("file", "f", b"", 0o644), ("file", "d", None, 0o755)],
    ],
)
def test_no_entries_over_others(tmp_path, members):
    with pytest.raises((FileExistsError, untar.Error)):
        untar.extract(io.BytesIO(make_tar(members)), str(tmp_path))
