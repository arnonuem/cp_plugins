<#
.SYNOPSIS
    Deploy plugins from this monorepo into the Code Puppy user plugin dir.

.DESCRIPTION
    Copies a plugin folder to $env:USERPROFILE\.code_puppy\plugins\<name>\.

    Run WITHOUT arguments for an interactive picker: it lists every plugin
    in the repo, shows whether each one is already deployed, and lets you
    choose some or all of them. -Name and -All stay available for scripted
    and non-interactive use.

    Only source files are deployed: *.py at the top level of the plugin
    folder, plus README.md. Tests, __pycache__ and everything else stay
    behind -- Code Puppy loads register_callbacks.py from that directory,
    so shipping test files there would put them on the plugin import path.

    Orphaned *.py files in the target (left over from a rename in an
    earlier deploy) are removed. Without that, a renamed module keeps
    living in the target and keeps getting imported.

    Code Puppy loads plugins at startup, so restart it after deploying.

.PARAMETER Name
    One or more plugin folder names to deploy. Skips the picker.

.PARAMETER All
    Deploy every plugin folder in the repo (any folder containing a
    register_callbacks.py). Skips the picker.

.PARAMETER WhatIf
    Show what would happen without touching the filesystem.

.EXAMPLE
    .\deploy.ps1
    Interactive picker.

.EXAMPLE
    .\deploy.ps1 user_msg_style

.EXAMPLE
    .\deploy.ps1 discord user_msg_style

.EXAMPLE
    .\deploy.ps1 -All -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true, DefaultParameterSetName = 'Interactive')]
param(
    [Parameter(ParameterSetName = 'Named', Position = 0, Mandatory = $true)]
    [string[]] $Name,

    [Parameter(ParameterSetName = 'All', Mandatory = $true)]
    [switch] $All
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = $PSScriptRoot
$TargetRoot = Join-Path $env:USERPROFILE '.code_puppy\plugins'

function Write-Failure {
    <#
        Plain one-line failure on stderr.

        Write-Error would wrap the message in a PowerShell error record --
        call site, category, FullyQualifiedErrorId -- which buries the one
        line the user needs. These are expected user errors (a typo in a
        plugin name), not exceptions, so they get a plain message and a
        non-zero exit code.
    #>
    param([string] $Message)

    $Host.UI.WriteErrorLine("ERROR: $Message")
}

function Get-PluginFolders {
    Get-ChildItem -Path $RepoRoot -Directory |
        Where-Object { Test-Path (Join-Path $_.FullName 'register_callbacks.py') } |
        Sort-Object Name
}

function Test-PluginName {
    param([string] $PluginName)

    # A bare folder name, nothing else. Both Join-Path calls below derive
    # from this value, and the target one feeds a destructive block
    # (Remove-Item on orphaned *.py, Remove-Item -Recurse on __pycache__).
    # A name that is a valid source folder AND contains a traversal segment
    # would resolve outside the plugins root and delete there.
    if ([string]::IsNullOrWhiteSpace($PluginName)) {
        Write-Failure "Empty plugin name."
        return $null
    }
    if ([System.IO.Path]::GetFileName($PluginName) -ne $PluginName) {
        Write-Failure "Plugin name must be a bare folder name (got '$PluginName')."
        return $null
    }

    $source = Join-Path $RepoRoot $PluginName

    if (-not (Test-Path -Path $source -PathType Container)) {
        Write-Failure "No such plugin folder: '$PluginName' (looked in $RepoRoot)"
        return $null
    }
    if (-not (Test-Path -Path (Join-Path $source 'register_callbacks.py') -PathType Leaf)) {
        Write-Failure "'$PluginName' has no register_callbacks.py -- not a plugin, nothing copied."
        return $null
    }
    return $source
}

function Test-TargetInsideRoot {
    <#
        Belt and braces to the bare-name check: assert the resolved target
        really sits under the plugins root before anything is deleted.
    #>
    param([string] $Target)

    $full = [System.IO.Path]::GetFullPath($Target)
    $root = [System.IO.Path]::GetFullPath($TargetRoot)
    if (-not $root.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {
        $root += [System.IO.Path]::DirectorySeparatorChar
    }
    return $full.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-DeployState {
    <#
        One line of context per plugin for the picker: is it already in the
        target, and how many .py files does it ship. Purely informational --
        the deploy itself never reads this.
    #>
    param([System.IO.DirectoryInfo] $Folder)

    $target = Join-Path $TargetRoot $Folder.Name
    $count = @(Get-ChildItem -Path $Folder.FullName -File -Filter '*.py').Count

    if (Test-Path -Path $target -PathType Container) {
        $state = 'deployed'
    }
    else {
        $state = 'not deployed'
    }

    return [pscustomobject]@{
        Name  = $Folder.Name
        State = $state
        Files = $count
    }
}

function Select-PluginsInteractively {
    <#
        Numbered picker. Read-Host rather than Out-GridView: this script is
        usually run from the same console the operator is already in, and a
        popup grid that can open BEHIND the window is a worse experience
        than four lines of text.
    #>
    $folders = @(Get-PluginFolders)

    if ($folders.Count -eq 0) {
        Write-Failure "No plugin folders found in $RepoRoot"
        return $null
    }

    Write-Host ''
    Write-Host "Plugins in $RepoRoot" -ForegroundColor Cyan
    Write-Host ''

    $index = 1
    foreach ($folder in $folders) {
        $info = Get-DeployState -Folder $folder
        $mark = if ($info.State -eq 'deployed') { '*' } else { ' ' }
        Write-Host ("  {0} [{1}] {2,-20} {3,2} files, {4}" -f $mark, $index, $info.Name, $info.Files, $info.State)
        $index++
    }

    Write-Host ''
    Write-Host '  * = already present in the target' -ForegroundColor DarkGray
    Write-Host ''
    Write-Host '  Enter numbers (e.g. 1 3), "a" for all, "q" to quit.'
    Write-Host ''

    $answer = Read-Host 'Deploy'
    if ($null -eq $answer) { $answer = '' }
    $answer = $answer.Trim()

    if ($answer -eq '' -or $answer -eq 'q' -or $answer -eq 'Q') {
        Write-Host 'Nothing selected.'
        return @()
    }

    if ($answer -eq 'a' -or $answer -eq 'A') {
        return @($folders | ForEach-Object { $_.Name })
    }

    $picked = New-Object System.Collections.Generic.List[string]
    $tokens = $answer -split '[,\s]+' | Where-Object { $_ -ne '' }

    foreach ($token in $tokens) {
        $number = 0
        if (-not [int]::TryParse($token, [ref] $number)) {
            Write-Failure "Not a number: '$token'"
            return $null
        }
        if ($number -lt 1 -or $number -gt $folders.Count) {
            Write-Failure "Out of range: $number (1..$($folders.Count))"
            return $null
        }
        $chosen = $folders[$number - 1].Name
        if (-not $picked.Contains($chosen)) {
            $picked.Add($chosen)
        }
    }

    return @($picked)
}

function Deploy-Plugin {
    param([string] $PluginName)

    $source = Test-PluginName -PluginName $PluginName
    if (-not $source) { return $false }

    $target = Join-Path $TargetRoot $PluginName

    if (-not (Test-TargetInsideRoot -Target $target)) {
        Write-Failure "Refusing to deploy outside $TargetRoot (resolved: $target)"
        return $false
    }

    # Top level only: a plugin's own subfolders (tests/) are never deployed.
    $files = @(Get-ChildItem -Path $source -File |
        Where-Object { $_.Extension -eq '.py' -or $_.Name -eq 'README.md' })

    if ($files.Count -eq 0) {
        Write-Failure "'$PluginName' has no deployable files."
        return $false
    }

    Write-Host "$PluginName -> $target"

    if (-not (Test-Path -Path $target)) {
        if ($PSCmdlet.ShouldProcess($target, 'Create directory')) {
            New-Item -Path $target -ItemType Directory -Force | Out-Null
        }
    }

    foreach ($file in $files) {
        if ($PSCmdlet.ShouldProcess((Join-Path $target $file.Name), 'Copy')) {
            Copy-Item -Path $file.FullName -Destination $target -Force
            Write-Host "    copy    $($file.Name)"
        }
    }

    # Remove *.py in the target that no longer exist in the source.
    if (Test-Path -Path $target) {
        $keep = $files | ForEach-Object { $_.Name }
        $orphans = @(Get-ChildItem -Path $target -File -Filter '*.py' |
            Where-Object { $keep -notcontains $_.Name })
        foreach ($orphan in $orphans) {
            if ($PSCmdlet.ShouldProcess($orphan.FullName, 'Remove orphan')) {
                Remove-Item -Path $orphan.FullName -Force
                Write-Host "    remove  $($orphan.Name) (orphan)"
            }
        }

        # A stale __pycache__ can shadow a removed module.
        $cache = Join-Path $target '__pycache__'
        if (Test-Path -Path $cache) {
            if ($PSCmdlet.ShouldProcess($cache, 'Remove __pycache__')) {
                Remove-Item -Path $cache -Recurse -Force
                Write-Host '    remove  __pycache__'
            }
        }
    }

    return $true
}

# --- pick what to deploy -----------------------------------------------

switch ($PSCmdlet.ParameterSetName) {
    'All' {
        $names = @(Get-PluginFolders | ForEach-Object { $_.Name })
        if ($names.Count -eq 0) {
            Write-Failure "No plugin folders found in $RepoRoot"
            exit 1
        }
    }
    'Named' {
        $names = @($Name)
    }
    default {
        # Read-Host on a redirected stdin returns empty forever, which would
        # look like "user picked nothing" instead of "there is nobody to ask".
        if ([System.Console]::IsInputRedirected) {
            Write-Failure 'No console for the picker. Use -Name <plugin> or -All.'
            exit 1
        }

        $names = Select-PluginsInteractively
        if ($null -eq $names) { exit 1 }
        if ($names.Count -eq 0) { exit 0 }

        Write-Host ''
    }
}

# --- deploy ------------------------------------------------------------

$failed = 0
foreach ($pluginName in $names) {
    if (-not (Deploy-Plugin -PluginName $pluginName)) { $failed++ }
}

if ($failed -gt 0) {
    Write-Host ''
    Write-Failure "$failed plugin(s) could not be deployed."
    exit 1
}

Write-Host ''
Write-Host 'Done. Restart Code Puppy to load the changes.'
exit 0
