# Attribution and provenance

This independent UM870 Slim system-fan-only derivative is distributed under
**GNU GPL version 2**; the complete license is in [LICENSE](LICENSE). Existing
GPL-2.0 source notices are retained. There is no warranty, including any
guarantee of cooling, hardware compatibility, or recovery after a fault.

## Linux foundation

The driver adapts the I2EC access and checked system-fan transactions from:

- Author/project: **mordilloSan**, [fan_control](https://github.com/mordilloSan/fan_control).
- Reviewed base: commit
  [`5d0845a7d49fc9e1a8fba0c7efae746e94c63211`](https://github.com/mordilloSan/fan_control/tree/5d0845a7d49fc9e1a8fba0c7efae746e94c63211),
  tagged `v1.1.0`.
- Source: [`module/minisforum_ec.c`](https://github.com/mordilloSan/fan_control/blob/5d0845a7d49fc9e1a8fba0c7efae746e94c63211/module/minisforum_ec.c),
  with its GPL-2.0 notice and upstream references.

Changes developed 2026-11-01 through 2026-11-06 include a separate module that
excludes all CPU control/repair/restore operations; the system-only write
allowlist and shared transaction tests; watchdog/stall/recovery checks; a
RAM/NVMe controller; calibrated quiet policy; boot service; managed installer
and uninstaller; and standalone documentation and portable tests.

The original checkout and its remote are not modified by this standalone
distribution. The original CPU-capable driver, binary releases, packaging,
workflows, and Git history are not included. This repository is not an upstream
release, and the original author is not responsible for this variant.

## Hardware research and earlier references

The Linux source credits **zuyan9** and
[FanControl.MinisforumUM](https://github.com/zuyan9/FanControl.MinisforumUM),
commit
[`d61e46cc985a829b783618e76d6d08fb4f73dc58`](https://github.com/zuyan9/FanControl.MinisforumUM/tree/d61e46cc985a829b783618e76d6d08fb4f73dc58),
especially `F7bsdProfile.cs` and `PawnIoF7bsdBackend.cs`, for firmware profiles,
register mappings, admission checks, and handoff/restoration sequences. These
references and upstream credit are preserved in the C source.

No Windows/C# source or binary is vendored here, and this notice does not
purport to relicense that separate project. Its references describe the
hardware/design provenance of the GPL Linux adaptation.

Minisforum and the upstream projects do not endorse or support this derivative.
