"""`--python` pre-script for an agent-launched Blender in a dev checkout:
puts the repo on sys.path so the agent's launch expression finds the
`qcbridge` package without the extension being installed."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
