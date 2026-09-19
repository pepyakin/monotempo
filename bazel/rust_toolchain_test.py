#!/usr/bin/env python3
"""Check that Cargo and Bazel pin the same Rust release (Python 3.11+)."""

import re
import sys
import tomllib
from pathlib import Path


def main():
    module = Path(sys.argv[1]).read_text()
    version = re.search(r'^RUST_VERSION = "(\d+\.\d+\.\d+)"$', module, re.MULTILINE)
    if version is None:
        sys.exit("MODULE.bazel must pin an exact RUST_VERSION")
    toolchain = tomllib.loads(Path(sys.argv[2]).read_text())
    channel = toolchain["toolchain"]["channel"]
    if channel != version[1]:
        sys.exit(
            f"Rust version mismatch: MODULE.bazel pins {version[1]}, "
            f"rust-toolchain.toml pins {channel}. Update both pins together."
        )


if __name__ == "__main__":
    main()
