#!/bin/sh

# Ensure that pip is installed (for Python >= 3.4)
python3 -m ensurepip --default-pip --upgrade

# Add all dependencies listed in the requirements.txt
pip install -r requirements.txt

# The shared core (a submodule) and puckutility itself, editable, so the
# `puckutility` / `puckutility-gui` commands run this checkout.
pip install -e ./p4core
pip install -e .
