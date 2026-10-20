"""Download CPython's test archives for test_cpython.py into the dir given as argument.

Usage: python tests/vendor/fetch_cpython_testdata.py DIR, then set FASTAR_CPYTHON_TESTDATA=DIR."""

import hashlib
import os
import sys
import urllib.request

COMMIT = "be41ce5d591ef267bd8e56b6897101562b1e1982"
URL = f"https://raw.githubusercontent.com/python/cpython/{COMMIT}/Lib/test/archivetestdata/"
FILES = {
    "testtar.tar": "760200dda3cfdff2cd31d8ab6c806794f3770faa465e7eae00a1cb3a2fbcbe3a",
    "testtar.tar.xz": "89e0326292b96a5700582a37ebf3d8ba60f1d136772b5cd15b2c2ae653fda188",
    "recursion.tar": "d80f55ac66a2570c8a19d2b1dad7c057cf4c944d9c2f8adaf5bf6c8539881e13",
}


def main() -> None:
    dest = sys.argv[1]
    os.makedirs(dest, exist_ok=True)
    for name, sha256 in FILES.items():
        with urllib.request.urlopen(URL + name) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != sha256:
            sys.exit(f"{name}: checksum mismatch")
        with open(os.path.join(dest, name), "wb") as f:
            f.write(data)


if __name__ == "__main__":
    main()
