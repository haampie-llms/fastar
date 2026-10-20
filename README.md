# fastar

Fast, streaming extraction of tar archives, in a single pure Python module without
dependencies, for Python 3.6 and later.

```python
import fastar

# Extract the contents of the dir that contains `.spack`, wherever it is in the archive
root = fastar.extract("pkg.tar.gz", "/opt/pkg", root_marker=".spack")

# Extract from any readable binary stream, such as an HTTP response
with urllib.request.urlopen(url) as response:
    fastar.extract(response, "dest")
```

## Why

- **One pass.** The archive is read front to back exactly once, without seeking, from a file,
  a pipe or a network stream. Gzip compression is detected from the first bytes.
- **Less work per member.** Only the header fields that extraction needs are parsed: ustar
  headers, pax extended headers and GNU long names. A regular file costs an `open`, `write`s
  and a `close`, like GNU tar.
- **Stripping a prefix that is only known from the contents.** With `root_marker`, the
  archive's contents are extracted relative to the parent of the shallowest member with that
  name, like `tar --strip-components` with a count that is only known after the last member.
  Other members must be in that dir, except for the dirs leading up to it.
- **No path length limits.** Entries are created relative to open dir file descriptors, so
  no path longer than a single name is passed to the operating system, however long the
  archive's names and the destination are.
- **Final permissions on creation.** Files get `file_mode` (without executable bits where the
  member has none for its owner) and dirs `dir_mode`, optionally with a group `gid`, so that
  no pass over the extracted tree is needed afterwards. Timestamps and ownership in the archive
  are not restored.

## Safety

fastar writes nothing outside the destination, whatever the archive contains:

- Absolute names and names with `..` are rejected.
- Dirs are opened with `O_NOFOLLOW` relative to their parent's file descriptor, so nothing is
  written through a symlink, including one swapped in concurrently.
- Files are created with `O_EXCL | O_NOFOLLOW`, so existing entries are never written into or
  replaced, also not through hardlinks.
- Modes and groups are set on open file descriptors, so never on a symlink's target.
- Device files, fifos and sparse files are rejected, and set-user-ID, set-group-ID and sticky
  bits from the archive are not restored.
- Pax and GNU long name headers are limited to 1 MiB.

Symlinks may point anywhere: creating one writes nothing outside, and fastar never follows them.
Where symlinks or hardlinks cannot be created, their targets are copied, also when the target
comes later in the archive.

On Windows, which has no dir file descriptors, entries are created by extended-length path.
Checks there are path based, and so not safe against concurrent changes to the destination.

## Development

```console
$ pip install -e '.[dev]'
$ python tests/vendor/fetch_cpython_testdata.py cpython-testdata
$ FASTAR_CPYTHON_TESTDATA=cpython-testdata pytest
$ ruff check && ruff format --diff && mypy
```

`tests/vendor/test_cpython.py` is adapted from CPython's `test_tarfile.py`, and is under the PSF
License Agreement included in it. It runs fastar on CPython's test archive and against the
attacks CPython tests its extraction filters with. The rest of fastar is under the MIT license.
