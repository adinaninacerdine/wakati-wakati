"""
setup_notifier_token.py
=======================
Echange un code d'autorisation contre un refresh token et l'ecrit dans
notifier_config.py sous le nom de variable que tu indiques.

Generique : sert pour Cliq, Mail, et tout token additionnel a venir.
Ne touche JAMAIS a zoho_config.py.

USAGE
-----
    python setup_notifier_token.py <NOM_VARIABLE> <CODE>

Exemples :
    python setup_notifier_token.py MAIL_REFRESH_TOKEN 1000.abc....def
    python setup_notifier_token.py CLIQ_REFRESH_TOKEN 1000.abc....def

SCOPES DE REFERENCE
-------------------
    Mail : ZohoMail.messages.CREATE,ZohoMail.accounts.READ
    Cliq : ZohoCliq.Webhooks.CREATE,ZohoCliq.Channels.READ

Le code expire en 2 a 3 minutes et ne sert qu'une fois. En cas de
« invalid_code », regenere simplement un nouveau code dans la console.
"""

import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "notifier_config.py"

# Cache a purger quand on renouvelle le token correspondant
CACHES = {
    "CLIQ_REFRESH_TOKEN": ROOT / ".cliq_token_cache.json",
    "MAIL_REFRESH_TOKEN": ROOT / ".mail_token_cache.json",
}


def mask(token):
    if not token or len(token) < 16:
        return "(vide)"
    return f"{token[:10]}...{token[-4:]}"


def echanger(code):
    try:
        from zoho_config import ACCOUNTS_URL, CLIENT_ID, CLIENT_SECRET
    except ImportError as exc:
        print(f"ERREUR : zoho_config.py illisible ({exc})")
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
        print(f"ERREUR : reponse illisible -> {resp.text[:200]}")
        sys.exit(1)

    if "refresh_token" not in data:
        print(f"\nEchec : {data}")
        if data.get("error") == "invalid_code":
            print("  -> Code expire ou deja utilise. Regenere-en un et relance")
            print("     dans les 2 minutes.")
        sys.exit(1)

    return data["refresh_token"]


def ecrire(variable, token):
    if not CONFIG.exists():
        CONFIG.write_text("# Configuration notifier — NE PAS COMMITER\n", encoding="utf-8")
        print(f"  {CONFIG.name} cree.")

    contenu = CONFIG.read_text(encoding="utf-8")
    ligne = f'{variable} = "{token}"'
    motif = rf"^{re.escape(variable)}\s*=.*$"

    if re.search(motif, contenu, flags=re.MULTILINE):
        contenu = re.sub(motif, ligne, contenu, count=1, flags=re.MULTILINE)
        action = "remplace"
    else:
        if not contenu.endswith("\n"):
            contenu += "\n"
        contenu += ligne + "\n"
        action = "ajoute"

    CONFIG.write_text(contenu, encoding="utf-8")
    print(f"  {variable} {action} : {mask(token)}")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        print("Usage : python setup_notifier_token.py <NOM_VARIABLE> <CODE>")
        sys.exit(1)

    variable = sys.argv[1].strip().upper()
    code = sys.argv[2].strip()

    if not re.fullmatch(r"[A-Z0-9_]+", variable):
        print(f"ERREUR : « {variable} » n'est pas un nom de variable valide.")
        sys.exit(1)

    if len(code) < 20:
        print(f"ERREUR : « {code} » est trop court pour etre un code Zoho.")
        sys.exit(1)

    print(f"\n=== {variable} ===\n")

    print("1) Echange du code")
    token = echanger(code)
    print(f"  Refresh token obtenu : {mask(token)}")

    print("\n2) Ecriture dans notifier_config.py")
    ecrire(variable, token)

    print("\n3) Purge du cache associe")
    cache = CACHES.get(variable)
    if cache and cache.exists():
        cache.unlink()
        print(f"  {cache.name} supprime.")
    else:
        print("  (aucun cache a purger)")

    print("\n" + "=" * 40)
    print("TERMINE.")
    if variable == "MAIL_REFRESH_TOKEN":
        print("Etape suivante — voir tes adresses d'expedition autorisees :")
        print("  ./venv/bin/python notifier.py --comptes")
        print("Puis reporte l'une d'elles dans MAIL_FROM_ADDRESS.")
    else:
        print("Test : ./venv/bin/python notifier.py")


if __name__ == "__main__":
    main()
