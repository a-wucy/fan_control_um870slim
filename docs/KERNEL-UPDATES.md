# Kernel upgrades and managed updates

## Policy

The installation is **kernel-specific**, not tied permanently to kernel 7.2.
There is intentionally no DKMS, automatic download/build at boot, or package
hook executing repository code as root. Every installed module must match its
kernel exactly. Never use force-vermagic, force-modversion, or compatibility
bypasses.

If a new kernel has no matching module under
`/usr/local/lib/minisforum-system-fan/modules/<kernel>/`, service preparation
fails before loading an old build or taking control. Firmware cooling remains
in charge. The system may be noisier until manual installation is completed.

The maintainer's effective pacman configuration checked on **2026-11-06** had
no active `IgnorePkg` or `IgnoreGroup` kernel hold. No package-manager settings
needed changing, and no kernel upgrade was performed for publication. Pacman's
`HoldPkg = pacman glibc` concerns removal confirmation, not a kernel version pin.

## Arch Linux upgrade checklist (including the planned 7.3 upgrade)

Kernel **7.3 has not yet been built or live-tested** for this project. Do not
interpret these instructions as a compatibility guarantee.

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

You can also leave the enabled service alone during a normal kernel upgrade:
with no matching module, it fails closed to firmware. Explicitly disabling it
above makes the first new-kernel validation deliberate and avoids surprise
takeover if an older matching build already exists.

Old module builds managed by this installer are retained when adding a new
kernel. They do **not** preserve the old kernel or boot files themselves; those
are controlled by the distribution/boot setup. No kernel pin is required for
safe firmware fallback.

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
