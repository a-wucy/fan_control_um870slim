#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Boot preparation and verified post-stop recovery. No automatic restart."""
import argparse
from pathlib import Path
import platform
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


def module_info(module, field, run=subprocess.run):
    return run(["/usr/bin/modinfo", "-F", field, str(module)], check=True,
               capture_output=True, text=True).stdout.strip()


def validate_module(module, kernel, run=subprocess.run):
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
    return version


class ServiceRuntime:
    def __init__(self, base=PREFIX, runtime=RUNTIME, modules=MODULES,
                 kernel=None, run=subprocess.run):
        self.base = base
        self.runtime = runtime
        self.modules = modules
        self.kernel = kernel or platform.release()
        self.run = run

    @property
    def marker(self):
        return self.runtime / "prepared"

    @property
    def module(self):
        return self.base / "modules" / self.kernel / f"{control.NAME}.ko"

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
        version = validate_module(self.module, self.kernel, self.run)
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
