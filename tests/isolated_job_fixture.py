import os
import subprocess
import sys
import time
from pathlib import Path

# How long the grandchild lives unless something kills it. A test that waits for it to disappear
# must give up well before this, or a survivor would simply run out and pass for dead.
CHILD_LIFETIME_S = 60


def run_forever_with_child():
    marker = Path(os.environ["OMNISEEK_TEST_MARKER"])
    child = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({CHILD_LIFETIME_S})"])
    marker.write_text(f"{os.getpid()} {child.pid}\n", encoding="ascii")
    while True:
        time.sleep(0.05)
