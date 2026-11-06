#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Boot preparation and verified post-stop recovery. No automatic restart."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
import control


UNIT = "minisforum-system-fan.service"
PREFIX = Path("/usr/local/lib/minisforum-system-fan")
RUNTIME = Path("/run/minisforum-system-fan")
MODULES = Path("/sys/module")
KERNEL_MODULES = Path("/usr/lib/modules")
DKMS_PACKAGE = "minisforum-system-fan"
DKMS_VERSION = "0.1.1"
DKMS_KERNEL_PATTERN = r"7[.]2[.][0-9]+-arch[0-9]+-[0-9]+"


def dkms_kernel_allowed(kernel):
    return re.fullmatch(DKMS_KERNEL_PATTERN, kernel) is not None


def module_info(module, field, run=subprocess.run):
    return run(["/usr/bin/modinfo", "-F", field, str(module)], check=True,
               capture_output=True, text=True).stdout.strip()


def validate_module(module, kernel, run=subprocess.run, *, expected_version=None,
                    expected_source=None):
    if module.is_symlink() or not module.is_file():
        raise RuntimeError(f"no regular module for kernel {kernel}: {module}; firmware stays in charge")
    if module_info(module, "name", run) != control.NAME:
        raise RuntimeError("refusing a module other than the CPU-write-blocked system-fan driver")
    vermagic = module_info(module, "vermagic", run).split()
    if not vermagic or vermagic[0] != kernel:
        raise RuntimeError("module kernel version mismatch; never force-load it")
    version = module_info(module, "srcversion", run)
    if not version:
        raise RuntimeError("module has no source version; cannot verify a preloaded module")
    if expected_source is not None and version != expected_source:
        raise RuntimeError("DKMS module source identity differs from the approved source")
    if expected_version is not None and module_info(module, "version", run) != expected_version:
        raise RuntimeError("DKMS module version differs from the approved version")
    return version


class ServiceRuntime:
    def __init__(self, base=PREFIX, runtime=RUNTIME, modules=MODULES,
                 kernel=None, run=subprocess.run, kernel_modules=KERNEL_MODULES):
        self.base = base
        self.runtime = runtime
        self.modules = modules
        self.kernel = kernel or platform.release()
        self.run = run
        self.kernel_modules = kernel_modules

    @property
    def marker(self):
        return self.runtime / "prepared"

    @property
    def private_module(self):
        return self.base / "modules" / self.kernel / f"{control.NAME}.ko"

    @staticmethod
    def trusted_file(path, base):
        path.relative_to(base)
        for parent in [path, *path.parents]:
            if parent.is_symlink():
                raise RuntimeError(f"refusing a symlinked DKMS path: {parent}")
            info = parent.stat()
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise RuntimeError(f"DKMS path is not privately administrator-owned: {parent}")
            if parent == base:
                break
        if not path.is_file():
            raise RuntimeError(f"not a regular DKMS file: {path}")

    def dkms_settings(self):
        path = self.base / "dkms.json"
        if not path.exists() and not path.is_symlink():
            return None
        self.trusted_file(path, self.base)
        data = json.loads(path.read_text())
        if (not isinstance(data, dict) or set(data) != {"format", "version", "srcversion"}
                or data["format"] != 1
                or not isinstance(data["version"], str)
                or re.fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+", data["version"]) is None
                or not isinstance(data["srcversion"], str)
                or re.fullmatch(r"[0-9A-F]{16,64}", data["srcversion"]) is None):
            raise RuntimeError("unrecognized DKMS opt-in configuration; refusing control")
        return data

    @property
    def module(self):
        if self.dkms_settings() is None:
            return self.private_module
        if not dkms_kernel_allowed(self.kernel):
            raise RuntimeError(f"kernel {self.kernel} is outside approved Arch 7.2.*; firmware stays in charge")
        directory = self.kernel_modules / self.kernel / "updates/dkms"
        candidates = [directory / (control.NAME + suffix)
                      for suffix in [".ko", ".ko.zst", ".ko.xz", ".ko.gz"]]
        candidates = [path for path in candidates if path.exists() or path.is_symlink()]
        if len(candidates) != 1:
            raise RuntimeError(f"expected one exact-kernel DKMS module for {self.kernel}; "
                               "check matching headers and DKMS build logs; firmware stays in charge")
        self.trusted_file(candidates[0], self.kernel_modules)
        return candidates[0]

    def validate_current_module(self):
        settings = self.dkms_settings()
        return validate_module(
            self.module, self.kernel, self.run,
            expected_version=settings["version"] if settings else None,
            expected_source=settings["srcversion"] if settings else None)

    def validate_recovery_module(self):
        # A failed DKMS migration or a package update may leave the installed
        # DKMS binary absent. A managed private exact-kernel copy is still valid
        # for verifying the loaded identity before restoration/unload, never as
        # a fallback for starting control.
        if not self.private_module.exists():
            return self.validate_current_module()
        settings = self.dkms_settings()
        return validate_module(
            self.private_module, self.kernel, self.run,
            expected_version=settings["version"] if settings else None,
            expected_source=settings["srcversion"] if settings else None)

    def reject_original_driver(self):
        if (self.modules / "minisforum_ec").exists():
            raise RuntimeError("original CPU-controlling driver is loaded; refusing to interfere")

    def check_loaded(self, version, *, require_firmware=True):
        loaded = self.modules / control.NAME
        if (loaded / "srcversion").read_text().strip() != version:
            raise RuntimeError("different experimental module already loaded; restore and unload it first")
        if (loaded / "parameters/allow_control").read_text().strip() != "Y":
            raise RuntimeError("loaded module is observation-only; unload it explicitly before installing")
        if (loaded / "parameters/min_rpm").read_text().strip() != "500":
            raise RuntimeError("loaded module must have min_rpm=500; restore and reload it explicitly")
        if require_firmware:
            status = control.Driver.find().status()
            if (status["state"] != "firmware" or not status["firmware_owned"] or
                    status["fault"] or status["release_pending"]):
                raise RuntimeError("system fan is not cleanly firmware-owned; restore it before starting")

    def wait_for_sensors(self):
        deadline = time.monotonic() + 30
        while True:
            control.check_stop_requested()
            try:
                control.memory_sensors()
                control.nvme_sensors()
                return
            except (OSError, ValueError, RuntimeError) as error:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"sensors unavailable after 30 seconds; refusing control: {error}") from error
                time.sleep(1)

    def prepare(self):
        self.reject_original_driver()
        version = self.validate_current_module()
        self.wait_for_sensors()
        with control.exclusive_control():
            control.check_stop_requested()
            if not (self.modules / control.NAME).exists():
                self.run(["/usr/bin/insmod", str(self.module), "allow_control=1", "min_rpm=500"],
                         check=True)
            self.check_loaded(version)
            self.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.marker.is_symlink():
                raise RuntimeError("refusing a symlinked service marker")
            self.marker.write_text(version + "\n")
        control.emit("service_prepared", kernel=self.kernel)

    def cleanup(self):
        # ExecStopPost also runs after a failed ExecStartPre. Do not repair a
        # pre-existing external state if this service never prepared control.
        if not self.marker.exists():
            return
        self.reject_original_driver()
        if not (self.modules / control.NAME).exists():
            raise RuntimeError("service module disappeared; cannot verify firmware restoration")
        with control.exclusive_control():
            control.Driver.find().restore()
            self.marker.unlink()  # Keep this recovery marker if verification fails.
        control.emit("service_cleanup_verified")
        # Deliberately keep the module loaded for recovery retries on failures.
        # Uninstallation unloads it only after a separate verified restoration.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "cleanup"])
    args = parser.parse_args(argv)
    runtime = ServiceRuntime()
    getattr(runtime, args.action)()
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, control.stop_requested)
    signal.signal(signal.SIGTERM, control.stop_requested)
    try:
        sys.exit(main())
    except control.StopRequested:
        control.emit("service_start_cancelled")
        sys.exit(1)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        control.emit("service_error", error_type=type(error).__name__, message=str(error))
        sys.exit(1)
