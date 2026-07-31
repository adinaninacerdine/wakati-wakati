"""
setup_cliq_token.py
===================
Fait TOUT en une commande, pour éliminer la confusion entre « code
d'autorisation » et « refresh token » :

  1. échange le code de la console contre un refresh token
  2. écrit ce refresh token directement dans notifier_config.py
  3. purge le cache Cliq
  4. envoie un message de test dans le canal et affiche le résultat

USAGE
-----
  1. Console API Zoho -> ton client -> onglet "Generate Code"
       Scope    : ZohoCliq.Webhooks.CREATE,ZohoCliq.Channels.READ
       Duration : 10 minutes
     Copie le code affiché (il commence par 1000.)

  2. Immédiatement :
       python setup_cliq_token.py LE_CODE

Le code d'autorisation expire en ~2 minutes et ne sert qu'une fois.
Si tu vois « invalid_code », régénère simplement un nouveau code : il n'y a
aucune limite au nombre de codes générés.

Ce script ne touche JAMAIS à zoho_config.py.
"""

import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "notifier_config.py"
CACHE = ROOT / ".cliq_token_cache.json"

CLIQ_API = "https://cliq.zoho.com/api/v2"

TEMPLATE = '''# --- Cliq ---
CLIQ_REFRESH_TOKEN = ""
CLIQ_CHANNEL = "ged"

# --- Email (a remplir plus tard) ---
SMTP_HOST = "smtp.zoho.com"
SMTP_PORT = 465
SMTP_USER = ""
SMTP_PASS = ""
MAIL_FROM = ""
MAIL_TO = []
MAIL_CC = []

# --- Zoho Sign (a remplir plus tard) ---
DG_NAME = ""
DG_EMAIL = ""
SIGN_EXPIRATION_DAYS = 15
'''


def mask(token):
    """N'affiche jamais un secret en entier."""
    if not token or len(token) < 16:
        return "(vide)"
    return f"{token[:10]}...{token[-4:]}"


def echanger_code(code):
    """Echange le code d'autorisation contre un refresh token."""
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
        print(f"ERREUR : reponse illisible de Zoho -> {resp.text[:200]}")
        sys.exit(1)

    if "refresh_token" not in data:
        print("\nEchec de l'echange. Reponse de Zoho :")
        print(f"  {data}")
        if data.get("error") == "invalid_code":
            print("\n  -> Le code est expire, deja utilise, ou ce n'est pas un code")
            print("     d'autorisation. Regenere un code dans la console et relance")
            print("     cette commande dans les 2 minutes.")
        elif data.get("error") == "invalid_client":
            print("\n  -> CLIENT_ID / CLIENT_SECRET refuses. Verifie zoho_config.py.")
        sys.exit(1)

    return data["refresh_token"]


def ecrire_config(refresh_token):
    """Ecrit ou remplace CLIQ_REFRESH_TOKEN dans notifier_config.py."""
    if not CONFIG.exists():
        CONFIG.write_text(TEMPLATE, encoding="utf-8")
        print(f"  {CONFIG.name} cree depuis le modele.")

    contenu = CONFIG.read_text(encoding="utf-8")
    ligne = f'CLIQ_REFRESH_TOKEN = "{refresh_token}"'

    if re.search(r"^CLIQ_REFRESH_TOKEN\s*=.*$", contenu, flags=re.MULTILINE):
        contenu = re.sub(
            r"^CLIQ_REFRESH_TOKEN\s*=.*$", ligne, contenu, count=1, flags=re.MULTILINE
        )
        action = "remplace"
    else:
        if not contenu.endswith("\n"):
            contenu += "\n"
        contenu += ligne + "\n"
        action = "ajoute"

    CONFIG.write_text(contenu, encoding="utf-8")
    print(f"  CLIQ_REFRESH_TOKEN {action} dans {CONFIG.name} : {mask(refresh_token)}")


def purger_cache():
    if CACHE.exists():
        CACHE.unlink()
        print(f"  Cache {CACHE.name} purge.")


def tester_envoi():
    """Envoie un message reel dans le canal via notifier.py."""
    for mod in ("notifier_config", "notifier"):
        sys.modules.pop(mod, None)

    try:
        from notifier import notify_cliq
    except ImportError as exc:
        print(f"  notifier.py introuvable ou illisible : {exc}")
        return False

    doc = {
        "numero": "HM26-SETUP-001",
        "nom_fichier": "test_configuration.pdf",
        "service": "Direction Generale",
        "type_doc": "Test de configuration",
        "deposant": "Nacer",
        "date": "30/07/2026",
    }

    ok, detail = notify_cliq(doc)
    print(f"  {'OK ' if ok else 'KO '} {detail}")
    return ok


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("Usage : python setup_cliq_token.py <code_de_la_console>")
        sys.exit(1)

    code = sys.argv[1].strip()

    if len(code) < 20:
        print(f"ERREUR : « {code} » est trop court pour etre un code Zoho.")
        print("Le code de la console ressemble a : 1000.abc123....def456")
        sys.exit(1)

    print("\n=== CONFIGURATION DU TOKEN CLIQ ===\n")

    print("1) Echange du code contre un refresh token")
    refresh = echanger_code(code)
    print(f"  Refresh token obtenu : {mask(refresh)}")

    print("\n2) Ecriture dans la configuration")
    ecrire_config(refresh)

    print("\n3) Purge du cache")
    purger_cache()

    print("\n4) Test d'envoi reel dans le canal Cliq")
    ok = tester_envoi()

    print("\n" + "=" * 40)
    if ok:
        print("TERMINE. Va verifier le message dans ton canal Cliq.")
        print("Tu peux maintenant lancer : python notifier.py")
    else:
        print("Le token est en place mais l'envoi a echoue.")
        print("Causes probables :")
        print("  - CLIQ_CHANNEL ne correspond pas au nom unique du canal")
        print("    (celui de l'URL quand tu ouvres le canal, en minuscules, sans #)")
        print("  - le scope Cliq n'etait pas coche lors de la generation du code")
        print("\nRappel : ne colle jamais le token dans un chat. Les codes")
        print("d'erreur suffisent pour diagnostiquer.")


if __name__ == "__main__":
    main()
