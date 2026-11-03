"""scripts/build-linux-installer.sh only builds on Ubuntu 20.04."""
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "build-linux-installer.sh")


def os_release():
    """The ID and VERSION_ID of this machine."""
    try:
        with open("/etc/os-release") as f:
            return dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', f.read(), re.M))
    except OSError:
        return {}


ON_2004 = os_release().get("ID") == "ubuntu" and os_release().get("VERSION_ID") == "20.04"


class IsUbuntu2004(unittest.TestCase):
    def check(self, text):
        with open(SCRIPT) as f:
            func = re.search(r"^is_ubuntu_2004\(\) \{.*?^\}$", f.read(), re.M | re.S).group()
        with tempfile.NamedTemporaryFile("w", suffix=".os-release") as rel:
            rel.write(text)
            rel.flush()
            return subprocess.run(["bash", "-c", f'{func}\nis_ubuntu_2004 "$1"', "-", rel.name]).returncode == 0

    def test_only_ubuntu_2004_passes(self):
        self.assertTrue(self.check('ID=ubuntu\nVERSION_ID="20.04"\n'))
        self.assertFalse(self.check('ID=ubuntu\nVERSION_ID="24.04"\n'))
        self.assertFalse(self.check('ID=elementary\nID_LIKE=ubuntu\nVERSION_ID="8"\n'))
        self.assertFalse(self.check('ID=debian\nVERSION_ID="20.04"\n'))
        self.assertFalse(self.check(""))


@unittest.skipIf(ON_2004, "this machine is Ubuntu 20.04, so a plain build is allowed")
class OffUbuntu2004(unittest.TestCase):
    """Plain builds never run on this machine: Docker takes them, or they stop."""

    def build(self, docker, *args):
        with tempfile.TemporaryDirectory() as bin_dir:
            log = os.path.join(bin_dir, "docker.log")
            if docker is not None:
                stub = os.path.join(bin_dir, "docker")
                with open(stub, "w") as f:
                    f.write(f'#!/bin/bash\necho "$@" >> {log}\n{docker}\n')
                os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
            # Only the tools the script needs before Docker, never a real docker.
            tools = os.path.join(bin_dir, "tools")
            os.mkdir(tools)
            for tool in ("dirname", "id"):
                os.symlink(shutil.which(tool), os.path.join(tools, tool))
            path = bin_dir + ":" + tools
            r = subprocess.run(["/bin/bash", SCRIPT, *args], capture_output=True, text=True,
                               env={"PATH": path, "HOME": bin_dir})
            if not os.path.exists(log):
                return r, ""
            with open(log) as f:
                return r, f.read()

    def test_plain_build_goes_to_the_container(self):
        for fmt in ("--pyinstaller", "--deb"):
            r, calls = self.build("exit 0", fmt, "--clean-pyinstaller")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("not Ubuntu 20.04", r.stdout)
            run = [c for c in calls.splitlines() if c.startswith("run ")]
            self.assertEqual(len(run), 1)
            self.assertIn(f"build-linux-installer.sh {fmt} --clean-pyinstaller", run[0])

    def test_refuses_without_docker(self):
        r, calls = self.build(None, "--pyinstaller")
        self.assertEqual(r.returncode, 1)
        self.assertIn("must run on Ubuntu 20.04", r.stderr)
        self.assertIn("not installed", r.stderr)

    def test_refuses_when_the_daemon_is_down(self):
        r, calls = self.build("exit 1", "--pyinstaller")
        self.assertEqual(r.returncode, 1)
        self.assertIn("daemon cannot be reached", r.stderr)
        self.assertNotIn("run ", calls)


if __name__ == "__main__":
    unittest.main()
