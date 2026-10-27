"""小范围协议验证；不训练。single 检查 Strike，bash 检查 Bash，bounded 检查闭环。"""

import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import queue
import random
import subprocess
import sys
import threading
import time
import traceback


project_dir = Path(__file__).resolve().parents[1]
source_dir = project_dir / ".setup/divine-sts2"
host_dir = source_dir / "src/Sts2.NativeSim.GodotHost"
request_timeout = 15
total_timeout = 30  # 从启动受控子进程开始；前置只读哈希检查不计入。
max_actions = 100
random_seed = 0
no_window = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
baseline = json.loads((project_dir / "configs/native_baseline.json").read_text(encoding="utf-8"))
scenario = json.loads((project_dir / "configs/combat_probe.json").read_text(encoding="utf-8"))


def save_json(path, value):
    with path.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")


def visible_summary(state):
    # 人工查看的窄白名单，不包含 seed、hash、handle、抽牌顺序和内部 action/card ID。
    combat = state["observation"]["combat"]
    creatures = []
    for creature in combat["creatures"]:
        item = {key: creature.get(key) for key in ("model_id", "side", "hp", "max_hp", "block", "alive")}
        item["intents"] = [{key: intent.get(key) for key in ("intent_type", "damage", "repeats")}
                           for intent in (creature.get("next_move") or {}).get("intents", [])]
        item["powers"] = [{key: power.get(key) for key in ("model_id", "amount")} for power in creature.get("powers", [])]
        creatures.append(item)
    hand = next(pile["cards"] for pile in combat["piles"] if pile["name"] == "Hand")
    return {
        "turn": combat["turn"], "phase": combat["phase"], "energy": combat["energy"],
        "creatures": creatures,
        "hand": [{key: card.get(key) for key in ("model_id", "energy_cost", "upgrades", "card_type", "target_type")} for card in hand],
        "pile_counts": {pile["name"]: len(pile["cards"]) for pile in combat["piles"]},
        "legal_action_kinds": [action["kind"] for action in state["legal_actions"]],
        "terminated": state["terminated"], "victory": state["victory"],
    }


def create_job():
    # KILL_ON_JOB_CLOSE 连同后代进程一起清理；子进程收到 GO 前不会启动 Godot。
    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(job)
        raise error
    return kernel, job


if len(sys.argv) > 1 and sys.argv[1] == "--child":
    run_dir = Path(sys.argv[2])
    mode = sys.argv[3]
    if sys.stdin.readline().strip() != "GO":
        raise SystemExit("Supervisor did not authorize startup.")
    launch = json.loads((run_dir / "launch.json").read_text(encoding="utf-8"))
    raw_output = (run_dir / "godot.stdout.log").open("w", encoding="utf-8")
    raw_errors = (run_dir / "godot.stderr.log").open("w", encoding="utf-8")
    transcript = (run_dir / "protocol.jsonl").open("w", encoding="utf-8")
    process = None
    result = {"status": "failed", "mode": mode, "training": False, "actions": 0}
    lines = queue.Queue()
    sequence = 0
    started = time.monotonic()

    def read_stdout():
        try:
            for line in process.stdout:
                raw_output.write(line)
                raw_output.flush()
                lines.put(line)
        finally:
            lines.put(None)

    def request(method, params=None):
        global sequence
        sequence += 1
        message = {"id": str(sequence), "method": method, "params": params or {}}
        transcript.write(json.dumps({"request": message}, ensure_ascii=False) + "\n")
        transcript.flush()
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + request_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method}: request exceeded {request_timeout}s")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(f"{method}: request exceeded {request_timeout}s") from None
            if line is None:
                raise RuntimeError(f"Godot exited before {method} responded")
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(response, dict) or "id" not in response:
                continue
            transcript.write(json.dumps({"response": response}, ensure_ascii=False) + "\n")
            transcript.flush()
            if response["id"] != message["id"]:
                raise RuntimeError("Protocol response ID mismatch")
            if not response.get("ok"):
                raise RuntimeError(json.dumps(response.get("error"), ensure_ascii=False))
            return response["result"]

    try:
        process = subprocess.Popen(launch["command"], cwd=run_dir, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=raw_errors, text=True,
                                   encoding="utf-8", bufsize=1, creationflags=no_window)
        save_json(run_dir / "worker.json", {"pid": process.pid, "supervisor_child_pid": os.getpid()})
        threading.Thread(target=read_stdout, daemon=True).start()
        hello = request("hello")
        if hello["game_build"] != scenario["game_build"]:
            raise RuntimeError("Runtime game_build differs from the reviewed fingerprint")
        catalog = request("catalog")
        if scenario["encounter"] not in {item["model_id"] for item in catalog["encounters"]}:
            raise RuntimeError("Configured encounter is absent from native catalog")
        initial = request("reset", scenario)
        state = initial
        result["initial"] = visible_summary(state)
        result["game_build"] = hello["game_build"]
        result["scenario"] = str(project_dir / "configs/combat_probe.json")
        result["reset_request"] = scenario  # 诊断配置；不是策略输入。
        save_json(run_dir / "result.json", result)
        if mode in ("single", "bash"):
            instance_id, model_id, energy_cost, damage = (
                ("strike-0", "STRIKE_IRONCLAD", 1, 6) if mode == "single"
                else ("bash-0", "BASH", 2, 8)
            )
            action = next(action for action in state["legal_actions"]
                          if action["kind"] == "play_card" and action["parameters"].get("instance_id") == instance_id)
            before_combat = state["observation"]["combat"]
            before_hand = next(pile["cards"] for pile in before_combat["piles"] if pile["name"] == "Hand")
            card = next(card for card in before_hand if card["instance_id"] == instance_id)
            target_id = action["parameters"]["target_id"]
            target_before = next(creature for creature in before_combat["creatures"] if creature["combat_id"] == target_id)
            if card["model_id"] != model_id or card["energy_cost"] != energy_cost or card["upgrades"] != 0 or target_before["block"] != 0:
                raise RuntimeError(f"Single-card probe requires unupgraded {model_id} at cost {energy_cost} and an unblocked target")
            state = request("step", {"action_id": action["action_id"]})
            result["actions"] = 1
            result["chosen_action"] = action
            result["after"] = visible_summary(state)
            after_combat = state["observation"]["combat"]
            target_after = next(creature for creature in after_combat["creatures"] if creature["combat_id"] == target_id)
            after_hand = next(pile["cards"] for pile in after_combat["piles"] if pile["name"] == "Hand")
            discarded = next(pile["cards"] for pile in after_combat["piles"] if pile["name"] == "DiscardPile")
            result["card_delta"] = {"target": target_before["model_id"],
                                    "energy": after_combat["energy"] - before_combat["energy"],
                                    "target_hp": target_after["hp"] - target_before["hp"],
                                    "hand_count": len(after_hand) - len(before_hand)}
            if mode == "single":
                result["strike_delta"] = result["card_delta"]
            result["power_changes"] = {"before": target_before["powers"], "after": target_after["powers"]}
            result["checks"] = {
                "state_hash_changed": state["state_hash"] != initial["state_hash"],
                "spent_one_energy" if mode == "single" else "spent_two_energy": result["card_delta"]["energy"] == -energy_cost,
                "dealt_six_damage" if mode == "single" else "dealt_eight_damage": result["card_delta"]["target_hp"] == -damage,
                "strike_left_hand" if mode == "single" else "bash_left_hand": not any(card["instance_id"] == instance_id for card in after_hand),
                "strike_in_discard" if mode == "single" else "bash_in_discard": any(card["instance_id"] == instance_id for card in discarded),
            }
            if not all(result["checks"].values()):
                raise RuntimeError(f"Native {model_id} did not satisfy the fixed baseline expectations; inspect deltas and raw protocol")
        else:
            rng = random.Random(random_seed)
            end_turns = 0
            while not state["terminated"] and result["actions"] < max_actions:
                legal = state["legal_actions"]
                if not legal:
                    raise RuntimeError("Nonterminal state has no legal actions")
                # 第一项明确验证结束回合，随后才用有固定随机种子的合法动作。
                action = next(action for action in legal if action["kind"] == "end_turn") if result["actions"] == 0 else rng.choice(legal)
                previous = state
                state = request("step", {"action_id": action["action_id"]})
                result["actions"] += 1
                if action["kind"] == "end_turn":
                    end_turns += 1
                    if end_turns == 1:
                        result["first_end_turn"] = {"before": visible_summary(previous), "after": visible_summary(state)}
                result["after"] = visible_summary(state)
                save_json(run_dir / "result.json", result)
            result["end_turns"] = end_turns
            result["termination_reason"] = "terminal" if state["terminated"] else "action_limit"
            repeated = request("reset", scenario)
            result["repeat_seed"] = {
                "same_state_hash": initial["state_hash"] == repeated["state_hash"],
                "same_observation": initial["observation"] == repeated["observation"],
                "same_legal_actions": initial["legal_actions"] == repeated["legal_actions"],
            }
            if not all(result["repeat_seed"].values()):
                raise RuntimeError("Repeated seed reset did not reproduce the initial state")
        request("close")
        process.wait(timeout=5)
        if process.returncode != 0:
            raise RuntimeError(f"Godot exited with code {process.returncode}")
        result["status"] = "passed"
    except Exception as error:
        result["error"] = str(error)
        result["traceback"] = traceback.format_exc()
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if process is not None:
            result["worker_returncode"] = process.poll()
        save_json(run_dir / "result.json", result)
        transcript.close()
    raise SystemExit(0 if result["status"] == "passed" else 1)

else:
    mode = sys.argv[1] if len(sys.argv) > 1 else "single"
    if os.name != "nt" or mode not in ("single", "bash", "bounded") or len(sys.argv) > 2:
        raise SystemExit("Usage on Windows: python scripts/probe_combat.py [single|bash|bounded]")
    if mode == "bounded":
        previous = list((project_dir / "results/probes").glob("*/result.json"))
        single_passed = False
        for path in previous:
            item = json.loads(path.read_text(encoding="utf-8"))
            supervision_file = path.with_name("supervision.json")
            if supervision_file.is_file():
                supervised = json.loads(supervision_file.read_text(encoding="utf-8"))
                if (item.get("mode") == "single" and item.get("status") == "passed"
                        and supervised.get("status") == "passed" and item.get("reset_request") == scenario):
                    single_passed = True
        if not single_passed:
            raise SystemExit("A successful single probe is required before bounded mode.")
    # 每次执行都重新计算安装指纹，绝不使用过去的通过报告代替本次检查。
    checked = subprocess.run([sys.executable, str(project_dir / "scripts/check_environment.py")],
                             cwd=project_dir, capture_output=True, text=True, encoding="utf-8",
                             timeout=60, creationflags=no_window)
    print(checked.stdout, end="")
    if checked.returncode != 0:
        print(checked.stderr, file=sys.stderr, end="")
        raise SystemExit("Preflight did not pass; no worker started.")
    checked_output = json.loads(checked.stdout)
    preflight = json.loads(Path(checked_output["report"]).read_text(encoding="utf-8"))
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source_dir, capture_output=True,
                              text=True, timeout=5, check=True, creationflags=no_window).stdout.strip()
    if revision != baseline["source_commit"]:
        raise SystemExit("Native source commit is not the reviewed baseline.")
    expected_build = {"version": baseline["product_version"],
                      "assembly_sha256": baseline["files"]["data_sts2_windows_x86_64/sts2.dll"],
                      "pck_sha256": baseline["files"]["SlayTheSpire2.pck"]}
    if scenario["game_build"] != expected_build or preflight["blockers"]:
        raise SystemExit("Scenario/preflight does not match the reviewed baseline.")
    godot = Path(os.environ.get("GODOT") or preflight["godot_candidates"][0]).resolve()
    assembly = Path(preflight["game_directory"]) / "data_sts2_windows_x86_64/sts2.dll"
    run_name = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S_%f") + "_" + mode
    run_dir = project_dir / "results/probes" / run_name
    run_dir.mkdir(parents=True)
    environment = os.environ.copy()
    isolated = {"APPDATA": run_dir / "profile/AppData/Roaming",
                "LOCALAPPDATA": run_dir / "profile/AppData/Local",
                "USERPROFILE": run_dir / "profile", "TEMP": run_dir / "tmp", "TMP": run_dir / "tmp",
                "DOTNET_CLI_HOME": run_dir / "dotnet-home"}
    for name, path in isolated.items():
        path.mkdir(parents=True, exist_ok=True)
        environment[name] = str(path)
    environment["HOME"] = environment["USERPROFILE"]
    environment["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    for name in ("STS2_NATIVE_TRACE", "STS2_NATIVE_TRACE_SOURCE"):
        environment.pop(name, None)
    dotnet_root = project_dir / ".tools/dotnet9"
    if (dotnet_root / "dotnet.exe").is_file():
        environment["DOTNET_ROOT"] = environment["DOTNET_ROOT_X64"] = str(dotnet_root)
        environment["PATH"] = str(dotnet_root) + os.pathsep + environment.get("PATH", "")
    command = [str(godot), "--headless", "--path", str(host_dir), "--log-file", str(run_dir / "godot.engine.log"),
               "--", "--server", str(assembly)]
    save_json(run_dir / "launch.json", {"command": command, "preflight_report": checked_output["report"],
              "source_commit": revision, "isolated_environment": {name: str(path) for name, path in isolated.items()},
              "total_timeout_seconds": total_timeout, "request_timeout_seconds": request_timeout,
              "max_actions": 1 if mode in ("single", "bash") else max_actions,
              "scope": "Reconstructed isolated combat; no training; raw protocol is diagnostic only."})
    kernel, job = create_job()
    child = None
    started = time.monotonic()
    supervision = {"mode": mode, "status": "failed", "cleanup": "pending"}
    try:
        with (run_dir / "supervisor-child.stdout.log").open("w", encoding="utf-8") as stdout, (run_dir / "supervisor-child.stderr.log").open("w", encoding="utf-8") as stderr:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child", str(run_dir), mode],
                                     stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, text=True,
                                     env=environment, cwd=run_dir, creationflags=no_window)
            if not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(child._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            child.stdin.write("GO\n")
            child.stdin.close()
            child.wait(timeout=max(0.01, total_timeout - (time.monotonic() - started)))
            supervision["status"] = "passed" if child.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        supervision["status"] = "total_timeout"
    except Exception as error:
        supervision["error"] = str(error)
    finally:
        # 即使 child 异常退出，关闭 Job 也会清理它启动的 Godot 和所有后代。
        supervision["job_handle_closed"] = bool(kernel.CloseHandle(job))
        if not supervision["job_handle_closed"]:
            supervision["status"] = "cleanup_failed"
        if child is not None:
            if child.poll() is None:  # 分配 Job 失败时，它还在等待 GO，尚未启动游戏。
                child.kill()
            child.wait(timeout=5)
            supervision["child_returncode"] = child.returncode
        supervision["cleanup"] = "kill_on_job_close" if supervision["job_handle_closed"] else "job_close_failed"
        supervision["elapsed_seconds"] = round(time.monotonic() - started, 3)
        save_json(run_dir / "supervision.json", supervision)
    print(json.dumps({"status": supervision["status"], "result_directory": str(run_dir)}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if supervision["status"] == "passed" else 1)
