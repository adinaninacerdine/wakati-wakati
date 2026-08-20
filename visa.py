"""
visa.py — Visa interne et suites a donner
==========================================
Remplace Zoho Sign pour les courriers ENTRANTS : le DG atteste avoir vu le
document et consigne ses observations, sans consommer d'enveloppe Sign.

POURQUOI CE MODULE
------------------
Le processus reel n'est pas une signature mais un visa : le DG annote le
courrier au stylo, et ses remarques declenchent des actions — corriger un
sortant, notifier un collaborateur, repondre a l'expediteur. Ce circuit
n'existait nulle part dans l'outil.

Zoho Sign reste utilise pour les courriers SORTANTS, qui engagent
juridiquement l'entreprise. Le visa interne n'a pas la meme valeur probante,
mais il correspond exactement a ce que faisait le paraphe manuscrit.

CIRCUIT
-------
  1. La responsable GED depose un courrier entrant -> a_viser/
  2. Le DG ouvre l'application, consulte le PDF, saisit ses observations
     et choisit « Viser » ou « Retourner au service »
  3. Vise    -> le document passe en a_classer/ pour archivage
     Retourne -> il revient a la responsable GED avec le motif
  4. Les observations non vides creent une SUITE A DONNER, suivie jusqu'a
     sa cloture

CONFIGURATION — a ajouter dans auth_config.py
---------------------------------------------
    # Adresses qui accedent a l'ecran de visa. Les autres utilisateurs
    # authentifies ne le voient pas.
    UTILISATEURS_DG = [
        "dg@hurimoney.com",
    ]

INTEGRATION — deux lignes dans app.py
-------------------------------------
    from visa import bp_visa, init_visa, deposer_pour_visa
    init_visa(app, connexion_requise)
"""

import functools
import json
import logging
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, send_from_directory, session, url_for)

# ---------------------------------------------------------------------------
# Emplacements
# ---------------------------------------------------------------------------
RACINE = Path(__file__).resolve().parent
DOSSIER_A_VISER = RACINE / "a_viser"
DOSSIER_A_CLASSER = RACINE / "a_classer"
FICHIER_VISAS = RACINE / "visas.json"

bp_visa = Blueprint("visa", __name__)

# Rempli par init_visa()
_protege = None


# ---------------------------------------------------------------------------
# Persistance
# ---------------------------------------------------------------------------
def _charger():
    if not FICHIER_VISAS.exists():
        return {}
    try:
        return json.loads(FICHIER_VISAS.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logging.error("visas.json illisible")
        return {}


def _sauver(donnees):
    FICHIER_VISAS.write_text(
        json.dumps(donnees, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _nettoyer(texte):
    """Translitere et normalise pour un nom de fichier sur."""
    if not texte:
        return "SANS_OBJET"
    texte = unicodedata.normalize("NFKD", texte.upper())
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    texte = texte.replace(" ", "_")
    return re.sub(r"[^A-Z0-9_\-]", "", texte)[:60]


def _reference():
    """Reference unique et lisible : VISA-AAAAMMJJ-HHMMSS."""
    return datetime.now().strftime("VISA-%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
def _adresses_dg():
    try:
        import auth_config
        return {a.strip().lower() for a in getattr(auth_config, "UTILISATEURS_DG", [])}
    except ImportError:
        return set()


def est_dg():
    """En local sans authentification, tout le monde voit l'ecran de visa."""
    email = session.get("email")
    if not email:
        return True
    return email.lower() in _adresses_dg()


def _reserve_dg(vue):
    @functools.wraps(vue)
    def enveloppe(*args, **kwargs):
        if not est_dg():
            flash("Cet écran est réservé à la Direction Générale.", "error")
            return redirect(url_for("index"))
        return vue(*args, **kwargs)
    return enveloppe


# ---------------------------------------------------------------------------
# API utilisee par app.py
# ---------------------------------------------------------------------------
def deposer_pour_visa(chemin_pdf, metadonnees):
    """Place un courrier dans le circuit de visa. Retourne la reference.

    chemin_pdf   : fichier temporaire, copie et non deplace
    metadonnees  : service, type_doc, date_doc, objet, sens, correspondant,
                   deposant
    """
    DOSSIER_A_VISER.mkdir(exist_ok=True)

    ref = _reference()
    objet = _nettoyer(metadonnees.get("objet", ""))
    nom = (f"AVISER_HM26-{metadonnees.get('service', 'XXX')}-"
           f"{metadonnees.get('type_doc', 'XXX')}_"
           f"{str(metadonnees.get('date_doc', '')).replace('-', '')}_"
           f"{objet}_{ref[-6:]}.pdf")

    shutil.copy2(chemin_pdf, DOSSIER_A_VISER / nom)

    donnees = _charger()
    donnees[ref] = {
        "fichier": nom,
        "service": metadonnees.get("service", ""),
        "type_doc": metadonnees.get("type_doc", ""),
        "date_doc": metadonnees.get("date_doc", ""),
        "objet": metadonnees.get("objet", ""),
        "objet_nettoye": objet,
        "sens": metadonnees.get("sens", "entrant"),
        "correspondant": metadonnees.get("correspondant", ""),
        "deposant": metadonnees.get("deposant", "Service GED"),
        "depose_le": datetime.now().isoformat(timespec="seconds"),
        "statut": "a_viser",
        "vise_par": "",
        "vise_le": "",
        "observations": "",
        "action_statut": "",
        "action_note": "",
    }
    _sauver(donnees)
    return ref


def _enrichir(ref, d, services, types):
    try:
        depose = datetime.fromisoformat(d.get("depose_le", ""))
    except (ValueError, TypeError):
        depose = datetime.now()

    return {
        "reference": ref,
        "fichier": d.get("fichier", ""),
        "objet": d.get("objet", "(sans objet)"),
        "service": d.get("service", ""),
        "type_doc": d.get("type_doc", ""),
        "service_lib": services.get(d.get("service", ""), d.get("service") or "—"),
        "type_lib": types.get(d.get("type_doc", ""), d.get("type_doc") or "—"),
        "date_doc": d.get("date_doc", ""),
        "sens": d.get("sens", "entrant"),
        "correspondant": d.get("correspondant", ""),
        "depose_affiche": depose.strftime("%d/%m/%Y"),
        "jours": (datetime.now() - depose).days,
        "observations": d.get("observations", ""),
        "vise_par": d.get("vise_par", ""),
        "vise_le": d.get("vise_le", ""),
        "action_statut": d.get("action_statut", ""),
        "statut": d.get("statut", ""),
    }


def lister_a_viser(services=None, types=None):
    """Courriers en attente du visa du DG, les plus anciens d'abord."""
    services, types = services or {}, types or {}
    liste = [_enrichir(r, d, services, types)
             for r, d in _charger().items() if d.get("statut") == "a_viser"]
    liste.sort(key=lambda x: x["jours"], reverse=True)
    return liste


def lister_suites(services=None, types=None):
    """Observations du DG appelant une action, non encore cloturees."""
    services, types = services or {}, types or {}
    liste = [_enrichir(r, d, services, types)
             for r, d in _charger().items()
             if d.get("action_statut") == "ouverte"]
    liste.sort(key=lambda x: x["jours"], reverse=True)
    return liste



def lister_approuves(services=None, types=None):
    """Courriers sortants approuves, en attente du scan signe."""
    services, types = services or {}, types or {}
    return [_enrichir(r, d, services, types)
            for r, d in _charger().items() if d.get("statut") == "approuve"]

def lister_retournes(services=None, types=None):
    """Courriers renvoyes au service par le DG."""
    services, types = services or {}, types or {}
    return [_enrichir(r, d, services, types)
            for r, d in _charger().items() if d.get("statut") == "retourne"]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@bp_visa.route("/visa")
def ecran_visa():
    from app import SERVICES, TYPES  # noqa: PLC0415
    if not est_dg():
        flash("Cet écran est réservé à la Direction Générale.", "error")
        return redirect(url_for("index"))
    return render_template("visa.html",
                           a_viser=lister_a_viser(SERVICES, TYPES),
                           services=SERVICES, types=TYPES)


@bp_visa.route("/visa/<reference>/document")
def document(reference):
    """Sert le PDF en consultation dans le navigateur."""
    d = _charger().get(reference)
    if not d:
        abort(404)
    # Garde-fou : seul un nom de fichier issu du registre est servi.
    nom = d.get("fichier", "")
    if not nom or nom != Path(nom).name:
        abort(404)
    return send_from_directory(DOSSIER_A_VISER, nom, mimetype="application/pdf")


@bp_visa.route("/visa/<reference>", methods=["POST"])
def enregistrer(reference):
    if not est_dg():
        flash("Action réservée à la Direction Générale.", "error")
        return redirect(url_for("index"))

    donnees = _charger()
    d = donnees.get(reference)
    if not d or d.get("statut") != "a_viser":
        flash("Ce courrier n'est plus en attente de visa.", "error")
        return redirect(url_for("visa.ecran_visa"))

    decision = request.form.get("decision", "")
    observations = (request.form.get("observations") or "").strip()

    d["observations"] = observations
    d["vise_par"] = session.get("nom") or session.get("email") or "Direction Générale"
    d["vise_le"] = datetime.now().isoformat(timespec="seconds")

    source = DOSSIER_A_VISER / d["fichier"]

    if decision == "viser":
        # Un courrier SORTANT approuve doit encore etre imprime, signe a la
        # main puis rescanne : il n'est pas archivable en l'etat. Un ENTRANT
        # vise, lui, part directement a l'archivage.
        if d.get("sens") == "sortant":
            d["statut"] = "approuve"
            d["approuve_le"] = datetime.now().isoformat(timespec="seconds")
            _sauver(donnees)
            flash("Courrier approuvé. À imprimer, faire signer, puis déposer "
                  "le scan signé.", "success")
            return redirect(url_for("visa.ecran_visa"))

        d["statut"] = "vise"

        # Le document rejoint le circuit d'archivage existant. On reprend le
        # prefixe ASIGNER_ pour que l'ecran de classement le reconnaisse sans
        # modification.
        DOSSIER_A_CLASSER.mkdir(exist_ok=True)
        cible = DOSSIER_A_CLASSER / d["fichier"].replace("AVISER_", "ASIGNER_", 1)
        try:
            if source.exists():
                shutil.move(str(source), str(cible))
        except Exception:  # noqa: BLE001
            logging.error("Déplacement vers a_classer impossible : %s", source)

        # Une observation non vide devient une suite a donner.
        if observations:
            d["action_statut"] = "ouverte"

        message = "Courrier visé."
        if observations:
            message += " Vos observations ont été transmises au service."

    elif decision == "retourner":
        # Sans motif, le service ne sait pas quoi corriger : on refuse.
        if not observations:
            flash("Merci d'indiquer le motif du retour.", "error")
            return redirect(url_for("visa.ecran_visa"))
        d["statut"] = "retourne"
        d["action_statut"] = "ouverte"
        message = "Courrier retourné au service documentaire."

    else:
        flash("Décision non reconnue.", "error")
        return redirect(url_for("visa.ecran_visa"))

    _sauver(donnees)

    # Notification du service documentaire, sans bloquer si elle echoue.
    try:
        from notifier import notify_cliq
        notify_cliq({
            "numero": "Visa DG" if decision == "viser" else "Retour DG",
            "nom_fichier": d.get("objet", ""),
            "service": d.get("service", "-"),
            "type_doc": d.get("type_doc", "-"),
            "deposant": d.get("correspondant", "-"),
            "date": d.get("date_doc", "-"),
        })
    except Exception as exc:  # noqa: BLE001
        logging.error("Notification visa impossible : %s", exc)

    flash(message, "success")
    return redirect(url_for("visa.ecran_visa"))


@bp_visa.route("/suites/<reference>/cloturer", methods=["POST"])
def cloturer(reference):
    """La responsable GED marque la suite comme traitee."""
    donnees = _charger()
    d = donnees.get(reference)
    if not d:
        flash("Référence inconnue.", "error")
        return redirect(url_for("index"))

    d["action_statut"] = "close"
    d["action_note"] = (request.form.get("note") or "").strip()
    d["action_close_le"] = datetime.now().isoformat(timespec="seconds")
    _sauver(donnees)

    flash("Suite marquée comme traitée.", "success")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------
def init_visa(app, protection=None):
    """Enregistre le blueprint et applique la protection d'authentification.

    protection : le decorateur connexion_requise d'app.py. Absent en local,
    ou l'authentification est desactivee.
    """
    global _protege
    _protege = protection

    if protection is not None:
        for nom, vue in list(app.view_functions.items()):
            if nom.startswith("visa."):
                app.view_functions[nom] = protection(vue)

    DOSSIER_A_VISER.mkdir(exist_ok=True)
    DOSSIER_A_CLASSER.mkdir(exist_ok=True)

    print(f"[VISA] Circuit de visa actif — {len(_adresses_dg())} adresse(s) DG")

@bp_visa.route("/visa/<reference>/resoumettre", methods=["POST"])
def resoumettre(reference):
    """Remet un document corrige en attente de visa.

    Le retour n'est pas une fin de parcours : le document repart chez le
    DG apres correction. Sans cette route, un courrier retourne restait
    bloque sans qu'aucun ecran ne permette de le relancer.
    """
    donnees = _charger()
    d = donnees.get(reference)
    if not d:
        flash("Document introuvable.", "error")
        return redirect(url_for("index"))

    if d.get("statut") != "retourne":
        flash("Seul un document retourné peut être resoumis.", "error")
        return redirect(url_for("index"))

    d["statut"] = "attente"
    d["action_statut"] = ""
    d["resoumis_le"] = datetime.now().isoformat(timespec="seconds")
    d["nb_retours"] = int(d.get("nb_retours", 0)) + 1
    # On conserve le motif du retour precedent dans l'historique.
    d.setdefault("historique_retours", []).append({
        "le": d.get("vise_le", ""),
        "motif": d.get("observations", ""),
    })
    d["observations"] = ""
    _sauver(donnees)

    flash(f"Courrier « {d.get('objet', '')} » resoumis au visa.", "success")
    return redirect(url_for("index"))

@bp_visa.route("/visa/<reference>/scan", methods=["POST"])
def deposer_scan(reference):
    """Recoit le scan du document signe a la main.

    Le fichier archive n'est pas celui qui a ete soumis : c'est le scan
    portant la signature manuscrite. On remplace donc le document avant
    de l'envoyer au circuit d'archivage.
    """
    donnees = _charger()
    d = donnees.get(reference)
    if not d:
        flash("Document introuvable.", "error")
        return redirect(url_for("index"))

    if d.get("statut") != "approuve":
        flash("Ce courrier n'est pas en attente de signature.", "error")
        return redirect(url_for("index"))

    fichier = request.files.get("scan")
    if not fichier or not fichier.filename:
        flash("Merci de joindre le scan du document signé.", "error")
        return redirect(url_for("index"))

    DOSSIER_A_CLASSER.mkdir(exist_ok=True)
    nom = d["fichier"].replace("AVISER_", "ASIGNER_", 1)
    cible = DOSSIER_A_CLASSER / nom
    fichier.save(str(cible))

    # Controle des quatre premiers octets : un scan mal converti ou un
    # fichier renomme passerait sinon dans la GED sans etre detecte.
    try:
        with open(cible, "rb") as f:
            if f.read(4) != b"%PDF":
                cible.unlink(missing_ok=True)
                flash("Le fichier déposé n'est pas un PDF valide.", "error")
                return redirect(url_for("index"))
    except Exception:  # noqa: BLE001
        flash("Lecture du fichier impossible.", "error")
        return redirect(url_for("index"))

    # Le projet non signe n'a plus lieu d'etre conserve.
    ancien = DOSSIER_VISA / d["fichier"] if "DOSSIER_VISA" in globals() else None
    try:
        if ancien is not None and ancien.exists():
            ancien.unlink()
    except Exception:  # noqa: BLE001
        pass

    d["statut"] = "vise"
    d["signe_le"] = datetime.now().isoformat(timespec="seconds")
    d["signature"] = "manuscrite"
    if d.get("observations"):
        d["action_statut"] = "ouverte"
    _sauver(donnees)

    flash(f"Scan signé enregistré. « {d.get('objet', '')} » est prêt à archiver.",
          "success")
    return redirect(url_for("index"))
