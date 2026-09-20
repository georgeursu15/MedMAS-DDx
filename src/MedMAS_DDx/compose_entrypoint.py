

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    args = sys.argv[1:]
    mode = args.pop(0) if args and args[0] in {"test", "normal"} else "normal"

    if mode == "test":
        command = [sys.executable, "-m", "MedMAS_DDx.evaluation.batch_runner", *args]
    else:
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "MedMAS_DDx.server:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            *args,
        ]

    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())