#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Foreground RAM/NVMe temperature controller. No module loading or installation.

Only the separate minisforum_system_fan module is accepted. The upstream
minisforum_ec driver is deliberately never controlled by this program.
"""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import statistics
import sys
import time


NAME = "minisforum_system_fan"
HWMON = Path("/sys/class/hwmon")
LOCK = Path("/run/lock/minisforum-system-fan.lock")
# A successful safety handoff is distinct from an unexpected failure. The
# service accepts this status but NEVER automatically restarts either case.
THERMAL_EXIT_STATUS = 75
_stop_requested = False


class RecoveryError(RuntimeError):
    pass


class ThermalLimitError(RuntimeError):
    pass


class StopRequested(Exception):
    pass


def check_stop_requested():
    # Raise only at safe loop boundaries, never asynchronously during restore.
    if _stop_requested:
        raise StopRequested("stop requested by SIGINT or SIGTERM")


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def choose_target(temperature, middle_rpm):
    """Original RAM-only profile, retained as an explicit rollback option."""
    if temperature < 30_000:
        return 0
    if temperature <= 50_000:
        return middle_rpm
    return 1000


def check_thermal_limits(ram, nvme):
    if ram >= 55_000 or nvme >= 70_000:
        raise ThermalLimitError(f"thermal fallback: RAM {ram / 1000:g} C (limit 55), "
                                f"NVMe {nvme / 1000:g} C (limit 70); returning to firmware")


@dataclass
class QuietPolicy:
    """Temperatures in millidegrees C; the clock must be monotonic.

    Minimum-on time is measured from requesting a running target, not from
    reaching a measured RPM. Kernel spin-up/ramping remains independent.
    """
    low_rpm: int = 600
    level: str = "off"
    started_at: float | None = None

    def select(self, ram, nvme, now):
        check_thermal_limits(ram, nvme)  # Never delay safety fallback for a timer.
        previous = self.level
        if ram >= 50_000 or nvme >= 60_000:
            self.level = "high"
        elif self.level == "high":
            if ram <= 45_000 and nvme <= 55_000:
                self.level = "low"
        elif self.level == "low":
            if (ram <= 38_000 and nvme <= 45_000 and self.started_at is not None
                    and now - self.started_at >= 120):
                self.level = "off"
        elif ram >= 40_000 or nvme >= 50_000:
            self.level = "low"
        if previous == "off" and self.level != "off":
            self.started_at = now
        elif self.level == "off":
            self.started_at = None
        return {"off": 0, "low": self.low_rpm, "high": 1000}[self.level]

    def minimum_on_remaining(self, now):
        if self.started_at is None:
            return 0
        return max(0, math.ceil(120 - (now - self.started_at)))


def validate_target(rpm, minimum):
    if rpm < 0 or rpm > 5100 or rpm % 100 or (rpm and rpm < minimum):
        raise ValueError(f"target {rpm}: use 0 or multiples of 100 in {minimum}..5100 RPM")


@dataclass(frozen=True)
class MemorySensor:
    directory: Path

    def read(self):
        # Recheck identity as well as existence: never reuse an hwmon number
        # for another device after removal/re-enumeration.
        if (self.directory / "name").read_text().strip() != "spd5118":
            raise RuntimeError(f"memory sensor identity changed: {self.directory}")
        value = int((self.directory / "temp1_input").read_text().strip())
        if not 0 <= value <= 125_000:
            raise ValueError(f"implausible RAM temperature: {value} at {self.directory}")
        return value


def memory_sensors(root=HWMON, expected=2):
    result = []
    for directory in sorted(root.glob("hwmon*")):
        try:
            name = (directory / "name").read_text().strip()
        except FileNotFoundError:
            continue
        if name == "spd5118":
            sensor = MemorySensor(directory.resolve())
            sensor.read()  # A present but unreadable DIMM is an error, not skipped.
            result.append(sensor)
    if len(result) != expected:
        raise RuntimeError(f"expected {expected} spd5118 sensors, found {len(result)}; refusing control")
    return result


def memory_temperature(sensors):
    values = [sensor.read() for sensor in sensors]
    if not values:
        raise RuntimeError("no memory temperature sensors")
    return max(values), values


@dataclass(frozen=True)
class NvmeSensor:
    directory: Path
    inputs: tuple[str, ...]

    def read(self):
        if (self.directory / "name").read_text().strip() != "nvme":
            raise RuntimeError(f"NVMe sensor identity changed: {self.directory}")
        if (self.directory / "temp1_label").read_text().strip() != "Composite":
            raise RuntimeError(f"NVMe composite sensor identity changed: {self.directory}")
        values = []
        for name in self.inputs:
            value = int((self.directory / name).read_text().strip())
            if not 0 <= value <= 125_000:
                raise ValueError(f"implausible NVMe temperature: {value} at {self.directory / name}")
            values.append(value)
        if not values:
            raise RuntimeError(f"no NVMe temperature inputs at {self.directory}")
        # Include internal sensor channels, not just composite: a controller
        # hotspot must not be hidden by a cooler composite temperature.
        return max(values)


def nvme_sensors(root=HWMON):
    result = []
    for directory in sorted(root.glob("hwmon*")):
        try:
            name = (directory / "name").read_text().strip()
        except FileNotFoundError:
            continue
        if name == "nvme":
            inputs = tuple(sorted(path.name for path in directory.glob("temp*_input")))
            if "temp1_input" not in inputs:
                raise RuntimeError(f"missing NVMe composite temperature at {directory}")
            sensor = NvmeSensor(directory.resolve(), inputs)
            sensor.read()
            result.append(sensor)
    if not result:
        raise RuntimeError("no readable NVMe temperature sensors; refusing unguarded control")
    return result


def nvme_temperature(sensors):
    values = [sensor.read() for sensor in sensors]
    if not values:
        raise RuntimeError("no NVMe temperature sensors")
    return max(values), values


@dataclass(frozen=True)
class Driver:
    directory: Path

    @classmethod
    def find(cls, root=HWMON):
        matches = []
        for directory in root.glob("hwmon*"):
            try:
                if (directory / "name").read_text().strip() == NAME:
                    matches.append(directory.resolve())
            except FileNotFoundError:
                continue
        if len(matches) != 1:
            raise RuntimeError(f"expected one {NAME} hwmon device, found {len(matches)}")
        return cls(matches[0])

    def status(self):
        if (self.directory / "name").read_text().strip() != NAME:
            raise RuntimeError("system-fan driver identity changed")
        fields = {}
        for line in (self.directory / "status").read_text().splitlines():
            key, value = line.split("=", 1)
            fields[key] = value if key == "state" else int(value)
        required = {"state", "firmware_owned", "release_pending", "fault", "allow_control",
                    "min_rpm", "applied_rpm", "fan_rpm", "ec_temp_millidegrees"}
        if not required <= fields.keys():
            raise RuntimeError("incomplete system-fan status")
        return fields

    def target(self, rpm):
        (self.directory / "target_rpm").write_text(f"{rpm}\n")

    def restore(self):
        (self.directory / "restore").write_text("1\n")
        state = self.status()
        if not state["firmware_owned"] or state["release_pending"] or state["fault"]:
            raise RecoveryError(f"firmware restoration was not verified: {state}")
        emit("firmware_restored", status=state)


@contextmanager
def exclusive_control(path=LOCK):
    # Do not unlink the lock: unlinking a held lock allows a second inode/owner.
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another controller is running; stop it with Ctrl-C first") from exc
        yield
    finally:
        os.close(fd)


@contextmanager
def control_session(driver):
    status = driver.status()
    if not status["allow_control"]:
        raise RuntimeError("module is observation-only; reload with allow_control=1 to control")
    if status["state"] != "firmware" or status["fault"] or status["release_pending"]:
        raise RuntimeError("not cleanly firmware-owned; run the explicit restore command first")
    try:
        yield
    finally:
        # Includes sensor failures, I/O exceptions, Ctrl-C, and SIGTERM.
        # SIGKILL/crashes instead rely on the kernel's 10-second watchdog.
        try:
            driver.restore()
        except Exception as exc:
            emit("RESTORE_FAILED", error=str(exc), error_type=type(exc).__name__,
                 action="keep module loaded for retries; fully power off if recovery fails")
            raise RecoveryError("could not verify return to firmware; do not assume warm reboot clears EC state") from exc


def checked_status(driver):
    status = driver.status()
    if status["fault"] or status["release_pending"]:
        raise RuntimeError(f"driver safety fallback: {status}")
    if status["state"] not in {"firmware", "controlled"}:
        raise RuntimeError(f"unexpected system-fan ownership: {status}")
    return status


def run_curve(driver, sensors, middle_rpm, duration, *, nvmes=None, profile="quiet", log_interval=1):
    if profile not in {"quiet", "legacy"}:
        raise ValueError(f"unknown profile: {profile}")
    validate_target(middle_rpm, driver.status()["min_rpm"])
    if not 0 < middle_rpm <= 1000:
        raise ValueError("middle-band speed must be nonzero and <=1000 RPM")
    if profile == "quiet" and not nvmes:
        raise RuntimeError("quiet profile requires NVMe temperature sensors")
    if not math.isfinite(log_interval) or log_interval <= 0:
        raise ValueError("log interval must be finite and positive")
    policy = QuietPolicy(middle_rpm)
    start = time.monotonic()
    last_logged = last_target = last_level = None
    with control_session(driver):
        while not duration or time.monotonic() - start < duration:
            check_stop_requested()
            temperature, values = memory_temperature(sensors)
            checked_status(driver)
            now = time.monotonic()
            details = {"profile": profile, "elapsed_seconds": round(now - start, 1)}
            if profile == "quiet":
                nvme, disk_values = nvme_temperature(nvmes)
                details["nvme_millidegrees"] = disk_values
                try:
                    target = policy.select(temperature, nvme, now)
                except ThermalLimitError as exc:
                    emit("thermal_fallback", ram_millidegrees=values,
                         message=str(exc), **details)
                    raise
                details.update(policy_state=policy.level,
                               minimum_on_remaining_seconds=policy.minimum_on_remaining(now))
            else:
                target = choose_target(temperature, middle_rpm)
            check_stop_requested()
            driver.target(target)
            status = checked_status(driver)
            level = details.get("policy_state")
            if (last_logged is None or target != last_target or level != last_level
                    or now - last_logged >= log_interval):
                emit("curve", ram_millidegrees=values, target_rpm=target,
                     status=status, **details)
                last_logged, last_target, last_level = now, target, level
            time.sleep(1)


def probe_speeds(driver, sensors, rpms, hold, settle_timeout, *, nvmes):
    if not nvmes:
        raise RuntimeError("calibration requires NVMe temperature sensors")
    minimum = driver.status()["min_rpm"]
    for rpm in rpms:
        validate_target(rpm, minimum)
        if not rpm:
            raise ValueError("probe takes nonzero targets; zero is tested by the curve's fan-off state")
    stable = []
    with control_session(driver):
        for target in rpms:
            started = time.monotonic()
            reached = None
            readings = []
            while True:
                check_stop_requested()
                temperature, values = memory_temperature(sensors)
                nvme, disk_values = nvme_temperature(nvmes)
                check_thermal_limits(temperature, nvme)
                checked_status(driver)
                check_stop_requested()
                driver.target(target)
                status = checked_status(driver)
                now = time.monotonic()
                emit("probe", target_rpm=target, ram_millidegrees=values,
                     nvme_millidegrees=disk_values, status=status)
                if status["applied_rpm"] == target:
                    if reached is None:
                        reached = now
                    # Ignore the first 10 seconds after reaching a target.
                    if now - reached >= 10:
                        readings.append(status["fan_rpm"])
                    if now - reached >= hold:
                        break
                else:
                    reached = None
                    readings.clear()
                if now - started > settle_timeout + hold:
                    raise RuntimeError(f"target {target} did not settle; previous validated targets: {stable}")
                time.sleep(1)
            mean = statistics.mean(readings)
            spread = max(readings) - min(readings)
            allowed_spread = min(600, max(150, target * 0.3))
            if (min(readings) <= 0 or spread > allowed_spread or
                    abs(mean - target) > max(100, target * 0.25)):
                raise RuntimeError(f"unstable/unresponsive at {target} RPM: mean={mean:.0f}, "
                                   f"spread={spread}; previous validated targets: {stable}")
            stable.append(target)
            emit("validated_target", requested_rpm=target, mean_rpm=round(mean), spread_rpm=spread)
    emit("probe_complete", validated_targets=stable, lowest_validated_target=min(stable))


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and >0")
    return number


def duration_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be finite and >=0 (0 means until Ctrl-C)")
    return number


def parse_rpms(value):
    try:
        result = [int(item) for item in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use comma-separated integer RPMs") from exc
    if not result or any(rpm <= 0 or rpm > 5100 or rpm % 100 for rpm in result):
        raise argparse.ArgumentTypeError("use nonzero multiples of 100 up to 5100 RPM")
    if any(a <= b for a, b in zip(result, result[1:])):
        raise argparse.ArgumentTypeError("probe targets must be strictly decreasing")
    return result


def profile_arguments(parser):
    parser.add_argument("--profile", choices=["quiet", "legacy"], default="quiet",
                        help="quiet: RAM/NVMe with hysteresis; legacy: original RAM-only 30/50 C curve")
    parser.add_argument("--middle-rpm", type=int,
                        help="low running target: default 600 for quiet, 800 for legacy")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-dimms", type=int, choices=range(1, 5), default=2)
    commands = parser.add_subparsers(dest="command", required=True)
    observe = commands.add_parser("observe", help="read-only; also works before loading any module")
    observe.add_argument("--samples", type=int, default=5)
    observe.add_argument("--interval", type=positive_seconds, default=1)
    profile_arguments(observe)
    run = commands.add_parser("run", help="temporary temperature curve, firmware restore on exit")
    profile_arguments(run)
    run.add_argument("--duration", type=duration_seconds, default=300)
    run.add_argument("--log-interval", type=positive_seconds, default=1,
                     help="seconds between routine logs; transitions and safety events log immediately")
    probe = commands.add_parser("probe", help="supervised descending-speed calibration")
    probe.add_argument("--rpms", type=parse_rpms, default=parse_rpms("1000,800"))
    probe.add_argument("--hold", type=positive_seconds, default=30)
    probe.add_argument("--settle-timeout", type=positive_seconds, default=180)
    commands.add_parser("restore", help="synchronously restore and verify firmware ownership")
    args = parser.parse_args(argv)
    if args.command in {"observe", "run"}:
        if args.middle_rpm is None:
            args.middle_rpm = 600 if args.profile == "quiet" else 800
        validate_target(args.middle_rpm, 100)
        if not 0 < args.middle_rpm <= 1000:
            parser.error("--middle-rpm must be nonzero and <=1000 RPM")
    if args.command == "observe":
        if args.samples <= 0:
            parser.error("--samples must be >0")
        sensors = memory_sensors(expected=args.expected_dimms)
        nvmes = nvme_sensors() if args.profile == "quiet" else None
        policy = QuietPolicy(args.middle_rpm)
        try:
            driver = Driver.find()
        except RuntimeError:
            driver = None
        for sample in range(args.samples):
            check_stop_requested()
            temperature, values = memory_temperature(sensors)
            details = {"profile": args.profile}
            if args.profile == "quiet":
                nvme, disk_values = nvme_temperature(nvmes)
                details["nvme_millidegrees"] = disk_values
                now = time.monotonic()
                try:
                    target = policy.select(temperature, nvme, now)
                    details.update(preview_state=policy.level,
                                   minimum_on_remaining_seconds=policy.minimum_on_remaining(now))
                except ThermalLimitError as exc:
                    target = None
                    details.update(preview_state="firmware", thermal_fallback=str(exc))
            else:
                target = choose_target(temperature, args.middle_rpm)
            emit("observe", ram_millidegrees=values, maximum_ram_millidegrees=temperature,
                 preview_target_rpm=target,
                 status=driver.status() if driver else "experimental module not loaded", **details)
            if sample + 1 < args.samples:
                time.sleep(args.interval)
        return 0
    if os.geteuid() != 0:
        parser.error("control and restore require root; observation does not")
    if Path("/sys/module/minisforum_ec").exists():
        raise RuntimeError("the original CPU-controlling module is loaded; refusing to proceed")
    driver = Driver.find()
    with exclusive_control():
        if args.command == "restore":
            driver.restore()
        else:
            sensors = memory_sensors(expected=args.expected_dimms)
            if args.command == "run":
                nvmes = nvme_sensors() if args.profile == "quiet" else None
                run_curve(driver, sensors, args.middle_rpm, args.duration,
                          nvmes=nvmes, profile=args.profile, log_interval=args.log_interval)
            else:
                if args.hold < 20:
                    parser.error("probe --hold must be >=20 seconds")
                probe_speeds(driver, sensors, args.rpms, args.hold, args.settle_timeout,
                             nvmes=nvme_sensors())
    return 0


def stop_requested(signum, frame):
    # A signal handler must NEVER raise inside the cleanup/verification path.
    # The control loop checks this flag at its next safe boundary, then its
    # finally block restores firmware even if Ctrl-C is pressed again.
    # Explicit `restore` also completes before honoring a stop request.
    global _stop_requested
    _stop_requested = True


def cli(argv=None):
    try:
        result = main(argv)
        if _stop_requested:
            emit("stopped")
        return result
    except StopRequested:
        emit("stopped")
        return 0
    except ThermalLimitError as error:
        emit("safety_stop", message=str(error))
        return THERMAL_EXIT_STATUS
    except (OSError, ValueError, RuntimeError) as error:
        emit("error", message=str(error))
        return 1


if __name__ == "__main__":
    signal.signal(signal.SIGINT, stop_requested)
    signal.signal(signal.SIGTERM, stop_requested)
    sys.exit(cli())
