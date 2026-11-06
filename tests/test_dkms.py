# SPDX-License-Identifier: GPL-2.0
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import service


class DkmsConfigurationTests(unittest.TestCase):
    def configuration(self, kernel):
        # Source the real DKMS configuration using its real shell syntax, but
        # never invoke DKMS, build a kernel module, or access live hardware.
        script = '''
kernelver="$2"
kernel_source_dir="/headers/for/$kernelver"
source "$1"
if [[ "$kernelver" =~ $BUILD_EXCLUSIVE_KERNEL ]]; then allowed=yes; else allowed=no; fi
printf '%s\\n' "$allowed" "$PACKAGE_NAME" "$PACKAGE_VERSION" "${MAKE[0]}" \
    "$AUTOINSTALL" "$BUILD_EXCLUSIVE_ARCH" "$NO_WEAK_MODULES"
'''
        conf = Path(__file__).resolve().parents[1] / "dkms.conf"
        return subprocess.run(["/bin/bash", "-c", script, "dkms-config-test", str(conf), kernel],
                              check=True, capture_output=True, text=True).stdout.splitlines()

    def test_shell_and_service_gates_agree_for_patch_and_package_releases(self):
        for kernel in ["7.2.0-arch1-1", "7.2.11-arch1-1", "7.2.14-arch1-1", "7.2.99-arch2-3"]:
            with self.subTest(kernel=kernel):
                self.assertTrue(service.dkms_kernel_allowed(kernel))
                self.assertEqual(self.configuration(kernel)[0], "yes")

    def test_next_minor_old_lts_other_flavors_and_malformed_releases_are_blocked(self):
        for kernel in ["7.3.0-arch1-1", "7.3.1-arch1-1", "7.20.1-arch1-1", "6.18.60-1-lts",
                       "7.2.14-zen1-1-zen", "7.2.14", "7.2.14-arch1-1-extra", "7.2-rc1-arch1-1",
                       "prefix7.2.14-arch1-1", "7x2x14-arch1-1", "7.2.14-arch1-1\n"]:
            with self.subTest(kernel=kernel):
                self.assertFalse(service.dkms_kernel_allowed(kernel))
                self.assertEqual(self.configuration(kernel)[0], "no")

    def test_dkms_uses_target_headers_not_the_running_kernel(self):
        values = self.configuration("7.2.99-arch2-1")
        self.assertEqual(values[1], service.DKMS_PACKAGE)
        self.assertEqual(values[2], service.DKMS_VERSION)
        self.assertEqual(values[3], "make -C module KDIR=/headers/for/7.2.99-arch2-1")
        self.assertEqual(values[4:], ["yes", "^x86_64$", "yes"])
        source = (Path(__file__).resolve().parents[1] / "module/minisforum_system_fan.c").read_text()
        self.assertIn(f'MODULE_VERSION("{service.DKMS_VERSION}")', source)


if __name__ == "__main__":
    unittest.main()
