import sys
import os

# Adds the project root to Python's path so 'src' is always findable
# regardless of where pytest is run from (fixes Windows path issues)
sys.path.insert(0, os.path.dirname(__file__))