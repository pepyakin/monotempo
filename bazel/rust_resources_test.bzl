"""Check the actual patched rules_rust scheduler without compiling Rust."""

load("@bazel_skylib//lib:unittest.bzl", "asserts", "unittest")
load("@rules_rust//rust/private:rustc_resource_set.bzl", "get_rustc_resource_set", "is_codegen_units_enabled")

def _rust_resources_test_impl(ctx):
    env = unittest.begin(ctx)
    # Compiler partitioning (including dev's 256 units and the disabled setting)
    # must not change scheduling or turn off memory accounting.
    for units in [-1, 1, 4, 256]:
        for cc_link in [False, True]:
            toolchain = struct(
                _codegen_units = units,
                _experimental_use_cc_common_link = cc_link,
            )
            resources = get_rustc_resource_set(toolchain)
            for inputs in [1, 10000]:
                asserts.equals(env, {"cpu": 1, "memory": 3072}, resources("linux", inputs))
            # The patch must not change whether rustc receives codegen-units.
            asserts.equals(env, units > 0 and not cc_link, is_codegen_units_enabled(toolchain))
    return unittest.end(env)

rust_resources_test = unittest.make(_rust_resources_test_impl)
