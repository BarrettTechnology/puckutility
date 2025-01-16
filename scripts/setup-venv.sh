#!/bin/sh
sudo apt update
sudo apt install -y python3-dev python3-venv python3-wheel libgtk-3-dev

# Set up the virtual environment required for development & building
python3 -m venv .

# Must run 'source ../bin/activate' now!
