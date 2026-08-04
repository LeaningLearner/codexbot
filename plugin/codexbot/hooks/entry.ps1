$ErrorActionPreference = "SilentlyContinue"

if ($env:CODEXBOT_DATA_DIR) {
    $dataDir = $env:CODEXBOT_DATA_DIR
} elseif ($env:LOCALAPPDATA) {
    $dataDir = Join-Path $env:LOCALAPPDATA "CodexBot"
} else {
    [Console]::Out.Write("{}")
    exit 0
}

$runtime = Join-Path $dataDir "runtime\Scripts\pythonw.exe"
$script = Join-Path $PSScriptRoot "entry.py"
if (-not (Test-Path -LiteralPath $runtime -PathType Leaf)) {
    [Console]::Out.Write("{}")
    exit 0
}

$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $runtime
$startInfo.Arguments = "-E `"$script`""
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardInput = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true

$child = New-Object System.Diagnostics.Process
$child.StartInfo = $startInfo
if (-not $child.Start()) {
    [Console]::Out.Write("{}")
    exit 0
}

$inputStream = [Console]::OpenStandardInput()
$inputStream.CopyTo($child.StandardInput.BaseStream)
$child.StandardInput.Close()
$inputStream.Dispose()

$output = $child.StandardOutput.ReadToEnd()
$child.WaitForExit()
if ($output) {
    [Console]::Out.Write($output)
} else {
    [Console]::Out.Write("{}")
}
exit 0
