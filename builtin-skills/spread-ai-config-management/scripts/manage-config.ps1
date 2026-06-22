<#
Windows wrapper for the cross-platform Spread AI configuration management REST client.
The core implementation lives in manage-config.mjs and runs with Node.
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0, Mandatory = $true)]
  [ValidateSet('agent', 'mcp', 'llm-model', 'llm-provider', 'llm-policy', 'prompt', 'skill', 'tool')]
  [string]$Resource,

  [Parameter(Position = 1, Mandatory = $true)]
  [ValidateSet('list', 'get', 'create', 'update', 'patch', 'delete', 'enable', 'disable', 'resolved', 'reorder')]
  [string]$Action,

  [string]$Id,
  [string]$SubPath,
  [string]$File,
  [string]$RuntimeUrl = $env:SPREAD_AI_RUNTIME_URL,
  [string]$TokenFile,
  [switch]$TokenStdin,
  [string]$AuthFile,
  [string]$LoginFile = $env:SPREAD_AI_RUNTIME_LOGIN_FILE
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Fail-Json {
  param([string]$Message, [int]$ExitCode = 1)
  [Console]::Error.WriteLine((@{ error = $Message } | ConvertTo-Json -Depth 20))
  exit $ExitCode
}

function Add-OptionalArgument {
  param(
    [System.Collections.Generic.List[string]]$Arguments,
    [string]$Name,
    [string]$Value
  )
  if (-not [string]::IsNullOrWhiteSpace($Value)) {
    $Arguments.Add($Name)
    $Arguments.Add($Value)
  }
}

function Invoke-Node {
  param([string]$ScriptPath, [string[]]$Arguments)
  if (-not [string]::IsNullOrWhiteSpace($env:SPREAD_AI_CONFIG_MANAGEMENT_NODE)) {
    & $env:SPREAD_AI_CONFIG_MANAGEMENT_NODE $ScriptPath @Arguments
    exit $LASTEXITCODE
  }
  $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
  if ($null -ne $nodeCommand) {
    & $nodeCommand.Source $ScriptPath @Arguments
    exit $LASTEXITCODE
  }
  Fail-Json 'Node.js is required to run this cross-platform management Skill script. Install Node or set SPREAD_AI_CONFIG_MANAGEMENT_NODE.'
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$coreScript = Join-Path $scriptDir 'manage-config.mjs'
if (-not (Test-Path -LiteralPath $coreScript)) {
  Fail-Json "Core script not found: $coreScript"
}

$forwardArgs = [System.Collections.Generic.List[string]]::new()
$forwardArgs.Add($Resource)
$forwardArgs.Add($Action)
Add-OptionalArgument -Arguments $forwardArgs -Name '--id' -Value $Id
Add-OptionalArgument -Arguments $forwardArgs -Name '--sub-path' -Value $SubPath
Add-OptionalArgument -Arguments $forwardArgs -Name '--file' -Value $File
Add-OptionalArgument -Arguments $forwardArgs -Name '--runtime-url' -Value $RuntimeUrl
Add-OptionalArgument -Arguments $forwardArgs -Name '--token-file' -Value $TokenFile
Add-OptionalArgument -Arguments $forwardArgs -Name '--auth-file' -Value $AuthFile
Add-OptionalArgument -Arguments $forwardArgs -Name '--login-file' -Value $LoginFile
if ($TokenStdin) {
  $forwardArgs.Add('--token-stdin')
}

Invoke-Node -ScriptPath $coreScript -Arguments $forwardArgs.ToArray()
