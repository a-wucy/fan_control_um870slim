# Contributing

This is an independent system-fan-only project, not the upstream CPU-capable
driver. Report issues and propose changes in this repository, not in the
original author's project on behalf of this variant.

- Preserve GPL-2.0, upstream attribution, and the CPU register/write prohibition.
- Keep exact host/EC/policy gates, verified firmware restoration, required
  RAM/NVMe monitoring, watchdog/fault latching, and `Restart=no`.
- Do not add force-load flags, silent sensor fallbacks, automatic restart after
  thermal handoff, or broad cleanup that discards unfamiliar files.
- Build and run `python3 tests/run_tests.py` without root. Add regression tests
  for behavioral changes, including recovery/error paths.
- Bump the module version for shared-core behavior changes and verify loaded
  versus built identity; see [kernel updates](docs/KERNEL-UPDATES.md).
- Do not commit generated `.ko`/`.o` files, installation manifests, credentials,
  raw machine journals, or other private diagnostics.
- Describe the exact hardware/firmware/kernel tested and distinguish mocked
  coverage from physical results. Never deliberately defeat cooling safeguards
  to manufacture a test result.

Contributions are made under the repository's GPL-2.0 license. No signed binary
release, universal compatibility, or unattended update mechanism is promised.
