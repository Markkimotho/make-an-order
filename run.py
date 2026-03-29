"""Quick launcher that prints startup progress."""
import sys
print("[run.py] Importing app...", flush=True)
from app import app
print("[run.py] App imported, starting server...", flush=True)
app.run(debug=True, port=5001)
