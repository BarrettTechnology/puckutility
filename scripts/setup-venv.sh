#!/bin/sh
sudo apt update
sudo apt install -y python3-dev python3-venv libgtk-3-dev

# Set up the virtual environment required for development & building
python3 -m venv ..

# Ensure that pip is installed (for Python >= 3.4)
python3 -m ensurepip --default-pip --upgrade

# Add all dependencies listed in the requirements.txt
pip install -r requirements.txt

