"""Property test against tarfile, which needs hypothesis."""

import io
import os
import tarfile

import pytest
from test_fastar import make_tar, needs_symlinks, snapshot, tarfile_extract

import fastar

hypothesis = pytest.importorskip("hypothesis")
st = hypothesis.strategies

names = st.sampled_from(["a", "b", "lib", ".spack", "x" * 120, "ü", "-x"])


@st.composite
def archives(draw):
    """Random valid archives: a tree in random order, possibly without some dir entries."""
    paths = draw(
        st.lists(st.lists(names, min_size=1, max_size=4), min_size=1, max_size=12)
    )
    members, files, seen = [], [], set()
    for parts in paths:
        for i in range(1, len(parts)):
            seen.add("/".join(parts[:i]))
        name = "/".join(parts)
        if name in seen:
            continue
        seen.add(name)
        kind = draw(st.sampled_from("fffsh" if files else "ffs"))
        if kind == "f":
            members.append(
                (
                    name,
                    "f",
                    draw(st.binary(max_size=2000)),
                    draw(st.sampled_from([0o644, 0o755])),
                )
            )
            files.append(name)
        elif kind == "s":
            members.append(
                (name, "s", draw(st.sampled_from(["a", "../b", "/abs", "x/y"])), 0o777)
            )
        else:
            # like tar, a hardlink records the mode of the inode it shares
            target = draw(st.sampled_from(files))
            mode = next(m[3] for m in members if m[0] == target)
            members.append((name, "h", target, mode))
    dirs = sorted(seen - {m[0] for m in members})
    members += [(d, "d", None, 0o755) for d in dirs if draw(st.booleans())]
    # no member below a non-dir, and hardlinks after their targets
    nondirs = {m[0] for m in members if m[1] != "d"}
    members = [m for m in members if not any(m[0].startswith(n + "/") for n in nondirs)]
    placed, result = set(), []
    for m in draw(st.permutations(members)):
        if m[1] == "h" and m[2] not in placed:
            continue
        placed.add(m[0])
        result.append(m)
    return result, draw(st.sampled_from([tarfile.GNU_FORMAT, tarfile.PAX_FORMAT]))


@needs_symlinks
@hypothesis.settings(
    max_examples=int(os.environ.get("FASTAR_EXAMPLES", "300")),
    deadline=None,
    suppress_health_check=list(hypothesis.HealthCheck),
)
@hypothesis.given(archive=archives())
def test_property_same_tree_as_tarfile(tmp_path_factory, archive):
    members, format = archive
    tmp = tmp_path_factory.mktemp("p")
    data = make_tar(members, format)
    tarfile_extract(data, tmp / "expected")
    fastar.extract(io.BytesIO(data), str(tmp / "actual"))
    assert snapshot(tmp / "actual") == snapshot(tmp / "expected")
