"""Opt-in subprocess counter, loaded first on PYTHONPATH by the probe."""

import json
import os
import subprocess


_original_popen = subprocess.Popen


class RecordingPopen(_original_popen):
    def __init__(self, args, *positional, **keywords):
        super().__init__(args, *positional, **keywords)
        record = {
            "pid": os.getpid(), "ppid": os.getppid(), "child": self.pid,
            "args": [os.fsdecode(arg) for arg in args],
        }
        descriptor = os.open(
            os.environ["SQUAD_PROCESS_LOG"],
            os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600,
        )
        try:
            os.write(descriptor, (json.dumps(record) + "\n").encode())
        finally:
            os.close(descriptor)


if os.environ.get("SQUAD_PROCESS_LOG"):
    subprocess.Popen = RecordingPopen
