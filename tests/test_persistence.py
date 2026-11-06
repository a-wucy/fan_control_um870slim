# SPDX-License-Identifier: GPL-2.0
from contextlib import nullcontext, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import control
import install
import service


KERNEL = "7.2.11-arch1-1"
VERSION = "1E9FD440C0008289545D4C2"


class FakeDriver:
    def __init__(self):
        self.values = dict(state="firmware", firmware_owned=1, release_pending=0,
                           fault=0, allow_control=1, min_rpm=500)
        self.restores = 0
        self.fail_restore = False

    def status(self):
        return self.values.copy()

    def restore(self):
        self.restores += 1
        if self.fail_restore:
            raise control.RecoveryError("injected restoration failure")
        self.values.update(state="firmware", firmware_owned=1, release_pending=0, fault=0)


class FakeCommands:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.enabled = self.active = False
        self.fail = None
        self.vermagic = KERNEL + " SMP preempt mod_unload"
        self.restart = "no"
        self.foreign_fragment = ""
        self.dropins = ""
        self.source_version = VERSION
        self.module_version = service.DKMS_VERSION
        self.dkms_registered = False
        self.dkms_kernels = set()

    def loaded(self):
        path = self.root / "sys/module" / control.NAME
        (path / "parameters").mkdir(parents=True, exist_ok=True)
        (path / "srcversion").write_text(self.source_version)
        (path / "parameters/allow_control").write_text("Y\n")
        (path / "parameters/min_rpm").write_text("500\n")
        return path

    def __call__(self, args, check=False, **kwargs):
        self.calls.append(args)
        code, output = 0, ""
        command = Path(args[0]).name
        if command == "modinfo":
            output = {"name": control.NAME, "vermagic": self.vermagic,
                      "srcversion": self.source_version, "version": self.module_version}[args[2]]
        elif command == "dkms":
            action = args[1]
            if self.fail == "dkms-" + action:
                code = 1
            elif action == "status":
                if self.dkms_registered:
                    output = f"{service.DKMS_PACKAGE}/{service.DKMS_VERSION}: added"
            elif action == "install":
                self.dkms_registered = True
                kernel = args[args.index("-k") + 1]
                self.dkms_kernels.add(kernel)
                module = self.root / "usr/lib/modules" / kernel / "updates/dkms" / (control.NAME + ".ko.zst")
                module.parent.mkdir(parents=True, exist_ok=True)
                module.write_bytes(b"mock DKMS module, never loaded")
            elif action == "remove":
                for kernel in self.dkms_kernels:
                    module = self.root / "usr/lib/modules" / kernel / "updates/dkms" / (control.NAME + ".ko.zst")
                    module.unlink(missing_ok=True)
                self.dkms_registered = False
                self.dkms_kernels.clear()
            else:
                raise AssertionError(f"unexpected DKMS action: {args}")
        elif command == "systemctl":
            action = args[1]
            if action == "is-enabled":
                code = 0 if self.enabled else 1
            elif action == "is-active":
                code = 0 if self.active else 3
            elif action == "show":
                field = args[3].removeprefix("--property=")
                unit = self.root / install.UNIT
                output = {
                    "FragmentPath": self.foreign_fragment or (str(unit) if unit.exists() else ""),
                    "DropInPaths": self.dropins,
                    "Restart": self.restart,
                }[field]
            elif action == "enable":
                self.enabled = True
            elif action == "disable":
                self.enabled = False
            elif action == "start":
                self.active = True
            elif action == "stop":
                self.active = False
            if action == self.fail:
                code = 1
        elif command == "insmod":
            self.loaded()
        elif command == "rmmod":
            if self.fail == "rmmod":
                code = 1
            else:
                loaded = self.root / "sys/module" / control.NAME
                for child in (loaded / "parameters").iterdir():
                    child.unlink()
                (loaded / "parameters").rmdir()
                (loaded / "srcversion").unlink()
                loaded.rmdir()
        else:
            raise AssertionError(f"unexpected external command: {args}")
        if check and code:
            raise subprocess.CalledProcessError(code, args, stderr="injected command failure")
        return subprocess.CompletedProcess(args, code, stdout=output, stderr="")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="system-fan-service-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "root"
        self.root.mkdir()
        self.source = Path(self.tmp.name) / "source"
        self.source.mkdir()
        (self.source / "module").mkdir()
        (self.source / "module" / f"{control.NAME}.ko").write_bytes(b"mock module, never loaded")
        for name in ["control.py", "service.py", "install.py", service.UNIT]:
            (self.source / name).write_text("fixture: " + name)
        self.commands = FakeCommands(self.root)
        self.driver = FakeDriver()
        self.installer = install.Installer(root=self.root, source=self.source,
                                           kernel=KERNEL, run=self.commands)
        self.runtime = self.installer.runtime
        self.now = 0
        patches = [
            patch.object(control, "exclusive_control", side_effect=lambda: nullcontext()),
            patch.object(control.Driver, "find", return_value=self.driver),
            patch.object(control, "memory_sensors", return_value=[object(), object()]),
            patch.object(control, "nvme_sensors", return_value=[object()]),
            patch.object(control, "_stop_requested", False),
            patch.object(service.time, "monotonic", side_effect=lambda: self.now),
            patch.object(service.time, "sleep", side_effect=self.advance),
        ]
        for replacement in patches:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.output = io.StringIO()
        redirect = redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def advance(self, seconds):
        self.now += seconds

    def installed_module(self):
        path = self.runtime.module
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mock module")
        return path

    def prepare_dkms(self):
        binaries = self.root / "usr/bin"
        binaries.mkdir(parents=True)
        for command in ["dkms", "make", "gcc", "pahole"]:
            (binaries / command).write_text("mock, never executed")
        release = self.runtime.kernel_modules / KERNEL / "build/include/config/kernel.release"
        release.parent.mkdir(parents=True)
        release.write_text(KERNEL + "\n")
        for name in install.DKMS_FILES:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture: " + name)

    def test_unit_stops_instead_of_restarting_after_fallback(self):
        unit = (Path(__file__).parents[1] / service.UNIT).read_text()
        self.assertIn("Restart=no\n", unit)
        self.assertIn(f"SuccessExitStatus={control.THERMAL_EXIT_STATUS}\n", unit)
        self.assertIn("ExecStopPost=", unit)
        self.assertIn("--profile quiet --middle-rpm 600 --duration 0", unit)
        self.assertIn("--log-interval 60", unit)
        self.assertIn("ProtectHome=yes", unit)
        self.assertNotIn("ProtectKernelTunables=yes", unit)  # Required sysfs writes must remain possible.

    def test_installer_requires_root_before_any_installation(self):
        with patch.object(install.os, "geteuid", return_value=1000):
            with patch.object(install, "Installer") as constructor:
                with redirect_stdout(io.StringIO()), patch("sys.stderr", new=io.StringIO()):
                    with self.assertRaises(SystemExit):
                        install.main(["install"])
                constructor.assert_not_called()

    def test_thermal_exit_is_successful_safety_stop_not_generic_error(self):
        with patch.object(control, "main", side_effect=control.ThermalLimitError("RAM 55")):
            self.assertEqual(control.cli(), 75)
        self.assertIn("safety_stop", self.output.getvalue())
        self.assertNotIn('"event": "error"', self.output.getvalue())

    def test_restore_failure_remains_error_even_for_service(self):
        with patch.object(control, "main", side_effect=control.RecoveryError("restore failed")):
            self.assertEqual(control.cli(), 1)

    def test_start_loads_only_versioned_system_module(self):
        module = self.installed_module()
        self.runtime.prepare()
        calls = [call for call in self.commands.calls if Path(call[0]).name == "insmod"]
        self.assertEqual(calls, [["/usr/bin/insmod", str(module), "allow_control=1", "min_rpm=500"]])
        self.assertTrue(self.runtime.marker.exists())
        self.assertEqual(self.driver.restores, 0)  # No automatic repair at startup.

    def test_missing_current_kernel_build_does_not_load_anything(self):
        with self.assertRaises(RuntimeError):
            self.runtime.prepare()
        self.assertFalse(self.runtime.marker.exists())
        self.assertEqual(self.commands.calls, [])

    def test_mismatched_kernel_build_does_not_load_anything(self):
        self.installed_module()
        self.commands.vermagic = "some-other-kernel SMP"
        with self.assertRaises(RuntimeError):
            self.runtime.prepare()
        self.assertFalse(any(Path(call[0]).name == "insmod" for call in self.commands.calls))

    def test_original_driver_blocks_preparation(self):
        (self.runtime.modules / "minisforum_ec").mkdir(parents=True)
        with self.assertRaises(RuntimeError):
            self.runtime.prepare()
        self.assertEqual(self.commands.calls, [])

    def test_ready_preloaded_module_is_reused_without_reload(self):
        self.installed_module()
        self.commands.loaded()
        self.runtime.prepare()
        self.assertFalse(any(Path(call[0]).name == "insmod" for call in self.commands.calls))
        self.assertTrue(self.runtime.marker.exists())

    def test_observation_module_is_not_silently_reconfigured(self):
        self.installed_module()
        path = self.commands.loaded()
        (path / "parameters/allow_control").write_text("N")
        with self.assertRaises(RuntimeError):
            self.runtime.prepare()
        self.assertFalse(self.runtime.marker.exists())
        self.assertEqual(self.driver.restores, 0)

    def test_different_preloaded_module_is_refused(self):
        self.installed_module()
        path = self.commands.loaded()
        (path / "srcversion").write_text("different")
        with self.assertRaises(RuntimeError):
            self.runtime.prepare()
        self.assertFalse(self.runtime.marker.exists())

    def test_unexpected_existing_control_is_not_repaired_by_start(self):
        self.installed_module()
        self.commands.loaded()
        self.driver.values.update(state="external-fixed", firmware_owned=0)
        with self.assertRaises(RuntimeError):
            self.runtime.prepare()
        self.assertEqual(self.driver.restores, 0)
        self.assertFalse(self.runtime.marker.exists())

    def test_sensors_can_arrive_late_without_unconditional_boot_sleep(self):
        self.installed_module()
        with patch.object(control, "memory_sensors", side_effect=[RuntimeError("not yet"), [], []]):
            self.runtime.prepare()
        self.assertEqual(self.now, 1)

    def test_missing_sensors_timeout_without_loading_module(self):
        self.installed_module()
        with patch.object(control, "nvme_sensors", side_effect=RuntimeError("missing")):
            with self.assertRaises(RuntimeError):
                self.runtime.prepare()
        self.assertEqual(self.now, 30)
        self.assertFalse(any(Path(call[0]).name == "insmod" for call in self.commands.calls))

    def test_cleanup_only_after_preparation(self):
        self.runtime.cleanup()
        self.assertEqual(self.driver.restores, 0)
        self.installed_module()
        self.runtime.prepare()
        self.runtime.cleanup()
        self.assertEqual(self.driver.restores, 1)
        self.assertFalse(self.runtime.marker.exists())
        self.assertTrue((self.runtime.modules / control.NAME).exists())  # Keep recovery driver available.

    def test_cleanup_failure_preserves_recovery_marker(self):
        self.installed_module()
        self.runtime.prepare()
        self.driver.fail_restore = True
        with self.assertRaises(control.RecoveryError):
            self.runtime.cleanup()
        self.assertTrue(self.runtime.marker.exists())

    def test_disappeared_service_module_is_not_reported_as_restored(self):
        self.runtime.runtime.mkdir(parents=True)
        self.runtime.marker.write_text(VERSION)
        with self.assertRaises(RuntimeError):
            self.runtime.cleanup()
        self.assertTrue(self.runtime.marker.exists())

    def test_install_and_uninstall_manage_only_dedicated_paths(self):
        self.installer.install()
        self.assertTrue(self.commands.enabled)
        self.assertTrue(self.commands.active)
        files = self.installer.managed_files()
        self.assertEqual(len(files), 5)
        self.assertTrue((self.root / install.PREFIX / "install.py").is_file())
        self.commands.loaded()
        self.installer.uninstall()
        self.assertEqual(self.driver.restores, 1)
        self.assertFalse((self.runtime.modules / control.NAME).exists())
        self.assertFalse((self.root / install.PREFIX).exists())
        self.assertFalse((self.root / install.UNIT).exists())
        self.assertFalse(self.commands.enabled)

    def test_modified_installed_file_is_preserved(self):
        self.installer.install()
        modified = self.root / install.PREFIX / "control.py"
        modified.write_text("user edits")
        with self.assertRaises(RuntimeError):
            self.installer.install()
        with self.assertRaises(RuntimeError):
            self.installer.uninstall()
        self.assertEqual(modified.read_text(), "user edits")

    def test_unfamiliar_existing_unit_is_never_overwritten(self):
        unit = self.root / install.UNIT
        unit.parent.mkdir(parents=True)
        unit.write_text("user unit")
        with self.assertRaises(RuntimeError):
            self.installer.install()
        self.assertEqual(unit.read_text(), "user unit")

    def test_extra_user_files_survive_uninstall(self):
        self.installer.install()
        extra = self.root / install.PREFIX / "notes.txt"
        extra.write_text("keep this")
        self.installer.uninstall()
        self.assertEqual(extra.read_text(), "keep this")

    def test_manifest_cannot_delete_outside_owned_paths(self):
        self.installer.install()
        outside = self.root / "etc/keep.txt"
        outside.write_text("keep")
        manifest = self.root / install.MANIFEST
        data = json.loads(manifest.read_text())
        data["files"]["etc/keep.txt"] = install.digest(b"keep")
        manifest.write_text(json.dumps(data))
        with self.assertRaises(RuntimeError):
            self.installer.uninstall()
        self.assertEqual(outside.read_text(), "keep")

    def test_symlinked_install_path_is_refused(self):
        prefix = self.root / install.PREFIX
        prefix.parent.mkdir(parents=True)
        external = Path(self.tmp.name) / "external"
        external.mkdir()
        prefix.symlink_to(external, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.installer.install()
        self.assertEqual(list(external.iterdir()), [])

    def test_writable_administrator_code_directory_is_refused(self):
        self.installer.install()
        prefix = self.root / install.PREFIX
        prefix.chmod(0o777)
        with self.assertRaises(RuntimeError):
            self.installer.install()

    def test_activation_failure_is_not_left_enabled_and_keeps_recovery_files(self):
        self.commands.fail = "start"
        with self.assertRaises(subprocess.CalledProcessError):
            self.installer.install()
        self.assertFalse(self.commands.enabled)
        self.assertTrue(self.installer.managed_files())

    def test_foreign_systemd_unit_is_not_shadowed(self):
        self.commands.foreign_fragment = "/usr/lib/systemd/system/another-unit.service"
        with self.assertRaises(RuntimeError):
            self.installer.install()
        self.assertFalse((self.root / install.PREFIX).exists())

    def test_existing_unit_dropins_are_not_removed_or_overridden(self):
        override = self.root / "etc/systemd/system" / (service.UNIT + ".d")
        override.mkdir(parents=True)
        note = override / "custom.conf"
        note.write_text("[Service]\nRestart=always\n")
        with self.assertRaises(RuntimeError):
            self.installer.install()
        self.assertTrue(note.exists())

    def test_automatic_restart_override_is_refused_before_activation(self):
        self.commands.restart = "always"
        with self.assertRaises(RuntimeError):
            self.installer.install()
        self.assertFalse(self.commands.enabled)
        self.assertFalse(self.commands.active)

    def test_restore_failure_prevents_unload_and_file_deletion(self):
        self.installer.install()
        self.commands.loaded()
        self.driver.fail_restore = True
        with self.assertRaises(control.RecoveryError):
            self.installer.uninstall()
        self.assertTrue((self.runtime.modules / control.NAME).exists())
        self.assertTrue(self.installer.managed_files())
        self.assertFalse(any(Path(call[0]).name == "rmmod" for call in self.commands.calls))
        self.assertFalse(self.commands.enabled)

    def test_unload_failure_keeps_recovery_files(self):
        self.installer.install()
        self.commands.loaded()
        self.commands.fail = "rmmod"
        with self.assertRaises(subprocess.CalledProcessError):
            self.installer.uninstall()
        self.assertTrue(self.installer.managed_files())
        self.assertEqual(self.driver.restores, 1)

    def test_reinstall_can_add_new_matching_kernel_without_deleting_old_build(self):
        self.installer.install()
        new_kernel = "7.2.12-arch1-1"
        self.commands.vermagic = new_kernel + " SMP"
        updated = install.Installer(root=self.root, source=self.source,
                                    kernel=new_kernel, run=self.commands)
        updated.install()
        files = updated.managed_files()
        modules = [path for path in files if path.endswith(".ko")]
        self.assertEqual(len(modules), 2)

    def test_file_write_failure_rolls_back_existing_managed_files(self):
        self.installer.install()
        before = self.installer.managed_files()
        (self.source / "control.py").write_text("new version")
        original_write = self.installer.atomic_write
        failed = False

        def fail_once(path, data, mode=0o644):
            nonlocal failed
            if path.name == "service.py" and not failed:
                failed = True
                raise OSError("injected write failure")
            original_write(path, data, mode)

        with patch.object(self.installer, "atomic_write", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.installer.install()
        self.assertEqual(self.installer.managed_files(), before)

    def test_interrupt_during_file_transaction_finishes_manifest_without_activation(self):
        original_write = self.installer.atomic_write

        def request_stop(path, data, mode=0o644):
            original_write(path, data, mode)
            control.stop_requested(control.signal.SIGINT, None)

        with patch.object(self.installer, "atomic_write", side_effect=request_stop):
            with self.assertRaises(control.StopRequested):
                self.installer.install()
        self.assertTrue(self.installer.managed_files())
        self.assertFalse(self.commands.enabled)
        self.assertFalse(self.commands.active)

    def test_interrupt_during_uninstall_does_not_abort_verified_recovery(self):
        self.installer.install()
        self.commands.loaded()
        original_restore = self.driver.restore

        def restore_with_stop():
            control.stop_requested(control.signal.SIGINT, None)
            original_restore()

        self.driver.restore = restore_with_stop
        self.installer.uninstall()
        self.assertEqual(self.driver.restores, 1)
        self.assertFalse((self.runtime.modules / control.NAME).exists())
        self.assertFalse((self.root / install.PREFIX).exists())

    def test_dkms_opt_in_migrates_manual_install_and_keeps_recovery_module(self):
        self.installer.install()
        self.prepare_dkms()
        self.installer.install(dkms=True)
        files = self.installer.managed_files()
        self.assertIn(str(install.DKMS_STATE), files)
        for name in install.DKMS_FILES:
            self.assertIn(str(install.DKMS_SOURCE / name), files)
        self.assertTrue(self.runtime.private_module.is_file())
        self.assertEqual(self.runtime.module.suffix, ".zst")
        self.assertTrue(self.commands.enabled)
        self.assertTrue(self.commands.active)
        self.assertTrue(self.commands.dkms_registered)
        self.assertFalse(any(Path(call[0]).name == "insmod" for call in self.commands.calls))

    def test_dkms_mode_is_preserved_by_later_install_without_flag(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        self.installer.install()
        self.assertTrue(self.runtime.dkms_settings())
        self.assertTrue(self.commands.dkms_registered)

    def test_dkms_next_patch_boot_uses_new_exact_module_without_private_copy(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        new_kernel = "7.2.14-arch1-1"
        self.installer.dkms("install", service.DKMS_VERSION, "-k", new_kernel)
        self.commands.vermagic = new_kernel + " SMP"
        self.runtime.kernel = new_kernel
        self.assertFalse(self.runtime.private_module.exists())
        self.runtime.prepare()
        self.assertTrue(self.runtime.marker.exists())
        self.assertEqual(self.commands.calls[-1], ["/usr/bin/insmod", str(self.runtime.module),
                                                  "allow_control=1", "min_rpm=500"])

    def test_dkms_73_is_blocked_even_with_existing_private_or_dkms_module(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        self.runtime.kernel = "7.3.1-arch1-1"
        self.installer.dkms("install", service.DKMS_VERSION, "-k", self.runtime.kernel)
        private = self.runtime.private_module
        private.parent.mkdir(parents=True)
        private.write_bytes(b"must not be loaded")
        self.commands.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "outside approved"):
            self.runtime.prepare()
        self.assertEqual(self.commands.calls, [])
        self.assertEqual(self.driver.restores, 0)

    def test_dkms_missing_patch_build_never_reuses_old_or_private_module(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        self.runtime.module.unlink()
        self.commands.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "expected one exact-kernel"):
            self.runtime.prepare()
        self.assertEqual(self.commands.calls, [])

    def test_dkms_mismatched_vermagic_is_not_force_loaded(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        self.commands.vermagic = "7.2.10-arch1-1 SMP"
        self.commands.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "kernel version mismatch"):
            self.runtime.prepare()
        self.assertFalse(any(Path(call[0]).name == "insmod" for call in self.commands.calls))

    def test_dkms_unapproved_source_identity_is_refused(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        self.commands.source_version = "A" * 24
        with self.assertRaisesRegex(RuntimeError, "source identity"):
            self.runtime.prepare()
        self.assertFalse(any(Path(call[0]).name == "insmod" for call in self.commands.calls))

    def test_dkms_unapproved_module_version_is_refused(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        self.commands.module_version = "0.0.0"
        with self.assertRaisesRegex(RuntimeError, "approved version"):
            self.runtime.prepare()

    def test_dkms_duplicate_compressed_and_uncompressed_modules_are_refused(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        self.runtime.module.with_suffix("").write_bytes(b"duplicate")
        with self.assertRaisesRegex(RuntimeError, "expected one"):
            self.runtime.prepare()

    def test_dkms_symlinked_module_is_refused(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        module = self.runtime.module
        module.unlink()
        module.symlink_to(self.runtime.private_module)
        with self.assertRaisesRegex(RuntimeError, "symlinked"):
            self.runtime.prepare()

    def test_dkms_writable_module_directory_is_refused(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        self.runtime.module.parent.chmod(0o777)
        with self.assertRaisesRegex(RuntimeError, "administrator-owned"):
            self.runtime.prepare()

    def test_dkms_invalid_opt_in_never_becomes_manual_mode(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        (self.root / install.DKMS_STATE).write_text("[]")
        with self.assertRaisesRegex(RuntimeError, "unrecognized DKMS"):
            self.runtime.prepare()

    def test_dkms_source_update_requires_deliberate_unregister(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        self.commands.source_version = "B" * 24
        before = self.installer.managed_files()
        with self.assertRaisesRegex(RuntimeError, "source identity changed"):
            self.installer.install()
        self.assertEqual(self.installer.managed_files(), before)

    def test_dkms_install_requires_matching_headers_before_any_file_changes(self):
        self.prepare_dkms()
        (self.runtime.kernel_modules / KERNEL / "build/include/config/kernel.release").write_text("7.3.0\n")
        with self.assertRaisesRegex(RuntimeError, "exact-kernel headers"):
            self.installer.install(dkms=True)
        self.assertFalse((self.root / install.PREFIX).exists())

    def test_dkms_missing_dependency_is_refused(self):
        self.prepare_dkms()
        (self.root / "usr/bin/pahole").unlink()
        with self.assertRaisesRegex(RuntimeError, "missing pahole"):
            self.installer.install(dkms=True)

    def test_dkms_foreign_registration_is_preserved(self):
        self.prepare_dkms()
        self.commands.dkms_registered = True
        with self.assertRaisesRegex(RuntimeError, "unfamiliar existing DKMS"):
            self.installer.install(dkms=True)
        self.assertTrue(self.commands.dkms_registered)
        self.assertFalse((self.root / install.DKMS_SOURCE).exists())

    def test_dkms_override_is_preserved_and_blocks_installation(self):
        self.prepare_dkms()
        override = self.root / "etc/dkms" / (service.DKMS_PACKAGE + ".conf")
        override.parent.mkdir(parents=True)
        override.write_text('BUILD_EXCLUSIVE_KERNEL=".*"\n')
        with self.assertRaisesRegex(RuntimeError, "unreviewed DKMS override"):
            self.installer.install(dkms=True)
        self.assertTrue(override.exists())

    def test_dkms_build_failure_keeps_recovery_assets_without_enabling_service(self):
        self.prepare_dkms()
        self.commands.fail = "dkms-install"
        with self.assertRaises(subprocess.CalledProcessError):
            self.installer.install(dkms=True)
        self.assertTrue(self.installer.managed_files())
        self.assertFalse(self.commands.enabled)
        self.assertFalse(self.commands.active)
        self.assertFalse(any(Path(call[0]).name == "insmod" for call in self.commands.calls))

    def test_dkms_uninstall_restores_unloads_then_unregisters_and_removes_sources(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        self.commands.loaded()
        self.installer.uninstall()
        names = [Path(call[0]).name for call in self.commands.calls]
        remove = next(i for i, call in enumerate(self.commands.calls) if call[:2] == ["/usr/bin/dkms", "remove"])
        self.assertLess(names.index("rmmod"), remove)
        self.assertEqual(self.driver.restores, 1)
        self.assertFalse(self.commands.dkms_registered)
        self.assertFalse((self.root / install.DKMS_SOURCE).exists())
        self.assertFalse((self.root / install.PREFIX).exists())

    def test_dkms_unregister_failure_keeps_sources_and_recovery_tools(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        self.commands.fail = "dkms-remove"
        with self.assertRaises(subprocess.CalledProcessError):
            self.installer.uninstall()
        self.assertTrue(self.installer.managed_files())
        self.assertTrue(self.commands.dkms_registered)
        self.assertFalse(self.commands.enabled)

    def test_dkms_restore_failure_prevents_unregister(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        self.commands.loaded()
        self.driver.fail_restore = True
        with self.assertRaises(control.RecoveryError):
            self.installer.uninstall()
        self.assertFalse(any(call[:2] == ["/usr/bin/dkms", "remove"] for call in self.commands.calls))
        self.assertTrue(self.installer.managed_files())

    def test_dkms_uninstall_on_unapproved_kernel_without_loading_module(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        self.installer.runtime.kernel = "7.3.1-arch1-1"
        self.installer.uninstall()
        self.assertFalse(self.commands.dkms_registered)
        self.assertFalse((self.root / install.DKMS_SOURCE).exists())

    def test_dkms_modified_source_is_preserved(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        path = self.root / install.DKMS_SOURCE / "module/system_fan_core.h"
        path.write_text("administrator changes")
        with self.assertRaisesRegex(RuntimeError, "missing or modified"):
            self.installer.uninstall()
        self.assertEqual(path.read_text(), "administrator changes")
        self.assertTrue(self.commands.dkms_registered)

    def test_dkms_failed_unregistered_install_can_be_uninstalled(self):
        self.prepare_dkms()
        self.commands.fail = "dkms-install"
        with self.assertRaises(subprocess.CalledProcessError):
            self.installer.install(dkms=True)
        self.commands.loaded()
        self.installer.uninstall()
        self.assertEqual(self.driver.restores, 1)
        self.assertFalse((self.root / install.PREFIX).exists())
        self.assertFalse((self.root / install.DKMS_SOURCE).exists())
        self.assertFalse(any(call[:2] == ["/usr/bin/dkms", "remove"] for call in self.commands.calls))

    def test_dkms_absent_binary_still_allows_verified_recovery_with_private_copy(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        self.commands.loaded()
        self.runtime.module.unlink()
        self.installer.uninstall()
        self.assertEqual(self.driver.restores, 1)
        self.assertFalse(self.commands.dkms_registered)

    def test_dkms_source_extra_user_file_survives_uninstall(self):
        self.prepare_dkms()
        self.installer.install(dkms=True)
        note = self.root / install.DKMS_SOURCE / "notes.txt"
        note.write_text("keep this")
        self.installer.uninstall()
        self.assertEqual(note.read_text(), "keep this")

    def test_manifest_cannot_delete_other_dkms_sources(self):
        self.installer.install()
        outside = self.root / "usr/src/other-module-1.0/Makefile"
        outside.parent.mkdir(parents=True)
        outside.write_text("keep")
        manifest = self.root / install.MANIFEST
        data = json.loads(manifest.read_text())
        data["files"][str(outside.relative_to(self.root))] = install.digest(b"keep")
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(RuntimeError, "unexpected managed path"):
            self.installer.uninstall()
        self.assertEqual(outside.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
