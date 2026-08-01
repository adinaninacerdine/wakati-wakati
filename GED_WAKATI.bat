@echo off
REM ============================================================
REM  GED WAKATI — lanceur pour la responsable GED
REM ============================================================
REM  Placer ce fichier a la RACINE du projet (a cote de zoho_config.py),
REM  puis creer un raccourci sur le Bureau : clic droit -> Envoyer vers
REM  -> Bureau (creer un raccourci). Renommer le raccourci « GED WAKATI ».
REM
REM  Elle double-clique, le navigateur s'ouvre, tout tourne.
REM  Pour arreter : fermer cette fenetre noire.
REM ============================================================

title GED WAKATI - Ne pas fermer cette fenetre
cd /d "%~dp0"

echo.
echo   ================================================
echo     GED WAKATI
echo   ================================================
echo.
echo   L'application demarre, patientez quelques secondes.
echo   Le navigateur va s'ouvrir automatiquement.
echo.
echo   NE FERMEZ PAS cette fenetre tant que vous
echo   utilisez l'application.
echo.

REM --- Recherche de l'interpreteur Python du projet ---
set PY=
if exist "venv\Scripts\python.exe" set PY=venv\Scripts\python.exe
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

if "%PY%"=="" (
  echo   ERREUR : environnement Python introuvable.
  echo   Le dossier venv\Scripts\ est absent.
  echo.
  echo   Contactez le support technique.
  echo.
  pause
  exit /b 1
)

"%PY%" scripts\app_ged\app.py

echo.
echo   L'application s'est arretee.
pause
