@echo off
chcp 65001 >nul
title Compilation — Gestionnaire de serveur
echo.
echo  ================================================
echo    Compilation du Gestionnaire de serveur
echo  ================================================
echo.

:: Vérifier Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERREUR : Python n'est pas installé.
    echo  Téléchargez-le sur https://www.python.org
    echo  Cochez bien "Add Python to PATH" lors de l'installation.
    echo.
    pause
    exit /b 1
)

echo  [1/3] Installation des dépendances...
python -m pip install customtkinter pyinstaller --quiet --upgrade

echo  [2/3] Compilation (patientez 1 à 2 minutes)...
pyinstaller --onefile --windowed ^
  --name "Gestionnaire de serveur" ^
  --hidden-import customtkinter ^
  --hidden-import PIL ^
  --hidden-import PIL._tkinter_finder ^
  --collect-all customtkinter ^
  doublon_finder.py

echo.
if exist "dist\Gestionnaire de serveur.exe" (
    echo  [3/3] Terminé !
    echo.
    echo  Le fichier se trouve ici :
    echo    %cd%\dist\Gestionnaire de serveur.exe
    echo.
    echo  Vous pouvez l'envoyer par mail, WeTransfer ou clé USB.
    echo  Votre destinataire n'a besoin d'installer rien du tout.
) else (
    echo  [3/3] La compilation a rencontré une erreur.
    echo  Regardez les messages ci-dessus pour le détail.
)
echo.
pause
