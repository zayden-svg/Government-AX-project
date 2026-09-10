@echo off
cd /d "C:\Users\STCLAB\Desktop\업무\업무지원\정부 IT 사업 AI 분석 시스템\Gov-Tracker"
call venv\Scripts\activate.bat
python gov_tracker.py >> run_log.txt 2>&1
