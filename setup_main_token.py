"""
setup_main_token.py
===================
Regenere le refresh token PRINCIPAL (zoho_config.py) avec l'ensemble des scopes
de production : WorkDrive + Sheet + Sign.

A utiliser quand un appel renvoie « Invalid OAuth scope » (F7007) ou
« oauthtoken_scope_invalid ».

CE QUE FAIT LE SCRIPT
---------------------
  1. sauvegarde zoho_config.py (horodatee)
  2. echange le code de la console contre un refresh token
  3. ecrit REFRESH_TOKEN dans zoho_config.py
  4. purge .zoho_token_cache.json
  5. VERIFIE les trois API une par une et dit laquelle passe

Il ne touche PAS a notifier_config.py ni au token Cliq.

USAGE
-----
  1. Console API Zoho -> ton client -> onglet "Generate Code"
       Scope    : la valeur de SCOPES ci-dessous (copie-la telle quelle)
       Duration : 10 minutes
     Copie le code affiche.

  2. Immediatement :
       python setup_main_token.py LE_CODE

Le code expire en ~2 minutes et ne sert qu'une fois. En cas de « invalid_code »,
regenere simplement un nouveau code.

SI UNE API RESTE EN ECHEC
-------------------------
Le scope correspondant est mal nomme. Ajuste SCOPES ci-dessous, regenere un
code et relance. Les noms varient legerement selon les editions Zoho.
"""

import json
import re
import shutil
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "zoho_config.py"
CACHE = ROOT / ".zoho_token_cache.json"

# --- SCOPES DE PRODUCTION -------------------------------------------------
# A copier tel quel dans le champ Scope de la console.
SCOPES = ",".join([
    "WorkDrive.files.ALL",
    "WorkDrive.team.ALL",
    "WorkDrive.organization.ALL",
    "ZohoSheet.dataAPI.UPDATE",
    "ZohoSheet.dataAPI.READ",
    "ZohoSign.documents.ALL",
])
# ---------------------------------------------------------------------------


def mask(token):
    if not token or len(token) < 16:
        return "(vide)"
    return f"{token[:10]}...{token[-4:]}"


def sauvegarder_config():
    if not CONFIG.exists():
        print(f"   ERREUR : {CONFIG} introuvable.")
        sys.exit(1)
    horodatage = time.strftime("%Y%m%d-%H%M%S")
    backup = ROOT / f"zoho_config.py.backup-{horodatage}"
    shutil.copy2(CONFIG, backup)
    print(f"   Sauvegarde : {backup.name}")
    return backup


def echanger_code(code):
    try:
        from zoho_config import ACCOUNTS_URL, CLIENT_ID, CLIENT_SECRET
    except ImportError as exc:
        print(f"   ERREUR d'import zoho_config : {exc}")
        sys.exit(1)

    resp = requests.post(
        f"{ACCOUNTS_URL}/oauth/v2/token",
        params={
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )

    try:
        data = resp.json()
    except ValueError:
        print(f"   ERREUR : reponse illisible -> {resp.text[:200]}")
        sys.exit(1)

    if "refresh_token" not in data:
        print(f"\n   Echec de l'echange : {data}")
        if data.get("error") == "invalid_code":
            print("   -> Code expire, deja utilise, ou ce n'est pas un code.")
            print("      Regenere un code et relance dans les 2 minutes.")
        sys.exit(1)

    return data["refresh_token"]


def ecrire_config(refresh_token):
    contenu = CONFIG.read_text(encoding="utf-8")
    ligne = f'REFRESH_TOKEN = "{refresh_token}"'

    # On cible REFRESH_TOKEN sans jamais toucher a CLIQ_REFRESH_TOKEN.
    motif = r"^REFRESH_TOKEN\s*=.*$"
    if re.search(motif, contenu, flags=re.MULTILINE):
        contenu = re.sub(motif, ligne, contenu, count=1, flags=re.MULTILINE)
        action = "remplace"
    else:
        if not contenu.endswith("\n"):
            contenu += "\n"
        contenu += ligne + "\n"
        action = "ajoute"

    CONFIG.write_text(contenu, encoding="utf-8")
    print(f"   REFRESH_TOKEN {action} : {mask(refresh_token)}")


def verifier():
    """Teste les trois API avec le nouveau token."""
    for mod in ("zoho_config", "zoho_connector"):
        sys.modules.pop(mod, None)

    from zoho_config import SIGN_API, WORKDRIVE_API, WORKDRIVE_TEAM_ID
    from zoho_connector import ZohoConnector

    zc = ZohoConnector()
    token = zc._token()
    entetes = {"Authorization": f"Zoho-oauthtoken {token}"}
    resultats = {}

    # --- WorkDrive : c'est CE test qui manquait la derniere fois ---------
    r = requests.get(
        f"{WORKDRIVE_API}/teams/{WORKDRIVE_TEAM_ID}/folders",
        headers=entetes,
        timeout=20,
    )
    ok = r.status_code == 200
    resultats["WorkDrive"] = (ok, f"HTTP {r.status_code} {r.text[:120]}")

    # --- Sheet -----------------------------------------------------------
    r = requests.post(
        "https://sheet.zoho.com/api/v2/dummy",
        headers=entetes,
        data={"method": "workbook.info.get"},
        timeout=20,
    )
    # 401 = scope absent. Tout autre code = le scope repond.
    ok = r.status_code != 401
    resultats["Sheet"] = (ok, f"HTTP {r.status_code} {r.text[:120]}")

    # --- Sign ------------------------------------------------------------
    r = requests.get(f"{SIGN_API}/api/v1/requests", headers=entetes, timeout=20)
    ok = r.status_code != 401
    resultats["Sign"] = (ok, f"HTTP {r.status_code} {r.text[:120]}")

    for api, (ok, detail) in resultats.items():
        print(f"   {'OK ' if ok else 'KO '} {api:11} {detail}")

    return all(ok for ok, _ in resultats.values())


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("Scope a coller dans la console :\n")
        print(f"  {SCOPES}\n")
        print("Usage : python setup_main_token.py <code_de_la_console>")
        sys.exit(1)

    code = sys.argv[1].strip()
    if len(code) < 20:
        print(f"ERREUR : « {code} » est trop court pour etre un code Zoho.")
        sys.exit(1)

    print("\n=== REGENERATION DU TOKEN PRINCIPAL ===\n")

    print("1) Sauvegarde de la configuration")
    sauvegarder_config()

    print("\n2) Echange du code")
    refresh = echanger_code(code)
    print(f"   Refresh token obtenu : {mask(refresh)}")

    print("\n3) Ecriture dans zoho_config.py")
    ecrire_config(refresh)

    print("\n4) Purge du cache")
    if CACHE.exists():
        CACHE.unlink()
        print(f"   {CACHE.name} supprime.")
    else:
        print("   (pas de cache a purger)")

    print("\n5) Verification des API")
    tout_ok = verifier()

    print("\n" + "=" * 45)
    if tout_ok:
        print("TERMINE. Relance l'application :")
        print("  ./venv/bin/python scripts/app_ged/app.py")
    else:
        print("Une API reste en echec.")
        print("Ajuste SCOPES en haut de ce fichier, regenere un code")
        print("dans la console et relance cette commande.")
        print("\nUne sauvegarde horodatee de zoho_config.py a ete creee")
        print("avant toute modification.")


if __name__ == "__main__":
    main()
