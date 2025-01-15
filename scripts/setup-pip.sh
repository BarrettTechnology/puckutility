#!/bin/sh
# Ensure that pip is installed (for Python >= 3.4)
python3 -m ensurepip --default-pip --upgrade

# Add all dependencies listed in the requirements.txt
pip install -r requirements.txt
