import ctypes
from ctypes import wintypes
from datetime import datetime , timedelta , timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

from game.environment import create_job , save_json


project_dir = Path(__file__).resolve().parent
no_window = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class VisibleCombatEnv:
    def __init__(self , max_actions = 100):
        if not isinstance(max_actions , int) or isinstance(max_actions , bool) or not 1 <= max_actions <= 100:
            raise ValueError("max_actions must be an integer between 1 and 100.")
        self.max_actions = max_actions
        self.startup_timeout = 45
        self.request_timeout = 60
        self.total_timeout = 300
        self.actions = 0
        self.truncated = False
        self.state = None
        self.reset_called = False
        self.run_dir = None
        self.sandbox = None
        self.process = None
        self.kernel = None
        self.job = None
        self.game_handle = None
        self.connection = None
        self.reader = None
        self.writer = None
        self.timer = None
        self.started = None
        self.closed = False
        self.timed_out = False
        self.job_handle_closed = False
        self.close_sent = False
        self.transport_failed = False
        self.request_id = 0
        self.failure = None
        self.cleanup_errors = []
        self.files = []
        self.lock = threading.RLock()

    def __enter__(self):
        if os.name != "nt":
            raise RuntimeError("The visible game environment requires Windows.")
        if self.closed or self.process is not None:
            raise RuntimeError("Create a new VisibleCombatEnv for a new game window.")
        try:
            package_dir = project_dir / ".setup/visible-bridge-build/bin/Release/net9.0/package"
            for name in ("sts2-visible-bridge.dll" , "sts2-visible-bridge.json"):
                if not (package_dir / name).is_file():
                    raise FileNotFoundError(f"Build the visible bridge first: {package_dir / name}")
            checked = subprocess.run(
                [sys.executable , str(project_dir / "scripts/check_environment.py")] ,
                cwd = project_dir , capture_output = True , text = True , encoding = "utf-8" ,
                timeout = 60 , creationflags = no_window
            )
            if checked.returncode != 0:
                raise RuntimeError("Preflight failed; no game started.\n" + checked.stdout + checked.stderr)
            checked_output = json.loads(checked.stdout)
            preflight = json.loads(Path(checked_output["report"]).read_text(encoding = "utf-8"))
            if preflight["blockers"]:
                raise RuntimeError("Preflight has unresolved blockers.")

            run_name = datetime.now(timezone(timedelta(hours = 8))).strftime("%Y%m%d_%H%M%S_%f")
            self.run_dir = Path(tempfile.gettempdir()) / "sts2-rl/visible_combat" / run_name
            self.sandbox = self.run_dir / "sandbox"
            self.sandbox.mkdir(parents = True)
            game_root = Path(preflight["game_directory"]).resolve()
            for source in game_root.iterdir():
                if source.is_file() and source.suffix.lower() in (".exe" , ".pck" , ".dll" , ".json"):
                    try:
                        os.link(source , self.sandbox / source.name)
                    except OSError:
                        if shutil.disk_usage(self.run_dir).free < source.stat().st_size + 512 * 1024 * 1024:
                            raise RuntimeError("Insufficient free space for isolated game assets.")
                        shutil.copy2(source , self.sandbox / source.name)

            powershell = shutil.which("pwsh") or shutil.which("powershell")
            if powershell is None:
                raise RuntimeError("PowerShell is needed to link the installed game assets.")
            for name in ("data_sts2_windows_x86_64" , "controller_config"):
                source = game_root / name
                destination = self.sandbox / name
                if source.is_dir():
                    quoted_source = str(source).replace("'" , "''")
                    quoted_destination = str(destination).replace("'" , "''")
                    script = "$ErrorActionPreference = 'Stop'\n"
                    script += f"New-Item -ItemType Junction -Path '{quoted_destination}' -Target '{quoted_source}' | Out-Null"
                    try:
                        subprocess.run(
                            [powershell , "-NoProfile" , "-NonInteractive" , "-Command" , script] ,
                            check = True , capture_output = True , text = True , timeout = 10 , creationflags = no_window
                        )
                    except subprocess.CalledProcessError:
                        if destination.exists() or destination.is_symlink() or destination.is_junction():
                            raise RuntimeError(f"Unexpected partial asset link: {destination}")
                        shutil.copytree(source , destination)

            mods_dir = self.sandbox / "mods/sts2-visible-bridge"
            mods_dir.mkdir(parents = True)
            for name in ("sts2-visible-bridge.dll" , "sts2-visible-bridge.json"):
                shutil.copy2(package_dir / name , mods_dir / name)

            environment = os.environ.copy()
            isolated = {
                "APPDATA" : self.run_dir / "profile/AppData/Roaming" ,
                "LOCALAPPDATA" : self.run_dir / "profile/AppData/Local" ,
                "USERPROFILE" : self.run_dir / "profile" ,
                "TEMP" : self.run_dir / "tmp" , "TMP" : self.run_dir / "tmp" ,
                "DOTNET_CLI_HOME" : self.run_dir / "dotnet-home"
            }
            isolated_paths = {}
            for name , path in isolated.items():
                path.mkdir(parents = True , exist_ok = True)
                environment[name] = str(path)
                isolated_paths[name] = str(path)
            environment["HOME"] = environment["USERPROFILE"]
            environment["PYTHONIOENCODING"] = "utf-8"
            for name in ("STS2_NATIVE_TRACE" , "STS2_NATIVE_TRACE_SOURCE" , "STS2_FORCE_CHARACTER"):
                environment.pop(name , None)
            port_file = self.run_dir / "bridge_port.txt"
            environment["STS2_FULL_APP_BRIDGE_PORT"] = "0"
            environment["STS2_FULL_APP_BRIDGE_PORT_FILE"] = str(port_file)

            # Copy display preferences, never normal saves or progress.
            source_settings = Path(os.environ["APPDATA"]) / "SlayTheSpire2/default/1/settings.save"
            original_settings = {}
            if source_settings.is_file():
                original_settings = json.loads(source_settings.read_text(encoding = "utf-8"))
            setting_keys = (
                "schema_version" , "language" , "aspect_ratio" , "controller_mapping" , "controller_mapping_type" ,
                "keyboard_mapping" , "keyboard_only_mapping" , "fps_limit" , "limit_fps_in_background" , "msaa" ,
                "resize_windows" , "target_display" , "volume_ambience" , "volume_bgm" ,
                "volume_master" , "volume_sfx" , "vsync"
            )
            settings = {}
            for key in setting_keys:
                if key in original_settings:
                    settings[key] = original_settings[key]
            settings.update({
                "mod_settings" : {"mods_enabled" : True , "mod_list" : []} ,
                "fullscreen" : False , "skip_intro_logo" : True , "seen_ea_disclaimer" : True ,
                "window_size" : {"X" : 1280 , "Y" : 720} ,
                "window_position" : {"X" : -1 , "Y" : -1}
            })
            settings_path = isolated["APPDATA"] / "SlayTheSpire2/default/1/settings.save"
            settings_path.parent.mkdir(parents = True)
            save_json(settings_path , settings)

            command = [str(self.sandbox / "SlayTheSpire2.exe") , "--force-steam=off" , "--windowed" ,
                       "--resolution" , "1280x720" , f"--log-file={self.run_dir / 'game.engine.log'}"]
            launch = {
                "command" : command , "preflight_report" : checked_output["report"] ,
                "isolated_environment" : isolated_paths , "visible" : True ,
                "total_timeout_seconds" : self.total_timeout , "request_timeout_seconds" : self.request_timeout ,
                "max_actions" : self.max_actions , "scope" : "One visible native combat." ,
                "bridge_dll_sha256" : hashlib.sha256((mods_dir / "sts2-visible-bridge.dll").read_bytes()).hexdigest().upper()
            }
            save_json(self.run_dir / "launch.json" , launch)
            save_json(self.sandbox / "launch.json" , launch)
            output = (self.run_dir / "game.stdout.log").open("w" , encoding = "utf-8")
            self.files.append(output)
            errors = (self.run_dir / "relay.stderr.log").open("w" , encoding = "utf-8")
            self.files.append(errors)
            self.protocol = (self.run_dir / "protocol.jsonl").open("w" , encoding = "utf-8")
            self.files.append(self.protocol)
            self.kernel , self.job = create_job()
            self.kernel.OpenProcess.argtypes = [wintypes.DWORD , wintypes.BOOL , wintypes.DWORD]
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE , wintypes.DWORD]
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
            self.started = time.monotonic()
            self.process = subprocess.Popen(
                [sys.executable , "-u" , str(project_dir / "worker.py") , str(self.sandbox)] ,
                cwd = self.sandbox , env = environment , stdin = subprocess.PIPE , stdout = output , stderr = errors ,
                text = True , encoding = "utf-8" , creationflags = no_window
            )
            if not self.kernel.AssignProcessToJobObject(self.job , wintypes.HANDLE(self.process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.timer = threading.Timer(self.total_timeout , self.expire)
            self.timer.daemon = True
            self.timer.start()
            self.process.stdin.write("GO\n")
            self.process.stdin.flush()

            port = None
            while time.monotonic() - self.started < self.startup_timeout:
                if not self.check_worker():
                    raise RuntimeError("The game window closed before bridge initialization.")
                if port_file.is_file():
                    value = port_file.read_text(encoding = "utf-8").strip()
                    if value.isdecimal() and 1 <= int(value) <= 65535:
                        port = int(value)
                        break
                time.sleep(0.1)
            if port is None:
                raise TimeoutError("The visible bridge did not publish a port within 45 seconds.")
            self.connection = socket.create_connection(("127.0.0.1" , port) , timeout = 5)
            self.reader = self.connection.makefile("r" , encoding = "utf-8-sig")
            self.writer = self.connection.makefile("w" , encoding = "utf-8")
            hello = self.request("hello")
            if (hello.get("bridge_id") != "sts2-visible-bridge" or hello.get("visible") is not True
                    or hello.get("presentation_suppressed") is not False or hello.get("single_combat") is not True):
                raise RuntimeError("Rebuild VisibleBridge with visible single-combat support.")
            if not hello.get("display_server") or hello["display_server"].lower() == "headless":
                raise RuntimeError("The game unexpectedly started without a visible display.")
            loaded_assembly = Path(hello.get("game_assembly_path" , "")).resolve()
            expected_assembly = (self.sandbox / "data_sts2_windows_x86_64/sts2.dll").resolve()
            if loaded_assembly != expected_assembly:
                raise RuntimeError("The bridge loaded a different game assembly.")
            expected_hash = preflight["baseline"]["files"]["data_sts2_windows_x86_64/sts2.dll"]
            if hashlib.sha256(loaded_assembly.read_bytes()).hexdigest().upper() != expected_hash:
                raise RuntimeError("The loaded game assembly differs from the verified fingerprint.")
            worker = json.loads((self.sandbox / "worker.json").read_text(encoding = "utf-8"))
            if hello.get("pid") != worker["pid"] or hello.get("bound_port") != port:
                raise RuntimeError("Bridge identity differs from the isolated game process.")
            return self
        except BaseException as error:
            self.close(error)
            raise

    def check_worker(self):
        if self.timed_out or time.monotonic() - self.started >= self.total_timeout:
            self.expire()
            raise TimeoutError("Visible combat exceeded its total runtime limit.")
        if self.process.poll() is not None:
            raise RuntimeError(f"Game relay exited with code {self.process.returncode}.")
        worker_path = self.sandbox / "worker.json"
        if self.game_handle is None and worker_path.is_file():
            try:
                worker = json.loads(worker_path.read_text(encoding = "utf-8"))
            except (OSError , ValueError):
                return True
            self.game_handle = self.kernel.OpenProcess(0x00100000 , False , worker["pid"])
            if not self.game_handle:
                raise RuntimeError("The game process exited before its window became ready.")
        if self.game_handle is not None:
            result = self.kernel.WaitForSingleObject(self.game_handle , 0)
            if result == 0:
                return False
            if result != 0x102:
                raise ctypes.WinError(ctypes.get_last_error())
        return True

    def request(self , method , params = None , timeout = None):
        if self.closed or self.connection is None:
            raise RuntimeError("The visible environment is not open.")
        self.request_id += 1
        message = {"id" : self.request_id , "method" : method , "params" : params or {}}
        self.protocol.write(json.dumps({"request" : message} , ensure_ascii = False) + "\n")
        self.protocol.flush()
        try:
            remaining = self.total_timeout - (time.monotonic() - self.started)
            if self.timed_out or remaining <= 0:
                raise TimeoutError("Visible combat exceeded its total runtime limit.")
            self.connection.settimeout(min(timeout or self.request_timeout , remaining))
            self.writer.write(json.dumps(message , ensure_ascii = False) + "\n")
            self.writer.flush()
            line = self.reader.readline()
            if not line:
                raise EOFError("The visible bridge closed its socket.")
            response = json.loads(line)
            self.protocol.write(json.dumps({"response" : response} , ensure_ascii = False) + "\n")
            self.protocol.flush()
            save_json(self.run_dir / "last_response.json" , response)
            if response.get("id") != self.request_id:
                raise RuntimeError("Bridge response ID mismatch.")
            if response.get("error"):
                raise RuntimeError(str(response["error"]))
            return response.get("result")
        except BaseException as error:
            self.transport_failed = True
            self.failure = str(error)
            raise

    def reset(self , seed = "A1B2C3D4E5"):
        if self.reset_called:
            raise RuntimeError("Create a new visible environment for another combat.")
        self.reset_called = True
        self.state = self.request("reset_combat" , {"seed" : seed})
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
        self.state = self.request("step_combat" , {"action_id" : action_id})
        self.actions += 1
        self.truncated = self.actions >= self.max_actions and not self.state["terminated"]
        return self.state

    def close_job(self):
        with self.lock:
            if self.job is not None:
                if not self.kernel.CloseHandle(self.job):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.job = None
                self.job_handle_closed = True

    def expire(self):
        with self.lock:
            if self.closed:
                return
            self.timed_out = True
            try:
                self.close_job()
            except OSError as error:
                self.cleanup_errors.append(str(error))
            if self.connection is not None:
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def close(self , error = None):
        if self.closed:
            return
        if self.connection is not None and not self.transport_failed and not self.timed_out:
            try:
                self.request("close" , timeout = 3)
                self.close_sent = True
            except Exception as close_error:
                self.cleanup_errors.append(f"Graceful close: {close_error}")
        if self.process is not None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout = 5)
            except Exception:
                pass
        with self.lock:
            self.closed = True
            if self.timer is not None:
                self.timer.cancel()
            try:
                self.close_job()
            except OSError as close_error:
                self.cleanup_errors.append(f"Job close: {close_error}")
        if self.process is not None:
            try:
                if self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout = 5)
            except Exception as close_error:
                self.cleanup_errors.append(f"Relay exit: {close_error}")
        for stream in (self.writer , self.reader , self.connection):
            if stream is not None:
                try:
                    stream.close()
                except Exception as close_error:
                    self.cleanup_errors.append(f"Socket close: {close_error}")
        if self.game_handle is not None:
            if not self.kernel.CloseHandle(self.game_handle):
                self.cleanup_errors.append(str(ctypes.WinError(ctypes.get_last_error())))
            self.game_handle = None
        for file in self.files:
            try:
                file.close()
            except Exception as close_error:
                self.cleanup_errors.append(f"Log close: {close_error}")
        failure = str(error) if error is not None else self.failure
        if self.process is not None and self.process.returncode not in (None , 0) and failure is None and not self.timed_out:
            failure = f"Game relay exited with code {self.process.returncode}."
            self.failure = failure
        status = "failed" if failure or self.cleanup_errors else "passed"
        if self.timed_out:
            status = "total_timeout"
        if self.run_dir is not None and self.run_dir.is_dir():
            try:
                save_json(self.run_dir / "supervision.json" , {
                    "status" : status , "error" : failure , "cleanup_errors" : self.cleanup_errors ,
                    "job_handle_closed" : self.job_handle_closed , "close_rpc_sent" : self.close_sent ,
                    "child_returncode" : self.process.returncode if self.process is not None else None ,
                    "actions" : self.actions , "truncated" : self.truncated ,
                    "elapsed_seconds" : round(time.monotonic() - self.started , 3) if self.started else 0
                })
            except OSError as close_error:
                self.cleanup_errors.append(f"Supervision log: {close_error}")
        if error is None:
            if self.cleanup_errors:
                raise RuntimeError("; ".join(self.cleanup_errors))
            if self.timed_out:
                raise TimeoutError("Visible combat exceeded its total runtime limit.")
            if self.failure:
                raise RuntimeError(self.failure)

    def __exit__(self , error_type , error , traceback):
        self.close(error)
        return False
