#!/usr/bin/env python3
"""Puck Utility launcher: the GUI with no arguments, else the command line.

Kept at the top of the checkout for the PyInstaller builds, the desktop
entry and old habits (``./puckutilityapp.py --can can0 --id 1 --flash FW``).
Installed with pip, the same thing is ``puckutility``.
"""

import multiprocessing
import sys

from puckutility.main import main

if __name__ == '__main__':
    # The GUI runs flashes in a child process; a frozen Windows build needs
    # this before anything else.
    multiprocessing.freeze_support()
    sys.exit(main())
