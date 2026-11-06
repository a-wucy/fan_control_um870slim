# Safety design and limitations

This document describes engineering safeguards, **not** a hardware-safety or
malware-free certification. The driver is privileged code interacting with
undocumented firmware behavior. Ordinary bugs can damage cooling or stability.

## System-only boundary

The dedicated module does not include the original CPU-capable driver. There
are no CPU table, override, repair, restoration, or CPU EC telemetry operations.
Both the shared transaction layer and physical EC-data write primitive allow
only `0x0885` (system target 0..51) and `0x088b` (override 0/255). Tests exhaust
all 65,536 EC addresses and 256 byte values against that write allowlist.

Indirect EC reads necessarily write address-selection latches; observation-only
means no **fan-control data** writes, not literally no port writes. CPU fan RPM
can still change naturally under its own firmware policy as load/airflow changes.
This module does not undo CPU changes made by unrelated software.

## Admission and ownership

Exact DMI identity, EC 0.5, controller signatures, and a system-policy fingerprint
are mandatory. The original system curve/table is never rewritten. Loading
defaults to observation-only and never automatically repairs external state.
`allow_control` is an immutable load-time opt-in, and explicit targets are still
needed to take control. ACPI/I/O-port conflicts are errors, not reasons to relax
resource checks.

All port operations are serialized and the ports are exclusively reserved.
Writes check transaction preconditions/readback. This coordinates participating
Linux code, not every possible undocumented firmware/SMM interaction.

## Two safety layers

The userspace controller checks the warmer DDR5 sensor and hottest available
NVMe channel. It checks identities/paths and refuses missing or implausible
required telemetry; it does not silently weaken monitoring to RAM-only. The
55-C RAM and 70-C NVMe guards restore firmware and exit 75. These are chosen
experimental limits, not manufacturer-certified limits for every component.

The independent kernel monitor checks every two seconds for a 10-second missing
refresh, EC system temperature invalid or >=55 C, unreadable telemetry, lost
ownership, failed writes, settled nonzero-target stalls, and excessive RPM
spread. The EC temperature's physical source is not established: it is **not**
treated as RAM temperature or converted using an assumed offset.

A fallback latches a fault and rejects new targets until an explicit restore.
Failed release is retried while the module remains loaded. The controller exits
on faults, and the systemd service has `Restart=no`. Cooling alone does not
silently retake control during that boot.

## Verified restoration

Restoration checks identity/policy, seeds the maximum system target, clears the
runtime override, and verifies firmware ownership. It can briefly increase RPM;
the lower startup assist does not reduce this safety sequence. Tachometer RPM
can lag the new firmware target and is not itself proof of a failed handoff.

Normal completion, exceptions, missing sensors, SIGINT, and SIGTERM all attempt
restoration. Signal handlers request stopping at safe boundaries instead of
interrupting restoration. The post-stop systemd helper independently verifies
cleanup. SIGKILL relies on the kernel watchdog instead of Python cleanup.

Suspend requires restoration first; resume does not automatically take control
again. Failed restoration/unload keeps recovery assets. Successful `rmmod`
alone does not prove restoration: the module makes final attempts but cannot
guarantee firmware accepted them. Prefer explicit verification while the module
is still available for retries.

## Installation boundary

Installation uses administrator-owned copies, never home-directory execution
at boot. Exact-kernel module checks, a private managed-file hash manifest,
symlink/ownership checks, and systemd override checks limit unintended changes.
Manual installations never compile as root. Explicit `--dkms` opt-in authorizes
root-run builds of the managed source copy, using Arch's standard DKMS hooks.
This expands the trusted build surface; review source before enabling it. Both
the build configuration and loader restrict automatic operation to standard Arch
7.2 patch releases, with exact vermagic and approved version/source checks.
Other kernels, missing headers, and build failures leave firmware in charge.
No boot-time downloads/builds, custom restart hooks, or forced module loads are
added. Modified/unfamiliar files are not silently overwritten or deleted.
The project changes no initramfs, BIOS, or Secure Boot enrollment configuration;
DKMS may create its own local signing assets. Uninstallation verifies restoration
and unload before unregistering the managed DKMS version, retaining sources and
tools if recovery/unregistration fails. Build outputs, DKMS private state, and
local installation manifests are not public source files.

## Residual risks

- **A frozen kernel can defeat every software watchdog and cleanup path.**
  While the override is held, the normal firmware temperature policy is not an
  independent fallback. Recovery may require full power removal/manufacturer
  EC reset; a warm reboot is not a proven reset.
- One machine and several workloads are not worst-case thermal validation.
  CPU, SSD, RAM, controller, PMIC, VRM, and enclosure hotspots are not all fully
  represented by monitored inputs. Stopping the system fan changes airflow.
- The demanding-game test could not stay below the RAM guard at a 1000-RPM
  target. Firmware handoff is intentional; do not raise limits or add repeated
  automatic takeover merely to suppress it.
- Nominal targets do not guarantee physical RPM. Fans age and identical models
  can behave differently. Short running-speed probes do not prove every startup
  from rest will work.
- Different kernels, firmware, boards, fan batches, suspend/resume, complete
  uninstall, and real fault recovery need their own validation. Mock tests do
  not prove physical emergency paths.
- No general operating-system/toolchain/firmware audit or reproducible-build
  attestation is claimed. Upstream credit is not upstream endorsement.

See [TESTING.md](TESTING.md) for evidence and unverified paths, and
[SECURITY.md](../SECURITY.md) for reporting. Preserve these limitations when
redistributing the project.
