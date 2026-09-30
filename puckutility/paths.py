"""paths.py — where puckutility's files are, however it is run.

Read-only files shipped with the app (images, the CANable firmware) are
package data: data() finds them in a checkout, a pip install or a frozen
build (see p4core.paths.resource).  The object dictionary is p4core's.

The working folders a technician fills — firmware/, config/, logs/ and the
system-config INI files that name them — live in APP_DIR:

  from a checkout   the repository root (as before the package existed)
  frozen build      beside the executable
  pip install       ~/.local/share/puckutility (the per-user data dir)
"""

import os
import sys

from p4core import paths as _core

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


def data(*parts):
    """A file from puckutility's own ``data`` directory."""
    return _core.resource(PACKAGE_DIR, 'data', *parts)


def image(name):
    return data('images', name)


def eds():
    """The P4 object dictionary (p4core's copy)."""
    return _core.puck4_eds()


def _app_dir():
    if _core.frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    checkout = os.path.dirname(PACKAGE_DIR)
    if os.path.isdir(os.path.join(checkout, '.git')) or \
            os.path.isfile(os.path.join(checkout, 'setup-pip.sh')):
        return checkout
    return _core.user_data_dir('puckutility')


APP_DIR = _app_dir()


def resource_path(relative_path):
    """A path in APP_DIR; ``images/...`` resolves to the package's images."""
    parts = relative_path.replace('\\', '/').split('/')
    if parts[0] == 'images':
        return data(*parts)
    return os.path.join(APP_DIR, relative_path)


# Set by _setup_logging() at startup to the per-session subdirectory inside
# logs/. All calibration outputs (plots, CSVs, JSON) are written here so
# every app run keeps its files together with the console log.
SESSION_LOG_DIR = None


def session_path(filename):
    """Return an absolute path for a calibration output file inside the
    current session log directory. Falls back to logs/ if the session dir
    was never initialised (e.g. CLI mode, tests)."""
    if SESSION_LOG_DIR is not None:
        return os.path.join(SESSION_LOG_DIR, filename)
    return resource_path(os.path.join('logs', filename))


# Conventional locations for system-config payloads.
MAIN_DIR     = resource_path('')
FIRMWARE_DIR = resource_path('firmware')
CONFIG_DIR   = resource_path('config')


def _resolve_path(value, folder):
    """Resolve a path value read from a system-config INI.

    Strips optional surrounding double-quotes, then:
      - bare filenames (no path separator) are resolved under `folder`
      - absolute paths and any value containing a path separator are
        returned as-is, so older .ini files with full paths still work.
    """
    if not value:
        return value
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    if os.path.isabs(value) or '/' in value or '\\' in value:
        return value
    return os.path.join(folder, value)
