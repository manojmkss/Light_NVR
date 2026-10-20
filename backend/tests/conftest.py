import os
import sys

# Tests import the app package the same way the container does (cwd = backend/).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
