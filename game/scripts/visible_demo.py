# Run: python -m game.scripts.visible_demo
# One JSON command per stdin line; no automatic actions.

import codecs
import ctypes
from ctypes import wintypes
from datetime import datetime , timedelta , timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback

from game.environment import create_job , save_json

project_dir = Path(__file__).resolve().parents[1]

startup_timeout = 45
request_timeout = 30
total_timeout = 600
no_window = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
run_name = datetime.now(timezone(timedelta(hours = 8))).strftime("%Y%m%d_%H%M%S_%f")
run_dir = Path(tempfile.gettempdir()) / "sts2-rl/visible_demo" / run_name
sandbox = run_dir / "sandbox"
package_dir = project_dir / ".setup/visible-bridge-build/bin/Release/net9.0/package"
process = job = kernel = game_handle = connection = reader = writer = timer = None
started = None
closed = False
timed_out = False
job_handle_closed = False
close_sent = False
transport_failed = False
run_started = False
request_id = 0
status = "failed"
error_text = None
cleanup_errors = []
files = []
commands = queue.Queue()
lock = threading.RLock()


def close_job():
    global job , job_handle_closed
    with lock:
        if job is not None:
            if not kernel.CloseHandle(job):
                raise ctypes.WinError(ctypes.get_last_error())
            job = None
            job_handle_closed = True


def expire():
    global timed_out
    with lock:
        if closed:
            return
        timed_out = True
        try:
            close_job()
        except OSError as error:
            cleanup_errors.append(str(error))
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        commands.put(None)


def read_stdin():
    # Read the fd to avoid a buffered stdin lock during exit.
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    try:
        while True:
            data = os.read(sys.stdin.fileno() , 4096)
            if not data:
                break
            pending += decoder.decode(data)
            while "\n" in pending:
                line , pending = pending.split("\n" , 1)
                if line.strip():
                    commands.put(line.strip())
        pending += decoder.decode(b"" , final = True)
        if pending.strip():
            commands.put(pending.strip())
    finally:
        commands.put(None)


def check_worker():
    global game_handle
    if timed_out or (started is not None and time.monotonic() - started >= total_timeout):
        expire()
        raise TimeoutError("Visible demo reached its 600-second limit.")
    if process.poll() is not None:
        raise RuntimeError(f"Game relay exited with code {process.returncode}.")
    worker_path = sandbox / "worker.json"
    if game_handle is None and worker_path.is_file():
        try:
            worker = json.loads(worker_path.read_text(encoding = "utf-8"))
        except (OSError , ValueError):
            return True  # The relay may still be writing this file.
        game_handle = kernel.OpenProcess(0x00100000 , False , worker["pid"])
        if not game_handle:
            raise RuntimeError("Game process exited before its window became ready.")
    if game_handle is not None:
        result = kernel.WaitForSingleObject(game_handle , 0)
        if result == 0:
            return False
        if result != 0x102:
            raise ctypes.WinError(ctypes.get_last_error())
    return True


def rpc(method , params = None , timeout = None):
    global request_id , transport_failed
    request_id += 1
    request = {"id" : request_id , "method" : method , "params" : params or {}}
    protocol.write(json.dumps({"request" : request} , ensure_ascii = False) + "\n")
    protocol.flush()
    try:
        remaining = total_timeout - (time.monotonic() - started)
        if remaining <= 0 or timed_out:
            raise TimeoutError("Visible demo reached its total runtime limit.")
        connection.settimeout(min(timeout or request_timeout , remaining))
        writer.write(json.dumps(request , ensure_ascii = False) + "\n")
        writer.flush()
        line = reader.readline()
        if not line:
            raise EOFError("The bridge closed its socket.")
        response = json.loads(line)
        protocol.write(json.dumps({"response" : response} , ensure_ascii = False) + "\n")
        protocol.flush()
        save_json(run_dir / "last_response.json" , response)
        if response.get("id") != request_id:
            raise RuntimeError("Bridge response ID mismatch.")
        if response.get("error"):
            raise RuntimeError(str(response["error"]))
        return response.get("result")
    except BaseException:
        transport_failed = True
        raise


def summarize(result):
    if isinstance(result , list):
        actions = []
        for item in result[ : 30]:
            action = {}
            for key in ("action_id" , "action_type" , "description"):
                action[key] = item.get(key)
            actions.append(action)
        return {"legal_actions" : actions , "action_count" : len(result)}
    if not isinstance(result , dict):
        return result
    obs = result.get("observation")
    if obs is None and "phase" in result:
        obs = result
    if not isinstance(obs , dict):
        summary = {}
        for key in ("status" , "closed" , "started" , "seed" , "character" , "ascension" , "actions"):
            if key in result:
                summary[key] = result[key]
        return summary
    summary = {}
    for key in ("phase" , "is_terminal" , "is_victory" , "character" , "floor" , "player_hp" , "player_max_hp" , "player_energy"):
        summary[key] = obs.get(key)
    combat = obs.get("combat") or {}
    summary["hand"] = []
    for card in combat.get("hand" , []):
        card_summary = {}
        for key in ("index" , "card_id" , "cost" , "can_play"):
            card_summary[key] = card.get(key)
        summary["hand"].append(card_summary)
    summary["enemies"] = []
    for enemy in combat.get("enemies" , []):
        enemy_summary = {}
        for key in ("combat_id" , "model_id" , "hp" , "max_hp" , "block" , "intent"):
            enemy_summary[key] = enemy.get(key)
        summary["enemies"].append(enemy_summary)
    if "legal_actions" in result:
        summary.update(summarize(result["legal_actions"]))
    return summary


try:
    if os.name != "nt":
        raise RuntimeError("This launcher currently supports Windows only.")
    sandbox.mkdir(parents = True)
    for name in ("sts2-visible-bridge.dll" , "sts2-visible-bridge.json"):
        if not (package_dir / name).is_file():
            raise FileNotFoundError(f"Build the visible bridge first: {package_dir / name}")
    checked = subprocess.run([sys.executable , str(project_dir / "scripts/check_environment.py")] ,
                             cwd = project_dir , capture_output = True , text = True , encoding = "utf-8" ,
                             timeout = 60 , creationflags = no_window)
    if checked.returncode != 0:
        raise RuntimeError("Preflight failed; no game started.\n" + checked.stdout + checked.stderr)
    checked_output = json.loads(checked.stdout)
    preflight = json.loads(Path(checked_output["report"]).read_text(encoding = "utf-8"))
    if preflight["blockers"]:
        raise RuntimeError("Preflight has unresolved blockers.")
    game_root = Path(preflight["game_directory"]).resolve()
    for source in game_root.iterdir():
        if source.is_file() and source.suffix.lower() in (".exe" , ".pck" , ".dll" , ".json"):
            try:
                os.link(source , sandbox / source.name)
            except OSError:
                # Copy assets when hardlinks are unavailable.
                if shutil.disk_usage(run_dir).free < source.stat().st_size + 512 * 1024 * 1024:
                    raise RuntimeError("Insufficient free space for isolated game assets.")
                shutil.copy2(source , sandbox / source.name)
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        raise RuntimeError("PowerShell is needed to create isolated asset junctions.")
    for name in ("data_sts2_windows_x86_64" , "controller_config"):
        source = game_root / name
        destination = sandbox / name
        if source.is_dir():
            quoted_source = str(source).replace("'" , "''")
            quoted_destination = str(destination).replace("'" , "''")
            script = "$ErrorActionPreference = 'Stop'\n" + (
                f"New-Item -ItemType Junction -Path '{quoted_destination}' -Target '{quoted_source}' | Out-Null")
            try:
                subprocess.run([powershell , "-NoProfile" , "-NonInteractive" , "-Command" , script] ,
                               check = True , capture_output = True , text = True , timeout = 10 , creationflags = no_window)
            except subprocess.CalledProcessError:
                if destination.exists() or destination.is_symlink() or destination.is_junction():
                    raise RuntimeError(f"Unexpected partial asset link: {destination}")
                shutil.copytree(source , destination)
    mods_dir = sandbox / "mods/sts2-visible-bridge"
    mods_dir.mkdir(parents = True)
    for source in package_dir.iterdir():
        if source.is_file():
            shutil.copy2(source , mods_dir / source.name)
    environment = os.environ.copy()
    isolated = {"APPDATA" : run_dir / "profile/AppData/Roaming" , "LOCALAPPDATA" : run_dir / "profile/AppData/Local" ,
                "USERPROFILE" : run_dir / "profile" , "TEMP" : run_dir / "tmp" , "TMP" : run_dir / "tmp" ,
                "DOTNET_CLI_HOME" : run_dir / "dotnet-home"}
    for name , path in isolated.items():
        path.mkdir(parents = True , exist_ok = True)
        environment[name] = str(path)
    environment["HOME"] = environment["USERPROFILE"]
    environment["PYTHONIOENCODING"] = "utf-8"
    for name in ("STS2_NATIVE_TRACE" , "STS2_NATIVE_TRACE_SOURCE" , "STS2_FORCE_CHARACTER"):
        environment.pop(name , None)
    port_file = run_dir / "bridge_port.txt"
    environment["STS2_FULL_APP_BRIDGE_PORT"] = "0"
    environment["STS2_FULL_APP_BRIDGE_PORT_FILE"] = str(port_file)
    source_settings = Path(os.environ["APPDATA"]) / "SlayTheSpire2/default/1/settings.save"
    original_settings = json.loads(source_settings.read_text(encoding = "utf-8"))
    setting_keys = ("schema_version" , "language" , "aspect_ratio" , "controller_mapping" , "controller_mapping_type" ,
                    "keyboard_mapping" , "keyboard_only_mapping" , "fps_limit" , "limit_fps_in_background" , "msaa" ,
                    "resize_windows" , "seen_ea_disclaimer" , "target_display" , "volume_ambience" , "volume_bgm" ,
                    "volume_master" , "volume_sfx" , "vsync")
    settings = {}
    for key in setting_keys:
        if key in original_settings:
            settings[key] = original_settings[key]
    settings.update({"mod_settings" : {"mods_enabled" : True , "mod_list" : []} , "fullscreen" : False ,
                     "skip_intro_logo" : True , "window_size" : {"X" : 1280 , "Y" : 720} ,
                     "window_position" : {"X" : -1 , "Y" : -1}})
    settings_path = isolated["APPDATA"] / "SlayTheSpire2/default/1/settings.save"
    settings_path.parent.mkdir(parents = True)
    save_json(settings_path , settings)
    command = [str(sandbox / "SlayTheSpire2.exe") , "--force-steam=off" , "--windowed" ,
               "--resolution" , "1280x720" , f"--log-file={run_dir / 'game.engine.log'}"]
    isolated_paths = {}
    for key , value in isolated.items():
        isolated_paths[key] = str(value)
    launch = {"command" : command , "preflight_report" : checked_output["report"] ,
              "isolated_environment" : isolated_paths ,
              "visible" : True , "total_timeout_seconds" : total_timeout , "request_timeout_seconds" : request_timeout ,
              "bridge_dll_sha256" : hashlib.sha256((mods_dir / "sts2-visible-bridge.dll").read_bytes()).hexdigest().upper() ,
              "worker_file" : str(sandbox / "worker.json") , "scope" : "Visible native-action demonstration; Actor not connected."}
    save_json(run_dir / "launch.json" , launch)
    save_json(sandbox / "launch.json" , launch)
    output = (run_dir / "game.stdout.log").open("w" , encoding = "utf-8")
    files.append(output)
    errors = (run_dir / "relay.stderr.log").open("w" , encoding = "utf-8")
    files.append(errors)
    protocol = (run_dir / "protocol.jsonl").open("w" , encoding = "utf-8")
    files.append(protocol)
    kernel , job = create_job()
    kernel.OpenProcess.argtypes = [wintypes.DWORD , wintypes.BOOL , wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE , wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    started = time.monotonic()
    process = subprocess.Popen([sys.executable , "-u" , str(project_dir / "worker.py") , str(sandbox)] ,
                               cwd = sandbox , env = environment , stdin = subprocess.PIPE , stdout = output , stderr = errors ,
                               text = True , encoding = "utf-8" , creationflags = no_window)
    if not kernel.AssignProcessToJobObject(job , wintypes.HANDLE(process._handle)):
        raise ctypes.WinError(ctypes.get_last_error())
    timer = threading.Timer(max(0 , total_timeout - (time.monotonic() - started)) , expire)
    timer.daemon = True
    timer.start()
    process.stdin.write("GO\n")
    process.stdin.flush()
    port = None
    while time.monotonic() - started < startup_timeout:
        if not check_worker():
            raise RuntimeError("Game window closed before bridge initialization.")
        if port_file.is_file():
            value = port_file.read_text(encoding = "utf-8").strip()
            if value.isdecimal() and 1 <= int(value) <= 65535:
                port = int(value)
                break
        time.sleep(0.1)
    if port is None:
        raise TimeoutError("Bridge did not publish a port within 45 seconds.")
    connection = socket.create_connection(("127.0.0.1" , port) , timeout = 5)
    reader = connection.makefile("r" , encoding = "utf-8-sig")
    writer = connection.makefile("w" , encoding = "utf-8")
    hello = rpc("hello")
    if (hello.get("bridge_id") != "sts2-visible-bridge" or hello.get("visible") is not True
            or hello.get("presentation_suppressed") is not False):
        raise RuntimeError("Bridge did not confirm visible mode without presentation suppression.")
    if not hello.get("display_server") or hello["display_server"].lower() == "headless":
        raise RuntimeError("The game unexpectedly started with a headless display.")
    loaded_assembly = Path(hello.get("game_assembly_path" , "")).resolve()
    expected_assembly = (sandbox / "data_sts2_windows_x86_64/sts2.dll").resolve()
    if loaded_assembly != expected_assembly:
        raise RuntimeError("The bridge loaded a different game assembly.")
    if hashlib.sha256(loaded_assembly.read_bytes()).hexdigest().upper() != preflight["baseline"]["files"]["data_sts2_windows_x86_64/sts2.dll"]:
        raise RuntimeError("The loaded game assembly differs from the verified fingerprint.")
    worker = json.loads((sandbox / "worker.json").read_text(encoding = "utf-8"))
    if hello.get("pid") != worker["pid"] or hello.get("bound_port") != port:
        raise RuntimeError("Bridge process identity differs from the isolated game process.")
    print(json.dumps({"status" : "ready" , "run_dir" : str(run_dir) , "worker_file" : str(sandbox / "worker.json") ,
                      "hello" : hello} , ensure_ascii = False) , flush = True)
    threading.Thread(target = read_stdin , daemon = True).start()
    while True:
        if not check_worker():
            status = "window_closed"
            break
        try:
            line = commands.get(timeout = 0.2)
        except queue.Empty:
            continue
        if line is None:
            status = "input_closed"
            break
        try:
            request = json.loads(line)
            if not isinstance(request , dict):
                raise ValueError("Each input line must be a JSON object.")
            method = request["method"]
            params = request.get("params") or {}
            if not isinstance(params , dict):
                raise ValueError("params must be a JSON object.")
            if method not in {"hello" , "start_run" , "observe" , "legal_actions" , "step" , "history" , "close"}:
                raise ValueError("Unsupported RPC method.")
            if method == "start_run":
                if run_started:
                    raise ValueError("This demo starts at most one run.")
                if params.get("ascension" , 0) != 0:
                    raise ValueError("This first visible demonstration supports ascension 0 only.")
        except (ValueError , KeyError , TypeError) as error:
            print(json.dumps({"status" : "invalid_input" , "error" : str(error)} , ensure_ascii = False) , flush = True)
            continue
        result = rpc(method , params)
        print(json.dumps({"method" : method , "result" : summarize(result)} , ensure_ascii = False) , flush = True)
        if method == "start_run":
            run_started = True
        if method == "close":
            close_sent = True
            status = "closed"
            break
except BaseException as error:
    status = "interrupted" if isinstance(error , KeyboardInterrupt) else "failed"
    error_text = str(error)
    if run_dir.is_dir():
        (run_dir / "error.txt").write_text(traceback.format_exc() , encoding = "utf-8")
finally:
    if connection is not None and not close_sent and not timed_out and not transport_failed:
        try:
            rpc("close" , timeout = 3)
            close_sent = True
        except Exception as error:
            cleanup_errors.append(f"Graceful close: {error}")
    if process is not None:
        try:
            process.stdin.close()
            process.wait(timeout = 5)
        except Exception:
            pass
    with lock:
        closed = True
        if timer is not None:
            timer.cancel()
        try:
            close_job()
        except OSError as error:
            cleanup_errors.append(f"Job close: {error}")
    if process is not None:
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout = 5)
        except Exception as error:
            cleanup_errors.append(f"Relay exit: {error}")
    for stream in (writer , reader , connection):
        if stream is not None:
            try:
                stream.close()
            except Exception as error:
                cleanup_errors.append(f"Socket close: {error}")
    if game_handle is not None:
        kernel.CloseHandle(game_handle)
    for file in files:
        file.close()
    if timed_out:
        status = "total_timeout"
    report = {"status" : status , "error" : error_text , "cleanup_errors" : cleanup_errors ,
              "job_handle_closed" : job_handle_closed , "close_rpc_sent" : close_sent ,
              "child_returncode" : process.returncode if process is not None else None ,
              "elapsed_seconds" : round(time.monotonic() - started , 3) if started else 0 ,
              "run_dir" : str(run_dir) , "actor_connected" : False}
    if run_dir.is_dir():
        save_json(run_dir / "supervision.json" , report)
    print(json.dumps(report , ensure_ascii = False) , flush = True)

raise SystemExit(0 if status in {"closed" , "window_closed" , "input_closed"} and not cleanup_errors else 1)
