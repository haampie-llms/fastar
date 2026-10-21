"""The example in the README that decompresses in a thread."""

import gzip
import os
import re

import pytest
from test_untar import SAMPLE, make_tar, needs_symlinks, snapshot, tarfile_extract

import untar

README = os.path.join(os.path.dirname(__file__), os.pardir, "README.md")


@pytest.fixture(scope="module")
def Gunzip():
    with open(README, encoding="utf-8") as f:
        (code,) = re.findall(r"```python\n(import gzip\n.*?)```", f.read(), re.DOTALL)
    # Without the usage at the end
    namespace: dict = {}
    exec(code.split("\nwith Gunzip(")[0], namespace)  # noqa: S102
    return namespace["Gunzip"]


@needs_symlinks
def test_extracts(tmp_path, Gunzip):
    data = make_tar(SAMPLE, compress=True)
    (tmp_path / "pkg.tar.gz").write_bytes(data)
    with Gunzip(str(tmp_path / "pkg.tar.gz")) as stream:
        untar.extract(stream, str(tmp_path / "dest"))
    tarfile_extract(data, tmp_path / "expected")
    assert snapshot(tmp_path / "dest") == snapshot(tmp_path / "expected")


@pytest.mark.parametrize("corrupt", ["checksum", "truncated", "missing"])
def test_gzip_errors(tmp_path, Gunzip, corrupt):
    data = make_tar([("file", "f", b"data", 0o644)], compress=True)
    if corrupt == "checksum":
        # The CRC-32 in the trailer
        data = data[:-8] + bytes(b ^ 1 for b in data[-8:-4]) + data[-4:]
    elif corrupt == "truncated":
        data = data[:-4]
    if corrupt != "missing":
        (tmp_path / "pkg.tar.gz").write_bytes(data)
    path = str(tmp_path / "pkg.tar.gz")
    with pytest.raises((OSError, EOFError)), Gunzip(path) as stream:
        untar.extract(stream, str(tmp_path / "dest"))
    assert os.listdir(tmp_path / "dest") == []


def test_extraction_stops_early(tmp_path, Gunzip):
    # Much more data than a pipe holds follows the member that fails
    data = make_tar(
        [("../evil", "f", b"x", 0o644), ("big", "f", bytes(1 << 24), 0o644)]
    )
    (tmp_path / "pkg.tar.gz").write_bytes(gzip.compress(data))
    path = str(tmp_path / "pkg.tar.gz")
    with pytest.raises(untar.Error, match="unsafe path"), Gunzip(path) as stream:
        untar.extract(stream, str(tmp_path / "dest"))
    assert not stream.thread.is_alive()
    assert os.listdir(tmp_path / "dest") == []
