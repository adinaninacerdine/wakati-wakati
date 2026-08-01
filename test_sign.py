"""
test_sign.py
============
Test end-to-end de l'envoi en signature Zoho Sign, isolé du reste de l'app.

Envoie un PDF réel en signature à l'adresse que TU indiques — pas au DG.
Tant que la chaîne n'est pas validée, ne mets jamais l'adresse du DG ici :
une demande de signature envoyée par erreur ne s'annule pas discrètement.

USAGE
-----
    python test_sign.py <chemin_du_pdf> <ton_email>

Exemple :
    python test_sign.py ~/Documents/exemple.pdf animation.reseau@hurimoney.com

Prends n'importe quel PDF déjà présent sur ton disque — un document que tu as
déjà déposé dans la GED fait parfaitement l'affaire.

CE QUE FAIT LE SCRIPT
---------------------
  1. vérifie que le PDF existe et est lisible
  2. vérifie que le token principal répond à Zoho Sign
  3. crée la demande de signature (brouillon)
  4. la soumet -> déclenche l'email de signature vers l'adresse indiquée
  5. affiche le request_id et l'état final

Il n'écrit rien dans le registre et ne touche à aucune configuration.
"""

import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        print("Usage : python test_sign.py <chemin_du_pdf> <ton_email>")
        sys.exit(1)

    pdf_path = Path(sys.argv[1]).expanduser()
    destinataire = sys.argv[2].strip()

    print("\n=== TEST ZOHO SIGN ===\n")

    # --- 1. Le PDF -------------------------------------------------------
    print("1) Verification du PDF")
    if not pdf_path.exists():
        print(f"   ERREUR : {pdf_path} introuvable.")
        sys.exit(1)

    pdf_bytes = pdf_path.read_bytes()
    if not pdf_bytes.startswith(b"%PDF"):
        print(f"   ERREUR : {pdf_path.name} ne semble pas etre un vrai PDF.")
        sys.exit(1)

    taille_ko = len(pdf_bytes) / 1024
    print(f"   {pdf_path.name} — {taille_ko:.1f} Ko — OK")

    # --- 2. Le token -----------------------------------------------------
    print("\n2) Verification du token Sign")
    try:
        from zoho_config import SIGN_API
        from zoho_connector import ZohoConnector
    except ImportError as exc:
        print(f"   ERREUR d'import : {exc}")
        sys.exit(1)

    try:
        zc = ZohoConnector()
        token = zc._token()
    except Exception as exc:  # noqa: BLE001
        print(f"   ERREUR : token principal indisponible ({exc})")
        sys.exit(1)

    probe = requests.get(
        f"{SIGN_API}/requests",
        headers={"Authorization": f"Zoho-oauthtoken {token}"},
        timeout=20,
    )
    if probe.status_code == 401:
        print(f"   ERREUR 401 : {probe.text[:200]}")
        print("   -> le scope ZohoSign.documents.ALL manque sur le token principal.")
        sys.exit(1)
    print(f"   HTTP {probe.status_code} — le token est accepte par Sign")

    # --- 3. Envoi --------------------------------------------------------
    print(f"\n3) Envoi en signature vers {destinataire}")

    doc = {
        "numero": "HM26-TEST-SIGN",
        "nom_fichier": pdf_path.name,
    }

    try:
        from notifier import send_for_signature
    except ImportError as exc:
        print(f"   ERREUR : notifier.py illisible ({exc})")
        sys.exit(1)

    # On force le destinataire du test, sans toucher a notifier_config.py
    import notifier

    class _CfgTest:
        DG_EMAIL = destinataire
        DG_NAME = "Test GED"
        SIGN_EXPIRATION_DAYS = 15

    cfg_original = notifier.CFG
    notifier.CFG = _CfgTest
    try:
        ok, detail = send_for_signature(zc, doc, pdf_bytes=pdf_bytes)
    finally:
        notifier.CFG = cfg_original

    print("\n" + "=" * 45)
    if ok:
        print("SUCCES.")
        print(f"  request_id : {detail}")
        print(f"  Un email de signature part vers {destinataire}.")
        print("  Verifie aussi https://sign.zoho.com -> Documents envoyes.")
        print("\n  Pense a annuler ou supprimer cette demande de test dans")
        print("  l'interface Sign pour ne pas polluer l'historique.")
    else:
        print("ECHEC.")
        print(f"  {detail}")
        print("\n  Pistes selon le message :")
        print("    - 'scope' dans l'erreur -> ajouter ZohoSign.documents.ALL")
        print("    - 'recipient' -> adresse email invalide")
        print("    - 'plan' ou 'limit' -> quota Sign atteint sur le compte")


if __name__ == "__main__":
    main()
