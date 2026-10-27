import ctypes
from ctypes import wintypes
from datetime import datetime , timedelta , timezone
import json
import os
from pathlib import Path
import queue
import random
import subprocess
import sys
import tempfile
import threading
import time


project_dir = Path(__file__).resolve().parent
temporary_dir = Path(tempfile.gettempdir()) / "sts2-rl"
no_window = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def save_json(path , value):
    text = json.dumps(value , ensure_ascii = False , indent = 2) + "\n"
    path.write_text(text , encoding = "utf-8")


def create_job():
    # Closing the Job stops every child process.
    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time" , ctypes.c_longlong) , ("job_time" , ctypes.c_longlong) ,
                    ("flags" , wintypes.DWORD) , ("min_working_set" , ctypes.c_size_t) ,
                    ("max_working_set" , ctypes.c_size_t) , ("active_limit" , wintypes.DWORD) ,
                    ("affinity" , ctypes.c_size_t) , ("priority" , wintypes.DWORD) , ("scheduling" , wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_ops" , ctypes.c_ulonglong) ,
            ("write_ops" , ctypes.c_ulonglong) ,
            ("other_ops" , ctypes.c_ulonglong) ,
            ("read_bytes" , ctypes.c_ulonglong) ,
            ("write_bytes" , ctypes.c_ulonglong) ,
            ("other_bytes" , ctypes.c_ulonglong)
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic" , BasicLimits) , ("io" , IoCounters) , ("process_memory" , ctypes.c_size_t) ,
                    ("job_memory" , ctypes.c_size_t) , ("peak_process_memory" , ctypes.c_size_t) ,
                    ("peak_job_memory" , ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32" , use_last_error = True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p , wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE , ctypes.c_int , ctypes.c_void_p , wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE , wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None , None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job , 9 , ctypes.byref(limits) , ctypes.sizeof(limits)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(job)
        raise error
    return kernel , job


class CombatEnv:
    def __init__(self , max_actions = 1):
        if not isinstance(max_actions , int) or isinstance(max_actions , bool) or not 1 <= max_actions <= 100:
            raise ValueError("max_actions must be an integer between 1 and 100.")
        self.max_actions = max_actions
        self.request_timeout = 15
        self.total_timeout = 30
        self.actions = 0
        self.truncated = False
        self.state = None
        self.run_dir = None
        self.process = None
        self.job = None
        self.timer = None
        self.reader = None
        self.started = None
        self.closed = False
        self.timed_out = False
        self.job_handle_closed = False
        self.failure = None
        self.cleanup_error = None
        self.files = []
        self.lines = queue.Queue()
        self.sequence = 0
        self.lock = threading.RLock()

    def __enter__(self):
        if os.name != "nt":
            raise RuntimeError("The current native environment requires Windows.")
        if self.closed or self.process is not None:
            raise RuntimeError("Create a new CombatEnv for a new worker.")
        # Verify the installed files before each worker starts.
        checked = subprocess.run([sys.executable , str(project_dir / "scripts/check_environment.py")] ,
                                 cwd = project_dir , capture_output = True , text = True , encoding = "utf-8" ,
                                 timeout = 60 , creationflags = no_window)
        if checked.returncode != 0:
            raise RuntimeError("Preflight failed; no worker started.\n" + checked.stdout + checked.stderr)
        checked_output = json.loads(checked.stdout)
        preflight = json.loads(Path(checked_output["report"]).read_text(encoding = "utf-8"))
        baseline = json.loads((project_dir / "configs/native_baseline.json").read_text(encoding = "utf-8"))
        self.scenario = json.loads((project_dir / "configs/combat_probe.json").read_text(encoding = "utf-8"))
        source_dir = project_dir / ".setup/divine-sts2"
        revision = subprocess.run(["git" , "rev-parse" , "HEAD"] , cwd = source_dir , capture_output = True ,
                                  text = True , check = True , timeout = 5 , creationflags = no_window).stdout.strip()
        expected = {"version" : baseline["product_version"] ,
                    "assembly_sha256" : baseline["files"]["data_sts2_windows_x86_64/sts2.dll"] ,
                    "pck_sha256" : baseline["files"]["SlayTheSpire2.pck"]}
        if revision != baseline["source_commit"] or self.scenario["game_build"] != expected or preflight["blockers"]:
            raise RuntimeError("Source, scenario or preflight differs from the reviewed baseline.")
        run_name = datetime.now(timezone(timedelta(hours = 8))).strftime("%Y%m%d_%H%M%S_%f")
        self.run_dir = temporary_dir / "environment" / run_name
        self.run_dir.mkdir(parents = True)
        environment = os.environ.copy()
        isolated = {"APPDATA" : self.run_dir / "profile/AppData/Roaming" ,
                    "LOCALAPPDATA" : self.run_dir / "profile/AppData/Local" ,
                    "USERPROFILE" : self.run_dir / "profile" , "TEMP" : self.run_dir / "tmp" ,
                    "TMP" : self.run_dir / "tmp" , "DOTNET_CLI_HOME" : self.run_dir / "dotnet-home"}
        isolated_environment = {}
        for name , path in isolated.items():
            path.mkdir(parents = True , exist_ok = True)
            environment[name] = str(path)
            isolated_environment[name] = str(path)
        environment["HOME"] = environment["USERPROFILE"]
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
        for name in ("STS2_NATIVE_TRACE" , "STS2_NATIVE_TRACE_SOURCE"):
            environment.pop(name , None)
        dotnet_root = project_dir / ".tools/dotnet9"
        if (dotnet_root / "dotnet.exe").is_file():
            environment["DOTNET_ROOT"] = str(dotnet_root)
            environment["DOTNET_ROOT_X64"] = str(dotnet_root)
            environment["PATH"] = str(dotnet_root) + os.pathsep + environment.get("PATH" , "")
        command = [preflight["godot_candidates"][0] , "--headless" , "--path" ,
                   str(source_dir / "src/Sts2.NativeSim.GodotHost") , "--log-file" ,
                   str(self.run_dir / "godot.engine.log") , "--" , "--server" ,
                   str(Path(preflight["game_directory"]) / "data_sts2_windows_x86_64/sts2.dll")]
        save_json(self.run_dir / "launch.json" , {"command" : command , "preflight_report" : checked_output["report"] ,
                  "source_commit" : revision , "isolated_environment" : isolated_environment ,
                  "request_timeout_seconds" : self.request_timeout , "total_timeout_seconds" : self.total_timeout ,
                  "max_actions" : self.max_actions , "scenario" : self.scenario})
        try:
            self.raw_output = (self.run_dir / "godot.stdout.log").open("w" , encoding = "utf-8")
            self.files.append(self.raw_output)
            self.transcript = (self.run_dir / "protocol.jsonl").open("w" , encoding = "utf-8")
            self.files.append(self.transcript)
            errors = (self.run_dir / "supervisor-child.stderr.log").open("w" , encoding = "utf-8")
            self.files.append(errors)
            self.kernel , self.job = create_job()
            self.started = time.monotonic()
            worker_path = Path(__file__).with_name("worker.py")
            worker_command = [sys.executable , "-u" , str(worker_path) , str(self.run_dir)]
            self.process = subprocess.Popen(worker_command , cwd = self.run_dir ,
                                            stdin = subprocess.PIPE , stdout = subprocess.PIPE , stderr = errors ,
                                            text = True , encoding = "utf-8" , bufsize = 1 , env = environment ,
                                            creationflags = no_window)
            if not self.kernel.AssignProcessToJobObject(self.job , wintypes.HANDLE(self.process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.reader = threading.Thread(target = self.read_stdout , daemon = True)
            self.reader.start()
            remaining = max(0 , self.total_timeout - (time.monotonic() - self.started))
            self.timer = threading.Timer(remaining , self.expire)
            self.timer.daemon = True
            self.timer.start()
            # Start the game only after Job assignment succeeds.
            self.process.stdin.write("GO\n")
            self.process.stdin.flush()
            hello = self.request("hello")
            if hello["game_build"] != self.scenario["game_build"]:
                raise RuntimeError("Runtime game_build differs from the reviewed fingerprint.")
            catalog = self.request("catalog")
            encounters = []
            for entry in catalog["encounters"]:
                encounters.append(entry["model_id"])
            if self.scenario["encounter"] not in encounters:
                raise RuntimeError("Configured encounter is absent from native catalog.")
            return self
        except BaseException as error:
            self.close(error)
            raise

    def read_stdout(self):
        try:
            for line in self.process.stdout:
                self.raw_output.write(line)
                self.raw_output.flush()
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def close_job(self):
        with self.lock:
            if self.job is not None:
                if not self.kernel.CloseHandle(self.job):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.job = None
                self.job_handle_closed = True

    def expire(self):
        with self.lock:
            if not self.closed:
                self.timed_out = True
                try:
                    self.close_job()
                except OSError as error:
                    self.cleanup_error = str(error)
                self.lines.put(None)

    def request(self , method , params = None):
        if self.closed or self.process is None:
            raise RuntimeError("Environment is not open.")
        try:
            remaining = self.total_timeout - (time.monotonic() - self.started)
            if self.timed_out or remaining <= 0:
                raise TimeoutError("Native environment exceeded its total runtime limit.")
            deadline = time.monotonic() + min(self.request_timeout , remaining)
            self.sequence += 1
            message = {"id" : str(self.sequence) , "method" : method , "params" : params or {}}
            self.transcript.write(json.dumps({"request" : message} , ensure_ascii = False) + "\n")
            self.transcript.flush()
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{method}: request deadline exceeded.")
                try:
                    line = self.lines.get(timeout = remaining)
                except queue.Empty:
                    raise TimeoutError(f"{method}: request deadline exceeded.") from None
                if self.timed_out:
                    raise TimeoutError("Native environment exceeded its total runtime limit.")
                if line is None:
                    raise RuntimeError(f"Worker exited before {method} responded.")
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(response , dict) or "id" not in response:
                    continue
                self.transcript.write(json.dumps({"response" : response} , ensure_ascii = False) + "\n")
                self.transcript.flush()
                if response["id"] != message["id"]:
                    raise RuntimeError("Protocol response ID mismatch.")
                if not response.get("ok"):
                    raise RuntimeError(json.dumps(response.get("error") , ensure_ascii = False))
                return response["result"]
        except BaseException as error:
            if time.monotonic() - self.started >= self.total_timeout:
                self.expire()
            self.failure = str(error)
            raise

    def reset(self , seed = None):
        if self.actions >= self.max_actions:
            raise RuntimeError("Action budget exhausted; create a new environment.")
        scenario = self.scenario.copy()
        if seed is not None:
            scenario["seed"] = str(seed)
            cards = []
            for card in scenario["deck"]:
                cards.append(card["instance_id"])
            scenario["initial_hand"] = random.Random(scenario["seed"]).sample(cards , 5)
        self.state = self.request("reset" , scenario)
        self.truncated = False
        return self.state

    def step(self , action_id):
        if self.state is None:
            raise RuntimeError("Call reset before step.")
        if self.state["terminated"] or self.truncated or self.actions >= self.max_actions:
            raise RuntimeError("This combat ended or reached its action limit.")
        legal_actions = []
        for action in self.state["legal_actions"]:
            legal_actions.append(action["action_id"])
        if action_id not in legal_actions:
            raise ValueError("The action is not legal in the current state.")
        self.state = self.request("step" , {"action_id" : action_id})
        self.actions += 1
        self.truncated = self.actions >= self.max_actions and not self.state["terminated"]
        return self.state

    def close(self , error = None):
        if self.closed:
            return
        closing_error = None
        try:
            if self.process is not None and not error and not self.failure and not self.timed_out:
                if self.process.poll() is None:
                    self.request("close")
                    self.process.stdin.close()
                    remaining = self.total_timeout - (time.monotonic() - self.started)
                    self.process.wait(timeout = max(0.01 , min(5 , remaining)))
                if self.process.returncode != 0:
                    raise RuntimeError(f"Worker relay exited with code {self.process.returncode}.")
        except BaseException as close_error:
            closing_error = close_error
        finally:
            with self.lock:
                self.closed = True
                if self.timer is not None:
                    self.timer.cancel()
                try:
                    self.close_job()
                except OSError as close_error:
                    self.cleanup_error = str(close_error)
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.kill()  # Job assignment can fail while the child waits for GO.
                self.process.wait(timeout = 5)
                if self.reader is not None:
                    self.reader.join(timeout = 2)
                for pipe in (self.process.stdin , self.process.stdout):
                    if pipe is not None:
                        pipe.close()
            for file in self.files:
                file.close()
            if self.run_dir is not None:
                failure = self.failure
                if error:
                    failure = str(error)
                elif not failure and closing_error:
                    failure = str(closing_error)
                status = "failed" if failure or self.cleanup_error else "passed"
                if self.timed_out:
                    status = "total_timeout"
                save_json(self.run_dir / "supervision.json" , {"status" : status , "error" : failure ,
                          "cleanup_error" : self.cleanup_error , "job_handle_closed" : self.job_handle_closed ,
                          "cleanup" : "kill_on_job_close" if self.job_handle_closed else "no_job_closed" ,
                          "actions" : self.actions , "truncated" : self.truncated ,
                          "child_returncode" : self.process.returncode if self.process is not None else None ,
                          "elapsed_seconds" : round(time.monotonic() - self.started , 3) if self.started else 0})
        if error is None:
            if self.cleanup_error:
                raise RuntimeError(self.cleanup_error)
            if self.timed_out:
                raise TimeoutError("Native environment exceeded its total runtime limit.")
            if closing_error:
                raise closing_error
            if self.failure:
                raise RuntimeError(self.failure)

    def __exit__(self , error_type , error , traceback):
        self.close(error)
        return False
