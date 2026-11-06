# Kernel upgrades and managed updates

## Policy

The installation remains **exact-kernel-specific**: a `7.2.11` binary must not
be loaded into `7.2.14`. Automatic patch-release support means **rebuilding the
same source for each new kernel**, not weakening vermagic checks.

There are now two modes:

- **Opt-in DKMS:** automatically build for standard Arch
  `7.2.<patch>-arch<revision>-<release>` on x86_64. Both DKMS and the service gate
  the approved series; `7.3.*`, LTS, other flavors, and release candidates are
  excluded. Loading also checks exact vermagic and approved source/version.
- **Manual:** keep the original explicit per-kernel installation. No DKMS
  registration or package-hook builds are enabled in this mode.

Never use force-vermagic, force-modversion, or compatibility bypasses. No mode
downloads code or builds at boot. Missing/failed builds leave firmware cooling
in charge. Neither package updates nor safety handoffs restart the controller.

The maintainer's effective pacman configuration checked on **2026-11-06** had
no active `IgnorePkg` or `IgnoreGroup` kernel hold. No package-manager settings
needed changing, and no kernel upgrade was performed for publication. Pacman's
`HoldPkg = pacman glibc` concerns removal confirmation, not a kernel version pin.

## Enable automatic 7.2 patch-release builds

Install `dkms`, `linux-headers`, and `pahole` through your normal full upgrade,
alongside the usual compiler/`make` dependencies. If you have just completed a
full upgrade and the repositories still contain headers matching the running
kernel, installing these missing dependencies without another refresh is fine:

```sh
sudo pacman -S --needed linux-headers pahole dkms
```

Do not use `pacman -Sy` or selectively upgrade to headers for a different kernel.
Confirm the running kernel and installed header target match. Then, as your
ordinary user, test/build before installing:

```sh
python3 tests/run_tests.py
make -C module
modinfo -F vermagic module/minisforum_system_fan.ko
sudo python3 install.py install --dkms
```

The installer migrates a recognized manual installation without deleting older
recovery modules. It copies a source/license whitelist to
`/usr/src/minisforum-system-fan-0.1.1/`, records the approved module source/version,
and invokes DKMS for the exact running kernel before enabling/starting the
service. DKMS builds **as root**: this is an explicit change from the original
manual-only design. Only enable it for source you trust. No BIOS/Secure Boot
enrollment settings are changed; DKMS may manage its own local signing assets.

Subsequent builds use the administrator-owned source copy, not the home checkout.
Arch's existing DKMS package hooks build/install during transactions that update
the kernel/headers. No custom project hook is installed, no module load/reload
is requested by this project during the transaction, and `Restart=no` remains.
Unreviewed module-specific DKMS overrides are refused, not overwritten.

## Ordinary 7.2 updates with DKMS enabled

1. Run your normal full `pacman -Syu`. Keep `linux-headers` installed: it will
   update alongside `linux`.
2. Check that pacman's DKMS step succeeded. To inspect the registered builds:

   ```sh
   dkms status -m minisforum-system-fan
   ```

3. Reboot normally. The enabled fan service loads the exact new build from
   `/usr/lib/modules/<kernel>/updates/dkms/`, including compressed `.ko` files,
   only after its admission/sensor/identity checks. It does not retake control
   automatically in the same boot after a thermal stop.
4. Check `systemctl status minisforum-system-fan.service` and the journal.

A successful compilation does not prove identical physical behavior on every
patch release. Observe cooling after updates. If a build fails, firmware remains
in charge; keep it that way until the error is resolved rather than bypassing
checks. Global administrator DKMS policy can affect builds; do not enable
automatic modprobe/restart policies for this module.

## New minor series (7.3) or manual-mode upgrade checklist

Kernel **7.3 has not yet been built or live-tested**. DKMS deliberately skips it,
and the DKMS-enabled service refuses it even if someone supplies a matching
binary. Review the source/API and update **both** series gates with tests before
opting in. Until then, leave cooling with firmware. The checklist below also
applies to every kernel update when using manual mode.

1. Save work and arrange physical access. Have a working recovery boot option
   and know how to disable the fan service. A known-good alternative kernel can
   be useful, but its own module would need its own matching build. This project
   does not alter your bootloader or install a fallback kernel automatically.
2. Use your normal **full Arch system upgrade**, including build dependencies
   and headers for your actual kernel package. For the standard `linux` kernel:

   ```sh
   sudo pacman -Syu --needed base-devel linux-headers pahole python
   ```

   This is an upgrade command to run when you choose to upgrade, not a setup
   prerequisite to execute blindly today. Other kernel packages require their
   corresponding headers. Do not refresh repositories and selectively upgrade
   only headers or force a partial kernel upgrade.
3. Before rebooting, restore firmware control and prevent quiet takeover while
   validating the new kernel:

   ```sh
   sudo systemctl disable --now minisforum-system-fan.service
   journalctl -u minisforum-system-fan.service -n 15 --no-pager
   ```

   Confirm successful firmware restoration and normal fan behavior. On any
   restoration failure, stop here; keep the recovery module/assets available
   and follow [recovery guidance](../README.md#recovery-and-removal).
4. Reboot normally into the new kernel. This does not prove that EC state was
   reset. Confirm normal firmware cooling and identify the running kernel:

   ```sh
   uname -r
   ```

5. In this checkout, build and test **as your normal user**:

   ```sh
   make -C module
   python3 tests/run_tests.py
   modinfo -F vermagic module/minisforum_system_fan.ko
   python3 control.py observe --samples 5
   ```

   Vermagic's first word must exactly equal `uname -r`. Both DIMMs and the NVMe
   sensors must be readable. If the build, admission, or sensors fail, leave the
   service disabled and keep firmware cooling; investigate rather than bypass.
6. While present and under a light workload, install the matching build:

   ```sh
   sudo python3 install.py install
   systemctl status minisforum-system-fan.service --no-pager
   journalctl -u minisforum-system-fan.service -n 30 --no-pager
   ```

   The installer re-enables the unit and starts it. Check loaded identity,
   ownership, faults, actual RPM, natural fan startup, and ordinary cooldown.
   Test a clean stop/restoration before relying on unattended operation again.
   Do not deliberately overheat components to test emergency limits.

You can also leave the enabled service alone: an excluded kernel or missing
matching build fails closed to firmware. Explicitly disabling it above makes
new-series validation deliberate. For a DKMS installation, do not try the
installation step on 7.3 until its support has actually been reviewed/enabled.

Private recovery builds are retained when adding kernels. DKMS owns its generated
builds and follows the distribution's kernel-removal hooks. Neither preserves
the old kernel/boot files themselves: keep a separate recovery boot option.

## Updating the driver itself

First build/test without root. A changed loaded module is not replaced by
copying a new `.ko` file or restarting Python. When the module source identity
changes, run these commands **one at a time; stop on any error**:

```sh
sudo systemctl stop minisforum-system-fan.service
sudo python3 control.py restore
sudo rmmod minisforum_system_fan
sudo python3 install.py install
```

The restore command must report `firmware_restored` before unloading. Never
force removal after a failed restoration. Installation verifies existing file
hashes and refuses to silently overwrite local administrator edits or unit
drop-ins. Reconcile/preserve unfamiliar changes first.

**For an opted-in DKMS installation, first use the installed uninstaller** to
restore/unload and unregister the old managed version, then install the new
version with `--dkms` after testing/building. An ordinary reinstall preserves
DKMS mode, but refuses a changed source identity: DKMS must not silently reuse
a cached binary for altered source under the same registration. Keep
`PACKAGE_VERSION`, `DKMS_VERSION`, and `MODULE_VERSION` synchronized when changing
the driver. The uninstaller removes only this package/version, not DKMS or its
global configuration; failed restoration/unregistration retains recovery files.

For shared-core **behavior** changes, bump `MODULE_VERSION` as well as the code
and verify built-versus-loaded `srcversion` differs. In the original build, a
header-only behavior change altered the binary but did not alter `srcversion`
until the module version was bumped. A matching ID is an operational check,
not cryptographic proof of arbitrary external code's behavior.

## Rollback

- Return to firmware now and at future boots:

  ```sh
  sudo systemctl disable --now minisforum-system-fan.service
  ```

- Use the installed uninstaller to remove managed files, independently of the
  checkout:

  ```sh
  sudo python3 /usr/local/lib/minisforum-system-fan/install.py uninstall
  ```

- Use your distribution's normal recovery/boot procedure for a bad kernel.
  Do not load a module built for another kernel to try to restore quiet control.
- A thermal fallback intentionally stays firmware-owned until a deliberate
  manual start or the next enabled boot. Do not add automatic restarts to hide it.
