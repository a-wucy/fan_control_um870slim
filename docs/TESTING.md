# Validation and calibration

## Automated tests

Run as an ordinary user; no live hardware or root access is used:

```sh
python3 tests/run_tests.py
```

The runner executes Python unit tests and compiles the **same shared C
transaction code used by the module**, with AddressSanitizer and
UndefinedBehaviorSanitizer. A C compiler with those sanitizers is required.
Temporary files follow Python's normal selection; set `TMPDIR` if desired.

The current suite has **120 Python tests**, covering thresholds, hysteresis,
minimum-on timing, sensor identity/errors, signals, thermal exit status,
restoration, service startup/cleanup, exact-kernel checks, managed installation,
file-transaction rollback, preservation of unfamiliar files, and failed
uninstallation. DKMS tests exercise the real Bash configuration's 7.2/7.3 gates,
target-header selection, manual-to-DKMS migration, new-patch boot selection,
compressed modules, approved source/version checks, dependency/build failures,
override/symlink rejection, and recovery-preserving unregistration. They mock
DKMS/hardware operations and do not require the DKMS package. The shared C
tests exhaust the write allowlist over every
address/byte combination and exercise checked ownership/release, failures,
rate stepping, and the 1200->600 startup target sequence.

On **2026-11-06**, the standalone checkout passed all 90 Python tests and the
sanitized C tests again, plus systemd unit verification. It also rebuilt for
`7.2.11-arch1-1` with `W=1`/`-Werror`, retaining module version `0.1.1` and
source identity `59EA28AF6718E93890861ED`. Runtime controller/helper/unit code
was unchanged; driver changes were provenance comments only. This build was
not loaded over the working installation. Matching headers/build tools were
hash-checked and extracted without installing system packages.

Later on **2026-11-06**, a user-initiated full upgrade booted `7.2.14-arch1-1`.
The previous manual installation correctly refused to load its 7.2.11 binary;
missing matching module/headers caused firmware-only cooling, not a driver
compatibility failure. The user installed matching headers, `pahole`, and DKMS.
The unchanged driver rebuilt cleanly for 7.2.14 with `W=1`/`-Werror`, retaining
version `0.1.1` and source identity `59EA28AF6718E93890861ED`. Opt-in DKMS support
was added without changing the shared C core, controller policy, or systemd unit.
The expanded 120-test suite and sanitized C tests passed. A real isolated DKMS
build (ordinary user, temporary source/state trees, no live install) also built
7.2.14 with the expected identity and rejected a simulated 7.3 target with
DKMS's exclusion exit code 77. This validates the actual build configuration,
not just mocks; it is not a physical 7.3 test.

The user then ran the managed `install --dkms` migration on 7.2.14. Read-only
verification found DKMS `installed`, the exact-kernel compressed `.ko.zst`
module loaded with the approved identity, and the service enabled/running with
`Restart=no`. Quiet idle-off operation was active with no fault or pending
release. Installed scripts matched the checkout; core/controller/unit remained
unchanged, and the CPU-capable module was absent. This verifies installation
and initial control on 7.2.14, not a complete thermal regression or the first
future pacman-triggered rebuild/new boot.

Kernel builds use `W=1` and `-Werror`. Unit verification can also be run:

```sh
systemd-analyze verify minisforum-system-fan.service
```

CI runs only unprivileged Python/shared-C tests. It does not load a module,
install the service, or validate real hardware/firmware behavior. Unit verification
may need an existing installation because its commands reference installed paths.

## Recorded live results

These results are specific to the configuration in the main README, not a
promise for all UM870 Slim units.

| Check | Result |
|---|---|
| Observation-only module | Loaded and passed host/controller/policy gates with control writes disabled |
| Sensor distinction | EC system 24 C while RAM was about 31.5 C; they are not interchangeable |
| Nominal 800 RPM | Measured about 706..708 RPM with narrow spread |
| Nominal 600 RPM | Passed running-speed calibration; retained as normal low target |
| Nominal 400 RPM | Failed: mean 690 RPM, spread 879; 300/200 not attempted |
| Nominal 500 RPM | Passed a short probe: mean 545, spread 8; later measured 750, so not a guaranteed physical floor |
| Quiet-profile workload | Initial stop, load-triggered running, and clean firmware restoration |
| High target | Requested 1000; measured about 1031..1034 RPM |
| Demanding-game fallback | Hotter DIMM reached 55 C at about 703 s; RAM guard triggered, firmware restoration succeeded |
| Temperatures at that fallback | DIMMs 55/45.5 C, NVMe 42.85 C, EC system 47 C; specifically the RAM guard triggered |
| 2026-11-02 update | Old-service stop and post-stop recovery both verified firmware ownership; module 0.1.1 installed and idle-off operation verified |
| 2026-11-06 feedback | Maintainer reports positive multi-day use; read-only checks found the enabled/running service healthy |
| 2026-11-06 DKMS migration | Built/installed and started successfully on 7.2.14; approved source identity, idle-off, no fault/pending release |

The reduced 1200-RPM assist and 38-C stop threshold were installed on
2026-11-02. Positive everyday feedback is useful evidence, but is not a
quantitative timing/startup-reliability study. The update's verified shutdown
was on the previous build, not a separately instrumented stop of 0.1.1 after
a controlled fan-on session.

## New-machine validation

Be physically present, save work, use a light workload, and keep other EC/fan
utilities closed. Do not use these manual commands alongside an active service.
Do not load the original `minisforum_ec` driver.

1. Build and pass the automated tests. Load **without** `allow_control=1`:

   ```sh
   sudo insmod ./module/minisforum_system_fan.ko
   python3 control.py observe --samples 10
   ```

   Require `allow_control=0`, clean firmware ownership, plausible sensor/RPM
   readings, and no fault/pending release. Observation mode cannot repair a
   leftover override. Unload after successful observation:

   ```sh
   sudo rmmod minisforum_system_fan
   ```

2. Only after observation succeeds, load with the conservative default minimum
   of 800 RPM and test running speed/restoration:

   ```sh
   sudo insmod ./module/minisforum_system_fan.ko allow_control=1
   sudo python3 control.py probe --rpms 1000,800
   ```

   Confirm actual response, stable RPM, and the final `firmware_restored` and
   `probe_complete` events. Ctrl-C is supported for early stop; do not press it
   merely because the fan has settled. A failed target stops the sequence.
3. Test a supervised temporary quiet session at that conservative minimum:

   ```sh
   sudo python3 control.py run --profile quiet --middle-rpm 800 --duration 300
   ```

   Confirm startup/stop behavior and verified restoration. Lower targets require
   separate, deliberate calibration; the service's `min_rpm=500`/600 target is
   specific to the tested machine. Do not chase its failed 400-RPM target.
4. Before reloading another module/minimum, explicitly restore and verify first:

   ```sh
   sudo python3 control.py restore
   sudo rmmod minisforum_system_fan
   ```

   Run one command at a time; never continue after a restoration error. The
   calibrated service expects `allow_control=1 min_rpm=500`; the installer will
   refuse an already-loaded module with other parameters rather than silently
   reconfigure it.

## Remaining physical validation

Not every path is proven by ordinary positive use or mocks:

- Quantitatively verify natural 40->38-C cooldown/minimum-on timing and repeated
  starts from rest with the 1200-RPM assist, without stalls/hunting.
- Separately observe clean service stop/restart on 0.1.1 after a real fan-on
  session, fresh-boot preparation, and representative storage/load/cooldown use.
- Suspend/resume, the first future pacman-triggered DKMS rebuild/new boot,
  kernel rollback, and full physical uninstall remain dedicated end-to-end
  checks. Managed rebuild/install on 7.2.14 passed, but does not prove those
  other paths. The service is designed not to restart on resume.
- NVMe/EC emergency handoffs, real unreadable telemetry/write faults, live
  watchdog/crash recovery, failed restoration, and power-loss EC reset are not
  all physically validated. Never deliberately overheat components or freeze
  the kernel merely to check a box.
- Kernel 7.3 compatibility must be tested when that kernel is actually installed.

Record representative room conditions, temperatures, measured RPM/noise,
stop/start frequency, and handoffs. Redact hostnames, serial numbers, paths,
credentials, and unrelated journal entries before posting public diagnostics.
