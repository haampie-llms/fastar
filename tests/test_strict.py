"""Archives that untar rejects, or reads in one way only, where readers have disagreed."""

import errno
import gzip
import io
import itertools
import os
import sys
import tarfile

import pytest
from test_untar import make_tar, needs_symlinks

import untar

EOF = bytes(1024)


def header(name="f", type=b"0", size=b"", linkname="", magic=b"ustar\x0000", prefix=""):
    """A raw ustar header, with any size field and a correct checksum."""
    h = bytearray(512)
    h[0 : len(name.encode())] = name.encode()
    h[100:108] = b"0000644\0"
    h[124:136] = size.ljust(12, b"\0") if isinstance(size, bytes) else b"%011o\0" % size
    h[156:157] = type
    h[157 : 157 + len(linkname.encode())] = linkname.encode()
    h[257:265] = magic
    h[345 : 345 + len(prefix.encode())] = prefix.encode()
    return checksum(h)


def checksum(h: bytearray, field=lambda total: b"%06o\0 " % total) -> bytes:
    """The header h with the checksum field(total) of its bytes."""
    h = bytearray(h)
    h[148:156] = b" " * 8
    h[148:156] = field(sum(h))
    return bytes(h)


def data(content: bytes) -> bytes:
    return content + bytes(-len(content) % 512)


def member(name="f", content=b"", **kwargs):
    return header(name, size=len(content), **kwargs) + data(content)


def extended(type, content: bytes) -> bytes:
    return header(
        "././@LongLink" if type in b"LK" else "pax", type, len(content)
    ) + data(content)


def record(key, value) -> bytes:
    body = (
        b" "
        + key.encode()
        + b"="
        + (value if isinstance(value, bytes) else value.encode())
    )
    body += b"\n"
    n = len(body) + 1
    while len(b"%d" % n) + len(body) != n:
        n += 1
    return b"%d" % n + body


def members(archive: bytes, limit=100):
    reader = untar.TarReader(io.BytesIO(archive))
    return [(m.name, m.linkname, m.size) for m in itertools.islice(reader, limit)]


def rejects(archive: bytes, tmp_path, match=None):
    with pytest.raises(untar.Error, match=match):
        untar.extract(io.BytesIO(archive), str(tmp_path / "dest"))
    assert os.listdir(tmp_path / "dest") == []


# Numbers: octal and pax decimal, in ASCII digits only.


@pytest.mark.parametrize(
    "size", [b"-1777", b"+17", b"0o17", b"0_17", b"1 7", b"\xd9\xa3"]
)
def test_invalid_octal_numbers(tmp_path, size):
    archive = header(size=size) + member("secret", b"hello") + EOF
    with pytest.raises(untar.Error, match="invalid number"):
        members(archive)
    rejects(archive, tmp_path, "invalid number")


@pytest.mark.parametrize(
    "size",
    [
        b"\x0000000002000",
        b" \x000000002000",
        b"\n0000002000",
        b"\t0000002000",
        b"\x0b0000002000",
        b"1\x00777",
        b"2000\x00X",
    ],
)
def test_octal_numbers_with_odd_padding(size):
    """Readers disagree on the size of a member, and so on where the next header is: some
    skip a leading NUL or whitespace other than spaces, others stop there, and some read
    the digits after a NUL."""
    with pytest.raises(untar.Error, match="invalid number"):
        members(header(size=size) + member("hidden", bytes(1024)) + EOF)


@pytest.mark.parametrize(
    "size", ["", "abc", "-1536", "1_0", "+3", " 3", "٣", "0x3", "3.0"]
)
def test_invalid_pax_sizes(tmp_path, size):
    archive = extended(b"x", record("size", size)) + member("f", b"abc") + EOF
    rejects(archive, tmp_path, "invalid pax size")


@pytest.mark.parametrize(
    "records",
    [
        record("size", "0" * 5000 + "3"),
        b"1" * 5000 + b" path=x\n",
    ],
    ids=["size", "length"],
)
def test_pax_numbers_with_many_digits(tmp_path, records):
    """int() takes time quadratic in the number of digits, and from Python 3.11 on refuses
    more than 4300 of them with a ValueError."""
    rejects(
        extended(b"x", records) + member("f", b"abc") + EOF, tmp_path, "invalid pax"
    )


def test_pax_numbers_with_zero_padding():
    archive = extended(b"x", record("size", "0" * 21 + "3")) + member("f", b"abc") + EOF
    assert members(archive) == [("f", "", 3)]


def test_valid_numbers():
    """Leading and trailing spaces and NULs, octal without terminator, and base-256."""
    for size in [
        b"  3",
        b"3 ",
        b"00000000003",
        b"3\0   ",
        b"3\0 \0 ",
        b"\x80" + (3).to_bytes(11, "big"),
    ]:
        assert members(header(size=size) + data(b"abc") + EOF) == [("f", "", 3)]


@pytest.mark.parametrize(
    "field",
    [
        lambda total: b"\x80" + total.to_bytes(7, "big"),
        lambda total: b"%06o\0X" % total,
        lambda total: b"\t%06o\0" % total,
    ],
    ids=["base-256", "garbage", "tab"],
)
def test_odd_checksums(field):
    """Readers that refuse a checksum skip to the next block that is a valid header, which
    may be in the data of this member."""
    archive = checksum(header("f", size=1024), field) + member("hidden", bytes(1024))
    with pytest.raises(untar.Error, match="invalid number"):
        members(archive + EOF)


@pytest.mark.parametrize(
    "format", [b"%06o\0 ", b"%06o \0", b"%07o\0", b"%08o", b" %06o\0"]
)
def test_checksum_variants(format):
    """Space or NUL terminated, unterminated, and with leading spaces."""
    h = checksum(header("f", size=3), lambda total: format % total)
    assert members(h + data(b"abc") + EOF) == [("f", "", 3)]


# Pax records


@pytest.mark.parametrize(
    "records",
    [
        b"5 abc\n",
        b"6 =abc\n",
        b"+9 path=x\n",
        b"9path=x\n",
        b"30 path=x\n",
        b"0 path=x\n",
    ],
)
def test_invalid_pax_records(tmp_path, records):
    rejects(extended(b"x", records) + member() + EOF, tmp_path, "invalid pax")


@pytest.mark.parametrize(
    "key,value", [("path", ""), ("linkpath", ""), ("path", "a\0b")]
)
def test_invalid_pax_names(tmp_path, key, value):
    archive = (
        extended(b"x", record(key, value)) + member("f", type=b"2", linkname="t") + EOF
    )
    rejects(archive, tmp_path, f"invalid pax {key}")


@pytest.mark.parametrize("key", ["path", "linkpath", "size"])
def test_global_pax_names_and_sizes(tmp_path, key):
    """Readers disagree on whether global names and sizes apply to later members."""
    archive = extended(b"g", record(key, "0")) + member("a", bytes(1024)) + EOF
    rejects(archive, tmp_path, "global pax")


def test_global_pax_other_keys(tmp_path):
    archive = extended(b"g", record("comment", "hi")) + member("a", b"x") + EOF
    untar.extract(io.BytesIO(archive), str(tmp_path))
    assert (tmp_path / "a").read_bytes() == b"x"


# One name per member: readers resolve these conflicts differently.


@pytest.mark.parametrize(
    "headers",
    [
        extended(b"x", record("path", "a")) + extended(b"x", record("path", "b")),
        extended(b"x", record("path", "a")) + extended(b"X", record("size", "1")),
        extended(b"L", b"a\0") + extended(b"L", b"b\0"),
        extended(b"K", b"a\0") + extended(b"K", b"b\0"),
    ],
)
def test_two_extended_headers_of_a_kind(tmp_path, headers):
    rejects(headers + member("f", type=b"2", linkname="t") + EOF, tmp_path, "two")


@pytest.mark.parametrize(
    "headers",
    [
        extended(b"x", record("path", "a")) + extended(b"L", b"b\0"),
        extended(b"L", b"b\0") + extended(b"x", record("path", "a")),
        extended(b"x", record("linkpath", "a")) + extended(b"K", b"b\0"),
    ],
)
def test_pax_and_gnu_names(tmp_path, headers):
    rejects(headers + member("f", type=b"2", linkname="t") + EOF, tmp_path, "both")


@pytest.mark.parametrize("type", [b"x", b"L", b"K"])
@pytest.mark.parametrize("end", [b"", EOF])
def test_extended_header_without_member(tmp_path, type, end):
    """Dropping it silently would hide truncation."""
    content = record("path", "a") if type == b"x" else b"a\0"
    match = "ends after" if end else "truncated"
    rejects(member("first") + extended(type, content) + end, tmp_path, match)


@pytest.mark.parametrize("type", [b"L", b"K"])
def test_gnu_long_names_with_nul_inside(tmp_path, type):
    """Readers that stop at the NUL and readers that keep the rest see different names."""
    rejects(
        extended(type, b"good\0bad\0") + member("f", type=b"2", linkname="t") + EOF,
        tmp_path,
        "NUL inside",
    )


def test_gnu_long_names_end_with_nuls():
    archive = extended(b"L", b"name\0\0") + member("f", type=b"2", linkname="t") + EOF
    assert members(archive) == [("name", "t", 0)]


@pytest.mark.parametrize("type", [b"1", b"2", b"3", b"4", b"6"])
@pytest.mark.parametrize("pax", [False, True])
def test_size_without_data(type, pax):
    """tar and tarfile read what follows a link, device or fifo with a size as headers, the
    Rust tar crates skip it as data: either could miss a member."""
    hidden = member("hidden", b"x")
    if pax:
        archive = extended(b"x", record("size", "512")) + header("f", type, 0, "t")
    else:
        archive = header("f", type, 512, "t")
    with pytest.raises(untar.Error, match="with size 512"):
        members(archive + hidden + EOF)


def test_dir_size_without_data():
    """Some tar implementations give dirs a size, which tar and tarfile ignore."""
    archive = header("d/", b"5", 512) + member("after", b"x") + EOF
    assert members(archive) == [("d", "", 512), ("after", "", 1)]


@pytest.mark.parametrize("type", [b"L", b"K"])
def test_empty_gnu_long_names(tmp_path, type):
    rejects(
        extended(type, b"\0") + member("f", type=b"2", linkname="t") + EOF,
        tmp_path,
        "empty",
    )


def test_extended_headers_are_limited_per_member(tmp_path):
    big = "x" * (600 * 1024)
    archive = extended(b"L", big.encode() + b"\0") + extended(
        b"x", record("linkpath", big)
    )
    rejects(archive + member("f", type=b"1") + EOF, tmp_path, "more than 1 MiB")


# Reading


def test_unknown_types_have_data():
    """Like tarfile: the reader does not lose its place on a type it does not know."""
    archive = (
        member("mystery", b"DATA" * 200, type=b"Z") + member("after", b"after") + EOF
    )
    assert [name for name, _, _ in members(archive)] == ["mystery", "after"]


def test_prefix_only_in_posix_ustar():
    """GNU tar reads the prefix only with version 00; with another, the header is v7."""
    posix = member("name", prefix="prefix") + EOF
    v7 = member("name", prefix="prefix", magic=b"ustar\0\0\0") + EOF
    assert members(posix) == [("prefix/name", "", 0)]
    assert members(v7) == [("name", "", 0)]


@pytest.mark.parametrize(
    "headers,name",
    [
        (extended(b"x", record("path", "file")), "dir/"),
        (extended(b"L", b"file\0"), "dir/"),
        (extended(b"x", record("path", "dir/")), "file"),
    ],
    ids=["pax path", "GNU long name", "pax dir"],
)
def test_old_dirs_named_like_dirs_once(tmp_path, headers, name):
    """Old tar implementations write dirs as files named with a slash. Some readers look
    for it in the name in the header, others in the final name: a file there has data, and
    a dir has none, which is where the others see a member."""
    archive = headers + header(name, b"\0", 1024) + member("hidden", bytes(1024)) + EOF
    rejects(archive, tmp_path, "named like a directory")


def test_old_dirs_named_like_dirs():
    archive = extended(b"x", record("path", "long/")) + header("dir/", b"\0", 0) + EOF
    assert members(archive) == [("long", "", 0)]


# gzip


def gzipped(size: int) -> bytes:
    return gzip.compress(make_tar([("f", "f", os.urandom(size), 0o644)]))


@pytest.mark.parametrize("size", [1000, 1046528, 1047040, 2095616])
def test_gzip_checksum(tmp_path, size):
    """Corrupt data is detected in archives of any size."""
    archive = bytearray(gzipped(size))
    archive[len(archive) // 2] ^= 1
    rejects(bytes(archive), tmp_path, "gzip")


@pytest.mark.parametrize("cut", [10, 100, 0.5, -8, -1])
def test_gzip_truncated(tmp_path, cut):
    archive = gzipped(100_000)
    end = int(len(archive) * cut) if isinstance(cut, float) else cut
    rejects(archive[:end], tmp_path)


def test_gzip_invalid(tmp_path):
    archive = bytearray(gzipped(1000))
    archive[2] = 9  # an unknown compression method
    rejects(bytes(archive), tmp_path, "gzip")


# Links


@needs_symlinks
def test_empty_symlink_target(tmp_path):
    """os.symlink("") fails with ENOENT, which is not "symlinks are not supported"."""
    for archive in [
        member("s", type=b"2") + EOF,
        member("d/", type=b"5") + member("d/s", type=b"2") + EOF,
    ]:
        rejects(archive, tmp_path, "without a target")


@pytest.mark.skipif(
    sys.platform == "win32", reason="any symlink error means no privilege"
)
def test_copy_only_where_links_are_unsupported(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(os, "symlink", fail)
    with pytest.raises(OSError, match="I/O error"):
        untar.extract(
            io.BytesIO(member("t", b"x") + member("s", type=b"2", linkname="t") + EOF),
            str(tmp_path),
        )


@pytest.mark.parametrize(
    "archive",
    [
        member("d/", type=b"5") + member("h", type=b"1", linkname="d") + EOF,
        member("h", type=b"1", linkname="") + EOF,
        member("h", type=b"1", linkname="h") + EOF,
        member("h", type=b"1", linkname="missing") + EOF,
    ],
)
def test_invalid_hardlinks(tmp_path, archive):
    rejects(archive, tmp_path, "hardlinks")


def test_hardlink_to_itself(tmp_path):
    """tar cf a.tar f f archives f twice, the second time as a hardlink to itself."""
    archive = member("f", b"x") + member("f", type=b"1", linkname="f") + EOF
    untar.extract(io.BytesIO(archive), str(tmp_path))
    assert (tmp_path / "f").read_bytes() == b"x"


@pytest.mark.parametrize("name", ["file/", "dir/.", "dir/./"])
def test_non_directories_named_like_directories(tmp_path, name):
    rejects(member(name, b"x") + EOF, tmp_path, "named like a directory")


# The destination


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_entries_in_the_destination_are_not_replaced(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "a").write_text("mine")
    with pytest.raises(untar.Error, match="exists in the destination"):
        untar.extract(
            io.BytesIO(member("b", b"new") + member("a", b"theirs") + EOF), str(dest)
        )
    assert sorted(os.listdir(dest)) == ["a"]
    assert (dest / "a").read_text() == "mine"


def test_tarfile_reads_our_headers():
    """The raw headers here are valid: tarfile agrees on the members of a simple archive."""
    archive = member("a", b"x") + member("d/", type=b"5") + EOF
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        assert [(m.name, m.size) for m in tar] == [("a", 1), ("d", 0)]
