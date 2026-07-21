@echo off
echo Installation des dependances (premier lancement uniquement)...
pip install -r requirements.txt
echo.
echo Lancement de l'application GED WAKATI...
echo. 
echo Fermez cette fenetre pour arreter l'application.
python scripts\app_ged\app.py
pause