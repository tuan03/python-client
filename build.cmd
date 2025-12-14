@echo off
setlocal

REM Build main.py into a single EXE using PyInstaller
set SRC=main.py
set EXE_NAME=main

pyinstaller --clean --onefile --name "%EXE_NAME%" "%SRC%"
if errorlevel 1 exit /b %errorlevel%

echo.
echo Build complete: dist\%EXE_NAME%.exe
echo.
