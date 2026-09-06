@echo off
cd /d C:\Users\user\repos\grocery-tracker
call .venv\Scripts\activate.bat
python main.py update
python main.py categorize
python main.py export csv
python main.py history_append