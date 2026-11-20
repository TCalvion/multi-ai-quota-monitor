param(
    [string]$RepoDir = $PSScriptRoot,
    [string]$ShortcutPath = "$env:USERPROFILE\Desktop\Kvoot.lnk"
)

$ErrorActionPreference = "Stop"

function Test-UncPath([string]$PathValue) {
    if (-not $PathValue) { return $false }
    $trimmed = $PathValue.Trim()
    if ($trimmed -match '^(?:Microsoft\.PowerShell\.Core\\FileSystem::)?(\\\\|//)') {
        return $true
    }
    $uri = $null
    if ([System.Uri]::TryCreate($trimmed, [System.UriKind]::Absolute, [ref]$uri) -and $uri.IsUnc) {
        return $true
    }
    return $false
}

if (Test-UncPath $RepoDir) {
    throw "Refusing to create shortcut from UNC path '$RepoDir'. Run this from a local Windows folder (e.g. C:\...), not from a WSL/network path."
}
if (Test-Path -LiteralPath $RepoDir) {
    $resolvedRepo = (Resolve-Path -LiteralPath $RepoDir).Path
    if (Test-UncPath $resolvedRepo) {
        throw "Refusing to create shortcut from UNC path '$resolvedRepo'. Run this from a local Windows folder (e.g. C:\...), not from a WSL/network path."
    }
    $RepoDir = $resolvedRepo
}

$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)
if (-not $pyw) {
    $cand = Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\pythonw.exe"
    if (Test-Path $cand) {
        $pyw = $cand
    } else {
        throw "pythonw.exe not found"
    }
}

$targetScript = Join-Path $RepoDir "kvoot_ava.pyw"
$iconFile = Join-Path $RepoDir "kvoot.ico"
if (-not (Test-Path $targetScript)) { throw "Missing file: $targetScript" }
if (-not (Test-Path $iconFile)) { throw "Missing icon: $iconFile" }

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($ShortcutPath)
$sc.TargetPath = $pyw
$sc.Arguments = "`"$targetScript`""
$sc.WorkingDirectory = $RepoDir
$sc.IconLocation = "$iconFile,0"
$sc.Description = "Multi-AI Quota Monitor (Codex, Claude, Antigravity)"
$sc.Save()

Write-Output "Shortcut created: $ShortcutPath -> $targetScript (icon: $iconFile)"
