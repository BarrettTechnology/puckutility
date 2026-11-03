"""scripts/build-linux-installer.sh builds only on Ubuntu 20.04, and the .run it makes."""
import glob
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
        for fmt in ("--pyinstaller", "--deb", "--installer"):
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


class RunInstaller(unittest.TestCase):
    """The .run the build makes: the script's own block, on fake debs."""

    @classmethod
    def setUpClass(cls):
        with open(SCRIPT) as f:
            text = f.read()
        block = re.search(r"^    # --- self-extracting installer -+\n(.*?^    fi\n)", text, re.M | re.S).group(1)
        with open(os.path.join(REPO, "scripts", "linux-installer", "install.sh")) as f:
            cls.app = re.search(r"^APP_PKG=(\w+)$", f.read(), re.M).group(1)
        cls.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(cls.tmp.name, "scripts"))
        shutil.copytree(os.path.join(REPO, "scripts", "linux-installer"),
                        os.path.join(cls.tmp.name, "scripts", "linux-installer"))
        debs = os.path.join(cls.tmp.name, "build", "deb")
        os.makedirs(debs)
        names = [f"{cls.app}_1.2.3_amd64.deb"]
        names += [f"puck-can-{p}_1.2.3_all.deb" for p in ("common", "peak", "canable")]
        for name in names:
            with open(os.path.join(debs, name), "w") as f:
                f.write(name)
        os.makedirs(os.path.join(cls.tmp.name, "build", "lin"))
        env = {**os.environ, "FORMAT": "installer", "VERSION": "v1.2.3", "DEB_VERSION": "1.2.3",
               "DEB_ROOT": f"build/deb/{cls.app}_1.2.3_amd64"}
        subprocess.run(["bash", "-ec", block], cwd=cls.tmp.name, env=env, check=True,
                       capture_output=True, text=True)
        cls.run_file = glob.glob(os.path.join(cls.tmp.name, "build", "*-lin-v1.2.3-installer.run"))[0]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def extract(self):
        dest = tempfile.mkdtemp(dir=self.tmp.name)
        r = subprocess.run(["sh", self.run_file, "--extract", dest], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return dest

    def test_extract_gives_the_debs_and_the_versioned_script(self):
        dest = self.extract()
        self.assertEqual(sorted(os.listdir(os.path.join(dest, "debs"))), sorted(
            [f"{self.app}_1.2.3_amd64.deb"] + [f"puck-can-{p}_1.2.3_all.deb" for p in ("canable", "common", "peak")]))
        with open(os.path.join(dest, "install.sh")) as f:
            self.assertNotIn("@VERSION@", f.read())

    def test_damaged_file_is_refused(self):
        with open(self.run_file, "rb") as f:
            data = f.read()
        bad = os.path.join(self.tmp.name, "bad.run")
        with open(bad, "wb") as f:
            f.write(data[:-100])
        r = subprocess.run(["sh", bad, "--extract", os.path.join(self.tmp.name, "bad")],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("damaged", r.stdout + r.stderr)

    def dry_run(self, *args, installed=()):
        """install.sh --dry-run with dpkg-query reporting these packages installed."""
        dest = self.extract()
        bin_dir = os.path.join(dest, "stubs")
        os.mkdir(bin_dir)
        stub = os.path.join(bin_dir, "dpkg-query")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\nfor a; do p=$a; done\ncase " %s " in *" $p "*) echo "install ok installed";; esac\n'
                    % " ".join(installed))
        os.chmod(stub, 0o755)
        env = {**os.environ, "PATH": bin_dir + ":" + os.environ["PATH"]}
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        return subprocess.run(["bash", os.path.join(dest, "install.sh"), "--dry-run", "--yes", "--text",
                               "--no-kernel-check", *args], capture_output=True, text=True, env=env,
                              stdin=subprocess.DEVNULL)

    def test_adapter_options_pick_the_packages(self):
        r = self.dry_run("--no-peak")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"{self.app}_1.2.3_amd64", r.stdout)
        self.assertIn("puck-can-common_1.2.3_all", r.stdout)
        self.assertIn("puck-can-canable_1.2.3_all", r.stdout)
        self.assertNotIn("puck-can-peak_1.2.3_all", r.stdout)

    def test_uninstall_removes_the_can_packages(self):
        can = ("puck-can-common", "puck-can-peak", "puck-can-canable")
        r = self.dry_run("--uninstall", installed=(self.app,) + can)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for p in can:
            self.assertIn(p, r.stdout)

    def test_uninstall_keeps_the_can_packages_another_app_uses(self):
        sibling = "pucktunerapp" if self.app == "puckutilityapp" else "puckutilityapp"
        can = ("puck-can-common", "puck-can-peak", "puck-can-canable")
        r = self.dry_run("--uninstall", installed=(self.app, sibling) + can)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(self.app, r.stdout)
        for p in can:
            self.assertNotIn(p, r.stdout)


class MaintainerScripts(unittest.TestCase):
    """The app deb leaves the NetworkManager setting to puck-can-common."""

    def script(self, name):
        with open(SCRIPT) as f:
            return re.search(rf"<< '{name}'\n(.*?)^{name}$", f.read(), re.M | re.S).group(1)

    def test_postinst_restores_what_an_old_deb_removes_on_upgrade(self):
        text = self.script("POSTINST")
        self.assertIn("puck-can-common", text)
        self.assertIn("> /etc/NetworkManager/conf.d/99-puck-can.conf", text)

    def test_postrm_leaves_the_setting_alone(self):
        self.assertNotIn("99-puck-can", self.script("POSTRM"))


class CanDebs(unittest.TestCase):
    def test_builds_the_three_packages(self):
        if not shutil.which("dpkg-deb"):
            self.skipTest("no dpkg-deb")
        with tempfile.TemporaryDirectory() as out:
            r = subprocess.run(["bash", "scripts/build-can-debs.sh", "1.2.3", out], cwd=REPO,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            debs = sorted(f for f in os.listdir(out) if f.endswith(".deb"))
        self.assertEqual(debs, [f"puck-can-{p}_1.2.3_all.deb" for p in ("canable", "common", "peak")])


if __name__ == "__main__":
    unittest.main()
