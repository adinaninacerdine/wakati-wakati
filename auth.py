"""
auth.py — Authentification SSO Zoho pour l'application GED
===========================================================
Aucun mot de passe n'est stocké ni géré par l'application. L'utilisateur
s'authentifie chez Zoho, qui renvoie son adresse ; l'accès est ensuite
accordé ou refusé selon une liste blanche.

Conséquence importante : un départ traité dans la console Zoho ferme
immédiatement l'accès à la GED, sans intervention sur l'application.

CONFIGURATION — à placer dans auth_config.py (JAMAIS versionné)
---------------------------------------------------------------
    # Client OAuth de type « Server-based Application » créé sur
    # api-console.zoho.com. Ce n'est PAS le Self Client des appels API :
    # celui-ci exige une URI de redirection.
    SSO_CLIENT_ID     = "1000.XXXXXXXX"
    SSO_CLIENT_SECRET = "xxxxxxxxxxxx"
    SSO_REDIRECT_URI  = "https://ged.wakati.km/oauth/callback"

    # Clé de signature des sessions. Générer une valeur aléatoire longue :
    #   python -c "import secrets; print(secrets.token_hex(32))"
    SECRET_KEY = "..."

    # Seules ces adresses peuvent entrer. Casse ignorée.
    UTILISATEURS_AUTORISES = [
        "nacer-dine.a@hurimoney.com",
        "ged@hurimoney.com",
    ]

    # Durée de session en heures.
    DUREE_SESSION_H = 12

MISE EN PLACE DU CLIENT OAUTH
-----------------------------
  1. api-console.zoho.com -> Add Client -> Server-based Application
  2. Authorized Redirect URI : https://ged.wakati.km/oauth/callback
     (en développement local : http://127.0.0.1:5000/oauth/callback)
  3. Récupérer Client ID et Client Secret

INTÉGRATION DANS app.py
-----------------------
    from auth import init_auth, connexion_requise

    init_auth(app)          # après la création de l'objet Flask

    @app.route('/')
    @connexion_requise      # sur chaque route à protéger
    def index():
        ...
"""

import functools
import os
import secrets
import sys
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import requests
from flask import (redirect, render_template_string, request, session, url_for)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
for parent in [CURRENT_DIR] + list(CURRENT_DIR.parents):
    if (parent / "zoho_config.py").exists():
        if str(parent) not in sys.path:
            sys.path.insert(0, str(parent))
        break

try:
    import auth_config as CFG
except ImportError:
    class CFG:  # noqa: N801
        SSO_CLIENT_ID = os.getenv("SSO_CLIENT_ID", "")
        SSO_CLIENT_SECRET = os.getenv("SSO_CLIENT_SECRET", "")
        SSO_REDIRECT_URI = os.getenv("SSO_REDIRECT_URI", "")
        SECRET_KEY = os.getenv("SECRET_KEY", "")
        UTILISATEURS_AUTORISES = [
            x.strip() for x in os.getenv("UTILISATEURS_AUTORISES", "").split(",") if x.strip()
        ]
        DUREE_SESSION_H = int(os.getenv("DUREE_SESSION_H", "12"))

ACCOUNTS = "https://accounts.zoho.com"
SCOPE_SSO = "AaaServer.profile.READ"


def _get(nom, defaut=None):
    v = getattr(CFG, nom, defaut)
    return defaut if v is None else v


def _autorises():
    return {a.strip().lower() for a in _get("UTILISATEURS_AUTORISES", []) if a.strip()}


# ---------------------------------------------------------------------------
# Récupération de l'identité
# ---------------------------------------------------------------------------
# Le nom exact de l'endpoint de profil varie selon les comptes Zoho. On teste
# les variantes connues au premier appel — même approche que pour les autres
# API : on ne suppose pas, on vérifie.
ENDPOINTS_PROFIL = [
    f"{ACCOUNTS}/oauth/user/info",
    f"{ACCOUNTS}/oauth/v2/userinfo",
]


def _identite(access_token):
    """Retourne (email, nom) ou (None, message d'erreur)."""
    entetes = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    erreurs = []

    for url in ENDPOINTS_PROFIL:
        try:
            r = requests.get(url, headers=entetes, timeout=20)
        except Exception as exc:  # noqa: BLE001
            erreurs.append(f"{url} : {exc}")
            continue

        if r.status_code != 200:
            erreurs.append(f"{url} : HTTP {r.status_code} {r.text[:120]}")
            continue

        try:
            d = r.json()
        except ValueError:
            erreurs.append(f"{url} : réponse illisible")
            continue

        email = (d.get("Email") or d.get("email")
                 or d.get("primary_email") or d.get("ZUID_EMAIL"))
        nom = (d.get("Display_Name") or d.get("displayName")
               or d.get("name") or d.get("First_Name") or "")

        if email:
            return email, nom

        erreurs.append(f"{url} : aucun email dans {list(d)[:8]}")

    return None, " | ".join(erreurs)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
PAGE_CONNEXION = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GED WAKATI — Connexion</title>
<style>
  body { font-family: Calibri, "Segoe UI", system-ui, sans-serif; margin: 0;
         min-height: 100vh; display: flex; align-items: center;
         justify-content: center; background: #F7F8FA; color: #1B2733; }
  .carte { background: #fff; border: 1px solid #DFE4EA; border-radius: 8px;
           padding: 44px 40px; max-width: 420px; width: 92%; text-align: center; }
  .marque { font-family: "Calibri Light", Calibri, sans-serif; font-size: 30px;
            letter-spacing: 7px; color: #03224C; font-weight: 300; }
  .slogan { color: #E60E06; font-size: 12px; letter-spacing: 2px;
            font-weight: 600; margin-bottom: 26px; }
  h1 { font-size: 17px; font-weight: 400; color: #03224C;
       margin: 0 0 8px; font-family: "Calibri Light", Calibri, sans-serif; }
  p { font-size: 14px; color: #5B6B7F; margin: 0 0 26px; line-height: 1.55; }
  a.bouton { display: block; background: #03224C; color: #fff; padding: 12px;
             border-radius: 4px; text-decoration: none; font-weight: 600;
             font-size: 15px; letter-spacing: .3px; }
  a.bouton:hover { background: #0A2E5E; }
  .err { background: #FDECEB; border-left: 4px solid #E60E06; color: #8E1109;
         padding: 11px 15px; border-radius: 4px; font-size: 13.5px;
         text-align: left; margin-bottom: 22px; }
  .pied { margin-top: 28px; font-size: 11.5px; color: #5B6B7F;
          border-top: 1px solid #DFE4EA; padding-top: 16px; }
</style></head>
<body>
  <div class="carte">
    <div class="marque">W A K A T I</div>
    <div class="slogan">NDUWO UWO</div>
    <h1>Gestion électronique des documents</h1>
    {% if erreur %}<div class="err">{{ erreur }}</div>{% endif %}
    <p>L'accès est réservé aux personnes habilitées.
       Utilisez votre compte professionnel Zoho.</p>
    <a class="bouton" href="{{ url_for('oauth_connexion') }}">Se connecter avec Zoho</a>
    <div class="pied">Huri Money SA — WAKATI Mobile Money<br>Union des Comores</div>
  </div>
</body></html>"""


# ---------------------------------------------------------------------------
# Décorateur
# ---------------------------------------------------------------------------
def connexion_requise(vue):
    """Protège une route : redirige vers la connexion si la session est absente
    ou expirée."""
    @functools.wraps(vue)
    def enveloppe(*args, **kwargs):
        email = session.get("email")
        expire = session.get("expire")

        if not email or not expire:
            return redirect(url_for("page_connexion"))

        try:
            if datetime.fromisoformat(expire) < datetime.now():
                session.clear()
                return redirect(url_for("page_connexion", motif="expiree"))
        except (ValueError, TypeError):
            session.clear()
            return redirect(url_for("page_connexion"))

        # Contrôle à chaque requête : retirer une adresse de la liste blanche
        # coupe l'accès immédiatement, sans attendre l'expiration de session.
        if email.lower() not in _autorises():
            session.clear()
            return redirect(url_for("page_connexion", motif="revoque"))

        return vue(*args, **kwargs)
    return enveloppe


# ---------------------------------------------------------------------------
# Enregistrement des routes
# ---------------------------------------------------------------------------
def init_auth(app):
    """Ajoute les routes d'authentification et configure la session."""

    cle = _get("SECRET_KEY", "")
    if not cle:
        cle = secrets.token_hex(32)
        print("[AUTH] SECRET_KEY absente : clé provisoire générée. "
              "Les sessions seront perdues au redémarrage.")
    app.secret_key = cle

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Le cookie n'est transmis qu'en HTTPS. À laisser à False uniquement
        # pour un test en local sur http://127.0.0.1.
        SESSION_COOKIE_SECURE=_get("COOKIE_SECURE", True),
    )

    @app.route("/connexion")
    def page_connexion():
        motifs = {
            "expiree": "Votre session a expiré. Veuillez vous reconnecter.",
            "revoque": "Votre accès n'est plus actif.",
            "refuse": "Cette adresse n'est pas autorisée à accéder à l'application.",
        }
        erreur = motifs.get(request.args.get("motif"))
        if request.args.get("motif") == "technique":
            erreur = "La connexion a échoué. Contactez l'administration technique."
        return render_template_string(PAGE_CONNEXION, erreur=erreur)

    @app.route("/oauth/connexion")
    def oauth_connexion():
        if not _get("SSO_CLIENT_ID"):
            return "Configuration SSO absente (auth_config.py).", 500

        # Jeton anti-rejeu : vérifié au retour de Zoho.
        etat = secrets.token_urlsafe(24)
        session["oauth_etat"] = etat

        params = {
            "scope": SCOPE_SSO,
            "client_id": _get("SSO_CLIENT_ID"),
            "response_type": "code",
            "redirect_uri": _get("SSO_REDIRECT_URI"),
            "access_type": "online",
            "state": etat,
            "prompt": "consent",
        }
        return redirect(f"{ACCOUNTS}/oauth/v2/auth?" + urllib.parse.urlencode(params))

    @app.route("/oauth/callback")
    def oauth_callback():
        if request.args.get("state") != session.pop("oauth_etat", None):
            print("[AUTH] État OAuth invalide — tentative rejetée.")
            return redirect(url_for("page_connexion", motif="technique"))

        code = request.args.get("code")
        if not code:
            return redirect(url_for("page_connexion", motif="technique"))

        try:
            r = requests.post(f"{ACCOUNTS}/oauth/v2/token", params={
                "code": code,
                "client_id": _get("SSO_CLIENT_ID"),
                "client_secret": _get("SSO_CLIENT_SECRET"),
                "redirect_uri": _get("SSO_REDIRECT_URI"),
                "grant_type": "authorization_code",
            }, timeout=30)
            jeton = r.json().get("access_token")
        except Exception as exc:  # noqa: BLE001
            print(f"[AUTH] Échange du code impossible : {exc}")
            return redirect(url_for("page_connexion", motif="technique"))

        if not jeton:
            print(f"[AUTH] Aucun access_token dans la réponse : {r.text[:200]}")
            return redirect(url_for("page_connexion", motif="technique"))

        email, info = _identite(jeton)
        if not email:
            print(f"[AUTH] Identité illisible — {info}")
            return redirect(url_for("page_connexion", motif="technique"))

        if email.lower() not in _autorises():
            print(f"[AUTH] Accès refusé : {email}")
            return redirect(url_for("page_connexion", motif="refuse"))

        heures = _get("DUREE_SESSION_H", 12)
        session["email"] = email
        session["nom"] = info or email.split("@")[0]
        session["expire"] = (datetime.now() + timedelta(hours=heures)).isoformat()
        session.permanent = False

        print(f"[AUTH] Connexion : {email}")
        return redirect(url_for("index"))

    @app.route("/deconnexion")
    def deconnexion():
        email = session.get("email", "?")
        session.clear()
        print(f"[AUTH] Déconnexion : {email}")
        return redirect(url_for("page_connexion"))

    # Rend l'utilisateur disponible dans tous les gabarits.
    @app.context_processor
    def _contexte():
        return {
            "utilisateur": session.get("nom"),
            "utilisateur_email": session.get("email"),
        }

    print(f"[AUTH] SSO Zoho actif — {len(_autorises())} utilisateur(s) autorisé(s)")
