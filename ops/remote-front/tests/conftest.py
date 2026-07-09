"""Pytest conftest for the remote-front suite.

Deliberately thin: the reusable fixtures/helpers live in `_helpers.py` (imported
relatively by the test modules, matching the platform's `_gateway_helpers`
convention). This file exists so pytest treats the directory as the suite root.
"""
