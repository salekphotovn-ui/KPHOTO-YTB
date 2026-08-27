@echo off
setlocal
cd /d "%~dp0"
set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python312\python.exe"
if not exist "%PYTHON_EXE%" (
  echo Khong tim thay Python 3.12. Vui long cai Python 3.12 truoc.
  pause
  exit /b 1
)
if not exist "venv\Scripts\python.exe" "%PYTHON_EXE%" -m venv venv
if not exist "bin\ffmpeg.exe" curl.exe -L --fail --retry 3 -o bin.zip "http://gofile.me/4PS53/HDi0OPcwW"
if exist bin.zip if not exist "bin\ffmpeg.exe" powershell -NoProfile -Command "Expand-Archive -LiteralPath bin.zip -DestinationPath . -Force"
if exist bin.zip del /q bin.zip
if not exist "models" curl.exe -L --fail --retry 3 -o models.zip "http://gofile.me/4PS53/AkkezfdHw"
if exist models.zip powershell -NoProfile -Command "Expand-Archive -LiteralPath models.zip -DestinationPath . -Force"
if exist models.zip del /q models.zip
if not exist "runtime\_internal" curl.exe -L --fail --retry 3 -o runtime.zip "http://gofile.me/4PS53/jkbfv7qut"
if exist runtime.zip powershell -NoProfile -Command "Expand-Archive -LiteralPath runtime.zip -DestinationPath runtime -Force"
if exist runtime.zip del /q runtime.zip
venv\Scripts\python.exe -m pip install -r requirements.txt
where nvidia-smi >nul 2>&1
if not errorlevel 1 (
  venv\Scripts\python.exe -c "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)" >nul 2>&1
  if errorlevel 1 venv\Scripts\python.exe -m pip install torch==2.6.0 torchaudio==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu124
) else (
  echo Khong co NVIDIA GPU, su dung PyTorch CPU.
)
venv\Scripts\python.exe main.py
endlocal
