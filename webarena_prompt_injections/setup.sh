#!/bin/bash
# Copyright (c) Meta Platforms, Inc. and affiliates.
# unifies the setup scripts of the various agents and sets up the virtual environments in locations where our run scripts should expect them

# VisualWebarena
cd ../visualwebarena
echo "Installing required packages for visualwebarena..."
python3.10 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install
pip install -e .
deactivate

# set up prompt injector
cd ../webarena_prompt_injections
echo "Installing required packages for prompt injection tests..."
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install
deactivate

# (The Claude computer-use agent venv is intentionally omitted -- AgentPI uses the tool-calling
# GPTWebAgent via run_agentpi.py, not the Claude computer-use agent.)