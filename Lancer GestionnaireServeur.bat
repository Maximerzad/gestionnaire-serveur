@echo off
chcp 65001 >nul
title Gestionnaire de serveur

:: Vérifier Python
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  Python n'est pas installé sur cet ordinateur.
    echo.
    echo  Étapes à suivre UNE SEULE FOIS :
    echo    1. Allez sur https://www.python.org/downloads/
    echo    2. Cliquez "Download Python"
    echo    3. Lors de l'installation, COCHEZ "Add Python to PATH"
    echo    4. Relancez ce fichier une fois l'installation terminée.
    echo.
    pause
    exit /b 1
)

:: Installer les dépendances si besoin (silencieux)
python -m pip install customtkinter --quiet 2>nul

:: Lancer l'application
python doublon_finder.py
