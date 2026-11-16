# SPDX-License-Identifier: GPL-2.0
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import service


class DkmsConfigurationTests(unittest.TestCase):
    def configuration(self, kernel, arch="x86_64"):
        # Source the real DKMS configuration using its real shell syntax, but
        # never invoke DKMS, build a kernel module, or access live hardware.
        script = '''
kernelver="$2"
arch="$3"
kernel_source_dir="/headers/for/$kernelver"
source "$1"
if [[ "$kernelver" =~ $BUILD_EXCLUSIVE_KERNEL && "$arch" =~ $BUILD_EXCLUSIVE_ARCH ]]; then
    allowed=yes
else
    allowed=no
fi
printf '%s\\n' "$allowed" "$PACKAGE_NAME" "$PACKAGE_VERSION" "${MAKE[0]}" \
    "$AUTOINSTALL" "$BUILD_EXCLUSIVE_ARCH" "$NO_WEAK_MODULES"
'''
        conf = Path(__file__).resolve().parents[1] / "dkms.conf"
        return subprocess.run(["/bin/bash", "-c", script, "dkms-config-test", str(conf), kernel, arch],
                              check=True, capture_output=True, text=True).stdout.splitlines()

    def test_shell_and_service_gates_agree_for_patch_and_package_releases(self):
        for kernel in ["7.2.0-arch1-1", "7.2.11-arch1-1", "7.2.14-arch1-1", "7.2.99-arch2-3",
                       "7.3.0-arch1-1", "7.3.4-arch1-2", "7.3.99-arch2-3"]:
            with self.subTest(kernel=kernel):
                self.assertTrue(service.dkms_kernel_allowed(kernel))
                self.assertEqual(self.configuration(kernel)[0], "yes")

    def test_next_minor_old_lts_other_flavors_and_malformed_releases_are_blocked(self):
        for kernel in ["7.1.1-arch1-1", "7.4.0-arch1-1", "7.4.1-arch1-1", "7.20.1-arch1-1",
                       "7.23.1-arch1-1", "7.30.1-arch1-1", "8.3.1-arch1-1", "6.18.60-1-lts",
                       "7.2.14-zen1-1-zen", "7.2.14", "7.2.14-arch1-1-extra", "7.2-rc1-arch1-1",
                       "prefix7.2.14-arch1-1", "7x2x14-arch1-1", "7.2.14-arch1-1\n",
                       "7.3.4-zen1-1-zen", "7.3.4-1-lts", "7.3.4-hardened1-1-hardened",
                       "7.3.4", "7.3.4-arch1-2-extra", "7.3-rc1-arch1-1",
                       "prefix7.3.4-arch1-2", "7x3x4-arch1-2", "7.3.4-arch1-2\n"]:
            with self.subTest(kernel=kernel):
                self.assertFalse(service.dkms_kernel_allowed(kernel))
                self.assertEqual(self.configuration(kernel)[0], "no")

    def test_only_reviewed_minor_series_are_allowed(self):
        for minor in range(40):
            kernel = f"7.{minor}.1-arch1-1"
            with self.subTest(kernel=kernel):
                self.assertEqual(service.dkms_kernel_allowed(kernel), minor in {2, 3})
                self.assertEqual(self.configuration(kernel)[0], "yes" if minor in {2, 3} else "no")

    def test_other_architectures_remain_excluded_in_both_series(self):
        for kernel in ["7.2.14-arch1-1", "7.3.4-arch1-2"]:
            for arch in ["i686", "aarch64", "x86_64-extra", "prefix-x86_64", "x86_64\n"]:
                with self.subTest(kernel=kernel, arch=arch):
                    self.assertEqual(self.configuration(kernel, arch)[0], "no")

    def test_dkms_uses_target_headers_not_the_running_kernel(self):
        for kernel in ["7.2.99-arch2-1", "7.3.99-arch2-1"]:
            with self.subTest(kernel=kernel):
                values = self.configuration(kernel)
                self.assertEqual(values[1], service.DKMS_PACKAGE)
                self.assertEqual(values[2], service.DKMS_VERSION)
                self.assertEqual(values[3], f"make -C module KDIR=/headers/for/{kernel}")
                self.assertEqual(values[4:], ["yes", "^x86_64$", "yes"])
        source = (Path(__file__).resolve().parents[1] / "module/minisforum_system_fan.c").read_text()
        self.assertIn(f'MODULE_VERSION("{service.DKMS_VERSION}")', source)


if __name__ == "__main__":
    unittest.main()
