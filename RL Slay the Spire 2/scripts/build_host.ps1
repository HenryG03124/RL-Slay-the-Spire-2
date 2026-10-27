$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$sourceRoot = Join-Path $projectRoot '.setup\divine-sts2'
$dotnetRoot = Join-Path $projectRoot '.tools\dotnet9'
$buildProfile = Join-Path $projectRoot '.setup\build-profile'
$hostProject = Join-Path $sourceRoot 'src\Sts2.NativeSim.GodotHost\Sts2.NativeSim.GodotHost.csproj'

# 本进程及其子进程使用项目内缓存，不修改系统环境变量。
$env:DOTNET_ROOT = $dotnetRoot
$env:DOTNET_ROOT_X64 = $dotnetRoot
$env:DOTNET_CLI_HOME = Join-Path $buildProfile 'dotnet'
$env:NUGET_PACKAGES = Join-Path $buildProfile 'nuget-packages'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_NOLOGO = '1'
$env:DOTNET_ADD_GLOBAL_TOOLS_TO_PATH = 'false'
$env:MSBUILDDISABLENODEREUSE = '1'
$env:APPDATA = Join-Path $buildProfile 'appdata'
$env:LOCALAPPDATA = Join-Path $buildProfile 'localappdata'
$env:TEMP = Join-Path $buildProfile 'temp'
$env:TMP = $env:TEMP
$env:PATH = "$dotnetRoot;$env:PATH"
foreach ($path in @($env:DOTNET_CLI_HOME, $env:NUGET_PACKAGES, $env:APPDATA, $env:LOCALAPPDATA, $env:TEMP)) {
    New-Item -ItemType Directory -Path $path -Force | Out-Null
}
$sourceCommit = & git -C $sourceRoot rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $sourceCommit -ne '7cb715916fc9abfb0281c0adf4939c74468e05a4') {
    throw 'The native source revision does not match the reviewed baseline.'
}

# 使用已校验的 Godot 官方压缩包附带的本地 NuGet 包。
$packageSource = Join-Path $projectRoot '.tools\godot-4.5.1-mono\Godot_v4.5.1-stable_mono_win64\GodotSharp\Tools\nupkgs'
$packageSourceXml = [System.Security.SecurityElement]::Escape($packageSource)
@"
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <packageSources>
    <clear />
    <add key="Godot bundled" value="$packageSourceXml" />
  </packageSources>
</configuration>
"@ | Set-Content -LiteralPath (Join-Path $sourceRoot 'NuGet.Config') -Encoding utf8

$logDirectory = Join-Path $projectRoot 'results\setup'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$log = Join-Path $logDirectory ('build-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '.txt')
& (Join-Path $dotnetRoot 'dotnet.exe') build $hostProject -c Debug --disable-build-servers -nodeReuse:false -maxcpucount:1 -p:UseSharedCompilation=false 2>&1 | Tee-Object -FilePath $log
if ($LASTEXITCODE -ne 0) { throw "Native host build failed. See $log" }
