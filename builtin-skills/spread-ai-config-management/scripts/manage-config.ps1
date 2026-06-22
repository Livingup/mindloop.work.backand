<#
Spread AI built-in configuration management REST client.
This script is intentionally thin: it calls the local Runtime REST API and does not write storage directly.
,#>
[CmdletBinding()]
param(
  [Parameter(Position = 0, Mandatory = $true)]
  [ValidateSet('agent', 'mcp', 'llm-model', 'llm-provider', 'llm-policy', 'prompt', 'skill')]
  [string]$Resource,

  [Parameter(Position = 1, Mandatory = $true)]
  [ValidateSet('list', 'get', 'create', 'update', 'patch', 'delete', 'enable', 'disable', 'resolved', 'reorder')]
  [string]$Action,

  [string]$Id,
  [string]$SubPath,
  [string]$File,
  [string]$RuntimeUrl = $env:SPREAD_AI_RUNTIME_URL,
  [string]$TokenFile = $env:SPREAD_AI_RUNTIME_TOKEN_FILE,
  [switch]$TokenStdin,
  [string]$AuthFile = $env:SPREAD_AI_RUNTIME_AUTH_FILE
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Write-JsonOutput {
  param([object]$Value)
  if ($null -eq $Value) {
    '{}' | Write-Output
    return
  }
  $Value | ConvertTo-Json -Depth 100 | Write-Output
}

function Fail-Json {
  param([string]$Message, [int]$ExitCode = 1)
  [Console]::Error.WriteLine((@{ error = $Message } | ConvertTo-Json -Depth 20))
  exit $ExitCode
}

function Get-RuntimeUrl {
  if ([string]::IsNullOrWhiteSpace($RuntimeUrl)) {
    return 'http://127.0.0.1:8000'
  }
  return $RuntimeUrl.TrimEnd('/')
}

function Get-AuthToken {
  if (-not [string]::IsNullOrWhiteSpace($TokenFile)) {
    if (-not (Test-Path -LiteralPath $TokenFile)) {
      Fail-Json "Token file not found: $TokenFile"
    }
    return (Get-Content -LiteralPath $TokenFile -Raw).Trim()
  }
  if ($TokenStdin) {
    return ([Console]::In.ReadToEnd()).Trim()
  }
  if (-not [string]::IsNullOrWhiteSpace($AuthFile)) {
    if (-not (Test-Path -LiteralPath $AuthFile)) {
      Fail-Json "Auth file not found: $AuthFile"
    }
    $auth = Get-Content -LiteralPath $AuthFile -Raw | ConvertFrom-Json
    if ($auth.access_token) { return [string]$auth.access_token }
    if ($auth.token) { return [string]$auth.token }
    Fail-Json "Auth file must contain access_token or token."
  }
  if (-not [string]::IsNullOrWhiteSpace($env:SPREAD_AI_RUNTIME_TOKEN)) {
    return $env:SPREAD_AI_RUNTIME_TOKEN
  }
  return $null
}

function Get-ResourcePath {
  switch ($Resource) {
    'agent' { return '/api/agents' }
    'mcp' { return '/api/mcp-config' }
    'llm-model' { return '/api/llm-registry/models' }
    'llm-provider' { return '/api/llm-registry/providers' }
    'llm-policy' { return '/api/llm-registry/execution-policies' }
    'prompt' { return '/api/prompt-snippets' }
    'skill' { return '/api/skills' }
  }
  Fail-Json "Unsupported resource: $Resource"
}

function Read-Payload {
  if ([string]::IsNullOrWhiteSpace($File)) {
    return $null
  }
  if ($File -eq '-') {
    $text = [Console]::In.ReadToEnd()
  } else {
    if (-not (Test-Path -LiteralPath $File)) {
      Fail-Json "Payload file not found: $File"
    }
    $text = Get-Content -LiteralPath $File -Raw
  }
  if ([string]::IsNullOrWhiteSpace($text)) {
    Fail-Json 'Payload file/stdin is empty.'
  }
  try {
    $null = $text | ConvertFrom-Json
    return $text
  } catch {
    # Fall through to YAML parsing. The REST API still receives JSON.
  }
  try {
    $payloadObject = ConvertFrom-YamlPayload -Text $text
    return ($payloadObject | ConvertTo-Json -Depth 100)
  } catch {
    Fail-Json ("Payload must be JSON or YAML: " + $_.Exception.Message)
  }
}

function ConvertFrom-YamlPayload {
  param([string]$Text)
  $yamlCommand = Get-Command ConvertFrom-Yaml -ErrorAction SilentlyContinue
  if ($null -ne $yamlCommand) {
    return ($Text | ConvertFrom-Yaml)
  }
  return ConvertFrom-SimpleYaml -Text $Text
}

function ConvertFrom-SimpleYaml {
  param([string]$Text)
  $lines = @($Text -split "`r?`n")
  $index = 0
  $result = ConvertFrom-SimpleYamlBlock -Lines $lines -Index ([ref]$index) -Indent 0
  Move-ToNextYamlContent -Lines $lines -Index ([ref]$index)
  if ($index -lt $lines.Count) {
    throw "Unsupported YAML structure near line $($index + 1)."
  }
  return $result
}

function Move-ToNextYamlContent {
  param([string[]]$Lines, [ref]$Index)
  while ($Index.Value -lt $Lines.Count) {
    $trimmed = $Lines[$Index.Value].Trim()
    if (-not [string]::IsNullOrWhiteSpace($trimmed) -and -not $trimmed.StartsWith('#')) {
      return
    }
    $Index.Value += 1
  }
}

function Get-YamlIndent {
  param([string]$Line)
  return ([regex]::Match($Line, '^\s*')).Value.Length
}

function Get-NextYamlContentIndent {
  param([string[]]$Lines, [int]$StartIndex)
  for ($i = $StartIndex; $i -lt $Lines.Count; $i += 1) {
    $trimmed = $Lines[$i].Trim()
    if (-not [string]::IsNullOrWhiteSpace($trimmed) -and -not $trimmed.StartsWith('#')) {
      return Get-YamlIndent $Lines[$i]
    }
  }
  return -1
}

function ConvertFrom-SimpleYamlBlock {
  param([string[]]$Lines, [ref]$Index, [int]$Indent)
  Move-ToNextYamlContent -Lines $Lines -Index $Index
  if ($Index.Value -ge $Lines.Count) { return $null }
  $lineIndent = Get-YamlIndent $Lines[$Index.Value]
  if ($lineIndent -lt $Indent) { return $null }
  $trimmed = $Lines[$Index.Value].Substring($lineIndent).TrimEnd()
  if ($trimmed.StartsWith('-')) {
    return ConvertFrom-SimpleYamlList -Lines $Lines -Index $Index -Indent $lineIndent
  }
  return ConvertFrom-SimpleYamlMap -Lines $Lines -Index $Index -Indent $lineIndent
}

function ConvertFrom-SimpleYamlMap {
  param([string[]]$Lines, [ref]$Index, [int]$Indent)
  $map = [ordered]@{}
  while ($true) {
    Move-ToNextYamlContent -Lines $Lines -Index $Index
    if ($Index.Value -ge $Lines.Count) { break }
    $line = $Lines[$Index.Value]
    $lineIndent = Get-YamlIndent $line
    if ($lineIndent -lt $Indent) { break }
    if ($lineIndent -ne $Indent) { break }
    $trimmed = $line.Substring($lineIndent).TrimEnd()
    if ($trimmed.StartsWith('-')) { break }
    $match = [regex]::Match($trimmed, '^([^:]+):(.*)$')
    if (-not $match.Success) {
      throw "Expected key/value YAML mapping near line $($Index.Value + 1)."
    }
    $key = ConvertFrom-YamlKey $match.Groups[1].Value
    $rest = $match.Groups[2].Value.Trim()
    $Index.Value += 1
    $map[$key] = Read-YamlValue -Rest $rest -Lines $Lines -Index $Index -ParentIndent $lineIndent
  }
  return $map
}

function ConvertFrom-SimpleYamlList {
  param([string[]]$Lines, [ref]$Index, [int]$Indent)
  $list = New-Object System.Collections.ArrayList
  while ($true) {
    Move-ToNextYamlContent -Lines $Lines -Index $Index
    if ($Index.Value -ge $Lines.Count) { break }
    $line = $Lines[$Index.Value]
    $lineIndent = Get-YamlIndent $line
    if ($lineIndent -lt $Indent) { break }
    if ($lineIndent -ne $Indent) { break }
    $trimmed = $line.Substring($lineIndent).TrimEnd()
    if (-not $trimmed.StartsWith('-')) { break }
    $itemText = $trimmed.Substring(1).TrimStart()
    $Index.Value += 1
    if ([string]::IsNullOrWhiteSpace($itemText)) {
      $nextIndent = Get-NextYamlContentIndent -Lines $Lines -StartIndex $Index.Value
      if ($nextIndent -gt $lineIndent) {
        $item = ConvertFrom-SimpleYamlBlock -Lines $Lines -Index $Index -Indent $nextIndent
      } else {
        $item = $null
      }
    } else {
      $match = [regex]::Match($itemText, '^([^:]+):(.*)$')
      if ($match.Success) {
        $item = [ordered]@{}
        $key = ConvertFrom-YamlKey $match.Groups[1].Value
        $rest = $match.Groups[2].Value.Trim()
        $item[$key] = Read-YamlValue -Rest $rest -Lines $Lines -Index $Index -ParentIndent $lineIndent
        $nextIndent = Get-NextYamlContentIndent -Lines $Lines -StartIndex $Index.Value
        if ($nextIndent -gt $lineIndent) {
          $extra = ConvertFrom-SimpleYamlMap -Lines $Lines -Index $Index -Indent $nextIndent
          foreach ($extraKey in $extra.Keys) {
            $item[$extraKey] = $extra[$extraKey]
          }
        }
      } else {
        $item = ConvertFrom-YamlScalar $itemText
      }
    }
    [void]$list.Add($item)
  }
  return @($list)
}

function Read-YamlValue {
  param([string]$Rest, [string[]]$Lines, [ref]$Index, [int]$ParentIndent)
  if ([string]::IsNullOrWhiteSpace($Rest)) {
    $nextIndent = Get-NextYamlContentIndent -Lines $Lines -StartIndex $Index.Value
    if ($nextIndent -gt $ParentIndent) {
      return ConvertFrom-SimpleYamlBlock -Lines $Lines -Index $Index -Indent $nextIndent
    }
    return $null
  }
  if ($Rest -eq '|' -or $Rest -eq '>') {
    return Read-YamlBlockScalar -Lines $Lines -Index $Index -ParentIndent $ParentIndent -Style $Rest
  }
  return ConvertFrom-YamlScalar $Rest
}

function Read-YamlBlockScalar {
  param([string[]]$Lines, [ref]$Index, [int]$ParentIndent, [string]$Style)
  $scalarLines = New-Object System.Collections.ArrayList
  $stripIndent = $ParentIndent + 2
  while ($Index.Value -lt $Lines.Count) {
    $line = $Lines[$Index.Value]
    if ([string]::IsNullOrWhiteSpace($line)) {
      [void]$scalarLines.Add('')
      $Index.Value += 1
      continue
    }
    $lineIndent = Get-YamlIndent $line
    if ($lineIndent -le $ParentIndent) { break }
    $removeCount = [Math]::Min($stripIndent, $line.Length)
    if ($lineIndent -lt $stripIndent) { $removeCount = $lineIndent }
    [void]$scalarLines.Add($line.Substring($removeCount))
    $Index.Value += 1
  }
  if ($Style -eq '>') {
    return (($scalarLines | ForEach-Object { ([string]$_).Trim() }) -join ' ')
  }
  return ($scalarLines -join "`n")
}

function ConvertFrom-YamlKey {
  param([string]$Value)
  return $Value.Trim().Trim('"').Trim("'")
}

function ConvertFrom-YamlScalar {
  param([string]$Value)
  $trimmed = $Value.Trim()
  if ($trimmed -eq 'null' -or $trimmed -eq '~') { return $null }
  if ($trimmed -eq 'true') { return $true }
  if ($trimmed -eq 'false') { return $false }
  if (($trimmed.StartsWith('"') -and $trimmed.EndsWith('"')) -or ($trimmed.StartsWith("'") -and $trimmed.EndsWith("'"))) {
    return $trimmed.Substring(1, $trimmed.Length - 2)
  }
  if ($trimmed.StartsWith('{') -or $trimmed.StartsWith('[')) {
    try { return ($trimmed | ConvertFrom-Json) } catch { }
  }
  $integerValue = 0
  if ([int64]::TryParse($trimmed, [ref]$integerValue)) { return $integerValue }
  $numberValue = 0.0
  if ([double]::TryParse($trimmed, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$numberValue)) { return $numberValue }
  return $trimmed
}

function Escape-PathSegment {
  param([string]$Value)
  return [System.Uri]::EscapeDataString($Value)
}

function Join-RelativePath {
  param([string]$PrimaryId, [string]$ChildPath = '')
  $segments = New-Object System.Collections.Generic.List[string]
  if (-not [string]::IsNullOrWhiteSpace($PrimaryId)) {
    $segments.Add((Escape-PathSegment $PrimaryId))
  }
  if (-not [string]::IsNullOrWhiteSpace($ChildPath)) {
    foreach ($segment in ($ChildPath -split '[\\/]')) {
      if (-not [string]::IsNullOrWhiteSpace($segment)) {
        $segments.Add((Escape-PathSegment $segment))
      }
    }
  }
  return ($segments -join '/')
}

function Join-Endpoint {
  param([string]$BasePath, [string]$RelativePath = '')
  $root = Get-RuntimeUrl
  if ([string]::IsNullOrWhiteSpace($RelativePath)) {
    return "$root$BasePath"
  }
  if ($RelativePath.StartsWith('?')) {
    return "$root$BasePath$RelativePath"
  }
  return "$root$BasePath/$RelativePath"
}

$basePath = Get-ResourcePath
$headers = @{ Accept = 'application/json' }
$token = Get-AuthToken
if (-not [string]::IsNullOrWhiteSpace($token)) {
  $headers['Authorization'] = "Bearer $token"
}

$method = 'GET'
$relative = ''
$body = $null

switch ($Action) {
  'list' {
    $method = 'GET'
    if ([string]::IsNullOrWhiteSpace($Id)) {
      $relative = '?summary=true'
    } else {
      $relative = Join-RelativePath -PrimaryId $Id -ChildPath $SubPath
    }
  }
  'get' {
    if ([string]::IsNullOrWhiteSpace($Id)) { Fail-Json 'get requires --Id.' }
    $method = 'GET'
    $relative = Join-RelativePath -PrimaryId $Id -ChildPath $SubPath
  }
  'create' {
    if ($Resource -eq 'skill') { Fail-Json 'Skill metadata is read-only.' }
    $method = 'POST'
    if (($Resource -like 'llm-*') -and [string]::IsNullOrWhiteSpace($SubPath)) {
      if ([string]::IsNullOrWhiteSpace($Id)) { Fail-Json 'LLM create/save requires --Id.' }
      $method = 'PUT'
    }
    $relative = Join-RelativePath -PrimaryId $Id -ChildPath $SubPath
    $body = Read-Payload
  }
  'update' {
    if ($Resource -eq 'skill') { Fail-Json 'Skill metadata is read-only.' }
    if ([string]::IsNullOrWhiteSpace($Id)) { Fail-Json 'update requires --Id.' }
    $method = 'PUT'
    if ($Resource -eq 'mcp') { $method = 'PATCH' }
    $relative = Join-RelativePath -PrimaryId $Id -ChildPath $SubPath
    $body = Read-Payload
  }
  'patch' {
    if ($Resource -eq 'skill') { Fail-Json 'Skill metadata is read-only.' }
    if ([string]::IsNullOrWhiteSpace($Id)) { Fail-Json 'patch requires --Id.' }
    $method = 'PATCH'
    $relative = Join-RelativePath -PrimaryId $Id -ChildPath $SubPath
    $body = Read-Payload
  }
  'delete' {
    if ($Resource -eq 'skill') { Fail-Json 'Skill metadata is read-only.' }
    if ([string]::IsNullOrWhiteSpace($Id)) { Fail-Json 'delete requires --Id.' }
    $method = 'DELETE'
    $relative = Join-RelativePath -PrimaryId $Id -ChildPath $SubPath
  }
  'enable' {
    if ($Resource -notin @('agent', 'mcp')) { Fail-Json 'enable is only supported for agent and mcp.' }
    if ([string]::IsNullOrWhiteSpace($Id)) { Fail-Json 'enable requires --Id.' }
    $method = 'POST'
    $relative = ([System.Uri]::EscapeDataString($Id) + '/enable')
  }
  'disable' {
    if ($Resource -notin @('agent', 'mcp')) { Fail-Json 'disable is only supported for agent and mcp.' }
    if ([string]::IsNullOrWhiteSpace($Id)) { Fail-Json 'disable requires --Id.' }
    $method = 'POST'
    $relative = ([System.Uri]::EscapeDataString($Id) + '/disable')
  }
  'resolved' {
    if ($Resource -ne 'llm-policy') { Fail-Json 'resolved is only supported for llm-policy.' }
    if ([string]::IsNullOrWhiteSpace($Id)) { Fail-Json 'resolved requires --Id.' }
    $method = 'GET'
    $relative = Join-RelativePath -PrimaryId $Id -ChildPath 'resolved'
  }
  'reorder' {
    if ($Resource -eq 'skill') { Fail-Json 'Skill metadata is read-only.' }
    if ([string]::IsNullOrWhiteSpace($Id) -or [string]::IsNullOrWhiteSpace($SubPath)) {
      Fail-Json 'reorder requires --Id and --SubPath.'
    }
    $method = 'POST'
    $relative = Join-RelativePath -PrimaryId $Id -ChildPath ($SubPath.TrimEnd('/\\') + '/reorder')
    $body = Read-Payload
  }
}

$uri = Join-Endpoint -BasePath $basePath -RelativePath $relative

try {
  $params = @{ Method = $method; Uri = $uri; Headers = $headers }
  if ($null -ne $body) {
    $params['ContentType'] = 'application/json; charset=utf-8'
    $params['Body'] = $body
  }
  $response = Invoke-RestMethod @params
  if ($method -eq 'DELETE' -and $null -eq $response) {
    Write-JsonOutput @{ status = 'success'; resource = $Resource; id = $Id }
  } else {
    Write-JsonOutput $response
  }
} catch {
  $message = $_.Exception.Message
  if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
    $message = $_.ErrorDetails.Message
  }
  Fail-Json $message
}
