#!/bin/bash
echo "Installation des dépendances (premier lancement uniquement)..."
pip3 install -r requirements.txt
echo ""
echo "Lancement de l'application GED WAKATI..."
echo "Appuyez sur Ctrl+C pour arrêter l'application."
echo ""
python3 scripts/app_ged/app.py