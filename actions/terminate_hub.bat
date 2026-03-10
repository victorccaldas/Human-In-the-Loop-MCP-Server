@echo off
REM ============================================================
REM  Terminate all running HITL MCP Shared Telegram Hub processes
REM  and clean up their descriptor files.

REM echos can NOT contain . at the end, or else the bug ". was unexpected at this time" may occur
REM ============================================================

setlocal enabledelayedexpansion

set "HUB_ROOT=%LOCALAPPDATA%\hitl-mcp-server\shared-telegram"
set "FOUND=0"

echo starting 

if not exist "%HUB_ROOT%" (
    echo No hub runtime directory found at %HUB_ROOT%
    echo No hub processes to terminate
    goto :done
)

echo Searching for hub descriptors in %HUB_ROOT% ...

for /r "%HUB_ROOT%" %%F in (hub-descriptor.json) do (
    if exist "%%F" (
        set "FOUND=1"
        echo.
        echo Found descriptor: %%F

        REM Extract PID from JSON using PowerShell
        for /f "usebackq" %%P in (`powershell -NoProfile -Command "(Get-Content '%%F' | ConvertFrom-Json).pid"`) do (
            set "HUB_PID=%%P"
        )

        if defined HUB_PID (
            echo   Hub PID: !HUB_PID!

            REM Check if the process is running
            tasklist /fi "PID eq !HUB_PID!" 2>nul | find "!HUB_PID!" >nul
            if !errorlevel! equ 0 (
                echo   Terminating process !HUB_PID! ...
                taskkill /F /PID !HUB_PID! >nul 2>&1
                if !errorlevel! equ 0 (
                    echo   Process !HUB_PID! terminated successfully
                ) else (
                    echo   WARNING: Failed to terminate process !HUB_PID!
                )
            ) else (
                echo   Process !HUB_PID! is not running (stale descriptor)
            )
        ) else (
            echo   WARNING: Could not read PID from descriptor
        )

        REM Delete the descriptor file
        del "%%F" 2>nul
        if not exist "%%F" (
            echo   Descriptor file removed
        ) else (
            echo   WARNING: Could not remove descriptor file
        )

        set "HUB_PID="
    )
)

if "!FOUND!"=="0" (
    echo No hub descriptors found. No hub processes to terminate
)

:done
echo.
echo Done
endlocal
