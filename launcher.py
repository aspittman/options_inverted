import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RESTART_DELAY_SECONDS = 30
PROJECT_DIR = Path(__file__).resolve().parent
VENV_PYTHON = PROJECT_DIR / "venv" / "bin" / "python"
BOT_PYTHON = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

while True:
    print("\nStarting options bot...")

    result = subprocess.run([BOT_PYTHON, str(PROJECT_DIR / "main.py")], cwd=PROJECT_DIR)

    print(f"Options bot exited with return code: {result.returncode}")

    if result.returncode == 0:
        print("Options bot exited normally. Not restarting.")
        break

    with open(PROJECT_DIR / "crash.log", "a") as file:
        file.write(
            f"{datetime.now()} - Options bot crashed with return code {result.returncode}\n"
        )

    print(f"Options bot crashed. Restarting in {RESTART_DELAY_SECONDS} seconds...")
    time.sleep(RESTART_DELAY_SECONDS)
