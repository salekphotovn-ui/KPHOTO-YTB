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
