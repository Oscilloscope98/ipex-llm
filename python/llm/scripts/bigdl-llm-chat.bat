@echo off

set PYTHONUNBUFFERED=1

set /p modelpath="Please enter the model path: "
python chat.py --model-path="%modelpath%"