# Contributing to PuckUtility

Pull requests are welcome. This page covers setting up a development machine,
running the tests, and the release checklist. Everything that has to pass
before a release is run **on your machine, before you tag**: the GitLab
pipeline only builds and stores the release once the tag is pushed.

## Prerequisites

Any Ubuntu 20.04 → 26.04 machine (x86-64) with `sudo`.

**1. The source, with the `p4core` submodule**
```
git clone --recurse-submodules git@git.barrett.com:software/puckutility
cd puckutility
```
(In an existing clone: `git submodule update --init`.)

**2. The development environment**
```
./scripts/setup-venv.sh     # uv, standalone CPython 3.12, the wxPython runtime libs (apt, asks for sudo)
source scripts/activate
./scripts/setup-pip.sh      # requirements.txt (prebuilt wxPython wheel), p4core and puckutility, editable
```
`requirements.txt` is shared with p4gui and pucktuner and must stay identical in
all three repos. Edit `requirements.in` there too, then regenerate it with the
command in its header.

**3. Docker** — the release build and the cross-Ubuntu tests run in containers.
```
sudo apt install docker.io
sudo usermod -aG docker $USER
```
Log out and back in (or prefix the commands below with `sg docker -c '…'`).

**4. A kernel ≥ 5.6** — check with `uname -r`. The Ubuntu 26.04 test container
runs on your host's kernel, and its `tar` needs a system call (`openat2`) that
older kernels lack: the 26.04 install fails with `Cannot open: Function not
implemented`. Ubuntu 22.04 and later are fine; on 20.04 install the HWE kernel
and reboot:
```
sudo apt install --install-recommends linux-generic-hwe-20.04
```

**5. The puck firmware** — every build ships the firmware that
`system-config.ini` names, from `firmware/`. Fetch it from the stm32 tag
pipeline with a GitLab personal access token (scope `read_api`):
```
GITLAB_TOKEN=<token> scripts/fetch-firmware.sh
```
It takes the tag `v<fw_version>` from `system-config.ini` (override with
`FIRMWARE_TAG=v4.3.5`) and fails if a file `system-config.ini` names is missing.

## Unit tests

```
python -m unittest discover -s test -t . -v
```
Run them before every push. On a machine without a display, prefix with
`xvfb-run -a` (`sudo apt install xvfb`).

## Cross-Ubuntu tests

`scripts/test-cross-ubuntu.sh` checks both halves of "works on Ubuntu 20.04 →
26.04", each in a clean `ubuntu:XX.04` container:

- **source** (`scripts/xtest/source.sh`): runs this repo's `setup-venv.sh`,
  then `import wx`, `wx.App()` and every dependency under a virtual display —
  the developer setup on that release's own GTK.
- **deb** (`scripts/xtest/deb.sh`): builds the `.run` installer once (in the
  Ubuntu 20.04 build image), then in each container installs it unattended,
  checks the CAN adapter options (both, deselect Peak, re-add), launches the
  installed app and uninstalls — the artifact customers get.

```
scripts/test-cross-ubuntu.sh                 # 20.04 22.04 24.04 26.04
scripts/test-cross-ubuntu.sh 24.04 26.04     # a subset / a new release
scripts/test-cross-ubuntu.sh --source-only   # skip the installer half
```

Nothing is installed on your host and nothing opens on screen. The first run
pulls the Ubuntu images, builds the `burt/puck-deb-u20` build image and
downloads Python and the wheels, so it takes a while; later runs are faster.
It ends with a per-release summary, then `ALL PASSED` (exit 0) or `SOME FAILED`
(exit 1). On a failure, scroll up to that release's `SOURCE` / `DEB` banner for
the error.

Run it before every release, and whenever you change `requirements.txt`,
`scripts/setup-venv.sh`, the build or installer scripts, or a new Ubuntu ships.

## Releasing

1. Set `__version__` in `puckutility/__init__.py` and remove `DEV` from the
   window title (`SetTitle` in `puckutility/gui/app.py`).
2. Fetch the firmware the release ships (prerequisite 5), and update
   `system-config.ini` if the firmware changed.
3. Unit tests pass.
4. `scripts/test-cross-ubuntu.sh` (all four releases) prints `ALL PASSED`.
5. Commit, tag and push:
   ```
   git tag release-vX.Y.Z
   git push origin release-vX.Y.Z
   ```
6. The tag pipeline's `release` job checks that the tag matches `__version__`
   (a suffix such as `release-vX.Y.Z-linux` is allowed) and that the title has
   no `DEV`, fetches the firmware, builds the `.run`
   installer and the debs, and keeps them (Build > Pipelines > the tag's
   pipeline > `release` > Download artifacts).

The Windows installer is built on Windows (see the README, *Building a
release*).

## The build image

The tag pipeline and `scripts/build-linux-installer.sh --docker` build in
`burt/puck-deb-u20` (`scripts/Dockerfile.builder`, identical in pucktuner):
Ubuntu 20.04 for the oldest glibc/GTK, CPython 3.12, the prebuilt wxPython
wheel and `requirements.txt`. `--docker` builds it when it is missing. Rebuild it
after a Python, wxPython or `Dockerfile.builder` change — on your machine with
`--rebuild-docker`, and on the GitLab runner with:
```
docker build --no-cache -t burt/puck-deb-u20 -f scripts/Dockerfile.builder .
```
A `requirements.txt` change does not need a rebuild: each build syncs it first.
