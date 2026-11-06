# UM870 Slim system-fan control

Quiet, reversible **system-fan-only** control for a tested Minisforum UM870 Slim
on Linux. The CPU fan is left entirely to firmware: this driver has no CPU
fan-control, repair, restoration, or CPU EC telemetry interface.

This is an **independent, hardware-specific derivative**, not an upstream release
or manufacturer-supported tool. It builds on
[mordilloSan/fan_control](https://github.com/mordilloSan/fan_control) and the
hardware documentation in
[FanControl.MinisforumUM](https://github.com/zuyan9/FanControl.MinisforumUM).
See [NOTICE.md](NOTICE.md) for attribution and [LICENSE](LICENSE) for GPL-2.0.

> **Cooling is safety-critical.** Successful use on one machine does not certify
> other units, firmware, workloads, or kernels. A frozen kernel can defeat the
> watchdog and leave the last target applied. Keep the physical fan connected.
> Never bypass compatibility checks or force-load a mismatched module.

## Status and supported hardware

The maintainer reports positive multi-day use of the current policy as of
2026-11-06. Automated tests and live calibration/restoration have passed on:

| Component | Tested configuration |
|---|---|
| Computer | Minisforum UM870 Slim, Ryzen 7 8745H |
| Board | F7BSI, revision 1.0 |
| DMI product / version / family / SKU | EliteMini Series / 1.0 / EliteMini / MGF7BSI |
| BIOS / EC | BIOS 1.09; EC 0.5 |
| System | Arch Linux, kernel 7.2.11-arch1-1 |
| Module | minisforum_system_fan 0.1.1 |

Host identity, EC version, controller signatures, and the system-policy
fingerprint must all pass. There is no force option. Kernel **7.3 is not yet
validated**. See [validation results and remaining checks](docs/TESTING.md).

## How it works

- **`module/`**: the kernel driver accepts nominal RPM targets, ramps gradually,
  provides a 1200-RPM startup assist, and checks ownership, telemetry, stalls,
  hunting, and a userspace watchdog. Its only permitted EC-data writes are
  `0x0885` (system target, codes 0..51) and `0x088b` (override, 0 or 255).
- **`control.py`**: the controller uses the warmer of both DDR5 `spd5118` sensors
  and the hottest available NVMe temperature channel to choose a target.
- **`service.py` and the systemd unit**: load only a matching module, start the
  controller at boot, and independently verify firmware restoration after exit.
- **`install.py`**: manages administrator-owned copies and an independent
  uninstaller, preserving unfamiliar or modified files.

Loading the module defaults to observation-only and never automatically takes
control or repairs firmware state. Control requires `allow_control=1` and an
explicit target. The firmware system curve is never rewritten; restoration
clears the runtime override and verifies firmware ownership.

The driver provides `fan1_input`/`temp1_input` for monitoring tools, but uses
custom `target_rpm`, `restore`, and `status` controls. The `lm_sensors`
`fancontrol` program is **not a drop-in controller** for this interface.
Do not run competing EC/fan-control tools or automatic `pwmconfig` probing.

## Quiet policy

Temperatures below are degrees Celsius. Either component can request more
cooling; both must cool sufficiently before reducing it.

| Rising condition | Action |
|---|---|
| RAM >=55 or NVMe >=70 | Restore firmware and exit |
| RAM >=50 or NVMe >=60 | Request 1000 nominal RPM |
| RAM >=40 or NVMe >=50 | Request 600 nominal RPM |
| Initially below RAM 40 and NVMe 50 | Request stopped |

- Reduce high to low only at **RAM <=45 and NVMe <=55**.
- Stop again only at **RAM <=38 and NVMe <=45**, after at least **120 seconds**
  since first requesting a running target. Refreshes and low/high transitions
  do not reset this timer; safety handoff is never delayed by it.
- From a stopped target, startup assist requests **1200 nominal RPM**, then
  approaches the goal in 100-RPM steps every four seconds (about 24 seconds to
  600). Takeover from an already-running firmware target instead ramps from
  that target. This is not a global 1200-RPM cap.
- Targets are nominal EC codes, not guaranteed physical speeds. This unit
  passed 600/800/1000-RPM probes; 400 RPM was unstable and rejected.

Independently, the kernel returns control to firmware for EC system temperature
>=55 C, invalid/unreadable telemetry, ownership/write faults, settled stalls,
hunting, or 10 seconds without a userspace refresh. Required RAM/NVMe sensor
failures also stop the controller. See [SAFETY.md](docs/SAFETY.md).

**There is no automatic restart after a safety stop or resume.** The service has
`Restart=no`; cooling alone does not retake control. An enabled unit tries again
on the next boot, subject to all checks.

## Build and install

Use Python 3.10 or later, a C compiler, `make`, `kmod`, `pahole`, systemd, and
headers matching the **running** kernel. On Arch, the relevant packages include
`base-devel`, `linux-headers`, `pahole`, and `python`. Obtain them through your
normal full system upgrade, reboot if the kernel changed, and verify that the
running kernel and headers match; do not perform a partial kernel/header upgrade.

Run tests and build **without sudo**:

```sh
git clone https://github.com/a-wucy/fan_control_um870slim.git
cd fan_control_um870slim
python3 tests/run_tests.py
python3 control.py observe --samples 5
make -C module
uname -r
modinfo -F vermagic module/minisforum_system_fan.ko
```

The first word of vermagic must equal `uname -r`. Never force-load a build for
another kernel. If this is a new machine, first follow the supervised
[observation, calibration, and restoration checks](docs/TESTING.md#new-machine-validation).
The low-speed service settings were calibrated on one unit, not every UM870 Slim.

For a validated machine, install and enable boot operation:

```sh
sudo python3 install.py install
systemctl status minisforum-system-fan.service --no-pager
journalctl -u minisforum-system-fan.service -n 30 --no-pager
```

The installer does not compile or download code as root. It checks exact kernel,
module identity/parameters, sensors, managed-file hashes, and systemd overrides.
An existing matching service is stopped with verified cleanup before updating.
A different loaded build must be explicitly restored/unloaded first; see
[managed updates](docs/KERNEL-UPDATES.md#updating-the-driver-itself).

Installation creates copies under `/usr/local/lib/minisforum-system-fan/`,
kernel-versioned modules there, a private file-hash manifest, and
`/etc/systemd/system/minisforum-system-fan.service`. The service is enabled and
started. It loads with `allow_control=1 min_rpm=500`, while the normal low target
is 600. Sensor checks and watchdog refreshes run every second; routine logs are
limited to once per minute, with transitions and safety events logged immediately.

**The installed service does not depend on this checkout's location.** Moving
to this standalone repository does not require changing a working installation.

## Everyday operation

Read-only observation works alongside the service:

```sh
python3 control.py observe --profile quiet --samples 5
```

The preview has its own new policy history; its proposed target can differ from
the running service. Use the reported `status` for actual targets/ownership/RPM.
Under quiet control, a zero target still has override 255: stopped is not the
same as firmware-owned.

Stop quiet control and verify the journal's restoration messages:

```sh
sudo systemctl stop minisforum-system-fan.service
journalctl -u minisforum-system-fan.service -n 15 --no-pager
```

Successful restoration requires `firmware_owned=1`, `override=0`, `fault=0`, and
`release_pending=0`. The fan may briefly speed up during the original safety
restoration sequence. Normal stop leaves the module loaded for recovery and
does not disable next-boot startup.

After cooling, deliberately start again:

```sh
sudo systemctl start minisforum-system-fan.service
```

To remain firmware-controlled across boots:

```sh
sudo systemctl disable --now minisforum-system-fan.service
```

## Recovery and removal

Stop the service before manual control/restoration; never race controllers.
If needed, explicitly restore and verify while the module remains available:

```sh
sudo python3 /usr/local/lib/minisforum-system-fan/control.py restore
```

Do not force an unload or treat a warm reboot as proof of recovery. If
restoration fails, keep the module loaded for retries. Persistent failure may
require full shutdown, power removal, and the manufacturer's EC-reset procedure.

Complete removal is independent of this checkout:

```sh
sudo python3 /usr/local/lib/minisforum-system-fan/install.py uninstall
```

Removal disables/stops the unit, verifies restoration before unloading a present
matching module, and removes only unchanged managed files. Failed restoration
or unload retains recovery assets; modified/unfamiliar files are preserved.

## Kernel upgrades and maintenance

**The kernel need not remain pinned.** This project deliberately uses explicit,
per-kernel builds rather than DKMS or root-executed package hooks. A new kernel
without a matching installed module leaves cooling with firmware, potentially
noisier, until you build/test/install for that kernel. An old module is never
force-loaded. There is no automatic rebuild or restart after a safety handoff.

See **[KERNEL-UPDATES.md](docs/KERNEL-UPDATES.md)** for the Arch/7.3 upgrade and
rollback checklist. Do not assume an untested kernel has already been validated.

- [Safety and limitations](docs/SAFETY.md)
- [Validation, calibration, and tests](docs/TESTING.md)
- [Change history](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md) and [security reporting](SECURITY.md)
- [Provenance and license](NOTICE.md)
