#!/bin/bash
set -e

# Install Playwright browsers if not already installed
playwright install --with-deps

# Start your app
python app.py
