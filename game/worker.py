import json
import os
from pathlib import Path
import subprocess
import sys


if len(sys.argv) != 2:
    raise SystemExit("Pass the worker directory.")

run_dir = Path(sys.argv[1])
no_window = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# Wait until the supervisor assigns this process to its Job.
if sys.stdin.readline().strip() != "GO":
    raise SystemExit("Supervisor did not authorize startup.")

launch = json.loads((run_dir / "launch.json").read_text(encoding = "utf-8"))
with (run_dir / "godot.stderr.log").open("w" , encoding = "utf-8") as errors:
    process = subprocess.Popen(
        launch["command"] , cwd = run_dir , stdin = subprocess.PIPE , stdout = sys.stdout ,
        stderr = errors , text = True , encoding = "utf-8" , bufsize = 1 , creationflags = no_window
    )
    worker = {"pid" : process.pid , "supervisor_child_pid" : os.getpid()}
    worker_path = run_dir / "worker.json"
    worker_path.write_text(json.dumps(worker , indent = 2) + "\n" , encoding = "utf-8")

    try:
        for line in sys.stdin:
            process.stdin.write(line)
            process.stdin.flush()
        process.stdin.close()
        process.wait(timeout = 5)
    finally:
        if not process.stdin.closed:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
        if process.poll() is None:
            process.kill()
            process.wait(timeout = 5)
        worker["returncode"] = process.returncode
        worker_path.write_text(json.dumps(worker , indent = 2) + "\n" , encoding = "utf-8")

raise SystemExit(process.returncode)
