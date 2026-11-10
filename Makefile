# make; make test; make package -- each target wraps a script in scripts/
# (CONTRIBUTING.md says more).
#
#   make               set up .venv (scripts/setup-venv.sh the first time,
#                      scripts/setup-pip.sh after requirements.txt or
#                      pyproject.toml changes)
#   make test          unit tests (on a private Xvfb when xvfb-run is installed)
#
#   make run           start the GUI from this checkout
#   make package       Linux .run installer (build/), built in the Ubuntu 20.04 image
#   make package-deb   the .deb only
#   make package-win   Windows zip and setup .exe (build/win/; under wine on Linux)
#   make xtest         cross-Ubuntu tests; XTEST_ARGS="--no-pip 24.04" for a subset
#   make clean         build output and caches; keeps .venv and the wine prefix
#                      (build/wine); make distclean removes .venv too
#
# PYTHON overrides the interpreter (default .venv/bin/python). FIRMWARE_DIR and
# CONTAINER pass through to the package and xtest scripts.

PYTHON ?= $(if $(shell test -x .venv/bin/python && echo y),.venv/bin/python,python3)
# The GUI tests run on a private Xvfb, never on your screen, when xvfb-run is
# installed (as p4gui's run-tests.sh does): X11 rather than Wayland, -noreset against
# the wx.App reopen race, and Mesa's EGL only, because NVIDIA's segfaults Xvfb
# where /dev/nvidia* is hidden.
MESA_EGL := $(wildcard /usr/share/glvnd/egl_vendor.d/50_mesa.json)
XVFB := $(if $(shell command -v xvfb-run 2>/dev/null),env $(if $(MESA_EGL),__EGL_VENDOR_LIBRARY_FILENAMES=$(MESA_EGL)) \
	xvfb-run -a -s "-screen 0 1280x1024x24 -noreset" env -u WAYLAND_DISPLAY GDK_BACKEND=x11)
XTEST_ARGS ?=
# Threaded BLAS only spins on the tests' small matrices.
export OPENBLAS_NUM_THREADS = 1
export OMP_NUM_THREADS = 1

.PHONY: all venv test run package package-deb package-win xtest clean distclean help

all: venv

venv: .venv/.installed

.venv/.installed: requirements.txt pyproject.toml
	@if [ -x .venv/bin/python ]; then scripts/setup-pip.sh; else scripts/setup-venv.sh; fi
	@touch $@

test:
	$(XVFB) $(PYTHON) -m unittest discover -s test -t .

run:
	$(PYTHON) -m puckutility gui

package:
	scripts/build-linux-installer.sh --docker --installer

package-deb:
	scripts/build-linux-installer.sh --docker --deb

package-win:
	scripts/build-win32-installer.sh

xtest:
	scripts/test-cross-ubuntu.sh $(XTEST_ARGS)

clean:
	@if [ -d build ]; then find build -mindepth 1 -maxdepth 1 ! -name wine -exec rm -rf {} +; fi
	rm -rf dist
	find . \( -path ./.venv -o -path ./lib -o -path ./lib64 -o -path ./build \) -prune -o -name __pycache__ -type d -prune -exec rm -rf {} +

distclean: clean
	rm -rf .venv

help:
	@sed -n '1,/^$$/p' Makefile
