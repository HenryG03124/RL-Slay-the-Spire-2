# Check installed files and tools without starting the game.

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime , timedelta , timezone


project_dir = Path(__file__).resolve().parents[1]
game_dir = Path(os.environ.get(
    "STS2_GAME_ROOT" ,
    r"C:\Program Files (x86)\Steam\steamapps\common\Slay the Spire 2" ,
)).resolve()
command_timeout = 10
started = time.perf_counter()
checked_at = datetime.now(timezone(timedelta(hours = 8)))
output_dir = Path(tempfile.gettempdir()) / "sts2-rl/preflight" / f"{checked_at:%Y%m%d_%H%M%S_%f}"
baseline = json.loads((project_dir / "configs/native_baseline.json").read_text(encoding = "utf-8"))
blockers = []
report = {
    "checked_at" : checked_at.isoformat() ,
    "game_directory" : str(game_dir) ,
    "baseline" : baseline ,
    "files" : {} ,
    "commands" : {} ,
    "blockers" : blockers ,
    "game_code_loaded" : False ,
    "combat_probe_run" : False ,
}

# Keep installation fields only.
manifest = game_dir.parent.parent / "appmanifest_2868840.acf"
try:
    manifest_text = manifest.read_text(encoding = "utf-8")
    report["steam"] = {}
    for key in ("appid" , "name" , "StateFlags" , "buildid" , "BetaKey"):
        match = re.search(r'"' + key + r'"\s+"([^"]+)"' , manifest_text)
        report["steam"][key] = match.group(1) if match else None
except OSError as error:
    report["steam"] = {"error" : str(error)}

try:
    release = json.loads((game_dir / "release_info.json").read_text(encoding = "utf-8"))
    report["release"] = release
    if release.get("version") != baseline["game_version"]:
        blockers.append("game_version_not_in_reviewed_baseline")
    if release.get("commit") != baseline["game_commit"]:
        blockers.append("game_commit_not_in_reviewed_baseline")
except (OSError , ValueError) as error:
    report["release"] = {"error" : str(error)}
    blockers.append("release_info_unreadable")

# Hash large files in chunks.
for relative_path , expected_hash in baseline["files"].items():
    path = game_dir / relative_path
    try:
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as file:
            while True:
                chunk = file.read(4 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        after = path.stat()
        actual_hash = digest.hexdigest().upper()
        stable = (before.st_size , before.st_mtime_ns) == (after.st_size , after.st_mtime_ns)
        matched = actual_hash == expected_hash
        report["files"][relative_path] = {
            "sha256" : actual_hash ,
            "expected_sha256" : expected_hash ,
            "matches_baseline" : matched ,
            "bytes" : after.st_size ,
            "stable_during_read" : stable ,
        }
        if not matched:
            blockers.append(f"fingerprint_mismatch:{relative_path}")
        if not stable:
            blockers.append(f"file_changed_during_read:{relative_path}")
    except OSError as error:
        report["files"][relative_path] = {"error" : str(error)}
        blockers.append(f"file_unreadable:{relative_path}")

report["python"] = {"executable" : sys.executable , "version" : sys.version.split()[0]}
if sys.version_info < (3 , 11):
    blockers.append("python_below_3_11")

# Read version metadata without loading the assembly.
assembly = game_dir / "data_sts2_windows_x86_64/sts2.dll"
powershell = shutil.which("pwsh") or shutil.which("powershell")
bundled_dotnet = project_dir / ".tools/dotnet9/dotnet.exe"
dotnet = os.environ.get("DOTNET") or (str(bundled_dotnet) if bundled_dotnet.is_file() else shutil.which("dotnet"))
commands = {}
if powershell and assembly.is_file():
    quoted_path = str(assembly).replace("'" , "''")
    commands["dll_product_version"] = [
        powershell , "-NoProfile" , "-NonInteractive" , "-Command" ,
        f"$ErrorActionPreference = 'Stop'; (Get-Item -LiteralPath '{quoted_path}').VersionInfo.ProductVersion" ,
    ]
if dotnet:
    commands["dotnet_sdks"] = [dotnet , "--list-sdks"]

for name , command in commands.items():
    try:
        result = subprocess.run(
            command , capture_output = True , text = True , errors = "replace" ,
            timeout = command_timeout , cwd = project_dir ,
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0 ,
        )
        report["commands"][name] = {
            "returncode" : result.returncode ,
            "stdout" : result.stdout.strip() ,
            "stderr" : result.stderr.strip() ,
        }
    except (OSError , subprocess.TimeoutExpired) as error:
        report["commands"][name] = {"error" : str(error)}

product = report["commands"].get("dll_product_version" , {})
if product.get("returncode") != 0 or product.get("stdout") != baseline["product_version"]:
    blockers.append("dll_product_version_unverified_or_mismatched")
sdks = report["commands"].get("dotnet_sdks" , {})
if sdks.get("returncode") != 0 or not re.search(r"^9\.\d+\.\d+\s" , sdks.get("stdout" , "") , re.MULTILINE):
    blockers.append("dotnet_9_sdk_not_discovered")

# Use configured tools and PATH.
godot_names = (
    "godot" , "godot4" ,
    "Godot_v4.5.1-stable_mono_win64_console.exe" ,
    "Godot_v4.5.1-stable_mono_win64.exe" ,
)
godot_candidates = []
if os.environ.get("GODOT"):
    godot_candidates.append(os.environ["GODOT"])
for name in godot_names:
    located = shutil.which(name)
    if located:
        godot_candidates.append(located)
tools_dir = project_dir / ".tools/godot-4.5.1-mono"
if tools_dir.is_dir():
    for name in godot_names[2 : ]:
        for path in tools_dir.rglob(name):
            godot_candidates.append(str(path))
report["godot_candidates"] = []
for candidate in godot_candidates:
    path = Path(candidate)
    if path.is_file():
        path = str(path.resolve())
        if path not in report["godot_candidates"]:
            report["godot_candidates"].append(path)
if not report["godot_candidates"]:
    blockers.append("godot_mono_not_discovered")
else:
    # Keep version-check files in the temporary directory.
    tool_environment = os.environ.copy()
    for variable in ("APPDATA" , "LOCALAPPDATA" , "USERPROFILE" , "TEMP" , "TMP"):
        isolated_path = output_dir / "tool-profile" / variable.lower()
        isolated_path.mkdir(parents = True , exist_ok = True)
        tool_environment[variable] = str(isolated_path)
    try:
        result = subprocess.run(
            [report["godot_candidates"][0] , "--headless" , "--version"] ,
            capture_output = True , text = True , errors = "replace" , timeout = command_timeout ,
            cwd = project_dir , env = tool_environment ,
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0 ,
        )
        report["commands"]["godot_version"] = {
            "returncode" : result.returncode , "stdout" : result.stdout.strip() , "stderr" : result.stderr.strip() ,
        }
        if result.returncode != 0 or not result.stdout.strip().startswith("4.5.1.stable.mono."):
            blockers.append("godot_4_5_1_mono_version_mismatch")
    except (OSError , subprocess.TimeoutExpired) as error:
        report["commands"]["godot_version"] = {"error" : str(error)}
        blockers.append("godot_version_unreadable")

report["status"] = "blocked" if blockers else "preflight_passed_pending_runtime_audit"
report["elapsed_seconds"] = round(time.perf_counter() - started , 3)
output_dir.mkdir(parents = True , exist_ok = True)
output = output_dir / "report.json"
with output.open("x" , encoding = "utf-8") as file:
    json.dump(report , file , ensure_ascii = False , indent = 2)
    file.write("\n")
print(json.dumps({"status" : report["status"] , "blockers" : blockers , "report" : str(output)} , ensure_ascii = False , indent = 2))
sys.exit(2 if blockers else 0)
