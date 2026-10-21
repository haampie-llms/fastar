# untar

Fast, safe, streaming extraction of tar archives that distribute software, in a single pure
Python module without dependencies, for Python 3.6 and later.

```python
import untar

# Extract an archive, from a path or any readable binary stream
untar.extract("pkg.tar.gz", "/opt/pkg")

# Extract the dir that contains `.meta`, wherever it is in the archive
root = untar.extract("pkg.tar.gz", "/opt/pkg", root_marker=".meta")
```

From the command line, like `tar xf pkg.tar.gz -C /opt/pkg`, with `-` to read standard input:

```console
$ python -m untar pkg.tar.gz /opt/pkg
```

Unlike tar, the destination is created if it does not exist, and an archive with an entry that
already exists there is an error, which exits with status 2.

### Decompressing in a thread

untar decompresses and extracts one after the other, in one thread, while `tar xzf` and
`gzip -d | tar xf -` decompress in another process at the same time. In Python, a thread can do
the same, since decompression and writes to a pipe release the GIL:

```python
import gzip
import os
import shutil
import threading

import untar


class Gunzip:
    """Decompress a gzip file in a thread, and read the result from a pipe."""

    def __init__(self, path):
        r, w = os.pipe()
        self.pipe = open(r, "rb", buffering=0)
        self.error = None
        self.thread = threading.Thread(target=self._decompress, args=(path, w))
        self.thread.start()

    def _decompress(self, path, w):
        try:
            # Writes block while the pipe is full, so decompression stays at most a pipe
            # buffer ahead of extraction
            with open(w, "wb") as out, gzip.open(path) as f:
                shutil.copyfileobj(f, out, 64 * 1024)
        except Exception as e:
            self.error = e

    def read(self, size):
        data = self.pipe.read(size)
        if not data:
            # The end of the pipe: wait until the gzip checksum is verified
            self.thread.join()
            if self.error:
                raise self.error
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        # If extraction stopped early, a blocked write then fails, and the thread ends
        self.pipe.close()
        self.thread.join()


with Gunzip("pkg.tar.gz") as stream:
    untar.extract(stream, "/opt/pkg")
```

untar reads the stream to its end before it moves anything into the destination, so an archive
with a bad gzip checksum still extracts nothing.

## Scope

untar is for installing software from archives, not for restoring backups. Owners, groups,
timestamps and permissions recorded in an archive describe the machine it was made on; on
another machine they are meaningless at best and a liability at worst. So untar does not
restore them: files are created with `file_mode` (without executable bits where the archive
has none for the owner), dirs with `dir_mode`, optionally with group `gid`, and only regular
files, dirs, symlinks and hardlinks are extracted.

## Features

- **One pass.** The archive is read front to back once, without seeking, from a file, pipe or
  network stream. Gzip is detected from the first bytes.
- **Less work per member.** Only the header fields that extraction needs are parsed (ustar,
  pax and GNU long names), and a file costs an `open`, `write`s and a `close`.
- **A root only known from the contents.** With `root_marker`, the parent of the shallowest
  member with that name is extracted, like `tar --strip-components` with a count that is only
  known after the last member.
- **No path length limits.** Entries are created relative to open dir file descriptors, so no
  path longer than one name is passed to the operating system.

## Safety

Nothing is written outside the destination, whatever the archive contains. Absolute names and
`..` are rejected, dirs are opened with `O_NOFOLLOW` so nothing is written through a symlink,
and files are created with `O_EXCL | O_NOFOLLOW` so existing entries are never replaced.
Symlinks may point anywhere, but untar never follows them, except where the destination itself
is one. The destination can also be an open dir file descriptor, so that a caller who opened it
safely extracts into that very dir. On Windows, which has no dir file descriptors, these checks
are path based and not safe against concurrent changes.

Archives that tar implementations read differently are rejected, so that no member can hide
from one of them: numbers must be plain digits, a member's name and size come from one header
only, global pax headers cannot set them, long names have no NUL inside, and links, devices and
fifos have no size. The gzip checksum is always verified, and an archive must end with two zero
blocks and only zeros after them, so that neither truncation between members nor a second
archive after the first goes unnoticed.

## Development

```console
$ pip install -e '.[dev]'
$ python tests/vendor/fetch_cpython_testdata.py cpython-testdata
$ UNTAR_CPYTHON_TESTDATA=cpython-testdata pytest
$ ruff check && ruff format --diff && mypy
```

`tests/vendor/test_cpython.py` is adapted from CPython's `test_tarfile.py` and is under the PSF
License Agreement included in it. The rest of untar is under the MIT license.
