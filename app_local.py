"""Run the same dashboard locally; no second copy of business logic."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name('app.py')), run_name='__main__')
