"""
notes.py — Courriers internes : notes de service, circulaires, demandes
internes et mises à disposition
=======================================================================

Mise en œuvre de la procédure « Courriers internes » du manuel de gestion
des courriers (Admin/Archives, § 5) dans la GED :

  1. Rédaction par une direction (ou par la responsable GED pour elle).
  2. Circuit de validation calculé selon le type :
       - validation de la direction émettrice quand la note est saisie
         pour elle par quelqu'un d'autre ;
       - visa du DAF pour les notes de service, circulaires et mises à
         disposition (sauf si le DAF en est l'émetteur) ;
       - signature : DG pour les circulaires, les mises à disposition et
         les notes de service marquées « importantes » ; DAF pour les
         autres notes de service ; la direction émettrice pour les
         demandes internes.
  3. Signature électronique interne : l'approbation du signataire, connecté
     par son compte Zoho, est horodatée et portée sur le PDF.
  4. Archivage automatique dans WorkDrive (nommage HM26, registre).
  5. Diffusion : chaque destinataire reçoit par Cliq et par e-mail un lien
     personnel ; il confirme « lu et pris connaissance ». Pour une demande
     interne, le service traitant prend en charge puis répond (24 h).

Les destinataires n'ont pas de compte GED : le lien personnel (jeton)
tient lieu d'accès à cette seule note.

Aides à la rédaction (comme pour les courriers) :
  - banque de modèles par type, avec zones « [à compléter : …] » qui bloquent
    la soumission tant qu'elles restent dans le texte ;
  - assistance IA (fournisseur, clé et anonymisation des Paramètres du
    courrier) : rédiger depuis des points clés ou corriger le texte ;
    désactivée pour une note marquée confidentielle ;
  - recherche dans les notes déjà diffusées et duplication.

Données : courrier/notes/notes.json, courrier/notes/personnel.json,
courrier/notes/modeles.json, courrier/notes/<id>/ (PDF).
"""

import functools
import logging
import re
import secrets
import threading
from datetime import datetime, timedelta

from flask import (Blueprint, abort, flash, has_request_context, redirect,
                   render_template, request, send_file, session, url_for)

import courrier as C

bp_notes = Blueprint("notes", __name__, url_prefix="/notes")
bp_note_public = Blueprint("note_public", __name__, url_prefix="/n")

DOSSIER = C.DONNEES / "notes"
F_NOTES = DOSSIER / "notes.json"
F_PERSONNEL = DOSSIER / "personnel.json"
F_MODELES = DOSSIER / "modeles.json"

# ---------------------------------------------------------------------------
# Référentiels
# ---------------------------------------------------------------------------
# type_ged : type du nommage HM26 et du dossier WorkDrive.
TYPES_NOTE = {
    "NS": {"nom": "Note de service", "titre": "NOTE DE SERVICE", "type_ged": "NOT",
           "visa_daf": True, "action": "lecture"},
    "CIR": {"nom": "Circulaire", "titre": "CIRCULAIRE", "type_ged": "CIR",
            "visa_daf": True, "action": "lecture"},
    "DEM": {"nom": "Demande interne", "titre": "DEMANDE INTERNE", "type_ged": "DEM",
            "visa_daf": False, "action": "traitement"},
    "MAD": {"nom": "Mise à disposition", "titre": "MISE À DISPOSITION", "type_ged": "MAD",
            "visa_daf": True, "action": "lecture"},
}

STATUTS = {
    "brouillon": "Brouillon",
    "en_circuit": "En validation",
    "retourne": "Retournée",
    "diffuse": "Diffusée",
    "traitee": "Traitée",
    "abandonne": "Abandonnée",
}

ROLES = {"validation": "Validation", "visa": "Visa", "signature": "Signature"}

# Service GED (dossier d'archive) proposé par défaut selon la direction émettrice
# DG : Direction / Admin ; DT : outils informatiques et application WAKATI (DSI) ;
# DOP : direction transversale des opérations (formations, ouverture de comptes,
# appui à l'animation), intérim assuré par le DG.
SERVICE_PAR_DIRECTION = {"DG": "DGA", "DAF": "FIN", "DT": "DSI", "DCOM": "COM", "SC": "COM",
                         "DOP": "OPS"}

DELAI_DEMANDE_H = 24
FORMAT_REFERENCE = "N°{annee}-{numero}/{type}/{emetteur}/HM"

_CTX = {"archiver_note": None, "dernier_numero": None}


# ---------------------------------------------------------------------------
# Données
# ---------------------------------------------------------------------------
def notes():
    return C._lire(F_NOTES, {})


def note(nid):
    return notes().get(nid)


def _sauver(toutes):
    C._ecrire(F_NOTES, toutes)


def personnel(actifs_seulement=False):
    liste = C._lire(F_PERSONNEL, [])
    if actifs_seulement:
        liste = [p for p in liste if p.get("actif", True)]
    return sorted(liste, key=lambda p: (p.get("service", ""), p.get("nom", "").lower()))


def _id_personne():
    return f"P-{secrets.token_hex(5)}"


def _dossier(nid):
    d = DOSSIER / nid
    d.mkdir(parents=True, exist_ok=True)
    return d


def directions():
    """Les directions sont les signataires déclarés dans Paramètres."""
    return C.parametres().get("signataires", [])


def direction(code):
    return C.signataire(code) or {"code": code, "fonction": code, "nom": "", "email": ""}


def _services():
    return C._CTX.get("services", {}) or {}


# ---------------------------------------------------------------------------
# Banque de modèles de notes
# ---------------------------------------------------------------------------
# Les zones « [à compléter : …] » bloquent la soumission tant qu'elles restent
# dans le texte (même règle que les courriers). Les modèles par défaut sont
# ajoutés une seule fois : un modèle supprimé n'est pas recréé.
Z = "[à compléter : {}]"


def _z(t):
    return Z.format(t)


MODELES_DEFAUT = [
    {"id": "DEF-NS-HORAIRES", "type": "NS", "nom": "Horaires de travail",
     "objet": f"Horaires de travail {_z('période')}",
     "tous": True,
     "corps": (f"Il est porté à la connaissance de l'ensemble du personnel qu'à compter du "
               f"{_z('date d’effet')}, les horaires de travail sont fixés comme suit :\n"
               f"- du lundi au jeudi : {_z('horaires')} ;\n"
               f"- le vendredi : {_z('horaires')} ;\n"
               f"- le samedi : {_z('horaires ou repos')}.\n\n"
               "Chaque responsable de service veille au respect de ces horaires et organise, "
               "si nécessaire, une permanence pendant la pause.\n\n"
               "La présente note annule et remplace toute disposition antérieure contraire.")},
    {"id": "DEF-NS-REUNION", "type": "NS", "nom": "Convocation à une réunion",
     "objet": f"Réunion {_z('objet de la réunion')}",
     "corps": (f"Le personnel {_z('services ou personnes concernés')} est convié à une réunion "
               f"le {_z('date')} à {_z('heure')}, {_z('lieu')}.\n\n"
               "Ordre du jour :\n"
               f"- {_z('point 1')} ;\n"
               f"- {_z('point 2')}.\n\n"
               "La présence de chacun est obligatoire. Toute absence doit être signalée à "
               f"l'avance à {_z('interlocuteur')}.")},
    {"id": "DEF-NS-CONGES", "type": "NS", "nom": "Planification des congés annuels",
     "objet": f"Planification des congés annuels {_z('année')}",
     "tous": True,
     "corps": (f"Dans le cadre de la planification des congés annuels {_z('année')}, chaque agent "
               "est invité à transmettre ses souhaits de dates à son responsable hiérarchique "
               f"au plus tard le {_z('date limite')}.\n\n"
               "Les responsables de service consolident les demandes et transmettent le planning "
               f"de leur service à la Direction Administrative et Financière avant le {_z('date')}.\n\n"
               "Le planning tient compte des nécessités de service : une permanence doit être "
               "assurée dans chaque service pendant toute la période.")},
    {"id": "DEF-NS-INTERIM", "type": "NS", "nom": "Désignation d'un intérim", "importante": True,
     "objet": f"Intérim de {_z('fonction du titulaire')}",
     "tous": True,
     "corps": (f"Pendant l'absence de {_z('nom et fonction du titulaire')}, du {_z('date de début')} "
               f"au {_z('date de fin')}, l'intérim est assuré par "
               f"{_z('nom et fonction de l’intérimaire')}.\n\n"
               f"À ce titre, l'intérimaire reçoit délégation pour {_z('périmètre de la délégation')}.\n\n"
               "Les services sont priés de lui adresser, pendant cette période, toutes les "
               f"affaires relevant de {_z('direction ou service')}.")},
    {"id": "DEF-NS-REGLEMENT", "type": "NS", "nom": "Rappel d'une règle du règlement intérieur",
     "objet": f"Rappel des règles de {_z('thème : ponctualité, tenue, absences…')}",
     "tous": True,
     "corps": (f"La Direction constate {_z('manquement constaté, sans nommer de personne')}.\n\n"
               f"Il est rappelé à l'ensemble du personnel que {_z('règle applicable')}, "
               f"conformément à {_z('article du règlement intérieur')}.\n\n"
               f"Tout manquement constaté à compter du {_z('date')} exposera son auteur aux "
               "mesures prévues par le règlement intérieur.")},
    {"id": "DEF-NS-CONFIDENTIALITE", "type": "NS", "nom": "Confidentialité des données clients WAKATI",
     "objet": "Confidentialité des données des clients WAKATI",
     "tous": True, "importante": True,
     "corps": ("Huri Money SA est placée sous la supervision de la Banque Centrale des Comores et "
               "tenue au secret professionnel sur les données de ses clients WAKATI.\n\n"
               "Il est rappelé à chaque agent que :\n"
               "- les informations d'un client (identité, numéro, solde, transactions) ne sont "
               "communiquées qu'au titulaire du compte, après vérification de son identité ;\n"
               "- aucun code secret ni mot de passe n'est jamais demandé à un client ni "
               "communiqué à un tiers ;\n"
               "- toute demande d'information émanant d'un tiers ou d'une autorité est "
               f"transmise à {_z('service compétent')}.\n\n"
               "Tout manquement à ces règles constitue une faute grave.")},
    {"id": "DEF-CIR-PROCEDURE", "type": "CIR", "nom": "Mise en place d'une procédure",
     "objet": f"Mise en place de la procédure {_z('nom de la procédure')}",
     "tous": True,
     "corps": (f"À compter du {_z('date d’entrée en vigueur')}, la procédure {_z('nom')} "
               f"s'applique à {_z('services ou personnes concernés')}.\n\n"
               f"Elle a pour objet {_z('objectif de la procédure')}.\n\n"
               "Ses principales étapes sont les suivantes :\n"
               f"- {_z('étape 1')} ;\n"
               f"- {_z('étape 2')} ;\n"
               f"- {_z('étape 3')}.\n\n"
               f"Le document complet est disponible {_z('emplacement')}. Pour toute question, "
               f"adressez-vous à {_z('interlocuteur')}.")},
    {"id": "DEF-CIR-FERMETURE", "type": "CIR", "nom": "Fermeture des bureaux / jour férié",
     "objet": f"Fermeture des bureaux le {_z('date')}",
     "tous": True,
     "corps": (f"À l'occasion de {_z('événement')}, les bureaux de Huri Money SA seront fermés "
               f"le {_z('date')}.\n\n"
               f"Le service client et la plateforme WAKATI restent {_z('disponibles / permanence prévue')}.\n\n"
               f"La reprise des activités est fixée au {_z('date et heure de reprise')}.")},
    {"id": "DEF-CIR-LANCEMENT", "type": "CIR", "nom": "Lancement d'une offre ou d'une campagne WAKATI",
     "objet": f"Lancement de {_z('offre ou campagne')}",
     "tous": True,
     "corps": (f"Huri Money SA lance, à compter du {_z('date')}, {_z('offre, service ou campagne WAKATI')}.\n\n"
               f"Cette opération s'adresse à {_z('cible')} et prévoit {_z('contenu de l’offre')}.\n\n"
               "Les équipes commerciales, le service client et le réseau d'agents doivent "
               f"{_z('actions attendues')}. Les supports d'information sont disponibles auprès "
               f"de {_z('service')}.")},
    {"id": "DEF-DEM-ACHAT", "type": "DEM", "nom": "Demande d'achat ou de fournitures",
     "objet": f"Demande de {_z('fournitures ou matériel')}",
     "corps": (f"Pour les besoins de {_z('service ou activité')}, nous sollicitons l'acquisition de :\n"
               f"- {_z('article et quantité')} ;\n"
               f"- {_z('article et quantité')}.\n\n"
               f"Ce besoin est motivé par {_z('justification')}.\n\n"
               "Nous vous remercions de nous indiquer la suite réservée à cette demande et, le "
               "cas échéant, le délai de livraison.")},
    {"id": "DEF-DEM-INFORMATIQUE", "type": "DEM", "nom": "Demande d'intervention informatique",
     "objet": f"Intervention technique : {_z('problème')}",
     "corps": (f"Nous constatons depuis le {_z('date')} "
               f"{_z('description du problème : poste, logiciel, accès, application WAKATI')}.\n\n"
               f"Ce dysfonctionnement affecte {_z('utilisateurs ou activités concernés')}.\n\n"
               "Nous vous remercions de bien vouloir intervenir et de nous informer de la "
               "solution apportée.")},
    {"id": "DEF-DEM-DOCUMENT", "type": "DEM", "nom": "Demande de document ou d'information",
     "objet": f"Demande de {_z('document ou information')}",
     "corps": (f"Dans le cadre de {_z('dossier ou activité')}, nous avons besoin de "
               f"{_z('document ou information demandé')}.\n\n"
               f"Ce document nous est nécessaire pour {_z('usage')}.\n\n"
               "Nous vous remercions de nous le transmettre dans le délai indiqué.")},
    {"id": "DEF-MAD-AGENT", "type": "MAD", "nom": "Mise à disposition d'un agent",
     "objet": f"Mise à disposition de {_z('nom de l’agent')}",
     "corps": (f"{_z('nom, fonction et service d’origine de l’agent')} est mis(e) à disposition de "
               f"{_z('direction ou service d’accueil')} à compter du {_z('date de début')}, pour "
               f"une durée de {_z('durée')}.\n\n"
               f"Pendant cette période, l'agent exerce les missions suivantes : {_z('missions')}.\n\n"
               f"Il ou elle reste rattaché(e) administrativement à {_z('service d’origine')}, qui "
               "conserve la gestion de sa paie et de ses congés. Le service d'accueil fixe son "
               "emploi du temps et en rend compte.")},
    {"id": "DEF-MAD-MATERIEL", "type": "MAD", "nom": "Mise à disposition de matériel",
     "objet": f"Mise à disposition de {_z('matériel')}",
     "corps": (f"Le matériel suivant est mis à la disposition de {_z('bénéficiaire')} à compter "
               f"du {_z('date')} :\n"
               f"- {_z('désignation et numéro de série')} ;\n"
               f"- {_z('désignation et numéro de série')}.\n\n"
               "Le bénéficiaire en assure la garde et le bon usage. Toute perte, panne ou "
               f"détérioration est signalée sans délai à {_z('service')}.\n\n"
               f"Le matériel est restitué {_z('date ou condition de restitution')}.")},
]


def _banque():
    """{"vus": [ids par défaut déjà proposés], "modeles": {id: modèle}}."""
    with C._verrou:
        b = C._lire(F_MODELES, None)
        if not isinstance(b, dict) or "modeles" not in b:
            b = {"vus": [], "modeles": {}}
        nouveaux = [m for m in MODELES_DEFAUT if m["id"] not in b["vus"]]
        if nouveaux:
            for m in nouveaux:
                b["modeles"].setdefault(m["id"], {
                    "id": m["id"], "nom": m["nom"], "type": m["type"],
                    "objet": m["objet"], "corps": m["corps"],
                    "importante": m.get("importante", False),
                    "destinataires": {"tous": m.get("tous", False), "services": [], "personnes": []},
                    "copies": "", "delai_h": DELAI_DEMANDE_H, "actif": True,
                    "origine": "Modèle fourni avec la GED", "utilisations": 0,
                    "cree_le": C._maintenant(), "cree_par": "GED", "maj_le": C._maintenant()})
                b["vus"].append(m["id"])
            C._ecrire(F_MODELES, b)
        return b


def modeles_note(actifs_seulement=True, type_=None):
    liste = list(_banque()["modeles"].values())
    if actifs_seulement:
        liste = [m for m in liste if m.get("actif", True)]
    if type_:
        liste = [m for m in liste if m.get("type") == type_]
    ordre = list(TYPES_NOTE)
    return sorted(liste, key=lambda m: (ordre.index(m["type"]) if m.get("type") in ordre else 9,
                                        -int(m.get("utilisations", 0)), m.get("nom", "").lower()))


def modele_note(mid):
    return _banque()["modeles"].get(mid or "")


def _sauver_modele(m):
    with C._verrou:
        b = _banque()
        m["maj_le"] = C._maintenant()
        b["modeles"][m["id"]] = m
        C._ecrire(F_MODELES, b)


def modele_depuis_note(n, nom):
    m = {"id": C._id_court("MOD"), "nom": (nom or n.get("objet") or "Modèle")[:120],
         "type": n.get("type", "NS"), "objet": n.get("objet", ""), "corps": n.get("corps", ""),
         "importante": bool(n.get("importante")),
         "destinataires": {"tous": bool((n.get("destinataires") or {}).get("tous")),
                           "services": list((n.get("destinataires") or {}).get("services", [])),
                           "personnes": []},
         "copies": n.get("copies", ""), "delai_h": n.get("delai_h") or DELAI_DEMANDE_H,
         "actif": True, "utilisations": 0,
         "origine": f"Enregistré depuis la note {n.get('reference') or n.get('id')}",
         "cree_le": C._maintenant(), "cree_par": C.utilisateur_courant()}
    _sauver_modele(m)
    return m


def appliquer_modele(n, m):
    """Pré-remplit une note neuve avec un modèle (sans toucher à l'émetteur)."""
    n.update({"type": m.get("type", n.get("type", "NS")), "objet": m.get("objet", ""),
              "corps": m.get("corps", ""), "importante": bool(m.get("importante")),
              "copies": m.get("copies", ""), "delai_h": m.get("delai_h") or DELAI_DEMANDE_H,
              "destinataires": dict(m.get("destinataires") or {}), "modele_id": m["id"],
              "modele_nom": m.get("nom", "")})
    return n


def _compter_utilisation(mid):
    with C._verrou:
        b = _banque()
        m = b["modeles"].get(mid)
        if m:
            m["utilisations"] = int(m.get("utilisations", 0)) + 1
            C._ecrire(F_MODELES, b)


# ---------------------------------------------------------------------------
# Droits
# ---------------------------------------------------------------------------
def _email():
    if not has_request_context():
        return ""
    return (session.get("email") or "").strip().lower()


def _sans_auth():
    return not _email()


def codes_utilisateur():
    """Directions au nom desquelles l'utilisateur connecté agit."""
    if _sans_auth():
        return {d["code"] for d in directions()}
    codes = set(C.codes_signataire_pour(_email()))
    if _email() in (C._auth_liste("UTILISATEURS_DG") or set()):
        codes.add("DG")
        codes |= {d["code"] for d in directions() if d.get("interim") == "DG"}
    return codes


def peut_rediger_pour(code):
    return C.est_responsable_ged() or code in codes_utilisateur()


def directions_redigeables():
    return [d for d in directions() if peut_rediger_pour(d["code"])]


def peut_modifier(n):
    return (n.get("statut") in ("brouillon", "retourne")
            and (C.est_responsable_ged() or n.get("emetteur") in codes_utilisateur()
                 or n.get("cree_par_email") == _email()))


def etape_courante(n):
    if n.get("statut") != "en_circuit":
        return None
    i = n.get("etape", 0)
    etapes = n.get("etapes", [])
    return etapes[i] if 0 <= i < len(etapes) else None


def _visible(n):
    """Ses propres notes, celles de son circuit, et toutes les notes diffusées
    non confidentielles (elles ont été envoyées au personnel)."""
    codes = codes_utilisateur()
    if C.est_responsable_ged() or n.get("emetteur") in codes \
            or any(e.get("code") in codes for e in n.get("etapes", [])):
        return True
    return n.get("statut") in ("diffuse", "traitee") and not n.get("confidentielle")


def peut_decider(n):
    e = etape_courante(n)
    return bool(e) and e["code"] in codes_utilisateur()


def _reserve(vue):
    """Écrans de rédaction : responsable GED ou membre d'une direction."""
    @functools.wraps(vue)
    def enveloppe(*args, **kwargs):
        if not (C.est_responsable_ged() or codes_utilisateur()):
            flash("Écran réservé aux directions et à la responsable GED.", "error")
            return redirect(url_for("index"))
        return vue(*args, **kwargs)
    return enveloppe


# ---------------------------------------------------------------------------
# Circuit
# ---------------------------------------------------------------------------
def signataire_final(n):
    t = n.get("type")
    if t in ("CIR", "MAD"):
        return "DG"
    if t == "NS":
        return "DG" if n.get("importante") else "DAF"
    return n.get("emetteur")


def calculer_etapes(n, soumis_par_emetteur):
    etapes = []
    em = n.get("emetteur")
    if not soumis_par_emetteur:
        etapes.append({"role": "validation", "code": em})
    if TYPES_NOTE[n["type"]]["visa_daf"] and em != "DAF" \
            and not (etapes and etapes[-1]["code"] == "DAF"):
        etapes.append({"role": "visa", "code": "DAF"})
    sig = signataire_final(n)
    if etapes and etapes[-1]["code"] == sig:
        etapes[-1]["role"] = "signature"
    elif not (soumis_par_emetteur and sig == em and not etapes):
        etapes.append({"role": "signature", "code": sig})
    for e in etapes:
        d = direction(e["code"])
        e.update({"fonction": d.get("fonction", e["code"]), "nom": d.get("nom", ""),
                  "statut": "attente", "le": "", "par": "", "observation": ""})
    return etapes


def apercu_circuit(n):
    """Étapes telles qu'elles seront à la soumission (pour l'écran)."""
    soumis = n.get("emetteur") in codes_utilisateur()
    return calculer_etapes(n, soumis)


def _historiser(n, action, detail="", par=None):
    n.setdefault("historique", []).append({
        "le": C._maintenant(), "par": par or C.utilisateur_courant(),
        "action": action, "detail": detail})
    n["maj_le"] = C._maintenant()


def _notifier_valideur(n, e):
    """Message privé Cliq au signataire attendu, avec le lien vers la note."""
    d = direction(e["code"])
    if not d.get("email"):
        return
    try:
        import notifier  # noqa: PLC0415
        lien = _url_ged("notes.edition", nid=n["id"])
        notifier.message_cliq_personne(
            d["email"],
            f"✍ {TYPES_NOTE[n['type']]['nom']} à {ROLES[e['role']].lower()} : "
            f"{n.get('objet', '')}\nÉmetteur : {direction(n['emetteur']).get('fonction')}\n{lien}")
    except Exception:  # noqa: BLE001
        logging.error("Notification du valideur impossible", exc_info=True)


def _racine():
    """https://ged.wakati.km derrière Caddy (la requête arrive en http)."""
    schema = request.headers.get("X-Forwarded-Proto", request.scheme)
    hote = request.headers.get("X-Forwarded-Host", request.host)
    return f"{schema}://{hote}"


def _url_ged(endpoint, **kw):
    return f"{_racine()}{url_for(endpoint, **kw)}"


def soumettre(nid):
    with C._verrou:
        toutes = notes()
        n = toutes.get(nid)
        if not n or n.get("statut") not in ("brouillon", "retourne"):
            return "Cette note n'est plus modifiable."
        erreurs = verifier(n)
        if erreurs:
            return " ".join(erreurs)
        soumis_par_emetteur = n["emetteur"] in codes_utilisateur()
        n["etapes"] = calculer_etapes(n, soumis_par_emetteur)
        n["etape"] = 0
        n["soumis_le"] = C._maintenant()
        n["soumis_par"] = C.utilisateur_courant()
        n["motif_retour"] = ""
        if n["etapes"]:
            n["statut"] = "en_circuit"
            _historiser(n, "Soumise au circuit",
                        " → ".join(f"{ROLES[e['role']]} {e['code']}" for e in n["etapes"]))
            _sauver(toutes)
            suivante = n["etapes"][0]
        else:
            _historiser(n, "Signée par l'émetteur", direction(n["emetteur"]).get("fonction", ""))
            _sauver(toutes)
            suivante = None
    if suivante:
        _notifier_valideur(n, suivante)
        return ""
    return finaliser(nid, C.utilisateur_courant())


def decider(nid, decision, observation=""):
    with C._verrou:
        toutes = notes()
        n = toutes.get(nid)
        e = etape_courante(n or {})
        if not e or e["code"] not in codes_utilisateur():
            return "Cette étape n'est pas la vôtre, ou elle a déjà été traitée."
        observation = (observation or "").strip()
        if decision == "retourner":
            if not observation:
                return "Indiquez ce qu'il faut corriger."
            e.update({"statut": "retour", "le": C._maintenant(),
                      "par": C.utilisateur_courant(), "observation": observation})
            n["statut"] = "retourne"
            n["motif_retour"] = observation
            _historiser(n, f"Retournée ({ROLES[e['role']]} {e['code']})", observation)
            _sauver(toutes)
            createur = n.get("cree_par_email")
            suivante = None
        else:
            e.update({"statut": "fait", "le": C._maintenant(),
                      "par": C.utilisateur_courant(), "observation": observation})
            _historiser(n, f"{ROLES[e['role']]} {e['code']}", observation)
            n["etape"] = n.get("etape", 0) + 1
            suivante = etape_courante(n)
            _sauver(toutes)
            createur = None
    if decision == "retourner":
        if createur:
            try:
                import notifier  # noqa: PLC0415
                notifier.message_cliq_personne(
                    createur, f"↩ Note retournée : {n.get('objet', '')}\n{observation}\n"
                              f"{_url_ged('notes.edition', nid=nid)}")
            except Exception:  # noqa: BLE001
                logging.error("Notification du retour impossible", exc_info=True)
        return ""
    if suivante:
        _notifier_valideur(n, suivante)
        return ""
    return finaliser(nid, C.utilisateur_courant())


# ---------------------------------------------------------------------------
# Signature, numérotation, PDF, archivage, diffusion
# ---------------------------------------------------------------------------
def _reserver_numero(n):
    if n.get("numero"):
        return n["numero"]
    type_ged = TYPES_NOTE[n["type"]]["type_ged"]
    service = n.get("service", "DGA")
    dernier = 0
    fn = _CTX.get("dernier_numero") or C._CTX.get("dernier_numero")
    if fn:
        try:
            dernier = fn(service, type_ged) or 0
        except Exception:  # noqa: BLE001
            dernier = 0
    annee = datetime.now().strftime("%Y")
    for autre in notes().values():
        if (autre.get("numero") and autre.get("service") == service
                and TYPES_NOTE.get(autre.get("type"), {}).get("type_ged") == type_ged
                and str(autre.get("signe_le", "")).startswith(annee)):
            try:
                dernier = max(dernier, int(autre["numero"]))
            except ValueError:
                pass
    n["numero"] = f"{dernier + 1:03d}"
    return n["numero"]


def reference(n):
    if not n.get("numero"):
        return ""
    annee = (n.get("signe_le") or C._maintenant())[:4]
    return FORMAT_REFERENCE.format(annee=annee, numero=n["numero"], type=n["type"],
                                   emetteur=n.get("emetteur", ""))


def finaliser(nid, par):
    """Dernière signature : numéro, PDF définitif, archivage et diffusion."""
    racine = _racine()
    with C._verrou:
        toutes = notes()
        n = toutes[nid]
        _reserver_numero(n)
        n["signe_le"] = C._maintenant()
        n["signe_par"] = par
        n["signataire"] = signataire_final(n)
        n["reference"] = reference(n)
        n["statut"] = "diffuse"
        if n["type"] == "DEM":
            n["echeance"] = (datetime.now() + timedelta(hours=int(n.get("delai_h") or DELAI_DEMANDE_H))
                             ).isoformat(timespec="seconds")
        n["lectures"] = _lectures(n)
        _historiser(n, "Signée", f"{n['reference']} — {direction(n['signataire']).get('fonction')}", par)
        _sauver(toutes)
    try:
        generer_pdf(n, _dossier(nid) / "note.pdf", definitif=True)
    except Exception:  # noqa: BLE001
        logging.error("PDF de la note %s", nid, exc_info=True)
    threading.Thread(target=_apres_signature, args=(nid, racine), daemon=True).start()
    return ""


def _apres_signature(nid, racine):
    diffuser(nid, racine)
    archiver(nid)


def _lectures(n):
    """Liste de diffusion résolue au moment de la signature."""
    dest = n.get("destinataires", {})
    pers = personnel(actifs_seulement=True)
    choisis = []
    if dest.get("tous"):
        choisis = list(pers)
    else:
        services = set(dest.get("services", []))
        ids = set(dest.get("personnes", []))
        choisis = [p for p in pers if p.get("service") in services or p.get("id") in ids]
    traitants = set(n.get("traitants", [])) if n.get("type") == "DEM" else set()
    if traitants:
        choisis += [p for p in pers if p.get("id") in traitants]
    vus, lectures = set(), []
    for p in choisis:
        cle = (p.get("email") or p.get("id")).lower()
        if cle in vus:
            continue
        vus.add(cle)
        lectures.append({
            "id": p.get("id"), "nom": p.get("nom", ""), "fonction": p.get("fonction", ""),
            "service": p.get("service", ""), "email": p.get("email", ""),
            "jeton": secrets.token_urlsafe(24),
            "traitant": p.get("id") in traitants,
            "envoye_le": "", "canaux": "", "lu_le": "", "pris_en_charge_le": "",
            "traite_le": "", "reponse": "",
        })
    return lectures


def _message(n, l, racine, relance=False):
    t = TYPES_NOTE[n["type"]]
    # Construit à la main : la diffusion tourne hors requête (tâche de fond).
    lien = f"{racine}/n/{l['jeton']}"
    action = ("Merci de la prendre en charge et d'y répondre" if l.get("traitant")
              else "Merci de la lire et de confirmer « lu et pris connaissance »")
    tete = "🔁 Rappel — " if relance else "📄 "
    return (f"{tete}{t['nom']} {n.get('reference', '')}\n"
            f"Objet : {n.get('objet', '')}\n"
            f"De : {direction(n['emetteur']).get('fonction', '')}\n"
            f"{action} :\n{lien}")


def diffuser(nid, racine, seulement_non_lus=False):
    """Envoie à chaque destinataire son lien personnel (Cliq + e-mail)."""
    try:
        import notifier  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        logging.error("notifier indisponible : diffusion impossible")
        return 0
    n = note(nid)
    if not n:
        return 0
    envoyes = 0
    resultats = {}
    for l in n.get("lectures", []):
        if seulement_non_lus and (l.get("lu_le") or l.get("traite_le")):
            continue
        if not l.get("email"):
            resultats[l["jeton"]] = ("", "sans adresse")
            continue
        texte = _message(n, l, racine, relance=seulement_non_lus)
        canaux = []
        ok, _ = notifier.message_cliq_personne(l["email"], texte)
        if ok:
            canaux.append("Cliq")
        ok, _ = notifier.envoyer_email(
            [l["email"]], f"{TYPES_NOTE[n['type']]['nom']} — {n.get('objet', '')}", texte)
        if ok:
            canaux.append("e-mail")
        resultats[l["jeton"]] = (C._maintenant(), " + ".join(canaux) or "échec d'envoi")
        envoyes += 1 if canaux else 0
    with C._verrou:
        toutes = notes()
        n = toutes.get(nid)
        for l in n.get("lectures", []):
            if l["jeton"] in resultats:
                le, canaux = resultats[l["jeton"]]
                if le:
                    l["envoye_le"] = le
                l["canaux"] = canaux
        _historiser(n, "Relance des non-lus" if seulement_non_lus else "Diffusée",
                    f"{envoyes} destinataire(s) prévenu(s)", "GED")
        _sauver(toutes)
    return envoyes


def _objet_fichier(texte):
    """Même nettoyage que l'archivage manuel (app.nettoyer_texte)."""
    import unicodedata  # noqa: PLC0415
    t = unicodedata.normalize("NFKD", texte or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).upper().replace(" ", "_")
    t = re.sub(r"[^A-Z0-9_\-]", "", t)
    return re.sub(r"_{2,}", "_", t).strip("_")[:60] or "SANS_OBJET"


def _nom_archive(n):
    objet = _objet_fichier(n.get("objet", ""))
    date = (n.get("signe_le") or C._maintenant())[:10].replace("-", "")
    return (f"HM26-{n.get('service', 'DGA')}-{TYPES_NOTE[n['type']]['type_ged']}_"
            f"{date}_{n['numero']}_{objet}.pdf")


def archiver(nid):
    fn = _CTX.get("archiver_note")
    n = note(nid)
    source = DOSSIER / nid / "note.pdf"
    if not fn or not n or not source.exists():
        return
    nom = _nom_archive(n)
    try:
        lien = fn(source, nom, n.get("service", "DGA"), TYPES_NOTE[n["type"]]["type_ged"],
                  n.get("signe_le", "")[:10], n.get("objet", ""),
                  _libelle_traitants(n) if n.get("type") == "DEM" else _libelle_destinataires(n))
    except Exception:  # noqa: BLE001
        logging.error("Archivage de la note %s impossible", nid, exc_info=True)
        lien = ""
    with C._verrou:
        toutes = notes()
        n = toutes.get(nid)
        if lien and str(lien).startswith("http"):
            n["nom_archive"], n["lien_archive"] = nom, lien
            _historiser(n, "Archivée", nom, "GED")
        else:
            n["archive_echec"] = C._maintenant()
            _historiser(n, "Archivage à relancer", "WorkDrive n'a pas répondu", "GED")
        _sauver(toutes)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _libelle_destinataires(n):
    dest = n.get("destinataires", {})
    if dest.get("tous"):
        return "Tout le personnel"
    services = _services()
    morceaux = [services.get(s, s) for s in dest.get("services", [])]
    noms = {p["id"]: p.get("nom", "") for p in personnel()}
    morceaux += [noms.get(i, i) for i in dest.get("personnes", [])]
    return ", ".join(m for m in morceaux if m) or "—"


def _libelle_traitants(n):
    noms = {p["id"]: f"{p.get('nom', '')}" + (f" ({p['fonction']})" if p.get("fonction") else "")
            for p in personnel()}
    return ", ".join(noms.get(i, i) for i in n.get("traitants", [])) or "—"


def generer_pdf(n, chemin, definitif=False):
    pdf, t = C._papier("serif", not definitif)
    pdf.c_margin = 0
    pdf.add_page()
    f = pdf.police
    L = 4.6
    cfg = TYPES_NOTE[n["type"]]
    em = direction(n.get("emetteur", ""))

    # Émetteur et date
    pdf.set_font(f, "B", 11)
    pdf.multi_cell(110, L, t(em.get("fonction", "")), new_x="LMARGIN", new_y="TOP")
    pdf.set_font(f, "", 11)
    date = (n.get("signe_le") or C._maintenant())[:10]
    pdf.cell(0, L, t(f"Moroni, le {C.date_longue(date)}"), align="R",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(9)

    # Titre et numéro
    pdf.set_font(f, "B", 15)
    pdf.cell(0, 7, t(cfg["titre"]), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(f, "", 11)
    ref = n.get("reference") or "N° …… (attribué à la signature)"
    pdf.cell(0, 6, t(ref), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(7)

    # Destinataires / objet
    def ligne(lib, val, gras=False):
        pdf.set_font(f, "BU", 11)
        pdf.write(L, t(lib))
        pdf.set_font(f, "B" if gras else "", 11)
        pdf.write(L, t(f" : {val}"))
        pdf.ln(L + 1.5)

    if n["type"] == "DEM":
        ligne("À l'attention de", _libelle_traitants(n))
        if n.get("destinataires") and _libelle_destinataires(n) != "—":
            ligne("Copie", _libelle_destinataires(n))
    else:
        ligne("Destinataires", _libelle_destinataires(n))
    ligne("Objet", n.get("objet") or "[à compléter : objet]", gras=True)
    if n["type"] == "DEM":
        delai = int(n.get("delai_h") or DELAI_DEMANDE_H)
        ech = f" (avant le {C.date_courte(n['echeance'][:10])} à {n['echeance'][11:16]})" \
            if n.get("echeance") else ""
        ligne("Réponse attendue", f"sous {delai} h{ech}")
    pdf.ln(4)

    # Corps, justifié
    pdf.set_font(f, "", 11)
    for paragraphe in C._paragraphes(n.get("corps")):
        pdf.multi_cell(0, L, t(paragraphe), align="J", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3.5)

    # Signature
    sig = direction(signataire_final(n))
    if pdf.get_y() > 297 - C.BAS_CORPS - 52:
        pdf.add_page()
    pdf.ln(8)
    x_sig = 210 - C.MARGE_D - 78
    pdf.set_x(x_sig)
    pdf.set_font(f, "", 11)
    pdf.multi_cell(78, L, t(sig.get("fonction", "")), align="C", new_x="LMARGIN", new_y="NEXT")
    if sig.get("interim"):
        pdf.set_x(x_sig)
        pdf.set_font(f, "I", 9.5)
        pdf.multi_cell(78, 4.2, t(f"par intérim : {C._sans_article(direction(sig['interim']).get('fonction', ''))}"),
                       align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    if definitif and n.get("signe_le"):
        y = pdf.get_y()
        with pdf.local_context(draw_color=(3, 34, 76), text_color=(3, 34, 76)):
            pdf.set_line_width(0.4)
            pdf.rect(x_sig + 6, y, 66, 15)
            pdf.set_xy(x_sig + 6, y + 1.6)
            pdf.set_font(f, "B", 8.5)
            pdf.cell(66, 4, t("SIGNÉ ÉLECTRONIQUEMENT"), align="C", new_x="LEFT", new_y="NEXT")
            pdf.set_font(f, "", 8)
            le = n["signe_le"]
            pdf.cell(66, 3.8, t(f"le {C.date_courte(le[:10])} à {le[11:16]} — GED Huri Money"),
                     align="C", new_x="LEFT", new_y="NEXT")
            pdf.cell(66, 3.8, t(f"réf. {n['id']}"), align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.set_y(y + 18)
    else:
        pdf.ln(16)
    pdf.set_x(x_sig)
    pdf.set_font(f, "B", 11)
    pdf.cell(78, L, t(sig.get("nom", "")), align="C", new_x="LMARGIN", new_y="NEXT")

    # Visas
    visas = [e for e in n.get("etapes", []) if e.get("role") in ("visa", "validation")
             and e.get("statut") == "fait"]
    if visas:
        pdf.ln(6)
        pdf.set_font(f, "I", 9)
        pdf.set_text_color(*C.GRIS)
        for e in visas:
            pdf.cell(0, 4.2, t(f"{ROLES[e['role']]} : {e.get('fonction', e['code'])} — "
                               f"le {C.date_courte(e['le'][:10])} à {e['le'][11:16]}"),
                     new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*C.TEXTE)

    # Ampliations
    copies = [c.strip() for c in re.split(r"[;\n]", n.get("copies") or "") if c.strip()]
    if copies:
        pdf.ln(5)
        pdf.set_font(f, "U", 10)
        pdf.cell(0, 5, t("Ampliation :"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(f, "", 10)
        for c in copies:
            pdf.cell(0, 4.8, t(f"- {c}"), new_x="LMARGIN", new_y="NEXT")

    chemin.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(chemin))
    return chemin


# ---------------------------------------------------------------------------
# Saisie
# ---------------------------------------------------------------------------
def verifier(n):
    erreurs = []
    if not (n.get("objet") or "").strip():
        erreurs.append("L'objet est obligatoire.")
    if len((n.get("corps") or "").strip()) < 20:
        erreurs.append("Le texte de la note est trop court.")
    zones = C.zones_a_completer(n.get("objet"), n.get("corps"))
    if zones:
        liste = " ; ".join(f"« {z} »" for z in zones[:6])
        suite = f" … (+{len(zones) - 6})" if len(zones) > 6 else ""
        erreurs.append(f"Il reste {len(zones)} zone(s) à compléter : {liste}{suite}.")
    if n.get("type") == "DEM":
        if not n.get("traitants"):
            erreurs.append("Choisissez au moins une personne pour traiter la demande.")
    else:
        d = n.get("destinataires", {})
        if not (d.get("tous") or d.get("services") or d.get("personnes")):
            erreurs.append("Choisissez les destinataires.")
    return erreurs


def _champs_formulaire(f, n):
    type_ = f.get("type", n.get("type", "NS"))
    if type_ not in TYPES_NOTE:
        type_ = "NS"
    em = f.get("emetteur", n.get("emetteur", ""))
    if not peut_rediger_pour(em):
        raise PermissionError("Vous ne pouvez pas rédiger au nom de cette direction.")
    service = f.get("service") or SERVICE_PAR_DIRECTION.get(em, "DGA")
    if service not in _services():
        service = "DGA"
    try:
        delai = max(1, min(240, int(f.get("delai_h") or DELAI_DEMANDE_H)))
    except ValueError:
        delai = DELAI_DEMANDE_H
    n.update({
        "type": type_, "emetteur": em, "service": service,
        "objet": f.get("objet", "").strip()[:200],
        "corps": f.get("corps", "").replace("\r\n", "\n").strip(),
        "importante": f.get("importante") == "on",
        "copies": f.get("copies", "").strip(),
        "destinataires": {
            "tous": f.get("tous") == "on",
            "services": [s for s in f.getlist("services") if s in _services()],
            "personnes": f.getlist("personnes"),
        },
        "traitants": f.getlist("traitants") if type_ == "DEM" else [],
        "delai_h": delai,
        "confidentielle": f.get("confidentielle") == "on",
        "points": f.get("points", n.get("points", "")).strip()[:3000],
    })
    return n


def creer(f):
    nid = C._id_court("NOTE")
    n = {"id": nid, "statut": "brouillon", "cree_le": C._maintenant(),
         "cree_par": C.utilisateur_courant(), "cree_par_email": _email(),
         "historique": []}
    _champs_formulaire(f, n)
    m = modele_note(f.get("modele_id"))
    if m:
        n["modele_id"], n["modele_nom"] = m["id"], m.get("nom", "")
        _compter_utilisation(m["id"])
    _historiser(n, "Créée", TYPES_NOTE[n["type"]]["nom"]
                + (f" — modèle « {m.get('nom')} »" if m else ""))
    with C._verrou:
        toutes = notes()
        toutes[nid] = n
        _sauver(toutes)
    return n


# ---------------------------------------------------------------------------
# Assistance IA (même fournisseur, même clé et mêmes réglages que le courrier)
# ---------------------------------------------------------------------------
_SYSTEME_NOTE = """Tu rédiges pour Huri Money SA (WAKATI Mobile Money, Union des Comores) le TEXTE d'un courrier interne en français : note de service, circulaire, demande interne ou mise à disposition. L'application ajoute elle-même l'en-tête, le titre, le numéro, les destinataires, l'objet, la date et la signature.

FORME :
- Ton administratif interne, direct et neutre. Une formule d'usage brève est admise en ouverture (« Il est porté à la connaissance de… »), une seule fois.
- Paragraphes courts : une idée par paragraphe, 2 à 3 phrases au plus, séparés par une ligne vide.
- Phrases courtes (25 mots au plus), à la voix active.
- Énumérations : une ligne par élément commençant par « - », terminée par « ; », la dernière par « . », précédée d'une phrase d'introduction.
- Ordre : d'abord la décision, l'information ou la demande ; ensuite les modalités (qui, quand, comment) ; enfin l'interlocuteur ou la suite attendue si elle est connue.

SELON LE TYPE :
- Note de service : une instruction ou une décision applicable, avec sa date d'effet et les personnes concernées.
- Circulaire : une information ou une procédure générale, ses étapes et où trouver le détail.
- Demande interne : ce qui est demandé, pourquoi, et ce qui est attendu du service traitant ; ton courtois.
- Mise à disposition : ce qui est mis à disposition (agent ou matériel), au profit de qui, pour quelle durée et à quelles conditions.

RÈGLES ABSOLUES :
- Écris uniquement le texte : pas de titre, pas de « Objet : », pas de destinataires, pas de date, pas de signature, pas de formule de politesse finale.
- N'invente JAMAIS une date, un horaire, un montant, un nom, une référence ou un engagement. Toute information nécessaire mais absente s'écrit exactement [à compléter : description courte].
- Conserve tels quels les repères entre crochets comme [courriel 1] ou [téléphone 2].
- WAKATI est un service de paiement mobile : n'écris jamais « banque » pour le désigner.
- Texte brut uniquement : pas de Markdown, pas de gras, pas de titres."""

_CONSIGNE_REFORMULER = """Corrige et améliore le texte ci-dessous : orthographe, grammaire, ponctuation, clarté, paragraphes courts.
- Garde exactement le même sens et le même niveau d'engagement.
- N'ajoute AUCUNE information. Ne supprime aucune information.
- Conserve à l'identique chaque date, chiffre, nom, et chaque zone [à compléter : …] ou repère entre crochets.
Rends uniquement le texte corrigé."""


def ia_autorisee_note(confidentielle=False):
    """(autorisée, raison, modèle). Le refus est toujours expliqué."""
    ia = C.parametres().get("ia", {})
    if not ia.get("actif"):
        return False, "Assistance IA désactivée dans les Paramètres du courrier.", ""
    if not C._cle_ia():
        return False, "Aucune clé d'API configurée (Paramètres du courrier).", ""
    if not ia.get("modele"):
        return False, "Aucun modèle d'IA renseigné dans les Paramètres.", ""
    if confidentielle:
        return False, "Note marquée confidentielle : l'IA est désactivée pour cette note.", ""
    return True, "", ia.get("modele", "")


def _masquer(texte, table):
    """Remplace courriels, comptes et téléphones par des repères numérotés
    ([courriel 1]…), restaurés dans la réponse : rien ne sort, rien ne se perd."""
    def remplacer(lib):
        def f(m):
            for k, v in table.items():
                if v == m.group(0):
                    return k
            cle = f"[{lib} {sum(1 for k in table if k.startswith('[' + lib)) + 1}]"
            table[cle] = m.group(0)
            return cle
        return f
    for motif, rempl in C._MASQUES:
        texte = motif.sub(remplacer(rempl.strip("[]")), texte or "")
    return texte


def _restaurer(texte, table):
    for cle, val in table.items():
        texte = texte.replace(cle, val)
    return texte


_LIGNES_PARASITES = re.compile(
    r"^[ \t]*(?:(?:note de service|circulaire|demande interne|mise à disposition)[ \t.:]*"
    r"|(?:objet|destinataires?|à l'attention de|copie|ampliation)[ \t]*:[^\n]{0,160}"
    r"|n°[ \t]*\S+[^\n]{0,60}|moroni,? le[^\n]{0,60})$", re.I | re.M)


def _nettoyer_note(texte):
    texte = _LIGNES_PARASITES.sub("", texte or "")
    return C._nettoyer_sortie(texte, {})


def _alertes_note(sortie, sources):
    src = C._normaliser(" ".join(s or "" for s in sources))
    alertes = []
    for nombre in sorted(set(re.findall(r"\d[\d\s.,h:]*\d|\d", sortie))):
        if C._normaliser(nombre) not in src:
            alertes.append(f"« {nombre.strip()} » n'apparaît ni dans vos points clés ni dans "
                           "le texte de départ : à vérifier.")
    for mois in C.MOIS:
        if re.search(rf"\b{mois}\b", sortie, re.I) and C._normaliser(mois) not in src:
            alertes.append(f"Date en « {mois} » absente de vos points clés : à vérifier.")
    return alertes[:8]


def assistance_ia(mode, donnees):
    """mode : « rediger » (depuis les points clés, en adaptant le texte
    existant s'il y en a un) ou « reformuler » (corrige le texte existant).
    Retourne (ok, texte_ou_erreur, alertes)."""
    ok, raison, _ = ia_autorisee_note(donnees.get("confidentielle"))
    if not ok:
        return False, raison, []
    ia = C.parametres()["ia"]
    t = TYPES_NOTE.get(donnees.get("type"), TYPES_NOTE["NS"])
    points = (donnees.get("points") or "").strip()
    corps = (donnees.get("corps") or "").strip()
    objet = (donnees.get("objet") or "").strip()
    table = {}
    if ia.get("anonymiser", True):
        points_e, corps_e, objet_e = (_masquer(points, table), _masquer(corps, table),
                                      _masquer(objet, table))
    else:
        points_e, corps_e, objet_e = points, corps, objet

    if mode == "reformuler":
        if len(corps) < 20:
            return False, "Il n'y a pas encore de texte à corriger.", []
        messages = [{"role": "system", "content": _SYSTEME_NOTE},
                    {"role": "user", "content": f"{_CONSIGNE_REFORMULER}\n\nType : {t['nom']}\n"
                                                f"Objet : {objet_e or 'non précisé'}\n\n"
                                                f"Texte :\n{corps_e[:7000]}"}]
    else:
        if not points and len(corps) < 20:
            return False, ("Indiquez d'abord les points clés : ce que la note doit dire, "
                           "à qui, à partir de quand."), []
        depart = (f"Texte de départ (modèle ou brouillon) à adapter. Remplace les zones "
                  f"[à compléter : …] par les informations des points clés quand elles y "
                  f"figurent ; laisse les autres zones telles quelles :\n{corps_e[:6000]}"
                  if corps else "Pas de texte de départ : rédige le texte complet.")
        messages = [{"role": "system", "content": _SYSTEME_NOTE},
                    {"role": "user", "content": (
                        f"Type : {t['nom']}\n"
                        f"Direction émettrice : {direction(donnees.get('emetteur', '')).get('fonction', '')}\n"
                        f"Destinataires : {donnees.get('destinataires_lib') or 'non précisés'}\n"
                        f"Objet : {objet_e or 'non précisé'}\n\n"
                        f"Points clés donnés par le rédacteur :\n{points_e or '(aucun)'}\n\n"
                        f"{depart}\n\nRédige le texte de la note.")}]
    ok, texte = C.appeler_ia(messages, ia)
    if not ok:
        return False, texte, []
    sortie = _restaurer(_nettoyer_note(texte), table)
    if not sortie.strip():
        return False, "L'IA n'a rien proposé. Réessayez en précisant les points clés.", []
    return True, sortie, _alertes_note(sortie, [points, corps, objet])


# ---------------------------------------------------------------------------
# Présentation
# ---------------------------------------------------------------------------
def _affichee(n):
    lectures = n.get("lectures", [])
    lus = sum(1 for l in lectures if l.get("lu_le") or l.get("traite_le"))
    traitants = [l for l in lectures if l.get("traitant")]
    try:
        retard = (n.get("type") == "DEM" and n.get("statut") == "diffuse" and n.get("echeance")
                  and datetime.fromisoformat(n["echeance"]) < datetime.now())
    except ValueError:
        retard = False
    e = etape_courante(n)
    return {
        **n,
        "type_nom": TYPES_NOTE.get(n.get("type"), {}).get("nom", n.get("type")),
        "statut_lib": STATUTS.get(n.get("statut"), n.get("statut")),
        "emetteur_lib": direction(n.get("emetteur", "")).get("fonction", ""),
        "destinataires_lib": _libelle_destinataires(n),
        "traitants_lib": _libelle_traitants(n),
        "nb_lectures": len(lectures), "nb_lus": lus,
        "taux_lecture": round(lus * 100 / len(lectures)) if lectures else None,
        "nb_traitants": len(traitants),
        "nb_traites": sum(1 for l in traitants if l.get("traite_le")),
        "retard": bool(retard),
        "etape_lib": (f"{ROLES[e['role']]} — {e.get('fonction', e['code'])}" if e else ""),
        "maj_affiche": C._date_heure(n.get("maj_le")),
        "echeance_affiche": (f"{C.date_courte(n['echeance'][:10])} {n['echeance'][11:16]}"
                             if n.get("echeance") else ""),
        "nb_a_completer": len(C.zones_a_completer(n.get("objet"), n.get("corps"))),
    }


def compteurs():
    """Pour la barre latérale : notes attendant l'utilisateur connecté."""
    try:
        codes = codes_utilisateur()
        a_valider = sum(1 for n in notes().values()
                        if (etape_courante(n) or {}).get("code") in codes)
        return {"notes_a_valider": a_valider}
    except Exception:  # noqa: BLE001
        return {"notes_a_valider": 0}


# ---------------------------------------------------------------------------
# Écrans GED
# ---------------------------------------------------------------------------
def _contexte():
    ia_ok, ia_raison, ia_modele = ia_autorisee_note(False)
    return {"types_note": TYPES_NOTE, "statuts_note": STATUTS, "roles": ROLES,
            "directions": directions(), "personnel": personnel(actifs_seulement=True),
            "ia_ok": ia_ok, "ia_raison": ia_raison, "ia_modele": ia_modele,
            **C._contexte_commun()}


@bp_notes.route("/")
@_reserve
def liste():
    codes = codes_utilisateur()
    toutes = sorted(notes().values(), key=lambda n: n.get("maj_le", ""), reverse=True)
    visibles = [n for n in toutes if C.est_responsable_ged() or n.get("emetteur") in codes
                or any(e.get("code") in codes for e in n.get("etapes", []))]
    a_valider = [_affichee(n) for n in visibles if (etape_courante(n) or {}).get("code") in codes]
    en_cours = [_affichee(n) for n in visibles
                if n.get("statut") in ("brouillon", "retourne", "en_circuit")
                and n not in a_valider]
    diffusees = [_affichee(n) for n in visibles if n.get("statut") in ("diffuse", "traitee")][:40]

    # Recherche : toutes les notes visibles, y compris celles diffusées par
    # les autres directions (réutilisation), hors abandonnées.
    q = (request.args.get("q") or "").strip()
    type_f = request.args.get("type", "")
    resultats = None
    if q or type_f in TYPES_NOTE:
        mots = C._normaliser(q).split()

        def correspond(n):
            if type_f in TYPES_NOTE and n.get("type") != type_f:
                return False
            texte = C._normaliser(" ".join([
                n.get("objet", ""), n.get("corps", ""), n.get("reference", ""),
                direction(n.get("emetteur", "")).get("fonction", ""),
                TYPES_NOTE.get(n.get("type"), {}).get("nom", "")]))
            return all(m in texte for m in mots)
        resultats = [_affichee(n) for n in toutes
                     if n.get("statut") != "abandonne" and _visible(n) and correspond(n)][:100]
    return render_template("notes.html", page="notes", a_valider=a_valider,
                           en_cours=[x for x in en_cours if x["id"] not in {a["id"] for a in a_valider}],
                           diffusees=diffusees, resultats=resultats, q=q, type_f=type_f,
                           **_contexte())


@bp_notes.route("/nouvelle", methods=["GET", "POST"])
@_reserve
def nouvelle():
    if request.method == "POST":
        try:
            n = creer(request.form)
        except PermissionError as err:
            flash(str(err), "error")
            return redirect(url_for("notes.nouvelle"))
        flash("Brouillon enregistré. Relisez l'aperçu, puis soumettez.", "success")
        return redirect(url_for("notes.edition", nid=n["id"]))
    codes = codes_utilisateur()
    defaut = next((d["code"] for d in directions_redigeables() if d["code"] in codes),
                  (directions_redigeables() or [{"code": "DG"}])[0]["code"])
    n = {"type": request.args.get("type", "NS"), "emetteur": defaut,
         "service": SERVICE_PAR_DIRECTION.get(defaut, "DGA"), "destinataires": {},
         "traitants": [], "delai_h": DELAI_DEMANDE_H, "statut": "brouillon"}
    if n["type"] not in TYPES_NOTE:
        n["type"] = "NS"
    m = modele_note(request.args.get("modele"))
    if m and m.get("actif", True):
        appliquer_modele(n, m)
    return render_template("note_edition.html", page="notes", n=_affichee(n), nouvelle=True,
                           modifiable=True, redigeables=directions_redigeables(),
                           circuit=apercu_circuit(n), modeles=modeles_note(), **_contexte())


@bp_notes.route("/<nid>", methods=["GET", "POST"])
@_reserve
def edition(nid):
    n = note(nid)
    if not n:
        abort(404)
    if request.method == "POST":
        if not peut_modifier(n):
            flash("Cette note n'est plus modifiable.", "error")
            return redirect(url_for("notes.edition", nid=nid))
        try:
            with C._verrou:
                toutes = notes()
                _champs_formulaire(request.form, toutes[nid])
                _historiser(toutes[nid], "Modifiée")
                _sauver(toutes)
        except PermissionError as err:
            flash(str(err), "error")
            return redirect(url_for("notes.edition", nid=nid))
        if request.form.get("action") == "soumettre":
            err = soumettre(nid)
            if err:
                flash(err, "error")
            else:
                n = note(nid)
                flash("Note signée et diffusée." if n.get("statut") == "diffuse"
                      else "Note soumise au circuit de validation.", "success")
        else:
            flash("Modifications enregistrées.", "success")
        return redirect(url_for("notes.edition", nid=nid))
    return render_template("note_edition.html", page="notes", n=_affichee(n), nouvelle=False,
                           modifiable=peut_modifier(n), decision=peut_decider(n),
                           peut_modele=C.est_responsable_ged() or n.get("emetteur") in codes_utilisateur(),
                           redigeables=directions_redigeables() or [direction(n["emetteur"])],
                           circuit=(n.get("etapes") if n.get("statut") not in ("brouillon", "retourne")
                                    else apercu_circuit(n)),
                           **_contexte())


@bp_notes.route("/<nid>/decision", methods=["POST"])
@_reserve
def decision(nid):
    choix = request.form.get("decision")
    if choix not in ("approuver", "retourner"):
        abort(400)
    err = decider(nid, choix, request.form.get("observation", ""))
    if err:
        flash(err, "error")
    else:
        n = note(nid)
        flash({"retourner": "Note retournée à son rédacteur.",
               "approuver": ("Note signée : elle est archivée et diffusée."
                             if n.get("statut") == "diffuse"
                             else "Visa enregistré : la note passe à l'étape suivante.")}[choix],
              "success")
    return redirect(url_for("notes.edition", nid=nid))


@bp_notes.route("/<nid>/apercu.pdf")
@_reserve
def apercu(nid):
    n = note(nid)
    if not n:
        abort(404)
    final = DOSSIER / nid / "note.pdf"
    if n.get("statut") in ("diffuse", "traitee") and final.exists():
        return send_file(final, mimetype="application/pdf")
    chemin = _dossier(nid) / "apercu.pdf"
    generer_pdf(n, chemin, definitif=False)
    return send_file(chemin, mimetype="application/pdf", max_age=0)


@bp_notes.route("/<nid>/relancer", methods=["POST"])
@_reserve
def relancer(nid):
    n = note(nid)
    if not n or n.get("statut") not in ("diffuse",):
        abort(404)
    nb = diffuser(nid, _racine(), seulement_non_lus=True)
    flash(f"Rappel envoyé à {nb} destinataire(s) qui n'ont pas encore confirmé.", "success")
    return redirect(url_for("notes.edition", nid=nid))


@bp_notes.route("/<nid>/archiver", methods=["POST"])
@_reserve
def rearchiver(nid):
    n = note(nid)
    if not n or n.get("statut") not in ("diffuse", "traitee") or n.get("lien_archive"):
        abort(404)
    threading.Thread(target=archiver, args=(nid,), daemon=True).start()
    flash("Archivage relancé. Le lien WorkDrive apparaît dans quelques secondes.", "success")
    return redirect(url_for("notes.edition", nid=nid))


@bp_notes.route("/<nid>/abandonner", methods=["POST"])
@_reserve
def abandonner(nid):
    with C._verrou:
        toutes = notes()
        n = toutes.get(nid)
        if not n or not peut_modifier(n):
            abort(404)
        n["statut"] = "abandonne"
        _historiser(n, "Abandonnée")
        _sauver(toutes)
    flash("Note abandonnée.", "success")
    return redirect(url_for("notes.liste"))


# --- Banque de modèles ---------------------------------------------------------
@bp_notes.route("/modeles", methods=["GET", "POST"])
@_reserve
def ecran_modeles():
    if request.method == "POST":
        if not C.est_responsable_ged():
            flash("Seule la responsable GED tient la banque de modèles.", "error")
            return redirect(url_for("notes.ecran_modeles"))
        f = request.form
        mid = f.get("id") or C._id_court("MOD")
        m = modele_note(mid) or {"id": mid, "utilisations": 0, "cree_le": C._maintenant(),
                                 "cree_par": C.utilisateur_courant(), "origine": "Saisi dans la banque"}
        type_ = f.get("type") if f.get("type") in TYPES_NOTE else "NS"
        try:
            delai = max(1, min(240, int(f.get("delai_h") or DELAI_DEMANDE_H)))
        except ValueError:
            delai = DELAI_DEMANDE_H
        m.update({"nom": f.get("nom", "").strip()[:120], "type": type_,
                  "objet": f.get("objet", "").strip()[:200],
                  "corps": f.get("corps", "").replace("\r\n", "\n").strip(),
                  "importante": f.get("importante") == "on",
                  "destinataires": {"tous": f.get("tous") == "on",
                                    "services": [s for s in f.getlist("services") if s in _services()],
                                    "personnes": []},
                  "copies": f.get("copies", "").strip(), "delai_h": delai,
                  "actif": f.get("actif") == "on"})
        if not m["nom"] or len(m["corps"]) < 20:
            flash("Le nom et un texte d'au moins 20 caractères sont obligatoires.", "error")
            return redirect(url_for("notes.ecran_modeles", id=f.get("id") or ""))
        _sauver_modele(m)
        flash(f"Modèle « {m['nom']} » enregistré.", "success")
        return redirect(url_for("notes.ecran_modeles"))
    edite = modele_note(request.args.get("id"))
    if request.args.get("nouveau"):
        edite = {"id": "", "type": request.args.get("type", "NS"), "actif": True,
                 "destinataires": {}, "delai_h": DELAI_DEMANDE_H}
    return render_template("notes_modeles.html", page="notes_modeles",
                           tout=modeles_note(actifs_seulement=False), edite=edite, **_contexte())


@bp_notes.route("/modeles/<mid>/supprimer", methods=["POST"])
@_reserve
def supprimer_modele(mid):
    if not C.est_responsable_ged():
        abort(403)
    with C._verrou:
        b = _banque()
        m = b["modeles"].pop(mid, None)
        C._ecrire(F_MODELES, b)
    flash(f"Modèle « {(m or {}).get('nom', mid)} » supprimé." if m else "Modèle introuvable.",
          "success" if m else "error")
    return redirect(url_for("notes.ecran_modeles"))


@bp_notes.route("/<nid>/modele", methods=["POST"])
@_reserve
def enregistrer_modele(nid):
    n = note(nid)
    if not n or not _visible(n):
        abort(404)
    if not (C.est_responsable_ged() or n.get("emetteur") in codes_utilisateur()):
        flash("Seuls la direction émettrice et la responsable GED peuvent en faire un modèle.", "error")
        return redirect(url_for("notes.edition", nid=nid))
    m = modele_depuis_note(n, request.form.get("nom", ""))
    flash(f"Modèle « {m['nom']} » ajouté à la banque. Remplacez-y les éléments propres à cette "
          "note (dates, noms…) par des zones [à compléter : …].", "success")
    if C.est_responsable_ged():
        return redirect(url_for("notes.ecran_modeles", id=m["id"]))
    return redirect(url_for("notes.edition", nid=nid))


# --- Réutilisation ------------------------------------------------------------
@bp_notes.route("/<nid>/dupliquer", methods=["POST"])
@_reserve
def dupliquer(nid):
    src = note(nid)
    if not src or not _visible(src):
        abort(404)
    em = src.get("emetteur") if peut_rediger_pour(src.get("emetteur")) else \
        (directions_redigeables() or [{"code": src.get("emetteur")}])[0]["code"]
    n = {"id": C._id_court("NOTE"), "statut": "brouillon", "cree_le": C._maintenant(),
         "cree_par": C.utilisateur_courant(), "cree_par_email": _email(), "historique": [],
         "type": src.get("type", "NS"), "emetteur": em,
         "service": src.get("service") if em == src.get("emetteur")
         else SERVICE_PAR_DIRECTION.get(em, "DGA"),
         "objet": src.get("objet", ""), "corps": src.get("corps", ""),
         "importante": bool(src.get("importante")), "copies": src.get("copies", ""),
         "destinataires": dict(src.get("destinataires") or {}),
         "traitants": list(src.get("traitants") or []),
         "delai_h": src.get("delai_h") or DELAI_DEMANDE_H,
         "confidentielle": bool(src.get("confidentielle")), "duplique_de": nid}
    _historiser(n, "Créée par duplication", src.get("reference") or src.get("objet", ""))
    with C._verrou:
        toutes = notes()
        toutes[n["id"]] = n
        _sauver(toutes)
    flash("Copie créée en brouillon : adaptez-la, puis soumettez-la.", "success")
    return redirect(url_for("notes.edition", nid=n["id"]))


# --- Assistance IA ---------------------------------------------------------------
@bp_notes.route("/ia", methods=["POST"])
@_reserve
def ia():
    f = request.form
    mode = f.get("mode", "rediger")
    nid = f.get("nid", "")
    n = note(nid) if nid else None
    if n and not peut_modifier(n):
        return {"ok": False, "erreur": "Cette note n'est plus modifiable."}, 400
    if not peut_rediger_pour(f.get("emetteur", "")):
        return {"ok": False, "erreur": "Vous ne pouvez pas rédiger au nom de cette direction."}, 403
    donnees = {"type": f.get("type"), "emetteur": f.get("emetteur", ""),
               "objet": f.get("objet", ""), "corps": f.get("corps", ""),
               "points": f.get("points", ""), "destinataires_lib": f.get("destinataires_lib", ""),
               "confidentielle": f.get("confidentielle") == "on"
               or bool((n or {}).get("confidentielle"))}
    ok, texte, alertes = assistance_ia("reformuler" if mode == "reformuler" else "rediger", donnees)
    if not ok:
        return {"ok": False, "erreur": texte}
    if n:
        ia_cfg = C.parametres().get("ia", {})
        with C._verrou:
            toutes = notes()
            cible = toutes.get(nid)
            if cible:
                cible["ia"] = {"utilisee": True, "le": C._maintenant(),
                               "modele": ia_cfg.get("modele"), "fournisseur": ia_cfg.get("fournisseur")}
                _historiser(cible, "Texte corrigé par l'IA" if mode == "reformuler"
                            else "Texte proposé par l'IA",
                            f"{ia_cfg.get('fournisseur')} / {ia_cfg.get('modele')}")
                _sauver(toutes)
    return {"ok": True, "texte": texte, "alertes": alertes}


# --- Personnel (annuaire interne) ------------------------------------------
@bp_notes.route("/personnel", methods=["GET", "POST"])
@_reserve
def ecran_personnel():
    if request.method == "POST":
        if not C.est_responsable_ged():
            flash("Seule la responsable GED tient l'annuaire du personnel.", "error")
            return redirect(url_for("notes.ecran_personnel"))
        f = request.form
        with C._verrou:
            liste = C._lire(F_PERSONNEL, [])
            if f.get("action") == "importer":
                ajoutes = 0
                connus = {(p.get("email") or "").lower() for p in liste if p.get("email")}
                for ligne in (f.get("lignes") or "").splitlines():
                    morceaux = [x.strip() for x in re.split(r"[;\t]", ligne)]
                    if len(morceaux) < 2 or not morceaux[0] or morceaux[0].lower() == "nom":
                        continue
                    nom, fonction, service, email = (morceaux + ["", "", ""])[:4]
                    service = service.upper() if service.upper() in _services() else "DGA"
                    if email and email.lower() in connus:
                        continue
                    liste.append({"id": _id_personne(), "nom": nom, "fonction": fonction,
                                  "service": service, "email": email.lower(), "actif": True})
                    connus.add(email.lower())
                    ajoutes += 1
                flash(f"{ajoutes} personne(s) ajoutée(s).", "success")
            else:
                pid = f.get("id") or _id_personne()
                p = next((x for x in liste if x.get("id") == pid), None)
                if p is None:
                    p = {"id": pid}
                    liste.append(p)
                p.update({"nom": f.get("nom", "").strip(), "fonction": f.get("fonction", "").strip(),
                          "service": f.get("service", "DGA"),
                          "email": f.get("email", "").strip().lower(),
                          "actif": f.get("actif") == "on"})
                if not p["nom"]:
                    liste.remove(p)
                    flash("Le nom est obligatoire.", "error")
                else:
                    flash(f"{p['nom']} enregistré(e).", "success")
            C._ecrire(F_PERSONNEL, liste)
        return redirect(url_for("notes.ecran_personnel"))
    edite = next((p for p in personnel() if p.get("id") == request.args.get("id")), None)
    return render_template("notes_personnel.html", page="notes_personnel",
                           tout=personnel(), edite=edite, **_contexte())


# ---------------------------------------------------------------------------
# Page du destinataire (sans compte, jeton personnel)
# ---------------------------------------------------------------------------
def _par_jeton(jeton):
    if not jeton or len(jeton) < 20:
        return None, None
    import hmac  # noqa: PLC0415
    for n in notes().values():
        for l in n.get("lectures", []):
            if hmac.compare_digest(str(l.get("jeton", "")), str(jeton)):
                return n, l
    return None, None


def _ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "")


@bp_note_public.after_request
def _pas_de_cache(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@bp_note_public.route("/<jeton>")
def page(jeton):
    n, l = _par_jeton(jeton)
    if not n:
        return render_template("note_lecture.html", n=None, l=None), 404
    return render_template("note_lecture.html", n=_affichee(n), l=l, jeton=jeton,
                           paragraphes=C._paragraphes(n.get("corps")),
                           message=request.args.get("m", ""))


@bp_note_public.route("/<jeton>/note.pdf")
def pdf_public(jeton):
    n, _ = _par_jeton(jeton)
    chemin = DOSSIER / (n or {}).get("id", "_") / "note.pdf"
    if not n or not chemin.exists():
        abort(404)
    return send_file(chemin, mimetype="application/pdf",
                     download_name=f"{n.get('reference', n['id']).replace('/', '-')}.pdf")


@bp_note_public.route("/<jeton>/action", methods=["POST"])
def action(jeton):
    n, l = _par_jeton(jeton)
    if not n:
        abort(404)
    quoi = request.form.get("quoi")
    reponse = (request.form.get("reponse") or "").strip()[:2000]
    if quoi == "traite" and not reponse:
        return redirect(url_for("note_public.page", jeton=jeton,
                                m="Indiquez la réponse ou la suite donnée."))
    message = ""
    with C._verrou:
        toutes = notes()
        n = toutes[n["id"]]
        l = next(x for x in n["lectures"] if x["jeton"] == jeton)
        trace = {"ip": _ip(), "appareil": (request.headers.get("User-Agent") or "")[:200]}
        if quoi == "lu" and not l.get("lu_le"):
            l["lu_le"] = C._maintenant()
            l["trace_lecture"] = trace
            _historiser(n, "Lu et pris connaissance", f"{l['nom']}", l["nom"])
            message = "Merci : votre lecture est enregistrée."
        elif quoi == "prise" and l.get("traitant") and not l.get("pris_en_charge_le"):
            l["pris_en_charge_le"] = C._maintenant()
            l["lu_le"] = l.get("lu_le") or l["pris_en_charge_le"]
            _historiser(n, "Prise en charge", l["nom"], l["nom"])
            message = "Prise en charge enregistrée."
        elif quoi == "traite" and l.get("traitant") and not l.get("traite_le"):
            l["traite_le"] = C._maintenant()
            l["reponse"] = reponse
            l["lu_le"] = l.get("lu_le") or l["traite_le"]
            l["trace_traitement"] = trace
            _historiser(n, "Demande traitée", f"{l['nom']} — {reponse[:200]}", l["nom"])
            traitants = [x for x in n["lectures"] if x.get("traitant")]
            if traitants and all(x.get("traite_le") for x in traitants):
                n["statut"] = "traitee"
                n["traitee_le"] = C._maintenant()
            message = "Réponse enregistrée. Merci."
        _sauver(toutes)
    if quoi == "traite":
        createur = n.get("cree_par_email") or direction(n["emetteur"]).get("email")
        try:
            import notifier  # noqa: PLC0415
            if createur:
                notifier.message_cliq_personne(
                    createur, f"✅ Demande traitée par {l['nom']} : {n.get('objet', '')}\n{reponse[:500]}")
        except Exception:  # noqa: BLE001
            logging.error("Notification de traitement impossible", exc_info=True)
    return redirect(url_for("note_public.page", jeton=jeton, m=message))


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------
def init_notes(app, protection=None, archiver_note=None, dernier_numero=None):
    """À appeler APRÈS l'enregistrement des deux blueprints. Seuls les écrans
    /notes sont protégés : /n/<jeton> est ouvert par le destinataire."""
    _CTX["archiver_note"] = archiver_note
    _CTX["dernier_numero"] = dernier_numero
    if protection is not None:
        for nom, vue in list(app.view_functions.items()):
            if nom.startswith("notes."):
                app.view_functions[nom] = protection(vue)
    DOSSIER.mkdir(parents=True, exist_ok=True)
    print(f"[NOTES] Module actif — {len(notes())} note(s), "
          f"{len(personnel(actifs_seulement=True))} personne(s) dans l'annuaire interne, "
          f"{len(modeles_note())} modèle(s)")
