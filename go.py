import os
import subprocess
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
MAIN = os.path.join(ROOT, "src", "main.py")

if not os.path.isfile(PY):
    print("Missing venv. Run: python -m venv .venv")
    print("Then: .venv\\Scripts\\pip install -r requirements.txt")
    input("Press Enter...")
    sys.exit(1)

try:
    r = subprocess.run([PY, MAIN], cwd=ROOT)
    code = r.returncode
except Exception:
    traceback.print_exc()
    code = 1

if code != 0:
    print()
    print("Exit code:", code)
    input("Press Enter to close...")
sys.exit(code)
