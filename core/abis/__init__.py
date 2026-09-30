"""Minimal ABIs for all CreditGraph contracts. Loaded by core/contracts.py."""
import json
import os

_HERE = os.path.dirname(__file__)

def load(name: str):
    with open(os.path.join(_HERE, f"{name}.json"), "r") as f:
        return json.load(f)