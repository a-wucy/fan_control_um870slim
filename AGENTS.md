# Project guidance

- This is `a-wucy/fan_control_um870slim`, an independent GPL-2.0 system-fan-only
  project. Do not modify or push from the sibling original `fan_control`
  checkout, and never add its upstream repository as a push destination here.
- Preserve the CPU write fence, verified firmware restoration, exact hardware
  gates, required sensors, watchdog/fault latching, and `Restart=no`.
- Source edits/tests do not authorize live hardware writes, service restarts,
  module reloads, firmware changes, kernel upgrades, or privileged installation.
  Obtain specific user authorization before those operations.
- Test with `python3 tests/run_tests.py` as an ordinary user. Shared C tests use
  the real transaction header and address/undefined-behavior sanitizers.
- Read `docs/KERNEL-UPDATES.md` before kernel/module updates. Builds must match
  the target running kernel; never force-load a mismatched module. Bump module
  version for shared-core behavior changes and verify source identity changes.
- Keep private state, journals, generated binaries, and credentials out of Git.
  Preserve licensing/provenance in `NOTICE.md` and `LICENSE`.
