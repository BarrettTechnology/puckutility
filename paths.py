import os
import sys


def resource_path(relative_path):
    # Anchor sibling-folder lookups on the running .exe in PyInstaller
    # --onefile builds (where __file__ points at the _MEIPASS temp extract
    # dir, but the build script copies firmware/, config/, etc. next to
    # the .exe). Falls back to __file__ for normal `python` runs.
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_path)


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
