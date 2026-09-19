#!/usr/bin/env bash
# Expose Bazel's pinned Solidity archives at Foundry's existing remapping paths.
set -euo pipefail

verify_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$verify_dir/../../.."

libraries=(forge-std tempo-std solady)
# Never overwrite an old submodule checkout or locally edited dependency.
for library in "${libraries[@]}"; do
    destination="$verify_dir/lib/$library"
    if [[ -e "$destination" && ! -L "$destination" ]]; then
        echo "Refusing to replace $destination; move it aside first." >&2
        exit 1
    fi
done

bazel build \
    @tempo_verify_forge_std//:srcs \
    @tempo_verify_tempo_std//:srcs \
    @tempo_verify_solady//:srcs
execution_root="$(bazel info execution_root)"
mkdir -p "$verify_dir/lib"
for library in "${libraries[@]}"; do
    # Ask Bazel for the path rather than assuming its canonical repository name.
    marker="$(bazel cquery "@tempo_verify_${library//-/_}//:foundry.toml" --output=files)"
    source_dir="$(dirname -- "$execution_root/$marker")"
    test -d "$source_dir/src"
    ln -sfn "$source_dir" "$verify_dir/lib/$library"
done
echo "Solidity libraries ready in $verify_dir/lib (managed by Bazel)."
