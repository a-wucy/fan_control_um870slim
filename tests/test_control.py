# SPDX-License-Identifier: GPL-2.0
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("fan_experiment", Path(__file__).parents[1] / "control.py")
control = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = control
spec.loader.exec_module(control)


class FakeDriver:
    def __init__(self):
        self.values = dict(state="firmware", firmware_owned=1, release_pending=0,
                           fault=0, allow_control=1, min_rpm=800, applied_rpm=1500,
                           fan_rpm=1500, ec_temp_millidegrees=35_000)
        self.targets = []
        self.restores = 0
        self.write_error = None
        self.restore_error = None

    def status(self):
        return self.values.copy()

    def target(self, rpm):
        self.targets.append(rpm)
        if self.write_error:
            raise self.write_error
        self.values.update(state="controlled", firmware_owned=0, applied_rpm=rpm, fan_rpm=rpm)

    def restore(self):
        self.restores += 1
        if self.restore_error:
            raise self.restore_error
        self.values.update(state="firmware", firmware_owned=1, fault=0, release_pending=0)


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ControllerTests(unittest.TestCase):
    def setUp(self):
        stop_patch = patch.object(control, "_stop_requested", False)
        stop_patch.start()
        self.addCleanup(stop_patch.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="system-fan-python-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.time_patch = patch.multiple(control.time, monotonic=self.clock.monotonic,
                                        sleep=self.clock.sleep)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.output = io.StringIO()
        self.redirect = redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def sensor(self, index, temperature, name="spd5118"):
        path = self.root / f"hwmon{index}"
        path.mkdir()
        (path / "name").write_text(name)
        (path / "temp1_input").write_text(str(temperature))
        return control.MemorySensor(path)

    def nvme(self, index=1, temperature=40_000, extra=None):
        directory = self.sensor(index, temperature, "nvme").directory
        (directory / "temp1_label").write_text("Composite")
        for name, value in (extra or {}).items():
            (directory / name).write_text(str(value))
        inputs = tuple(sorted(path.name for path in directory.glob("temp*_input")))
        return control.NvmeSensor(directory, inputs)

    def test_exact_thresholds(self):
        for temperature, expected in [(29_999, 0), (30_000, 800), (49_999, 800),
                                      (50_000, 800), (50_001, 1000)]:
            self.assertEqual(control.choose_target(temperature, 800), expected)

    def test_hottest_memory_sensor_not_other_device(self):
        self.sensor(6, 30_000)
        self.sensor(4, 50_001)
        self.sensor(3, 90_000, "k10temp")
        sensors = control.memory_sensors(self.root)
        self.assertEqual(control.memory_temperature(sensors)[0], 50_001)

    def test_missing_dimm_refused(self):
        self.sensor(4, 30_000)
        with self.assertRaises(RuntimeError):
            control.memory_sensors(self.root)

    def test_changed_identity_refused(self):
        sensor = self.sensor(4, 30_000)
        (sensor.directory / "name").write_text("k10temp")
        with self.assertRaises(RuntimeError):
            sensor.read()

    def test_invalid_temperature_refused(self):
        sensor = self.sensor(4, 126_000)
        with self.assertRaises(ValueError):
            sensor.read()

    def test_curve_renews_and_restores(self):
        driver = FakeDriver()
        sensors = [self.sensor(4, 32_000), self.sensor(6, 31_000)]
        control.run_curve(driver, sensors, 800, 3, profile="legacy")
        self.assertEqual(driver.targets, [800, 800, 800])
        self.assertEqual(driver.restores, 1)

    def test_stop_and_high_bands(self):
        for temperature, target in [(29_000, 0), (51_000, 1000)]:
            with self.subTest(temperature=temperature):
                driver = FakeDriver()
                sensor = self.sensor(temperature, temperature)
                control.run_curve(driver, [sensor], 800, 1, profile="legacy")
                self.assertEqual(driver.targets, [target])
                self.assertEqual(driver.restores, 1)

    def test_sensor_failure_restores(self):
        driver = FakeDriver()
        sensor = self.sensor(4, 32_000)
        (sensor.directory / "temp1_input").unlink()
        with self.assertRaises(OSError):
            control.run_curve(driver, [sensor], 800, 3, profile="legacy")
        self.assertEqual(driver.restores, 1)
        self.assertEqual(driver.targets, [])

    def test_write_failure_restores(self):
        driver = FakeDriver()
        driver.write_error = OSError("failed after possible handoff")
        with self.assertRaises(OSError):
            control.run_curve(driver, [self.sensor(4, 32_000)], 800, 3, profile="legacy")
        self.assertEqual(driver.restores, 1)

    def test_signal_restores(self):
        driver = FakeDriver()
        with self.assertRaises(control.StopRequested):
            with control.control_session(driver):
                driver.target(800)
                control.stop_requested(None, None)
                control.check_stop_requested()
        self.assertEqual(driver.restores, 1)

    def test_signal_handler_only_records_stop(self):
        control.stop_requested(control.signal.SIGINT, None)
        control.stop_requested(control.signal.SIGTERM, None)
        self.assertTrue(control._stop_requested)
        with self.assertRaises(control.StopRequested):
            control.check_stop_requested()

    def test_signal_before_curve_sends_no_target_and_restores(self):
        driver = FakeDriver()
        control.stop_requested(control.signal.SIGINT, None)
        with self.assertRaises(control.StopRequested):
            control.run_curve(driver, [self.sensor(4, 32_000)], 800, 3, profile="legacy")
        self.assertEqual(driver.targets, [])
        self.assertEqual(driver.restores, 1)

    def test_signal_between_probe_targets_restores(self):
        driver = FakeDriver()
        original_emit = control.emit

        def stop_after_validation(event, **fields):
            original_emit(event, **fields)
            if event == "validated_target":
                control.stop_requested(control.signal.SIGINT, None)

        with patch.object(control, "emit", side_effect=stop_after_validation):
            with self.assertRaises(control.StopRequested):
                control.probe_speeds(driver, [self.sensor(4, 32_000)], [1000, 800], 20, 180,
                                     nvmes=[self.nvme()])
        self.assertEqual(set(driver.targets), {1000})
        self.assertEqual(driver.restores, 1)
        self.assertNotIn("RESTORE_FAILED", self.output.getvalue())

    def test_repeated_signals_during_cleanup_do_not_abort_restore(self):
        driver = FakeDriver()
        original_restore = driver.restore

        def restore_with_signals():
            control.stop_requested(control.signal.SIGINT, None)
            control.stop_requested(control.signal.SIGINT, None)
            control.stop_requested(control.signal.SIGTERM, None)
            original_restore()

        driver.restore = restore_with_signals
        with control.control_session(driver):
            driver.target(800)
        self.assertEqual(driver.restores, 1)
        self.assertEqual(driver.status()["firmware_owned"], 1)
        self.assertNotIn("RESTORE_FAILED", self.output.getvalue())

    def test_signal_after_last_validation_finishes_cleanup(self):
        driver = FakeDriver()
        original_emit = control.emit

        def stop_after_last_validation(event, **fields):
            original_emit(event, **fields)
            if event == "validated_target":
                control.stop_requested(control.signal.SIGINT, None)

        with patch.object(control, "emit", side_effect=stop_after_last_validation):
            control.probe_speeds(driver, [self.sensor(4, 32_000)], [800], 20, 180,
                                 nvmes=[self.nvme()])
        self.assertEqual(driver.restores, 1)
        self.assertTrue(control._stop_requested)
        self.assertIn("probe_complete", self.output.getvalue())
        self.assertNotIn("RESTORE_FAILED", self.output.getvalue())

    def test_stop_during_cleanup_does_not_hide_a_real_restore_failure(self):
        driver = FakeDriver()

        def failed_restore():
            control.stop_requested(control.signal.SIGINT, None)
            raise OSError("EC write failed")

        driver.restore = failed_restore
        with self.assertRaises(control.RecoveryError):
            with control.control_session(driver):
                pass
        self.assertIn('"error_type": "OSError"', self.output.getvalue())
        self.assertIn("EC write failed", self.output.getvalue())

    def test_real_signals_during_restore_write_and_verification(self):
        directory = self.root / "hwmon8"
        directory.mkdir()
        (directory / "name").write_text(control.NAME)
        status = FakeDriver().values
        (directory / "status").write_text("".join(f"{key}={value}\n" for key, value in status.items()))
        driver = control.Driver(directory)
        original_write = Path.write_text
        original_status = driver.status
        verifying = False
        verified = False

        for sig in [control.signal.SIGINT, control.signal.SIGTERM]:
            previous = control.signal.signal(sig, control.stop_requested)
            self.addCleanup(control.signal.signal, sig, previous)

        def write_with_signal(path, data, *args, **kwargs):
            nonlocal verifying
            if path.name == "restore":
                verifying = True
                control.signal.raise_signal(control.signal.SIGINT)
            return original_write(path, data, *args, **kwargs)

        def verify_with_signal():
            nonlocal verified
            if verifying:
                control.signal.raise_signal(control.signal.SIGTERM)
            result = original_status()
            if verifying:
                verified = True
            return result

        with patch.object(Path, "write_text", new=write_with_signal):
            with patch.object(control.Driver, "status", side_effect=verify_with_signal):
                with control.control_session(driver):
                    pass
        self.assertTrue(verified)
        self.assertEqual((directory / "restore").read_text(), "1\n")
        self.assertIn("firmware_restored", self.output.getvalue())
        self.assertNotIn("RESTORE_FAILED", self.output.getvalue())

    def test_driver_fault_restores_and_stops(self):
        driver = FakeDriver()
        with self.assertRaises(RuntimeError):
            with control.control_session(driver):
                driver.values["fault"] = 1
                control.checked_status(driver)
        self.assertEqual(driver.restores, 1)

    def test_restoration_failure_not_hidden(self):
        driver = FakeDriver()
        driver.restore_error = OSError("EC stuck")
        with self.assertRaises(control.RecoveryError):
            with control.control_session(driver):
                pass
        self.assertIn("RESTORE_FAILED", self.output.getvalue())

    def test_observation_cannot_control(self):
        driver = FakeDriver()
        driver.values["allow_control"] = 0
        with self.assertRaises(RuntimeError):
            with control.control_session(driver):
                self.fail("must not yield")
        self.assertEqual(driver.restores, 0)

    def test_foreign_fixed_state_not_taken_over(self):
        driver = FakeDriver()
        driver.values["state"] = "external-fixed"
        with self.assertRaises(RuntimeError):
            with control.control_session(driver):
                self.fail("must not yield")
        self.assertEqual(driver.restores, 0)

    def test_targets_respect_kernel_floor_and_resolution(self):
        for rpm in [-1, 1, 250, 300, 799, 5200]:
            with self.subTest(rpm=rpm), self.assertRaises(ValueError):
                control.validate_target(rpm, 800)
        for rpm in [0, 800, 1000, 5100]:
            control.validate_target(rpm, 800)

    def test_probe_records_stable_actual_rpm(self):
        driver = FakeDriver()
        control.probe_speeds(driver, [self.sensor(4, 32_000)], [1000, 800], 20, 180,
                             nvmes=[self.nvme()])
        self.assertEqual(driver.restores, 1)
        self.assertIn('"lowest_validated_target": 800', self.output.getvalue())

    def test_probe_rejects_nonresponsive_fan_and_restores(self):
        driver = FakeDriver()
        original_target = driver.target

        def nonresponsive(rpm):
            original_target(rpm)
            driver.values["fan_rpm"] = 3500

        driver.target = nonresponsive
        with self.assertRaises(RuntimeError):
            control.probe_speeds(driver, [self.sensor(4, 32_000)], [1000], 20, 180,
                                 nvmes=[self.nvme()])
        self.assertEqual(driver.restores, 1)

    def test_probe_argument_validation(self):
        self.assertEqual(control.parse_rpms("1000,800,300,200"), [1000, 800, 300, 200])
        for text in ["250", "0", "800,1000", "800,800", ""]:
            with self.subTest(text=text), self.assertRaises(control.argparse.ArgumentTypeError):
                control.parse_rpms(text)
        for text in ["nan", "inf", "-1"]:
            with self.subTest(text=text), self.assertRaises(control.argparse.ArgumentTypeError):
                control.duration_seconds(text)

    def test_exclusive_lock(self):
        lock = self.root / "controller.lock"
        with control.exclusive_control(lock):
            with self.assertRaises(RuntimeError):
                with control.exclusive_control(lock):
                    self.fail("two controllers must not run together")

    def test_real_driver_files_and_restore_verification(self):
        directory = self.root / "hwmon8"
        directory.mkdir()
        (directory / "name").write_text(control.NAME)
        (directory / "cpu_sentinel").write_text("unchanged")
        driver = control.Driver.find(self.root)
        driver.target(800)
        self.assertEqual((directory / "target_rpm").read_text(), "800\n")
        status = FakeDriver().values
        (directory / "status").write_text("".join(f"{key}={value}\n" for key, value in status.items()))
        driver.restore()
        status["firmware_owned"] = 0
        (directory / "status").write_text("".join(f"{key}={value}\n" for key, value in status.items()))
        with self.assertRaises(control.RecoveryError):
            driver.restore()
        self.assertEqual((directory / "cpu_sentinel").read_text(), "unchanged")

    def test_quiet_rising_thresholds(self):
        for ram, nvme, expected in [
            (39_999, 49_999, 0), (40_000, 40_000, 600),
            (30_000, 50_000, 600), (49_999, 59_999, 600),
            (50_000, 40_000, 1000), (30_000, 60_000, 1000),
        ]:
            with self.subTest(ram=ram, nvme=nvme):
                self.assertEqual(control.QuietPolicy().select(ram, nvme, 0), expected)

    def test_quiet_off_state_stays_off_in_hysteresis_band(self):
        policy = control.QuietPolicy()
        self.assertEqual(policy.select(38_001, 46_000, 0), 0)
        self.assertEqual(policy.select(39_999, 49_999, 300), 0)
        self.assertEqual(policy.minimum_on_remaining(300), 0)

    def test_quiet_minimum_on_time_and_refreshes(self):
        policy = control.QuietPolicy()
        self.assertEqual(policy.select(40_000, 40_000, 0), 600)
        for second in range(1, 120):
            self.assertEqual(policy.select(38_000, 45_000, second), 600)
        self.assertEqual(policy.started_at, 0)
        self.assertEqual(policy.minimum_on_remaining(119), 1)
        self.assertEqual(policy.select(38_000, 45_000, 120), 0)
        self.assertEqual(policy.minimum_on_remaining(120), 0)
        self.assertEqual(policy.select(40_000, 40_000, 200), 600)
        self.assertEqual(policy.minimum_on_remaining(200), 120)

    def test_quiet_stopping_requires_both_sensors_cool(self):
        policy = control.QuietPolicy()
        policy.select(40_000, 50_000, 0)
        self.assertEqual(policy.select(38_001, 45_000, 120), 600)
        self.assertEqual(policy.select(38_000, 45_001, 121), 600)
        self.assertEqual(policy.select(38_000, 45_000, 122), 0)

    def test_quiet_stops_at_normal_idle_temperature_and_restarts_at_40(self):
        policy = control.QuietPolicy()
        self.assertEqual(policy.select(40_000, 41_850, 0), 600)
        self.assertEqual(policy.select(37_500, 41_850, 119), 600)
        self.assertEqual(policy.select(37_500, 41_850, 120), 0)
        self.assertEqual(policy.select(38_000, 41_850, 121), 0)
        self.assertEqual(policy.select(39_999, 41_850, 300), 0)
        self.assertEqual(policy.select(40_000, 41_850, 301), 600)
        self.assertEqual(policy.minimum_on_remaining(301), 120)

    def test_quiet_high_downshift_requires_both_sensors_cool(self):
        policy = control.QuietPolicy()
        self.assertEqual(policy.select(50_000, 40_000, 0), 1000)
        self.assertEqual(policy.select(45_001, 55_000, 200), 1000)
        self.assertEqual(policy.select(45_000, 55_001, 201), 1000)
        self.assertEqual(policy.select(45_000, 55_000, 202), 600)

    def test_quiet_high_transition_does_not_reset_minimum_on(self):
        policy = control.QuietPolicy()
        self.assertEqual(policy.select(40_000, 40_000, 0), 600)
        self.assertEqual(policy.select(50_000, 40_000, 10), 1000)
        self.assertEqual(policy.select(30_000, 40_000, 11), 600)
        self.assertEqual(policy.started_at, 0)
        self.assertEqual(policy.select(30_000, 40_000, 119), 600)
        self.assertEqual(policy.select(30_000, 40_000, 120), 0)

    def test_quiet_safety_thresholds_override_timer(self):
        for ram, nvme in [(55_000, 40_000), (30_000, 70_000)]:
            with self.subTest(ram=ram, nvme=nvme):
                policy = control.QuietPolicy()
                policy.select(40_000, 40_000, 0)
                with self.assertRaises(control.ThermalLimitError):
                    policy.select(ram, nvme, 1)
        self.assertEqual(control.QuietPolicy().select(54_999, 69_999, 0), 1000)

    def test_nvme_uses_hottest_channel_and_drive(self):
        self.nvme(1, 40_000, {"temp2_input": 65_000, "temp3_input": 32_000})
        self.nvme(2, 48_000)
        disks = control.nvme_sensors(self.root)
        self.assertEqual(control.nvme_temperature(disks), (65_000, [65_000, 48_000]))

    def test_missing_nvme_refused(self):
        with self.assertRaises(RuntimeError):
            control.nvme_sensors(self.root)

    def test_missing_nvme_composite_refused(self):
        disk = self.nvme()
        (disk.directory / "temp1_input").unlink()
        with self.assertRaises(RuntimeError):
            control.nvme_sensors(self.root)

    def test_changed_nvme_identity_refused(self):
        disk = self.nvme()
        (disk.directory / "name").write_text("k10temp")
        with self.assertRaises(RuntimeError):
            disk.read()

    def test_changed_nvme_composite_label_refused(self):
        disk = self.nvme()
        (disk.directory / "temp1_label").write_text("unknown")
        with self.assertRaises(RuntimeError):
            disk.read()

    def test_invalid_secondary_nvme_sensor_not_silently_skipped(self):
        self.nvme(extra={"temp2_input": -273_150})
        with self.assertRaises(ValueError):
            control.nvme_sensors(self.root)

    def test_quiet_curve_starts_off_when_cool_and_restores(self):
        driver = FakeDriver()
        driver.values["min_rpm"] = 500
        control.run_curve(driver, [self.sensor(4, 32_000)], 600, 3, nvmes=[self.nvme()])
        self.assertEqual(driver.targets, [0, 0, 0])
        self.assertEqual(driver.restores, 1)
        self.assertIn('"profile": "quiet"', self.output.getvalue())
        self.assertIn('"nvme_millidegrees": [40000]', self.output.getvalue())

    def test_quiet_curve_keeps_running_for_two_minutes(self):
        driver = FakeDriver()
        driver.values["min_rpm"] = 500
        sensor = self.sensor(4, 40_000)

        def ram_temperature(sensors):
            value = 40_000 if self.clock.now == 0 else 30_000
            return value, [value]

        with patch.object(control, "memory_temperature", side_effect=ram_temperature):
            control.run_curve(driver, [sensor], 600, 123, nvmes=[self.nvme()])
        self.assertEqual(driver.targets, [600] * 120 + [0] * 3)
        self.assertEqual(driver.restores, 1)

    def test_quiet_curve_stops_using_warmer_dimm_at_38(self):
        driver = FakeDriver()
        driver.values["min_rpm"] = 500
        sensors = [self.sensor(4, 40_000), self.sensor(6, 35_250)]

        def cool_warmer_dimm(seconds):
            self.clock.sleep(seconds)
            temperature = 38_250 if self.clock.now < 122 else 38_000
            (sensors[0].directory / "temp1_input").write_text(str(temperature))

        with patch.object(control.time, "sleep", side_effect=cool_warmer_dimm):
            control.run_curve(driver, sensors, 600, 125, nvmes=[self.nvme()])
        self.assertEqual(driver.targets, [600] * 122 + [0] * 3)
        self.assertEqual(driver.restores, 1)

    def test_reduced_log_frequency_keeps_watchdog_refreshes(self):
        driver = FakeDriver()
        driver.values["min_rpm"] = 500
        control.run_curve(driver, [self.sensor(4, 32_000)], 600, 5,
                          nvmes=[self.nvme()], log_interval=60)
        self.assertEqual(len(driver.targets), 5)
        self.assertEqual(self.output.getvalue().count('"event": "curve"'), 1)
        self.assertEqual(driver.restores, 1)

    def test_policy_transitions_log_immediately_despite_long_interval(self):
        driver = FakeDriver()
        driver.values["min_rpm"] = 500
        sensor = self.sensor(4, 32_000)

        def ram_temperature(sensors):
            value = 32_000 if self.clock.now == 0 else 40_000
            return value, [value]

        with patch.object(control, "memory_temperature", side_effect=ram_temperature):
            control.run_curve(driver, [sensor], 600, 5,
                              nvmes=[self.nvme()], log_interval=60)
        self.assertEqual(driver.targets, [0, 600, 600, 600, 600])
        self.assertEqual(self.output.getvalue().count('"event": "curve"'), 2)

    def test_quiet_curve_requires_permitted_restart_speed_even_when_cold(self):
        driver = FakeDriver()  # Kernel floor 800, proposed low target 600.
        with self.assertRaises(ValueError):
            control.run_curve(driver, [self.sensor(4, 32_000)], 600, 3, nvmes=[self.nvme()])
        self.assertEqual(driver.targets, [])

    def test_quiet_curve_refuses_unguarded_nvme(self):
        driver = FakeDriver()
        with self.assertRaises(RuntimeError):
            control.run_curve(driver, [self.sensor(4, 32_000)], 800, 3)
        self.assertEqual(driver.targets, [])

    def test_quiet_hot_ram_restores_before_sending_target(self):
        driver = FakeDriver()
        with self.assertRaises(control.ThermalLimitError):
            control.run_curve(driver, [self.sensor(4, 55_000)], 800, 3, nvmes=[self.nvme()])
        self.assertEqual(driver.targets, [])
        self.assertEqual(driver.restores, 1)
        self.assertIn("thermal_fallback", self.output.getvalue())

    def test_quiet_hot_nvme_restores_before_sending_target(self):
        driver = FakeDriver()
        with self.assertRaises(control.ThermalLimitError):
            control.run_curve(driver, [self.sensor(4, 32_000)], 800, 3,
                              nvmes=[self.nvme(temperature=70_000)])
        self.assertEqual(driver.targets, [])
        self.assertEqual(driver.restores, 1)

    def test_nvme_disappearance_during_control_restores(self):
        driver = FakeDriver()
        disk = self.nvme()
        original_target = driver.target

        def disappear_after_control(rpm):
            original_target(rpm)
            (disk.directory / "temp1_input").unlink()

        driver.target = disappear_after_control
        with self.assertRaises(OSError):
            control.run_curve(driver, [self.sensor(4, 32_000)], 800, 3, nvmes=[disk])
        self.assertEqual(driver.targets, [0])
        self.assertEqual(driver.restores, 1)

    def test_probe_hot_nvme_restores_before_sending_target(self):
        driver = FakeDriver()
        with self.assertRaises(control.ThermalLimitError):
            control.probe_speeds(driver, [self.sensor(4, 32_000)], [800], 20, 180,
                                 nvmes=[self.nvme(temperature=70_000)])
        self.assertEqual(driver.targets, [])
        self.assertEqual(driver.restores, 1)

    def test_probe_requires_nvme_guard(self):
        driver = FakeDriver()
        with self.assertRaises(RuntimeError):
            control.probe_speeds(driver, [self.sensor(4, 32_000)], [800], 20, 180, nvmes=[])
        self.assertEqual(driver.targets, [])

    def test_default_observe_previews_quiet_without_any_control_writes(self):
        driver = FakeDriver()
        with patch.object(control, "memory_sensors", return_value=[self.sensor(4, 40_000)]):
            with patch.object(control, "nvme_sensors", return_value=[self.nvme()]):
                with patch.object(control.Driver, "find", return_value=driver):
                    self.assertEqual(control.main(["observe", "--samples", "1"]), 0)
        self.assertIn('"preview_target_rpm": 600', self.output.getvalue())
        self.assertNotIn("default_curve_rpm", self.output.getvalue())
        self.assertEqual(driver.targets, [])
        self.assertEqual(driver.restores, 0)

    def test_legacy_observe_remains_available_without_nvme(self):
        with patch.object(control, "memory_sensors", return_value=[self.sensor(4, 32_000)]):
            with patch.object(control, "nvme_sensors") as discover_disks:
                with patch.object(control.Driver, "find", return_value=FakeDriver()):
                    self.assertEqual(control.main(["observe", "--profile", "legacy", "--samples", "1"]), 0)
        discover_disks.assert_not_called()
        self.assertIn('"preview_target_rpm": 800', self.output.getvalue())

    def test_hot_observe_reports_firmware_preview_without_writes(self):
        driver = FakeDriver()
        with patch.object(control, "memory_sensors", return_value=[self.sensor(4, 55_000)]):
            with patch.object(control, "nvme_sensors", return_value=[self.nvme()]):
                with patch.object(control.Driver, "find", return_value=driver):
                    self.assertEqual(control.main(["observe", "--samples", "1"]), 0)
        self.assertIn('"preview_state": "firmware"', self.output.getvalue())
        self.assertEqual(driver.targets, [])
        self.assertEqual(driver.restores, 0)


if __name__ == "__main__":
    unittest.main()
