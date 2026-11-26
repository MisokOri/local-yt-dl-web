@echo off
setlocal

cd /d "%~dp0"

set "VENV_DIR=venv"
set "REQ_FILE=requirements.txt"
set "APP_FILE=app.py"
set "URL=http://localhost:5000"

REM 1. Cek apakah venv sudah ada; jika belum, buat baru di folder saat ini
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Membuat virtual environment "%VENV_DIR%"...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo Gagal membuat venv. Pastikan Python terpasang dan centang "Add Python to PATH".
        pause
        exit /b 1
    )
)

set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

REM 2. Install requirement jika file requirements.txt ada
if exist "%REQ_FILE%" (
    echo Menginstal dependensi dari %REQ_FILE%...
    "%VENV_PYTHON%" -m pip install --upgrade pip
    "%VENV_PYTHON%" -m pip install -r "%REQ_FILE%"
    if errorlevel 1 (
        echo Gagal menginstal requirement.
        pause
        exit /b 1
    )
)

REM 3. Buka browser ke localhost:5000 setelah jeda 2 detik
start "" cmd /c "timeout /t 5 /nobreak >nul & start "" "%URL%""

REM 4. Jalankan app.py di folder saat ini
if not exist "%APP_FILE%" (
    echo File %APP_FILE% tidak ditemukan di folder ini.
    pause
    exit /b 1
)

echo Menjalankan %APP_FILE%...
"%VENV_PYTHON%" "%APP_FILE%"
pause

endlocal