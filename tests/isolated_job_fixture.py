import os
import subprocess
import sys
import time
from pathlib import Path


def run_forever_with_child():
    marker = Path(os.environ["OMNISEEK_TEST_MARKER"])
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    marker.write_text(f"{os.getpid()} {child.pid}\n", encoding="ascii")
    while True:
        time.sleep(0.05)
