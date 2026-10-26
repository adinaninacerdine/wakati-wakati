#!/usr/bin/env python3
"""
setup_calendar_token.py — relie la GED à Zoho Calendar, SANS toucher à l'existant
================================================================================
Crée un refresh token dédié au calendrier, CALENDAR_REFRESH_TOKEN, et l'écrit
dans zoho_config.py. Le jeton principal REFRESH_TOKEN (WorkDrive, Sheet, Sign)
et CLIQ_REFRESH_TOKEN ne sont pas modifiés : rien de ce qui marche ne casse.

ÉTAPES
------
  1. Connecte-toi à https://api-console.zoho.com avec le compte propriétaire
     du client de la GED (celui qui a servi à setup_main_token.py).
  2. Ouvre le client « Self Client » -> onglet « Generate Code » :
       Scope       : ZohoCalendar.calendar.ALL,ZohoCalendar.event.ALL
       Duration    : 10 minutes
       Description : GED calendrier
     Copie le code.
  3. Dans les 2 minutes, sur ton poste :
       ./venv/bin/python setup_calendar_token.py LE_CODE
  4. Le script sauvegarde zoho_config.py, ajoute CALENDAR_REFRESH_TOKEN,
     puis vérifie le jeton en listant les agendas visibles.
  5. Copie la ligne CALENDAR_REFRESH_TOKEN = "…" dans le zoho_config.py du
     serveur (/opt/ged-wakati/zoho_config.py) et redémarre :
       docker compose restart ged

AGENDAS DES DIRECTEURS
----------------------
Le jeton agit au nom du compte qui a généré le code. Pour écrire directement
dans l'agenda du DG, le DG partage son agenda Zoho avec ce compte (droit :
« Modifier les événements »). Sinon, la GED l'invite par e-mail.
"""

import re
import shutil
import sys
import time
from pathlib import Path

import requests

RACINE = Path(__file__).resolve().parent
CONFIG = RACINE / "zoho_config.py"
SCOPES = "ZohoCalendar.calendar.ALL,ZohoCalendar.event.ALL"


def masquer(t):
    return f"{t[:10]}…{t[-4:]}" if t and len(t) > 16 else "(vide)"


def main():
    if len(sys.argv) < 2 or sys.argv[1].startswith("-"):
        print(__doc__)
        print(f"Scope à saisir dans la console :\n  {SCOPES}")
        return
    code = sys.argv[1].strip()
    sys.path.insert(0, str(RACINE))
    try:
        from zoho_config import ACCOUNTS_URL, CLIENT_ID, CLIENT_SECRET
    except ImportError as exc:
        print(f"ERREUR : zoho_config.py illisible ({exc})")
        sys.exit(1)

    r = requests.post(f"{ACCOUNTS_URL}/oauth/v2/token", params={
        "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code"}, timeout=30)
    data = r.json()
    if "refresh_token" not in data:
        print(f"Échec de l'échange : {data}")
        if data.get("error") == "invalid_code":
            print("-> Code expiré ou déjà utilisé : régénère un code et relance dans les 2 minutes.")
        sys.exit(1)
    jeton = data["refresh_token"]
    portee = data.get("scope", "")
    if portee and "ZohoCalendar" not in portee:
        print(f"ATTENTION : le jeton ne porte pas le scope Calendar ({portee}).")

    sauvegarde = RACINE / f"zoho_config.py.backup-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(CONFIG, sauvegarde)
    contenu = CONFIG.read_text(encoding="utf-8")
    ligne = f'CALENDAR_REFRESH_TOKEN = "{jeton}"'
    # On ne remplace QUE la ligne CALENDAR_REFRESH_TOKEN ; REFRESH_TOKEN et
    # CLIQ_REFRESH_TOKEN restent intacts.
    if re.search(r"^CALENDAR_REFRESH_TOKEN\s*=.*$", contenu, flags=re.M):
        contenu = re.sub(r"^CALENDAR_REFRESH_TOKEN\s*=.*$", ligne, contenu, flags=re.M)
    else:
        contenu = contenu.rstrip("\n") + "\n\n# Zoho Calendar (jeton dédié, voir setup_calendar_token.py)\n" + ligne + "\n"
    CONFIG.write_text(contenu, encoding="utf-8")
    (RACINE / "courrier" / ".zoho_calendar_token.json").unlink(missing_ok=True)

    print(f"Sauvegarde : {sauvegarde.name}")
    print(f"CALENDAR_REFRESH_TOKEN écrit : {masquer(jeton)}")
    print("REFRESH_TOKEN et CLIQ_REFRESH_TOKEN : inchangés.\n")

    try:
        import importlib  # noqa: PLC0415
        import zoho_config  # noqa: PLC0415
        importlib.reload(zoho_config)
        import calendrier  # noqa: PLC0415
        cals = calendrier.lister_calendriers()
        print(f"Vérification OK — {len(cals)} agenda(s) visible(s) :")
        for c in cals:
            print(f"  {c['nom'][:40]:40} {c['categorie']:10} {c['droit']}")
    except Exception as exc:  # noqa: BLE001
        print(f"Vérification impossible : {exc}")
        sys.exit(1)

    print("\nÀ reporter sur le serveur, dans /opt/ged-wakati/zoho_config.py :")
    print(f"  {ligne}")
    print("puis : docker compose restart ged")


if __name__ == "__main__":
    main()
