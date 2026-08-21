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
"venv\Scripts\python.exe" main.py
popd
endlocal
