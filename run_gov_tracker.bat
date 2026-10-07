@echo off
REM 로컬 PC에서 자동수집 전체를 실행 (GitHub 서버가 해외라 일부 사이트가 막힐 때의 대안)
REM Windows 작업 스케줄러에 매일 08:00으로 등록해서 사용. 키는 같은 폴더의 .env 파일에 넣어 둠.
cd /d "%~dp0"
if exist venv\Scripts\activate.bat call venv\Scripts\activate.bat
set PYTHONIOENCODING=utf-8
python check_setup.py >> run_log.txt 2>&1
python main.py >> run_log.txt 2>&1
python briefing_batch.py >> run_log.txt 2>&1
python trend_batch.py >> run_log.txt 2>&1
python alert_mailer.py >> run_log.txt 2>&1
