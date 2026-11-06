#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Install/uninstall the quiet boot service, with optional guarded DKMS builds.

Only dedicated managed paths are changed. Modified or unfamiliar installed
files are preserved, not overwritten/deleted. --dkms explicitly opts into
root-run DKMS builds for standard Arch 7.2.*. No packages, modprobe, initramfs,
BIOS, or Secure Boot configuration are changed by this installer.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import stat
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
import control
import service


PREFIX = service.PREFIX.relative_to("/")
UNIT = Path("etc/systemd/system") / service.UNIT
MANIFEST = PREFIX / "manifest.json"
DKMS_STATE = PREFIX / "dkms.json"
DKMS_SOURCE = Path("usr/src") / f"{service.DKMS_PACKAGE}-{service.DKMS_VERSION}"
DKMS_FILES = ("dkms.conf", "module/Kbuild", "module/Makefile",
              "module/minisforum_system_fan.c", "module/system_fan_core.h", "LICENSE", "NOTICE.md")


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Installer:
    def __init__(self, root=Path("/"), source=None, kernel=None, run=subprocess.run):
        self.root = root.resolve()
        self.source = source or Path(__file__).resolve().parent
        self.kernel = kernel or platform.release()
        if not re.fullmatch(r"[A-Za-z0-9._+-]+", self.kernel):
            raise ValueError("unexpected kernel release string")
        self.run = run
        self.runtime = service.ServiceRuntime(
            base=self.root / PREFIX, runtime=self.root / service.RUNTIME.relative_to("/"),
            modules=self.root / service.MODULES.relative_to("/"), kernel=self.kernel, run=run,
            kernel_modules=self.root / service.KERNEL_MODULES.relative_to("/"))

    def ctl(self, *args, check=True):
        return self.run(["/usr/bin/systemctl", *args], check=check,
                        capture_output=True, text=True)

    def property(self, name, *, check=True):
        return self.ctl("show", service.UNIT, f"--property={name}", "--value", check=check).stdout.strip()

    def check_unit_configuration(self, *, installed):
        fragment = self.property("FragmentPath", check=False)
        if fragment and Path(fragment) != self.root / UNIT:
            raise RuntimeError(f"refusing an existing unit supplied by another location: {fragment}")
        if self.property("DropInPaths", check=False):
            raise RuntimeError("unreviewed systemd drop-ins affect this unit; refusing to change them")
        for base in ["etc", "run", "usr/local/lib", "usr/lib"]:
            directory = self.root / base / "systemd/system" / (service.UNIT + ".d")
            if directory.exists() or directory.is_symlink():
                raise RuntimeError(f"unreviewed unit override directory: {directory}")
        if installed:
            if not fragment:
                raise RuntimeError("systemd did not load the installed unit")
            if self.property("Restart") != "no":
                raise RuntimeError("service must never automatically restart after a safety handoff")

    def target(self, relative):
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"unsafe managed path: {relative}")
        result = self.root / path
        for parent in [result, *result.parents]:
            if parent == self.root:
                break
            if parent.is_symlink():
                raise RuntimeError(f"refusing symlinked install path: {parent}")
            if parent.exists():
                info = parent.stat()
                if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    raise RuntimeError(f"install path is not privately administrator-owned: {parent}")
        return result

    @staticmethod
    def allowed(relative):
        path = Path(relative)
        if str(path) != relative or path.is_absolute() or ".." in path.parts:
            return False
        if path in {PREFIX / "control.py", PREFIX / "service.py", PREFIX / "install.py", UNIT, DKMS_STATE}:
            return True
        if len(path.parts) >= 4 and path.parts[:2] == ("usr", "src"):
            return (re.fullmatch(re.escape(service.DKMS_PACKAGE) + r"-[0-9]+[.][0-9]+[.][0-9]+",
                                 path.parts[2]) is not None
                    and str(Path(*path.parts[3:])) in DKMS_FILES)
        try:
            parts = path.relative_to(PREFIX / "modules").parts
        except ValueError:
            return False
        return (len(parts) == 2 and re.fullmatch(r"[A-Za-z0-9._+-]+", parts[0]) is not None
                and parts[1] == f"{control.NAME}.ko")

    def managed_files(self):
        prefix = self.target(str(PREFIX))
        manifest = self.target(str(MANIFEST))
        unit = self.target(str(UNIT))
        if not prefix.exists() and not unit.exists():
            return {}
        if not manifest.is_file():
            raise RuntimeError("unfamiliar existing installation paths; refusing to overwrite or remove them")
        data = json.loads(manifest.read_text())
        if data.get("format") != 1 or not isinstance(data.get("files"), dict):
            raise RuntimeError("unrecognized installation manifest")
        files = data["files"]
        required = {str(PREFIX / "control.py"), str(PREFIX / "service.py"),
                    str(PREFIX / "install.py"), str(UNIT)}
        if not required <= files.keys():
            raise RuntimeError("incomplete installation manifest")
        for relative, expected in files.items():
            if not self.allowed(relative) or not isinstance(expected, str):
                raise RuntimeError(f"unexpected managed path/hash: {relative}")
            path = self.target(relative)
            if not path.is_file() or digest(path.read_bytes()) != expected:
                raise RuntimeError(f"installed file is missing or modified; preserve/reconcile it first: {path}")
        return files

    @staticmethod
    def atomic_write(path, data, mode=0o644):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".fan-install-", delete=False) as file:
                temporary = Path(file.name)
                os.fchmod(file.fileno(), mode)
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def write_files(self, payload, owned):
        manifest = dict(owned)
        manifest.update({relative: digest(data) for relative, data in payload.items()})
        payload = dict(payload)
        payload[str(MANIFEST)] = (json.dumps({"format": 1, "files": manifest}, indent=2) + "\n").encode()
        originals = {}
        directories = []
        try:
            for relative, data in payload.items():
                path = self.target(relative)
                if path.exists():
                    if relative not in owned and relative != str(MANIFEST):
                        raise RuntimeError(f"refusing to overwrite an unmanaged file: {path}")
                    originals[path] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                else:
                    originals[path] = None
                missing = []
                parent = path.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for parent in reversed(missing):
                    parent.mkdir(mode=0o755)
                    directories.append(parent)
                self.atomic_write(path, data, 0o600 if relative == str(MANIFEST) else 0o644)
        except Exception:
            # Roll back only exact paths touched by this transaction. Never
            # recursively delete a directory that could contain user work.
            for path, original in reversed(list(originals.items())):
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    self.atomic_write(path, *original)
            for directory in reversed(directories):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise

    def dkms(self, action, version=service.DKMS_VERSION, *extra):
        args = ["/usr/bin/dkms", action, "-m", service.DKMS_PACKAGE]
        if version is not None:
            args.extend(["-v", version])
        return self.run([*args, *extra], check=True, capture_output=True, text=True)

    def check_dkms_configuration(self):
        if not service.dkms_kernel_allowed(self.kernel):
            raise RuntimeError("automatic builds are restricted to standard Arch 7.2.* kernels")
        for command in ["dkms", "make", "gcc", "pahole"]:
            if not (self.root / "usr/bin" / command).is_file():
                raise RuntimeError(f"missing {command}; install matching headers and DKMS build dependencies first")
        release = self.runtime.kernel_modules / self.kernel / "build/include/config/kernel.release"
        if not release.is_file() or release.read_text().strip() != self.kernel:
            raise RuntimeError("missing exact-kernel headers; install headers matching the running kernel first")
        overrides = self.root / "etc/dkms"
        if overrides.exists():
            for path in overrides.iterdir():
                if (path.name == service.DKMS_PACKAGE or path.name == service.DKMS_PACKAGE + ".conf"
                        or path.name.startswith(service.DKMS_PACKAGE + "-")):
                    raise RuntimeError(f"unreviewed DKMS override; preserve/reconcile it first: {path}")

    def install(self, *, dkms=False):
        control.check_stop_requested()
        owned = self.managed_files()
        self.check_unit_configuration(installed=False)
        module = self.source / "module" / f"{control.NAME}.ko"
        version = service.validate_module(module, self.kernel, self.run)
        dkms = dkms or str(DKMS_STATE) in owned
        if dkms:
            self.check_dkms_configuration()
            if service.module_info(module, "version", self.run) != service.DKMS_VERSION:
                raise RuntimeError("built module version and DKMS package version differ")
            previous = self.runtime.dkms_settings()
            settings = {"format": 1, "version": service.DKMS_VERSION, "srcversion": version}
            if previous is not None and previous != settings:
                raise RuntimeError("DKMS source identity changed; uninstall the previous managed DKMS version first")
            registered = self.dkms("status", None).stdout.strip()
            expected = re.escape(service.DKMS_PACKAGE + "/" + service.DKMS_VERSION) + r"(?:,|:)"
            if registered and (previous is None or any(re.match(expected, line) is None
                                                      for line in registered.splitlines())):
                raise RuntimeError("unfamiliar existing DKMS registration; refusing to replace it")
        relative_module = PREFIX / "modules" / self.kernel / module.name
        payload = {
            str(PREFIX / "control.py"): (self.source / "control.py").read_bytes(),
            str(PREFIX / "service.py"): (self.source / "service.py").read_bytes(),
            str(PREFIX / "install.py"): (self.source / "install.py").read_bytes(),
            str(relative_module): module.read_bytes(),
            str(UNIT): (self.source / service.UNIT).read_bytes(),
        }
        if dkms:
            payload[str(DKMS_STATE)] = (json.dumps(settings, indent=2) + "\n").encode()
            payload.update({str(DKMS_SOURCE / name): (self.source / name).read_bytes()
                            for name in DKMS_FILES})
        for relative in payload:
            path = self.target(relative)
            if path.exists() and relative not in owned:
                raise RuntimeError(f"refusing to overwrite an unmanaged file: {path}")
        was_active = self.ctl("is-active", service.UNIT, check=False).returncode == 0
        if was_active:
            self.ctl("stop", service.UNIT)  # Uses verified service cleanup.
        self.runtime.reject_original_driver()
        with control.exclusive_control():
            control.memory_sensors()
            control.nvme_sensors()
            if (self.runtime.modules / control.NAME).exists():
                self.runtime.check_loaded(version)
            control.check_stop_requested()
            self.write_files(payload, owned)
        try:
            if dkms:
                control.check_stop_requested()
                # DKMS builds for this exact release, never loads/reloads it.
                # Arch's existing DKMS hooks handle subsequent kernel upgrades.
                self.dkms("install", service.DKMS_VERSION, "-k", self.kernel)
                self.runtime.validate_current_module()
            control.check_stop_requested()
            self.ctl("daemon-reload")
            self.check_unit_configuration(installed=True)
            control.check_stop_requested()
            self.ctl("enable", service.UNIT)
            control.check_stop_requested()
            self.ctl("start", service.UNIT)
            control.check_stop_requested()
        except (control.StopRequested, OSError, ValueError, RuntimeError, subprocess.CalledProcessError):
            # Keep the managed recovery files if activation fails. Uninstall
            # remains available, and no retry/autostart is enabled by accident.
            self.ctl("disable", service.UNIT, check=False)
            self.ctl("stop", service.UNIT, check=False)
            raise
        control.emit("installed", service=service.UNIT, kernel=self.kernel,
                      profile="quiet", low_rpm=600, automatic_restart=False,
                      dkms=dkms, automatic_kernel_series="Arch 7.2.*" if dkms else None)

    def uninstall(self):
        control.check_stop_requested()
        owned = self.managed_files()
        if not owned:
            control.emit("not_installed")
            return
        self.ctl("disable", service.UNIT)
        self.ctl("stop", service.UNIT)
        self.runtime.reject_original_driver()
        settings = self.runtime.dkms_settings()
        restoration_verified = False
        with control.exclusive_control():
            if (self.runtime.modules / control.NAME).exists():
                version = self.runtime.validate_recovery_module()
                self.runtime.check_loaded(version, require_firmware=False)
                control.Driver.find().restore()
                restoration_verified = True
                # Refuse to delete recovery assets if restoration or unload fails.
                self.run(["/usr/bin/rmmod", control.NAME], check=True)
            self.managed_files()  # Do not discard edits made during stop/recovery.
            if settings and self.dkms("status", settings["version"]).stdout.strip():
                # Only unregister this managed package/version, after verified
                # restoration/unload. A failure retains every recovery asset.
                self.dkms("remove", settings["version"], "--all")
            directories = {self.root / PREFIX}
            for relative in owned:
                path = self.target(relative)
                path.unlink()
                for parent in path.parents:
                    if parent == self.root / PREFIX:
                        break
                    if (self.root / PREFIX) in parent.parents:
                        directories.add(parent)
                if (self.root / "usr/src") in path.parents:
                    source = self.root / "usr/src" / path.relative_to(self.root / "usr/src").parts[0]
                    for parent in path.parents:
                        directories.add(parent)
                        if parent == source:
                            break
            self.target(str(MANIFEST)).unlink()
            for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass  # Preserve any extra files/directories, never recursive-delete.
            self.runtime.marker.unlink(missing_ok=True)
            try:
                self.runtime.runtime.rmdir()
            except OSError:
                pass
        self.ctl("daemon-reload")
        control.emit("uninstalled", restoration_verified=restoration_verified, module_loaded=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "uninstall"])
    parser.add_argument("--dkms", action="store_true",
                        help="opt into DKMS automatic builds for standard Arch 7.2.* only")
    args = parser.parse_args(argv)
    if args.dkms and args.action != "install":
        parser.error("--dkms is only valid with install")
    if os.geteuid() != 0:
        parser.error("run with sudo; tests and building should be done without sudo")
    if not Path("/run/systemd/system").is_dir():
        raise RuntimeError("this installer requires systemd as the system manager")
    if args.action == "install":
        Installer().install(dkms=args.dkms)
    else:
        Installer().uninstall()
    return 0


if __name__ == "__main__":
    # Do not asynchronously interrupt a file transaction or verified recovery.
    signal.signal(signal.SIGINT, control.stop_requested)
    signal.signal(signal.SIGTERM, control.stop_requested)
    try:
        sys.exit(main())
    except control.StopRequested:
        control.emit("installation_cancelled", recovery="any completed managed files remain uninstallable")
        sys.exit(1)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        message = str(error)
        if isinstance(error, subprocess.CalledProcessError):
            details = error.stderr or error.stdout
            if details:
                message += ": " + details.strip()
        control.emit("installation_error", error_type=type(error).__name__, message=message,
                     recovery="managed files are retained on activation/recovery failure; do not force an unload")
        sys.exit(1)
