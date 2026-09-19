# Tempo Specs

This directory contains Solidity spec verification tests and fuzz harnesses for Tempo's native precompile contracts.

`TempoTest.t.sol` assumes the Tempo precompiles already exist in the EVM and fails fast if they are missing.

## Dependencies in monotempo

The Solidity libraries (`forge-std`, `tempo-std`, and `solady`) are fetched as
commit- and SHA-256-pinned Bazel `http_archive`s, not Git submodules. The pins in
`tempo/tempo.MODULE.bazel` preserve the revisions in `foundry.lock`.

From the monorepo root, run:

```bash
./tempo/tips/verify/fetch-libs.sh
```

This builds the archives' `srcs` filegroups and creates ignored symlinks under
`lib/`, preserving the existing Foundry remappings. It requires Bazel, but not
Forge. Rerun it after changing pins or running `bazel clean --expunge`. Treat
the linked libraries as read-only: they live in Bazel's external repository
cache. Do not use `forge install` or `forge update` to manage them.

On an existing checkout, move any old `lib/forge-std`, `lib/tempo-std`, and
`lib/solady` directories aside first, preserving any local work. The script
refuses to overwrite directories; it can safely refresh its symlinks.

To update a dependency, update its revision in `foundry.lock` and its archive
URL, `strip_prefix`, and SHA-256 in `tempo/tempo.MODULE.bazel`, then rerun the
script. Compute the SHA-256 from the downloaded archive, not the Git commit.

This is dependency fetching only: `tips/verify` remains in `.bazelignore`, and
`bazel test //...` does not run the Solidity suite. Tests still require a
separately installed Tempo-capable Forge and its Solidity compiler. We may
revisit the `http_archive` approach in the future, including vendoring the
libraries or integrating the Solidity toolchain and tests into Bazel.

## Profiles

The checked-in `foundry.toml` uses two profiles, which default to the Tempo T6 hardfork and run against a Tempo-native EVM with enabled Rust precompiles:

- `default`: config for standard Foundry build/fmt/ABI tasks. Lighter optimizer and fuzz/invariant settings, for quicker output.
- `fuzz500`: config for extended invariant runs.

## Running Tests

Tests require a Tempo-capable `forge` binary.

Run the full suite:

```bash
cd tips/verify
forge test
```

Run with verbose output:

```bash
forge test -vvv
```

Run a specific test:

```bash
forge test --match-test test_mint
```

Use the lighter CI profile when you want to match CI settings locally:

```bash
cd tips/verify
FOUNDRY_PROFILE=fuzz500 forge test -vvv
```

If you frequently want the extended invariant profile by default, set it in your shell:

```bash
export FOUNDRY_PROFILE=fuzz500
forge test
```
