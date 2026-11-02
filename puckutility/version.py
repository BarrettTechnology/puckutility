"""The Puck Utility version, shown in the title bar and by --help / --version.

The one place it is set: puckutility/__init__.py imports it, pyproject.toml
reads it, and so do
the CI tag check and the installer build scripts.
"""

#: PEP 440 form (no leading 'v'); the title bar and the CLI show it as
#: 'v' + __version__.
__version__ = '1.4.0'
