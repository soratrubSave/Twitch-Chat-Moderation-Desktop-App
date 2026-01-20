@echo off
echo Installing requirements...
pip install -r requirements.txt

echo Starting Twitch Bad Word Detector...
python main_gui.py
pause