@echo off
rem Always runs the bot in its own folder. Needs "python" on PATH.
cd /d "%~dp0"
:menu
echo.
echo   JOB BOT
echo   1. Auto-apply to company jobs (real applications, uses settings.json)
echo   2. Auto-apply TEST run (fills forms, submits nothing)
echo   3. Find new company jobs (Greenhouse / Lever / Ashby)
echo   4. Assist mode: Naukri / LinkedIn / Wellfound, or any one job yourself
echo   5. Open the auto-apply log (what was sent, what needs you)
echo   6. Open my do-by-hand list (jobs to finish myself)
echo   7. Check government sites for new notices
echo   8. Quit
echo   9. Start the Chrome extension helper (keep that window open while you use it)
set /p choice="Pick: "
if "%choice%"=="1" python apply_auto.py
if "%choice%"=="2" python apply_auto.py --test
if "%choice%"=="3" python finder.py
if "%choice%"=="4" python autofill.py
if "%choice%"=="5" if exist auto_log.csv (start "" auto_log.csv) else (echo   No auto-apply runs yet.)
if "%choice%"=="6" if exist do_by_hand.csv (start "" do_by_hand.csv) else (echo   Nothing to do by hand yet.)
if "%choice%"=="7" python gov_watch.py
if "%choice%"=="8" exit /b
if "%choice%"=="9" start "Job Bot helper" python server.py
goto menu
