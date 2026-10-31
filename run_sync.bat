@echo off
REM Double-click to run the Kroll sync once. Windows Task Scheduler can run this every hour.
cd /d "%~dp0"
python kroll_inventory_sync.py >> sync_log.txt 2>&1
