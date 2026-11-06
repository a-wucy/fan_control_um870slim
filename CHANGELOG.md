# Change history

## Standalone publication — 2026-11-06

- Extract the system-fan-only implementation into an independent repository;
  retain GPL-2.0 and upstream attribution without copying the CPU-capable driver.
- Keep the tested module/controller policy unchanged; module version remains
  **0.1.1**. Only module provenance comments change for the new layout.
- Make unprivileged tests use normal temporary-directory selection (`TMPDIR`
  is supported), rather than depending on the original development harness.
- Add public installation, safety, calibration, kernel-upgrade, and removal
  documentation, plus unprivileged CI. Do not publish generated kernel modules,
  local installation manifests, private handoff notes, or machine journals.
- Record positive multi-day user feedback without claiming all physical safety
  paths or future kernel compatibility have been proven.
- Revalidate 90 Python tests, sanitized shared C tests, systemd unit syntax, and
  an exact-7.2.11 kernel build from the standalone checkout without reloading
  the live driver or changing the enabled/running service.

## Module 0.1.1 and policy tuning — 2026-11-02

- Raise the RAM stop threshold from 35 to **38 C**; start remains 40 C. Normal
  idle RAM around 37..38 C otherwise prevented a running fan from stopping.
- Reduce startup assist from 2000 to **1200 nominal RPM**, retaining the gradual
  100-RPM/four-second ramp and all restoration/safety checks.
- Preserve the NVMe thresholds, 600/1000 running targets, two-minute minimum-on
  time, CPU write prohibition, and no-auto-restart policy.
- Pass 90 Python tests, sanitized shared C tests, exact-kernel compilation with
  `W=1`/`-Werror`, and systemd unit verification.
- Install and verify module 0.1.1, matching controller/helper/unit copies, clean
  firmware restoration during the update, and subsequent idle-off operation.

## Initial implementation and live calibration — 2026-11-01

- Introduce observation-only-by-default `minisforum_system_fan`, restricted to
  the system-target and override registers, with verified firmware restoration.
- Use real DDR5/NVMe sensors, hysteresis, minimum-on time, explicit thermal
  handoff, and independent kernel checks/watchdog.
- Add the managed boot service and safe uninstaller; no DKMS, package hooks,
  firmware flashing, or CPU fan-control modifications.
- Validate stable 600/800/1000 nominal RPM; reject unstable 400 RPM. A separate
  short 500-RPM probe passed, but normal operation retains 600 for margin.
- Fix signal handling so Ctrl-C/SIGTERM cannot interrupt restoration verification.
- Verify idle-off/load-on, high-speed promotion, and RAM-triggered firmware
  fallback during a demanding workload.
