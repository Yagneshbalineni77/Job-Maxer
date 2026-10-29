@echo off
rem Creates a Windows scheduled task that runs every day at 9:00 AM:
rem   1. finds new company jobs   2. auto-applies (up to the daily limit in settings.json)
rem   3. checks government sites for new notices
rem A popup shows what happened. Run this file once.
rem To remove it later:  schtasks /Delete /TN "JobBot daily" /F
schtasks /Delete /TN "JobBot daily alerts" /F >nul 2>&1
schtasks /Create /F /SC DAILY /ST 09:00 /TN "JobBot daily" /TR "cmd /c cd /d \"%~dp0\" && python finder.py --notify & python apply_auto.py --notify & python gov_watch.py --notify"
echo.
echo Done. Every day at 9:00 AM the bot finds new jobs, applies, and pops up a summary.
echo The laptop must be on and logged in at 9:00 AM. If it's off, that day is skipped:
echo run start.bat option 1 by hand instead.
pause
