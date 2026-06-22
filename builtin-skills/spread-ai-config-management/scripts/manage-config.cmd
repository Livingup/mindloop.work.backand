@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "CORE_SCRIPT=%SCRIPT_DIR%manage-config.mjs"
if not exist "%CORE_SCRIPT%" (
  >&2 echo {"error":"Core script not found."}
  exit /b 1
)
if not "%SPREAD_AI_CONFIG_MANAGEMENT_NODE%"=="" (
  "%SPREAD_AI_CONFIG_MANAGEMENT_NODE%" "%CORE_SCRIPT%" %*
  exit /b %ERRORLEVEL%
)
where node >nul 2>nul
if %ERRORLEVEL%==0 (
  node "%CORE_SCRIPT%" %*
  exit /b %ERRORLEVEL%
)
>&2 echo {"error":"Node.js is required to run this cross-platform management Skill script. Install Node or set SPREAD_AI_CONFIG_MANAGEMENT_NODE."}
exit /b 1
