@echo off
setlocal
set "PROJECT_DIR=%~dp0"
set "PYTHON_EXE=C:\Users\Windows\AppData\Local\Programs\Python\Python312\python.exe"
pushd "%PROJECT_DIR%"

if not exist "%PYTHON_EXE%" (
    echo Khong tim thay Python 3.12.
    pause
    exit /b 1
)
if not exist "venv\Scripts\python.exe" (
    "%PYTHON_EXE%" -m venv venv
)
"venv\Scripts\python.exe" -c "import PyQt6" >nul 2>&1
if errorlevel 1 "venv\Scripts\python.exe" -m pip install -r requirements.txt
where nvidia-smi >nul 2>&1
if not errorlevel 1 (
    echo Phat hien NVIDIA GPU - dang kiem tra PyTorch CUDA...
    "venv\Scripts\python.exe" -c "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)" >nul 2>&1
    if errorlevel 1 (
        echo Dang cai PyTorch CUDA 12.4...
        "venv\Scripts\python.exe" -m pip install torch==2.6.0 torchaudio==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu124
    )
) else (
    echo Khong phat hien NVIDIA GPU - dung PyTorch CPU.
)
"venv\Scripts\python.exe" main.py
popd
endlocal
