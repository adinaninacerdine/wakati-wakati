"""
notifier.py — Couche de visibilité DG pour l'application GED
============================================================
Trois canaux, indépendants :
  - Zoho Cliq  : carte dans un canal (API REST, token dédié)
  - Email      : API REST Zoho Mail (défaut) ou SMTP (repli)
  - Zoho Sign  : envoi du document en signature au DG

RÈGLE D'OR : dispatch() ne lève JAMAIS. Un canal en panne n'empêche jamais
un dépôt de document d'aboutir.

POURQUOI L'API MAIL PLUTÔT QUE SMTP
-----------------------------------
smtp.zoho.com renvoie « 552 Your IP Address is blocked from further use »
de façon intermittente depuis certains réseaux. L'API REST passe en HTTPS
comme Cliq et WorkDrive : insensible aux blocages IP, pas de mot de passe
d'application à gérer. SMTP reste disponible en repli via EMAIL_MODE.

TROIS TOKENS SÉPARÉS, TROIS CACHES SÉPARÉS
------------------------------------------
  zoho_config.py     REFRESH_TOKEN       -> .zoho_token_cache.json  (WorkDrive/Sheet/Sign)
  notifier_config.py CLIQ_REFRESH_TOKEN  -> .cliq_token_cache.json  (Cliq)
  notifier_config.py MAIL_REFRESH_TOKEN  -> .mail_token_cache.json  (Mail)

Ne jamais partager un cache entre deux périmètres : ça provoque des 401
aléatoires impossibles à diagnostiquer.

CONFIGURATION — notifier_config.py
----------------------------------
    # --- Cliq ---
    CLIQ_REFRESH_TOKEN = "1000.xxxxx.yyyyy"
    CLIQ_CHANNEL       = "gednotifications"

    # --- Email : "api" (recommande) ou "smtp" ---
    EMAIL_MODE = "api"

    # mode api
    MAIL_REFRESH_TOKEN = "1000.xxxxx.yyyyy"
    MAIL_FROM_ADDRESS  = "nacer-dine.a@hurimoney.com"
    MAIL_ACCOUNT_ID    = ""      # laisse vide : detecte automatiquement

    # mode smtp (repli)
    SMTP_HOST = "smtp.zoho.com"
    SMTP_PORT = 465
    SMTP_USER = ""
    SMTP_PASS = ""
    MAIL_FROM = ""

    # commun aux deux modes
    MAIL_TO = ["animation.reseau@hurimoney.com"]
    MAIL_CC = []

    # --- Zoho Sign ---
    DG_NAME  = ""
    DG_EMAIL = ""
    SIGN_EXPIRATION_DAYS = 15

    echo "notifier_config.py"     >> .gitignore
    echo ".cliq_token_cache.json" >> .gitignore
    echo ".mail_token_cache.json" >> .gitignore

SCOPES
------
  Cliq : ZohoCliq.Webhooks.CREATE,ZohoCliq.Channels.READ
  Mail : ZohoMail.messages.CREATE,ZohoMail.accounts.READ
  Sign : ZohoSign.documents.ALL  (sur le token principal)

DIAGNOSTIC
----------
    python notifier.py            -> teste tous les canaux
    python notifier.py --comptes  -> liste les adresses d'envoi disponibles
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
# Racine du projet (même logique que zoho_connector.py)
# ---------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent

ROOT_DIR = None
for parent in [CURRENT_DIR] + list(CURRENT_DIR.parents):
    if (parent / "zoho_config.py").exists():
        ROOT_DIR = parent
        break

if ROOT_DIR and str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

BASE = Path(ROOT_DIR or CURRENT_DIR)
CLIQ_CACHE = BASE / ".cliq_token_cache.json"
MAIL_CACHE = BASE / ".mail_token_cache.json"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
try:
    import notifier_config as CFG
except ImportError:
    class CFG:  # noqa: N801
        CLIQ_REFRESH_TOKEN = os.getenv("CLIQ_REFRESH_TOKEN", "")
        CLIQ_CHANNEL = os.getenv("CLIQ_CHANNEL", "")
        EMAIL_MODE = os.getenv("EMAIL_MODE", "api")
        MAIL_REFRESH_TOKEN = os.getenv("MAIL_REFRESH_TOKEN", "")
        MAIL_FROM_ADDRESS = os.getenv("MAIL_FROM_ADDRESS", "")
        MAIL_ACCOUNT_ID = os.getenv("MAIL_ACCOUNT_ID", "")
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
MAIL_API = "https://mail.zoho.com/api"


def _sign_base():
    """Normalise SIGN_API : accepte avec ou sans le segment /api/v1.

    zoho_config.py peut contenir soit 'https://sign.zoho.com', soit
    'https://sign.zoho.com/api/v1'. Concatener /api/v1 a l'aveugle produit
    une URL doublee qui renvoie un 9004 trompeur.
    """
    base = str(SIGN_API).rstrip("/")
    if base.endswith("/api/v1"):
        return base
    return base + "/api/v1"


def _get(name, default=None):
    valeur = getattr(CFG, name, default)
    return default if valeur is None else valeur


# ===========================================================================
# TOKENS — un cache par périmètre
# ===========================================================================
def _token_depuis_refresh(refresh, cache_path, libelle):
    """Retourne un access token valide, avec cache dédié. Lève RuntimeError."""
    if cache_path.exists():
        try:
            c = json.loads(cache_path.read_text())
            if c.get("expiry", 0) > time.time() + 60 and c.get("access_token"):
                return c["access_token"]
        except Exception:
            pass

    if not refresh:
        raise RuntimeError(f"{libelle} absent de notifier_config.py")

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
        raise RuntimeError(f"Refresh {libelle} refusé : {data}")

    try:
        cache_path.write_text(json.dumps({
            "access_token": data["access_token"],
            "expiry": time.time() + data.get("expires_in", 3600),
        }))
    except Exception:
        pass

    return data["access_token"]


def _cliq_token():
    return _token_depuis_refresh(
        _get("CLIQ_REFRESH_TOKEN", ""), CLIQ_CACHE, "CLIQ_REFRESH_TOKEN"
    )


def _mail_token():
    return _token_depuis_refresh(
        _get("MAIL_REFRESH_TOKEN", ""), MAIL_CACHE, "MAIL_REFRESH_TOKEN"
    )


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
        "slides": [{
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
        }],
    }

    buttons = []
    if doc.get("workdrive_url"):
        buttons.append({
            "label": "Ouvrir le document",
            "type": "+",
            "action": {"type": "open.url", "data": {"web": doc["workdrive_url"]}},
        })
    if doc.get("sheet_url"):
        buttons.append({
            "label": "Voir le registre",
            "action": {"type": "open.url", "data": {"web": doc["sheet_url"]}},
        })
    if buttons:
        payload["buttons"] = buttons

    return payload


def notify_cliq(doc):
    """Poste une carte dans le canal Cliq. Retourne (ok, detail)."""
    channel = _get("CLIQ_CHANNEL", "")
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

    if resp.status_code == 401:
        try:
            CLIQ_CACHE.unlink(missing_ok=True)
        except Exception:
            pass

    return False, f"HTTP {resp.status_code} : {resp.text[:200]}"


# ===========================================================================
# 2. EMAIL
# ===========================================================================
def _corps_email(doc):
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
    return "Un nouveau document a été déposé dans la GED.\n\n" + "\n".join(lignes)


def _sujet_email(doc):
    return f"[GED] {doc.get('numero', '')} — {doc.get('nom_fichier', '')}"


def lister_comptes_mail():
    """Liste les comptes et adresses d'envoi accessibles par le token Mail.

    Sert à savoir depuis quelle adresse on a le droit d'expédier :
    l'API refuse un fromAddress qui n'appartient pas au compte authentifié.
    """
    token = _mail_token()
    resp = requests.get(
        f"{MAIL_API}/accounts",
        headers={"Authorization": f"Zoho-oauthtoken {token}"},
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} : {resp.text[:300]}")

    comptes = []
    for c in resp.json().get("data", []):
        adresses = []
        for envoi in c.get("sendMailDetails", []) or []:
            adr = envoi.get("fromAddress")
            if adr:
                adresses.append(adr)
        comptes.append({
            "accountId": c.get("accountId"),
            "principal": c.get("primaryEmailAddress"),
            "expediteurs": adresses,
        })
    return comptes


def _resoudre_account_id():
    """Retourne l'accountId Mail : celui configuré, sinon le premier trouvé."""
    fixe = _get("MAIL_ACCOUNT_ID", "")
    if fixe:
        return fixe
    comptes = lister_comptes_mail()
    if not comptes:
        raise RuntimeError("Aucun compte Mail accessible avec ce token")
    return comptes[0]["accountId"]


def notify_email_api(doc):
    """Envoie via l'API REST Zoho Mail. Retourne (ok, detail)."""
    to = _get("MAIL_TO", []) or []
    if not to:
        return False, "MAIL_TO vide"

    expediteur = _get("MAIL_FROM_ADDRESS", "")
    if not expediteur:
        return False, "MAIL_FROM_ADDRESS non configuré"

    try:
        token = _mail_token()
        account_id = _resoudre_account_id()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

    corps = {
        "fromAddress": expediteur,
        "toAddress": ",".join(to),
        "subject": _sujet_email(doc),
        "content": _corps_email(doc),
        "mailFormat": "plaintext",
        "askReceipt": "no",
    }
    if _get("MAIL_CC", []):
        corps["ccAddress"] = ",".join(_get("MAIL_CC", []))

    try:
        resp = requests.post(
            f"{MAIL_API}/accounts/{account_id}/messages",
            headers={
                "Authorization": f"Zoho-oauthtoken {token}",
                "Content-Type": "application/json",
            },
            json=corps,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

    if resp.status_code in (200, 201):
        return True, f"API — envoyé à {len(to)} destinataire(s)"

    if resp.status_code == 401:
        try:
            MAIL_CACHE.unlink(missing_ok=True)
        except Exception:
            pass

    return False, f"API HTTP {resp.status_code} : {resp.text[:250]}"


def notify_email_smtp(doc, pdf_bytes=None, attach=False):
    """Envoie via SMTP. Repli si l'API n'est pas configurée."""
    to = _get("MAIL_TO", []) or []
    if not (_get("SMTP_USER", "") and _get("SMTP_PASS", "") and to):
        return False, "SMTP non configuré"

    msg = EmailMessage()
    msg["Subject"] = _sujet_email(doc)
    msg["From"] = _get("MAIL_FROM", "") or _get("SMTP_USER", "")
    msg["To"] = ", ".join(to)
    if _get("MAIL_CC", []):
        msg["Cc"] = ", ".join(_get("MAIL_CC", []))
    msg.set_content(_corps_email(doc))

    if attach and pdf_bytes:
        msg.add_attachment(
            pdf_bytes, maintype="application", subtype="pdf",
            filename=doc.get("nom_fichier", "document.pdf"),
        )

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            _get("SMTP_HOST", "smtp.zoho.com"), _get("SMTP_PORT", 465),
            context=ctx, timeout=20,
        ) as s:
            s.login(_get("SMTP_USER", ""), _get("SMTP_PASS", ""))
            s.send_message(msg)
        return True, f"SMTP — envoyé à {len(to)} destinataire(s)"
    except Exception as exc:  # noqa: BLE001
        return False, f"SMTP — {exc}"


def notify_email(doc, pdf_bytes=None, attach=False):
    """Aiguille selon EMAIL_MODE, avec bascule automatique si l'API echoue."""
    mode = str(_get("EMAIL_MODE", "api")).lower()

    if mode == "smtp":
        return notify_email_smtp(doc, pdf_bytes=pdf_bytes, attach=attach)

    ok, detail = notify_email_api(doc)
    if ok:
        return ok, detail

    # L'API a echoue : on tente SMTP s'il est configure, sans masquer l'erreur.
    if _get("SMTP_USER", "") and _get("SMTP_PASS", ""):
        ok2, detail2 = notify_email_smtp(doc, pdf_bytes=pdf_bytes, attach=attach)
        if ok2:
            return True, f"{detail2} (repli, API KO : {detail})"
        return False, f"API KO : {detail} | SMTP KO : {detail2}"

    return False, detail


# ===========================================================================
# 3. ZOHO SIGN
# ===========================================================================
def _dimensions_page(pdf_bytes):
    """Largeur et hauteur de la page, lues dans le /MediaBox du PDF.

    Les courriers ne sont pas tous en A4 : scans en Letter, Legal, ou formats
    exotiques issus de photocopieurs. Supposer 595 x 842 place la signature
    hors de la page sur un document plus petit, ou trop haut sur un plus grand.

    Retourne (largeur, hauteur) en points, avec A4 en repli.
    """
    try:
        import re as _re
        boites = _re.findall(
            rb"/MediaBox\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)",
            pdf_bytes,
        )
        if not boites:
            return 595.0, 842.0
        # On prend la derniere : c'est la page ou ira la signature.
        x0, y0, x1, y1 = (float(v) for v in boites[-1])
        largeur = abs(x1 - x0)
        hauteur = abs(y1 - y0)
        if largeur < 50 or hauteur < 50:
            return 595.0, 842.0
        return largeur, hauteur
    except Exception:  # noqa: BLE001
        return 595.0, 842.0


def _nombre_de_pages(pdf_bytes):
    """Nombre de pages du PDF.

    La lecture directe de la structure du fichier (/Count, /Type /Page) s'est
    revelee peu fiable : sur certains documents elle surestime, la signature
    part alors sur une page inexistante et la derniere page reelle ne recoit
    qu'un paraphe. On passe donc par une bibliotheque quand elle est
    disponible, et la lecture brute ne sert plus que de dernier recours.
    """
    import io as _io

    # 1. pypdf / PyPDF2 : lecture fiable de l'arborescence des pages.
    for module, classe in (("pypdf", "PdfReader"), ("PyPDF2", "PdfReader")):
        try:
            mod = __import__(module)
            lecteur = getattr(mod, classe)(_io.BytesIO(pdf_bytes))
            n = len(lecteur.pages)
            if n > 0:
                return n
        except Exception:  # noqa: BLE001
            continue

    # 2. Repli : lecture brute. Le /Count du noeud racine est le plus sur,
    #    mais un PDF revise peut en contenir plusieurs — on prend le minimum
    #    plutot que le maximum, pour ne jamais viser une page inexistante.
    try:
        import re as _re
        comptes = [int(c) for c in
                   _re.findall(rb"/Type\s*/Pages[^>]*?/Count\s+(\d+)", pdf_bytes)]
        comptes = [c for c in comptes if c > 0]
        if comptes:
            return min(comptes)

        pages = _re.findall(rb"/Type\s*/Page[^s]", pdf_bytes)
        if pages:
            return len(pages)
    except Exception:  # noqa: BLE001
        pass

    return 1



def send_for_signature(connector, doc, pdf_bytes=None, pdf_path=None):
    """Crée une demande Zoho Sign et l'envoie au DG. Retourne (ok, detail)."""
    dg_email = _get("DG_EMAIL", "")
    dg_name = _get("DG_NAME", "")
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

    create_body = {
        "requests": {
            "request_name": f"{doc.get('numero', '')} — {filename}",
            "expiration_days": _get("SIGN_EXPIRATION_DAYS", 15),
            "is_sequential": True,
            "actions": [{
                "action_type": "SIGN",
                "recipient_email": dg_email,
                "recipient_name": dg_name,
                "signing_order": 0,
                "verify_recipient": False,
            }],
        }
    }

    # L'upload du PDF est la seule etape lourde du circuit : sur une liaison
    # lente, 60 s ne suffisent pas. On allonge et on retente une fois — un
    # timeout reseau ne doit pas obliger la responsable GED a tout ressaisir.
    out = None
    derniere_erreur = None
    for tentative in (1, 2):
        try:
            resp = requests.post(
                f"{_sign_base()}/requests",
                headers=headers,
                files={"file": (filename, pdf_bytes, "application/pdf")},
                data={"data": json.dumps(create_body)},
                timeout=(30, 180),
            )
            out = resp.json()
            break
        except requests.exceptions.RequestException as exc:
            derniere_erreur = exc
            if tentative == 1:
                print(f"[SIGN] Timeout, nouvelle tentative... ({exc})")
                time.sleep(3)
        except Exception as exc:  # noqa: BLE001
            return False, f"création : {exc}"

    if out is None:
        taille = len(pdf_bytes) / 1024
        return False, (f"création : connexion interrompue après 2 tentatives "
                       f"({taille:.0f} Ko) — {derniere_erreur}")

    if out.get("status") != "success":
        return False, f"création refusée : {json.dumps(out)[:300]}"

    try:
        req = out["requests"]
        request_id = req["request_id"]
        action_id = req["actions"][0]["action_id"]
        document_id = req["document_ids"][0]["document_id"]
    except (KeyError, IndexError) as exc:
        return False, f"réponse Sign inattendue : {exc}"

    # Zoho Sign refuse une soumission sans zone de signature positionnee sur le
    # document (erreur 9101). On place donc un champ Signature obligatoire.
    # Coordonnees en points PDF, origine en haut a gauche, page_no indexee a 0.
    # La signature doit figurer sur la DERNIERE page du courrier, comme sur un
    # document papier. SIGN_FIELD_PAGE permet de forcer une page precise
    # (indexee a 0) si un type de document l'exige.
    nb_pages_doc = _nombre_de_pages(pdf_bytes)

    page_forcee = _get("SIGN_FIELD_PAGE", None)
    if page_forcee is None:
        page_signature = max(0, nb_pages_doc - 1)
    else:
        page_signature = page_forcee

    # Garde-fou : si le comptage se trompait, la signature viserait une page
    # inexistante et seul un paraphe apparaitrait en fin de document.
    page_signature = max(0, min(page_signature, nb_pages_doc - 1))

    # Placement PROPORTIONNEL a la taille reelle de la page, pas en valeurs
    # absolues : un courrier en Letter, Legal ou format photocopieur n'a pas
    # les memes dimensions qu'un A4, et la signature tombait hors cadre.
    largeur, hauteur = _dimensions_page(pdf_bytes)

    largeur_champ = _get("SIGN_FIELD_WIDTH", 170)
    hauteur_champ = _get("SIGN_FIELD_HEIGHT", 45)

    # Exprimes en fraction de la page : 0.55 = 55 % depuis la gauche,
    # 0.72 = 72 % depuis le haut. Ajustables dans notifier_config.py.
    pos_x = _get("SIGN_POS_X", 0.55)
    pos_y = _get("SIGN_POS_Y", 0.72)

    x_coord = round(largeur * pos_x)
    y_coord = round(hauteur * pos_y)

    # Garde-fou : le champ doit rester entierement dans la page.
    x_coord = max(10, min(x_coord, largeur - largeur_champ - 10))
    y_coord = max(10, min(y_coord, hauteur - hauteur_champ - 10))

    # Les valeurs absolues restent prioritaires si elles sont definies.
    if _get("SIGN_FIELD_X", None) is not None:
        x_coord = _get("SIGN_FIELD_X")
    if _get("SIGN_FIELD_Y", None) is not None:
        y_coord = _get("SIGN_FIELD_Y")

    nb_pages = nb_pages_doc

    print(f"[SIGN] {nb_pages} page(s) — format {largeur:.0f}x{hauteur:.0f} pts "
          f"— signature page {page_signature + 1} en ({x_coord}, {y_coord})")

    champs = [{
        "field_name": "Signature",
        "field_label": "Signature",
        "field_type_name": "Signature",
        "field_category": "signature",
        "document_id": document_id,
        "page_no": page_signature,
        "x_coord": x_coord,
        "y_coord": y_coord,
        "abs_width": largeur_champ,
        "abs_height": hauteur_champ,
        "is_mandatory": True,
    }]

    # --- Paraphes ---------------------------------------------------------
    # Usage documentaire : chaque page est paraphee, la derniere est signee.
    # Desactivable via PARAPHER_PAGES = False dans notifier_config.py.
    if _get("PARAPHER_PAGES", True) and nb_pages > 1:
        p_largeur = _get("PARAPHE_WIDTH", 60)
        p_hauteur = _get("PARAPHE_HEIGHT", 30)
        # Bas de page, a droite : 78 % de la largeur, 90 % de la hauteur.
        p_x = round(largeur * _get("PARAPHE_POS_X", 0.78))
        p_y = round(hauteur * _get("PARAPHE_POS_Y", 0.90))
        p_x = max(10, min(p_x, largeur - p_largeur - 10))
        p_y = max(10, min(p_y, hauteur - p_hauteur - 10))

        for page in range(nb_pages):
            if page == page_signature:
                continue  # la page de signature n'est pas paraphee
            champs.append({
                "field_name": f"Paraphe_{page + 1}",
                "field_label": "Paraphe",
                "field_type_name": "Initial",
                "field_category": "initial",
                "document_id": document_id,
                "page_no": page,
                "x_coord": p_x,
                "y_coord": p_y,
                "abs_width": p_largeur,
                "abs_height": p_hauteur,
                "is_mandatory": True,
            })

        print(f"[SIGN] {len(champs) - 1} paraphe(s) ajoute(s)")

    submit_body = {
        "requests": {
            "actions": [{
                "action_id": action_id,
                "action_type": "SIGN",
                "recipient_email": dg_email,
                "recipient_name": dg_name,
                "verify_recipient": False,
                "fields": champs,
            }]
        }
    }

    try:
        resp = requests.post(
            f"{_sign_base()}/requests/{request_id}/submit",
            headers=headers,
            data={"data": json.dumps(submit_body)},
            timeout=(30, 120),
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
    """Déclenche tous les canaux. Ne lève jamais. Retourne un dict de statuts."""
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
    """(statut_cliq, statut_sign, sign_request_id) pour le registre Sheet."""
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
# DIAGNOSTIC : python notifier.py [--comptes]
# ===========================================================================
if __name__ == "__main__":
    if "--comptes" in sys.argv:
        print("\n=== COMPTES MAIL ACCESSIBLES ===\n")
        try:
            for c in lister_comptes_mail():
                print(f"  accountId  : {c['accountId']}")
                print(f"  principal  : {c['principal']}")
                print(f"  expediteurs: {', '.join(c['expediteurs']) or '(aucun)'}")
                print()
            print("Mets une de ces adresses dans MAIL_FROM_ADDRESS.")
        except Exception as exc:  # noqa: BLE001
            print(f"  ECHEC : {exc}")
        sys.exit(0)

    doc_test = {
        "numero": "HM26-TEST-001",
        "nom_fichier": "document_de_test.pdf",
        "service": "Direction Générale",
        "type_doc": "Note interne",
        "deposant": "Nacer",
        "date": "31/07/2026",
        "workdrive_url": "https://workdrive.zoho.com",
        "sheet_url": ("https://sheet.zoho.com/sheet/open/"
                      "avaji4d47ec1cbffc4f5184dc8b3127b83ae7"),
    }

    print("=== TEST NOTIFIER ===")
    print(f"Racine projet : {ROOT_DIR}")
    print(f"Canal Cliq    : {_get('CLIQ_CHANNEL', '') or '(non configure)'}")
    print(f"Mode email    : {_get('EMAIL_MODE', 'api')}")
    print(f"Expediteur    : {_get('MAIL_FROM_ADDRESS', '') or '(non configure)'}")
    print(f"Config        : {'notifier_config.py' if hasattr(CFG, '__file__') else 'variables d environnement'}")
    print()

    resultats = dispatch(doc_test)

    print()
    print("Statuts registre :", statuts_pour_registre(resultats))
