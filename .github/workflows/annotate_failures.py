"""Print the tail of failed test logs as workflow annotations.

Annotations are visible to anonymous visitors on the run page, so the
failure details can be reviewed without signing in.
"""
import sys
from pathlib import Path

MAX_LINES = 25
MAX_CHARS = 500


def emit(text):
    for raw_line in text.splitlines()[-MAX_LINES:]:
        line = raw_line.replace("%", "%25").replace("\r", " ")
        while line:
            print(f"::error::{line[:MAX_CHARS]}")
            line = line[MAX_CHARS:]


for name in sys.argv[1:]:
    path = Path(name)
    if not path.exists() or path.stat().st_size == 0:
        continue
    content = path.read_text(encoding="utf-8", errors="replace")
    if "FAIL" in content or "ERROR" in content or "Traceback" in content:
        emit(f"--- {path.name} (tail) ---")
        emit(content[-3000:])
