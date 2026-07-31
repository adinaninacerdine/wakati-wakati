"""
notifier.py — Couche de visibilité DG pour l'application GED
============================================================
Trois canaux, indépendants les uns des autres :
  - Zoho Cliq  : carte dans un canal (API REST, token OAuth dédié)
  - Email      : accusé de dépôt via SMTP Zoho Mail
  - Zoho Sign  : envoi du document en signature au DG

RÈGLE D'OR : dispatch() ne lève JAMAIS d'exception. Un canal en panne ne doit
jamais empêcher un dépôt de document d'aboutir.

IMPORTANT — DEUX TOKENS SÉPARÉS
-------------------------------
Le token Cliq a son propre refresh token ET son propre fichier de cache
(.cliq_token_cache.json). Il ne touche jamais à .zoho_token_cache.json utilisé
par ZohoConnector pour WorkDrive/Sheet/Sign. Mélanger les deux caches provoque
des 401 aléatoires impossibles à diagnostiquer.

CONFIGURATION
-------------
Créer `notifier_config.py` à la racine (à côté de zoho_config.py) :

    # --- Cliq ---
    CLIQ_REFRESH_TOKEN = "1000.xxxxx.yyyyy"
    CLIQ_CHANNEL       = "ged"          # nom unique du canal, sans le #

    # --- Email (mot de passe d'application, PAS celui du compte) ---
    SMTP_HOST = "smtp.zoho.com"
    SMTP_PORT = 465
    SMTP_USER = "ged@tondomaine.km"
    SMTP_PASS = "mot-de-passe-application"
    MAIL_FROM = "GED Huri Money <ged@tondomaine.km>"
    MAIL_TO   = ["dg@tondomaine.km"]
    MAIL_CC   = []

    # --- Zoho Sign ---
    DG_NAME  = "Nom Prenom"
    DG_EMAIL = "dg@tondomaine.km"
    SIGN_EXPIRATION_DAYS = 15

Puis, impérativement :

    echo "notifier_config.py"      >> .gitignore
    echo ".cliq_token_cache.json"  >> .gitignore

SCOPES OAUTH
------------
Cliq (refresh token dédié) : ZohoCliq.Webhooks.CREATE,ZohoCliq.Channels.READ
Sign (refresh token principal, déjà en place) : ZohoSign.documents.ALL

USAGE DANS app.py
-----------------
    from notifier import dispatch, statuts_pour_registre

    doc = {
        "numero": numero,
        "nom_fichier": nom_final,
        "service": service,
        "type_doc": type_doc,
        "deposant": deposant,
        "date": date_str,
        "workdrive_url": lien_workdrive,
        "sheet_url": lien_registre,
    }
    res = dispatch(doc, connector=zc, pdf_bytes=contenu, signature=besoin_signature)
    statut_cliq, statut_sign, sign_id = statuts_pour_registre(res)

TEST AUTONOME
-------------
    python notifier.py
"""

import json
import os
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Localisation de la racine du projet (même logique que zoho_connector.py)
# ---------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent

ROOT_DIR = None
for parent in [CURRENT_DIR] + list(CURRENT_DIR.parents):
    if (parent / "zoho_config.py").exists():
        ROOT_DIR = parent
        break

if ROOT_DIR and str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

CLIQ_CACHE = Path(ROOT_DIR or CURRENT_DIR) / ".cliq_token_cache.json"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
try:
    import notifier_config as CFG
except ImportError:  # repli sur les variables d'environnement
    class CFG:  # noqa: N801
        CLIQ_REFRESH_TOKEN = os.getenv("CLIQ_REFRESH_TOKEN", "")
        CLIQ_CHANNEL = os.getenv("CLIQ_CHANNEL", "")
        SMTP_HOST = os.getenv("SMTP_HOST", "smtp.zoho.com")
        SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
        SMTP_USER = os.getenv("SMTP_USER", "")
        SMTP_PASS = os.getenv("SMTP_PASS", "")
        MAIL_FROM = os.getenv("MAIL_FROM", "")
        MAIL_TO = [x for x in os.getenv("MAIL_TO", "").split(",") if x]
        MAIL_CC = []
        DG_NAME = os.getenv("DG_NAME", "")
        DG_EMAIL = os.getenv("DG_EMAIL", "")
        SIGN_EXPIRATION_DAYS = 15

try:
    from zoho_config import ACCOUNTS_URL, CLIENT_ID, CLIENT_SECRET
except ImportError:
    ACCOUNTS_URL = "https://accounts.zoho.com"
    CLIENT_ID = CLIENT_SECRET = ""

try:
    from zoho_config import SIGN_API
except ImportError:
    SIGN_API = "https://sign.zoho.com"

CLIQ_API = "https://cliq.zoho.com/api/v2"


def _get(name, default=None):
    return getattr(CFG, name, default)


# ===========================================================================
# TOKEN CLIQ — cache dédié, isolé de .zoho_token_cache.json
# ===========================================================================
def _cliq_token():
    """Retourne un access token Cliq valide. Lève RuntimeError si impossible."""
    if CLIQ_CACHE.exists():
        try:
            c = json.loads(CLIQ_CACHE.read_text())
            if c.get("expiry", 0) > time.time() + 60 and c.get("access_token"):
                return c["access_token"]
        except Exception:
            pass

    refresh = _get("CLIQ_REFRESH_TOKEN")
    if not refresh:
        raise RuntimeError("CLIQ_REFRESH_TOKEN absent de notifier_config.py")

    resp = requests.post(
        f"{ACCOUNTS_URL}/oauth/v2/token",
        params={
            "refresh_token": refresh,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Refresh Cliq refusé : {data}")

    try:
        CLIQ_CACHE.write_text(
            json.dumps(
                {
                    "access_token": data["access_token"],
                    "expiry": time.time() + data.get("expires_in", 3600),
                }
            )
        )
    except Exception:
        pass  # cache non écrit : on refera un refresh au prochain appel

    return data["access_token"]


# ===========================================================================
# 1. ZOHO CLIQ
# ===========================================================================
def _build_cliq_payload(doc):
    payload = {
        "text": f"*Nouveau document déposé* — {doc.get('service', 'N/A')}",
        "card": {
            "title": f"{doc.get('numero', '')} · {doc.get('nom_fichier', '')}",
            "theme": "modern-inline",
        },
        "slides": [
            {
                "type": "table",
                "title": "Détails du dépôt",
                "data": {
                    "headers": ["Champ", "Valeur"],
                    "rows": [
                        {"Champ": "Type", "Valeur": str(doc.get("type_doc", "-"))},
                        {"Champ": "Service", "Valeur": str(doc.get("service", "-"))},
                        {"Champ": "Déposant", "Valeur": str(doc.get("deposant", "-"))},
                        {"Champ": "Date", "Valeur": str(doc.get("date", "-"))},
                    ],
                },
            }
        ],
    }

    buttons = []
    if doc.get("workdrive_url"):
        buttons.append(
            {
                "label": "Ouvrir le document",
                "type": "+",
                "action": {"type": "open.url", "data": {"web": doc["workdrive_url"]}},
            }
        )
    if doc.get("sheet_url"):
        buttons.append(
            {
                "label": "Voir le registre",
                "action": {"type": "open.url", "data": {"web": doc["sheet_url"]}},
            }
        )
    if buttons:
        payload["buttons"] = buttons

    return payload


def notify_cliq(doc):
    """Poste une carte dans le canal Cliq. Retourne (ok: bool, detail: str)."""
    channel = _get("CLIQ_CHANNEL")
    if not channel:
        return False, "CLIQ_CHANNEL non configuré"

    try:
        token = _cliq_token()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

    try:
        resp = requests.post(
            f"{CLIQ_API}/channelsbyname/{channel}/message",
            headers={
                "Authorization": f"Zoho-oauthtoken {token}",
                "Content-Type": "application/json",
            },
            json=_build_cliq_payload(doc),
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

    if resp.status_code in (200, 201, 204):
        return True, f"posté dans #{channel}"

    # 401 : le cache est peut-être périmé -> on le purge pour le prochain appel
    if resp.status_code == 401:
        try:
            CLIQ_CACHE.unlink(missing_ok=True)
        except Exception:
            pass

    return False, f"HTTP {resp.status_code} : {resp.text[:200]}"


# ===========================================================================
# 2. EMAIL (SMTP Zoho Mail)
# ===========================================================================
def notify_email(doc, pdf_bytes=None, attach=False):
    """Envoie l'accusé de dépôt par email. Retourne (ok: bool, detail: str)."""
    to = _get("MAIL_TO") or []
    if not (_get("SMTP_USER") and _get("SMTP_PASS") and to):
        return False, "SMTP non configuré"

    msg = EmailMessage()
    msg["Subject"] = f"[GED] {doc.get('numero', '')} — {doc.get('nom_fichier', '')}"
    msg["From"] = _get("MAIL_FROM") or _get("SMTP_USER")
    msg["To"] = ", ".join(to)
    if _get("MAIL_CC"):
        msg["Cc"] = ", ".join(_get("MAIL_CC"))

    lignes = [
        f"Numéro     : {doc.get('numero', '-')}",
        f"Fichier    : {doc.get('nom_fichier', '-')}",
        f"Type       : {doc.get('type_doc', '-')}",
        f"Service    : {doc.get('service', '-')}",
        f"Déposant   : {doc.get('deposant', '-')}",
        f"Date       : {doc.get('date', '-')}",
    ]
    if doc.get("workdrive_url"):
        lignes += ["", f"Document   : {doc['workdrive_url']}"]
    if doc.get("sheet_url"):
        lignes += [f"Registre   : {doc['sheet_url']}"]
    lignes += ["", "-- ", "Message automatique de l'application GED."]

    msg.set_content(
        "Un nouveau document a été déposé dans la GED.\n\n" + "\n".join(lignes)
    )

    if attach and pdf_bytes:
        msg.add_attachment(
            pdf_bytes,
            maintype="application",
            subtype="pdf",
            filename=doc.get("nom_fichier", "document.pdf"),
        )

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            _get("SMTP_HOST"), _get("SMTP_PORT"), context=ctx, timeout=20
        ) as s:
            s.login(_get("SMTP_USER"), _get("SMTP_PASS"))
            s.send_message(msg)
        return True, f"envoyé à {len(to)} destinataire(s)"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# ===========================================================================
# 3. ZOHO SIGN
# ===========================================================================
def send_for_signature(connector, doc, pdf_bytes=None, pdf_path=None):
    """Crée une demande Zoho Sign et l'envoie au DG.

    connector : instance ZohoConnector (token principal, scope ZohoSign.documents.ALL).
    Retourne (ok: bool, detail: str) — detail = request_id si succès.
    """
    dg_email = _get("DG_EMAIL")
    dg_name = _get("DG_NAME")
    if not dg_email:
        return False, "DG_EMAIL non configuré"
    if connector is None:
        return False, "connector requis pour Zoho Sign"

    if pdf_bytes is None:
        if not pdf_path or not Path(pdf_path).exists():
            return False, "aucun contenu PDF fourni"
        pdf_bytes = Path(pdf_path).read_bytes()

    filename = doc.get("nom_fichier", "document.pdf")

    try:
        headers = {"Authorization": f"Zoho-oauthtoken {connector._token()}"}
    except Exception as exc:  # noqa: BLE001
        return False, f"token principal indisponible : {exc}"

    # --- 3a. Création du brouillon ---------------------------------------
    create_body = {
        "requests": {
            "request_name": f"{doc.get('numero', '')} — {filename}",
            "expiration_days": _get("SIGN_EXPIRATION_DAYS", 15),
            "is_sequential": True,
            "actions": [
                {
                    "action_type": "SIGN",
                    "recipient_email": dg_email,
                    "recipient_name": dg_name,
                    "signing_order": 0,
                    "verify_recipient": False,
                }
            ],
        }
    }

    try:
        resp = requests.post(
            f"{SIGN_API}/api/v1/requests",
            headers=headers,
            files={"file": (filename, pdf_bytes, "application/pdf")},
            data={"data": json.dumps(create_body)},
            timeout=60,
        )
        out = resp.json()
    except Exception as exc:  # noqa: BLE001
        return False, f"création : {exc}"

    if out.get("status") != "success":
        return False, f"création refusée : {json.dumps(out)[:300]}"

    try:
        req = out["requests"]
        request_id = req["request_id"]
        action_id = req["actions"][0]["action_id"]
    except (KeyError, IndexError) as exc:
        return False, f"réponse Sign inattendue : {exc}"

    # --- 3b. Soumission : déclenche l'email de signature au DG -------------
    submit_body = {
        "requests": {
            "actions": [
                {
                    "action_id": action_id,
                    "action_type": "SIGN",
                    "recipient_email": dg_email,
                    "recipient_name": dg_name,
                    "verify_recipient": False,
                }
            ]
        }
    }

    try:
        resp = requests.post(
            f"{SIGN_API}/api/v1/requests/{request_id}/submit",
            headers=headers,
            data={"data": json.dumps(submit_body)},
            timeout=60,
        )
        out = resp.json()
    except Exception as exc:  # noqa: BLE001
        return False, f"soumission : {exc}"

    if out.get("status") != "success":
        return False, f"soumission refusée : {json.dumps(out)[:300]}"

    return True, request_id


# ===========================================================================
# ORCHESTRATEUR
# ===========================================================================
def dispatch(doc, connector=None, pdf_bytes=None, signature=False, attach_email=False):
    """Déclenche tous les canaux. Ne lève jamais. Retourne un dict de statuts.

    Exemple :
        {"cliq": (True, "posté dans #ged"),
         "email": (True, "envoyé à 1 destinataire(s)"),
         "sign": (True, "1234000000123456")}
    """
    res = {}

    try:
        res["cliq"] = notify_cliq(doc)
    except Exception as exc:  # noqa: BLE001
        res["cliq"] = (False, f"exception : {exc}")

    try:
        res["email"] = notify_email(doc, pdf_bytes=pdf_bytes, attach=attach_email)
    except Exception as exc:  # noqa: BLE001
        res["email"] = (False, f"exception : {exc}")

    if signature:
        try:
            res["sign"] = send_for_signature(connector, doc, pdf_bytes=pdf_bytes)
        except Exception as exc:  # noqa: BLE001
            res["sign"] = (False, f"exception : {exc}")

    for canal, (ok, detail) in res.items():
        print(f"[GED][{canal.upper():5}] {'OK ' if ok else 'KO '} {detail}")

    return res


def statuts_pour_registre(res):
    """Convertit le retour de dispatch() en valeurs texte pour le registre Sheet.

    Retourne (statut_cliq, statut_sign, sign_request_id).
    """
    cliq_ok, _ = res.get("cliq", (False, ""))
    sign_ok, sign_detail = res.get("sign", (None, ""))

    statut_cliq = "OK" if cliq_ok else "KO"
    if sign_ok is None:
        statut_sign, sign_id = "Non requis", ""
    elif sign_ok:
        statut_sign, sign_id = "Envoyé", sign_detail
    else:
        statut_sign, sign_id = "Échec", ""

    return statut_cliq, statut_sign, sign_id


# ===========================================================================
# TEST AUTONOME : python notifier.py
# ===========================================================================
if __name__ == "__main__":
    doc_test = {
        "numero": "HM26-TEST-001",
        "nom_fichier": "document_de_test.pdf",
        "service": "Direction Générale",
        "type_doc": "Note interne",
        "deposant": "Nacer",
        "date": "30/07/2026",
        "workdrive_url": "https://workdrive.zoho.com",
        "sheet_url": (
            "https://sheet.zoho.com/sheet/open/"
            "avaji4d47ec1cbffc4f5184dc8b3127b83ae7"
        ),
    }

    print("=== TEST NOTIFIER ===")
    print(f"Racine projet : {ROOT_DIR}")
    print(f"Cache Cliq    : {CLIQ_CACHE}")
    print(f"Canal Cliq    : {_get('CLIQ_CHANNEL') or '(non configure)'}")
    print(f"Config        : {'notifier_config.py' if hasattr(CFG, '__file__') else 'variables d environnement'}")
    print()

    resultats = dispatch(doc_test)

    print()
    print("Statuts registre :", statuts_pour_registre(resultats))
