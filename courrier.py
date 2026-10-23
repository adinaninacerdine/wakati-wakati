"""
courrier.py — Module Courrier de réponse de la GED WAKATI
==========================================================
Après l'enregistrement d'un courrier entrant, la GED propose un projet de
réponse prêt à relire, adapté au destinataire grâce à une banque de
modèles. Le projet suit ensuite le circuit de validation existant (visa de
la Direction, signature manuscrite, dépôt du scan), puis s'archive en
courrier sortant (CSOR) relié au courrier d'origine.

DÉCISIONS DE LA DIRECTION (octobre 2026)
----------------------------------------
  - Les modèles sont rédigés et validés par la responsable GED : seuls les
    modèles validés sont proposés aux rédacteurs.
  - Délai de réponse : 48 h par défaut, 24 h (ou moins) si le courrier est
    marqué urgent. Réglable par catégorie dans l'écran Paramètres.
  - Assistance IA autorisée. Le fournisseur, le modèle et les catégories
    concernées se règlent dans l'écran Paramètres. Défaut : Groq,
    openai/gpt-oss-120b (le même que l'assistant du CRM Odoo).
  - Zoho Sign (lot 2) : branché plus tard. En attendant, « À valider » et
    « En signature » passent par le circuit de visa interne.

CE QUE LE MODULE STOCKE (dossier courrier/, hors Git)
-----------------------------------------------------
  categories.json      registre, signataire, délai et IA par catégorie
  correspondants.json  annuaire : civilité, formule d'appel, adresse...
  modeles.json         bibliothèque : une nature x une catégorie
  reponses.json        projets de réponse et leur historique
  entrants.json        suivi des courriers entrants : échéance, statut
  parametres.json      signataires, IA, délais
  secret_ia.json       clé d'API saisie depuis l'écran (chmod 600)

PAPIER À EN-TÊTE
----------------
Les PDF reproduisent les modèles officiels de la Direction (lettre standard
et décision du DG) : logos WAKATI et Huri Money, filigrane, pied de page.
Images dans entete/ (versionnées avec l'application).
  textes/              texte extrait (ou OCR) des courriers entrants
  projets/             PDF générés

CLÉ D'API IA — trois emplacements possibles, par ordre de priorité
------------------------------------------------------------------
  1. saisie dans l'écran Paramètres (courrier/secret_ia.json)
  2. IA_API_KEY dans notifier_config.py
  3. variable d'environnement IA_API_KEY (ou GROQ_API_KEY)

RÔLES — auth_config.py
----------------------
    RESPONSABLES_GED = ["animation.reseau@hurimoney.com"]
  Gèrent la banque, valident les modèles et règlent les paramètres. La
  Direction (UTILISATEURS_DG) a les mêmes droits. Sans cette liste, tout
  utilisateur authentifié y accède — en local, sans authentification,
  tout le monde.
"""

import copy
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import requests
from flask import (Blueprint, abort, flash, has_request_context, jsonify,
                   redirect, render_template, request, Response, send_file,
                   session, url_for)

# ---------------------------------------------------------------------------
# Emplacements
# ---------------------------------------------------------------------------
RACINE = Path(__file__).resolve().parent
DONNEES = Path(os.getenv("GED_DONNEES", str(RACINE))) / "courrier"
DOSSIER_TEXTES = DONNEES / "textes"
DOSSIER_PROJETS = DONNEES / "projets"

F_CATEGORIES = DONNEES / "categories.json"
F_CORRESPONDANTS = DONNEES / "correspondants.json"
F_MODELES = DONNEES / "modeles.json"
F_REPONSES = DONNEES / "reponses.json"
F_ENTRANTS = DONNEES / "entrants.json"
F_PARAMETRES = DONNEES / "parametres.json"
F_SECRET_IA = DONNEES / "secret_ia.json"

FICHIER_VISAS = RACINE / "visas.json"

bp_courrier = Blueprint("courrier", __name__, url_prefix="/courrier")

_verrou = threading.RLock()

# Rempli par init_courrier() : dépendances fournies par app.py, pour
# éviter un import circulaire de app.
_CTX = {
    "services": {},
    "types": {},
    "dernier_numero": None,
    "notifier": None,
}

# ---------------------------------------------------------------------------
# Référentiels fixes
# ---------------------------------------------------------------------------
NATURES = {
    "AR": "Accusé de réception",
    "FAV": "Réponse favorable",
    "DEF": "Réponse défavorable motivée",
    "COMP": "Demande de compléments",
    "TRANS": "Transmission de documents",
    "REL": "Relance",
    "RDV": "Proposition de rendez-vous",
    "LIBRE": "Courrier libre",
    "DEC": "Décision du Directeur Général",
}

MISES_EN_PAGE = {
    "lettre": "Lettre (modèle standard Huri Money)",
    "decision": "Décision (Vu… / DÉCIDE / articles)",
}

STATUTS = {
    "brouillon": "Brouillon",
    "a_valider": "À valider",
    "retourne": "Retourné",
    "en_signature": "En signature",
    "signe": "Signé",
    "archive": "Archivé",
    "abandonne": "Abandonné",
}

STATUTS_ENTRANT = {
    "a_preparer": "À préparer",
    "en_cours": "Réponse en cours",
    "repondu": "Répondu",
    "sans_objet": "Sans réponse attendue",
}

URGENCES = {
    "normale": "Normale",
    "urgente": "Urgente",
    "aucune": "Pas de réponse attendue",
}

FOURNISSEURS_IA = {
    "groq": {"nom": "Groq", "url": "https://api.groq.com/openai/v1"},
    "openai": {"nom": "OpenAI", "url": "https://api.openai.com/v1"},
    "mistral": {"nom": "Mistral AI", "url": "https://api.mistral.ai/v1"},
    "openrouter": {"nom": "OpenRouter", "url": "https://openrouter.ai/api/v1"},
    "autre": {"nom": "Autre (compatible OpenAI)", "url": ""},
}

A_COMPLETER = "[à compléter]"
MOTIF_A_COMPLETER = re.compile(r"\[à compléter[^\]]*\]", re.IGNORECASE)
PC_VIDE = "[à compléter : réponse aux points soulevés]"

LIBELLES_VARIABLES = {
    "civilite": "civilité",
    "formule_appel": "formule d'appel",
    "ref_entrant": "référence du courrier reçu",
    "date_entrant": "date du courrier reçu",
    "date_reception": "date de réception",
    "objet_entrant": "objet du courrier reçu",
    "objet": "objet",
    "ref_sortant": "référence du courrier",
    "signataire": "nom du signataire",
    "fonction_signataire": "fonction du signataire",
    "organisation": "organisation",
    "formule_politesse": "formule de politesse",
    "paragraphe_central": "réponse aux points soulevés",
    "date_jour": "date",
}

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]


# ---------------------------------------------------------------------------
# Données de démarrage
# ---------------------------------------------------------------------------
# Formules de politesse : {{formule_appel}} est remplacé à la fusion.
_POL_TRES_HAUTE = ("Nous vous prions d'agréer, {{formule_appel}}, l'expression "
                   "de notre très haute considération.")
_POL_HAUTE = ("Nous vous prions d'agréer, {{formule_appel}}, l'expression de "
              "notre haute considération.")
_POL_DISTINGUEE = ("Nous vous prions d'agréer, {{formule_appel}}, l'expression "
                   "de nos salutations distinguées.")
_POL_CORDIALE = "Nous vous prions de recevoir nos cordiales salutations."

CATEGORIES_DEFAUT = [
    {"code": "REG", "nom": "Régulateur", "exemples": "BCC, ANRTIC",
     "registre": "très formel, références réglementaires",
     "signataire": "DG", "delai_h": 48, "delai_urgent_h": 24,
     "ia_autorisee": False, "confidentiel": True,
     "formule_politesse": _POL_TRES_HAUTE},
    {"code": "PART", "nom": "Partenaire stratégique", "exemples": "Comores Télécom",
     "registre": "formel", "signataire": "DG", "delai_h": 48, "delai_urgent_h": 24,
     "ia_autorisee": True, "confidentiel": False,
     "formule_politesse": _POL_HAUTE},
    {"code": "BAN", "nom": "Banques", "exemples": "banques teneuses de comptes",
     "registre": "formel", "signataire": "DAF", "delai_h": 48, "delai_urgent_h": 24,
     "ia_autorisee": True, "confidentiel": False,
     "formule_politesse": _POL_HAUTE},
    {"code": "PROG", "nom": "Programmes et bailleurs", "exemples": "PFSS",
     "registre": "formel", "signataire": "DG", "delai_h": 48, "delai_urgent_h": 24,
     "ia_autorisee": True, "confidentiel": False,
     "formule_politesse": _POL_HAUTE},
    {"code": "ADM", "nom": "Administration",
     "exemples": "ministères, services fiscaux",
     "registre": "formel", "signataire": "DG", "delai_h": 48, "delai_urgent_h": 24,
     "ia_autorisee": True, "confidentiel": False,
     "formule_politesse": _POL_HAUTE},
    {"code": "GOUV", "nom": "Gouvernance", "exemples": "Conseil d'administration",
     "registre": "très formel", "signataire": "DG", "delai_h": 48, "delai_urgent_h": 24,
     "ia_autorisee": False, "confidentiel": True,
     "formule_politesse": _POL_HAUTE},
    {"code": "FOUR", "nom": "Fournisseurs et prestataires", "exemples": "",
     "registre": "courtois", "signataire": "DAF", "delai_h": 48, "delai_urgent_h": 24,
     "ia_autorisee": True, "confidentiel": False,
     "formule_politesse": _POL_DISTINGUEE},
    {"code": "RES", "nom": "Réseau", "exemples": "concessionnaires, agents",
     "registre": "direct", "signataire": "DCOM", "delai_h": 48, "delai_urgent_h": 24,
     "ia_autorisee": True, "confidentiel": False,
     "formule_politesse": _POL_CORDIALE},
    {"code": "PARTI", "nom": "Particuliers", "exemples": "clients, bénéficiaires",
     "registre": "simple, pédagogique", "signataire": "SC",
     "delai_h": 48, "delai_urgent_h": 24,
     "ia_autorisee": True, "confidentiel": False,
     "formule_politesse": _POL_DISTINGUEE},
]

SIGNATAIRES_DEFAUT = [
    {"code": "DG", "fonction": "Le Directeur Général", "nom": "Si Mohamed Souef",
     "email": ""},
    {"code": "DAF", "fonction": "Le Directeur Administratif et Financier",
     "nom": "Soilihi Hamadi Ali", "email": ""},
    {"code": "DT", "fonction": "Le Directeur Technique", "nom": "", "email": ""},
    {"code": "DCOM", "fonction": "La Direction Commerciale", "nom": "", "email": ""},
    {"code": "SC", "fonction": "Le Service Client", "nom": "", "email": ""},
]

# Annuaire de départ, tiré des dossiers « Courrier entrants » et « Courrier
# sortants » de la GED. Civilités et adresses restent à compléter par la
# responsable GED : chaque fiche porte la mention « à vérifier ».
_C = [
    ("Banque Centrale des Comores", "REG", "BCC, Banque Centrale",
     "Monsieur le Gouverneur", True),
    ("Autorité Nationale de Régulation des TIC", "REG", "ANRTIC", "", True),
    ("Comores Télécom", "PART", "Comores Telecom, CT", "", False),
    ("VISA Inc.", "PART", "VISA", "", False),
    ("Canal+", "PART", "Canal Plus", "", False),
    ("AFG Bank Comores", "BAN", "AFG Bank, AFG", "", False),
    ("Banque Postale des Comores", "BAN", "BPC", "", False),
    ("Exim Bank Comores", "BAN", "Exim Bank, Exim", "", False),
    ("PFSS-RRC Coordination Nationale", "PROG",
     "PFSS, RRC, Filets sociaux, Filets Sociaux de Sécurité", "", False),
    ("Banque Mondiale", "PROG", "Banque Mondial, World Bank", "", False),
    ("Direction Générale des Impôts", "ADM", "DGI, Impôts", "", False),
    ("Ministère des Affaires Étrangères", "ADM",
     "MIN DES AFF ETRANGERES, Affaires étrangères", "", False),
    ("Ministère des Postes et Télécommunications", "ADM",
     "Min des postes, MIN POSTE, Ministère des Postes", "", False),
    ("Secrétariat Général du Gouvernement", "ADM",
     "SGG, Secrétaire Général du Gouvernement", "", False),
    ("Ministère de l'Agriculture, de la Pêche et de l'Artisanat", "ADM",
     "Ministère de l'Agriculture", "", False),
    ("Commune de Moroni", "ADM", "Mairie de Moroni", "", False),
    ("Conseil d'Administration de Huri Money SA", "GOUV",
     "Conseil d'Administration", "", True),
    ("Aptiway", "FOUR", "", "", False),
    ("Étude de Maître Arfachade Hassane", "FOUR", "Arfachade", "", False),
]

CORRESPONDANTS_DEFAUT = [
    {"organisation": org, "categorie": cat, "alias": alias,
     "formule_appel": appel, "confidentiel": conf}
    for org, cat, alias, appel, conf in _C
]

_DEBUT_REPONSE = ("Par votre courrier référencé {{ref_entrant}} du "
                  "{{date_entrant}}, reçu le {{date_reception}}, vous avez "
                  "bien voulu nous saisir au sujet de : {{objet_entrant}}.")

MODELES_DEFAUT = [
    ("AR", "Accusé de réception",
     "{{formule_appel}},\n\n"
     "Nous accusons bonne réception de votre courrier référencé "
     "{{ref_entrant}} du {{date_entrant}}, reçu le {{date_reception}}, "
     "relatif à : {{objet_entrant}}.\n\n"
     "{{paragraphe_central}}\n\n"
     "Nous ne manquerons pas de revenir vers vous dans les meilleurs délais.\n\n"
     "{{formule_politesse}}"),
    ("FAV", "Réponse favorable",
     "{{formule_appel}},\n\n" + _DEBUT_REPONSE + "\n\n"
     "Nous avons le plaisir de vous informer que votre demande a reçu une "
     "suite favorable.\n\n"
     "{{paragraphe_central}}\n\n"
     "Nous restons à votre disposition pour toute information complémentaire.\n\n"
     "{{formule_politesse}}"),
    ("DEF", "Réponse défavorable motivée",
     "{{formule_appel}},\n\n" + _DEBUT_REPONSE + "\n\n"
     "Après examen attentif de votre demande, nous avons le regret de vous "
     "informer que nous ne sommes pas en mesure d'y donner une suite "
     "favorable, pour les raisons suivantes.\n\n"
     "{{paragraphe_central}}\n\n"
     "Nous demeurons ouverts à tout échange qui permettrait d'envisager une "
     "solution adaptée.\n\n"
     "{{formule_politesse}}"),
    ("COMP", "Demande de compléments",
     "{{formule_appel}},\n\n" + _DEBUT_REPONSE + "\n\n"
     "Afin de poursuivre l'instruction de ce dossier, nous vous serions "
     "reconnaissants de bien vouloir nous transmettre les éléments "
     "suivants.\n\n"
     "{{paragraphe_central}}\n\n"
     "Dès réception de ces éléments, nous reviendrons vers vous.\n\n"
     "{{formule_politesse}}"),
    ("TRANS", "Transmission de documents",
     "{{formule_appel}},\n\n"
     "Suite à votre courrier référencé {{ref_entrant}} du {{date_entrant}}, "
     "relatif à : {{objet_entrant}}, nous avons l'honneur de vous "
     "transmettre, sous ce pli, les documents suivants.\n\n"
     "{{paragraphe_central}}\n\n"
     "Nous restons à votre disposition pour toute information complémentaire.\n\n"
     "{{formule_politesse}}"),
    ("REL", "Relance",
     "{{formule_appel}},\n\n"
     "Nous revenons vers vous au sujet de notre courrier référencé "
     "{{ref_entrant}} du {{date_entrant}}, relatif à : {{objet_entrant}}, "
     "resté à ce jour sans réponse.\n\n"
     "{{paragraphe_central}}\n\n"
     "Nous vous serions reconnaissants de bien vouloir nous faire part de "
     "votre retour dans les meilleurs délais.\n\n"
     "{{formule_politesse}}"),
    ("RDV", "Proposition de rendez-vous",
     "{{formule_appel}},\n\n" + _DEBUT_REPONSE + "\n\n"
     "Afin d'échanger sur ce sujet, nous vous proposons de nous rencontrer le "
     "[à compléter : date et heure], dans nos locaux à Moroni, ou à toute "
     "autre date qui vous conviendrait.\n\n"
     "{{paragraphe_central}}\n\n"
     "{{formule_politesse}}"),
    ("LIBRE", "Courrier libre",
     "{{formule_appel}},\n\n"
     "{{paragraphe_central}}\n\n"
     "{{formule_politesse}}"),
]

# Modèles officiels transmis par la Direction Générale (octobre 2026).
# Installés une fois, validés d'office : ils viennent de la Direction.
MODELE_LETTRE_STANDARD = (
    "{{formule_appel}},\n\n"
    "{{paragraphe_central}}\n\n"
    "Nous restons à votre entière disposition pour toute information "
    "complémentaire et vous prions d'agréer, {{formule_appel}}, l'expression "
    "de nos salutations distinguées.")

MODELE_DECISION = """Vu la Constitution de l'Union des Comores du 23 décembre 2001, révisée par la loi référendaire du 30 juillet 2018 ;
Vu le décret n°07-151/PR du 03 septembre 2007, portant fixation de certaines modalités de gestion et d'administration des sociétés à capitaux publics et établissements publics à caractère industriel et commercial, modifié par le décret n°11-155/PR du 28 juillet 2011 ;
Vu le décret n°20-156/PR du 07 décembre 2020, portant mise en place du statut de la Société Anonyme « Huri Money » ;
Vu le décret n°24-186/PR du 13 août 2024, portant nomination du Directeur Général de la Société Anonyme Huri Money ;
Vu la nécessité du service ;

DÉCIDE

Article 1 · Création et composition
[à compléter : nature de l'organe ou de la mesure créée, et son rattachement.] Le Comité est composé des membres suivants :
| Nom | Matricule | Fonction / rôle dans le comité |
| [à compléter : nom] | [à compléter : matricule] | [à compléter : fonction] |
| [à compléter : nom] | [à compléter : matricule] | [à compléter : fonction] |

Article 2 · Missions
[à compléter : phrase d'introduction des missions.]
- [à compléter : mission 1] ;
- [à compléter : mission 2].

Article 3 · Fonctionnement
[à compléter : fréquence des réunions, quorum, modalités de compte-rendu.]

Article 4 · Entrée en vigueur
La présente décision prend effet à compter de sa date de signature. Elle sera notifiée aux membres concernés, communiquée et publiée partout où besoin sera."""

MODELES_OFFICIELS = [
    {"id": "m-libre-lettre-standard-hm", "nom": "Lettre standard Huri Money",
     "nature": "LIBRE", "mise_en_page": "lettre", "corps": MODELE_LETTRE_STANDARD},
    {"id": "m-dec-decision-dg", "nom": "Décision du Directeur Général",
     "nature": "DEC", "mise_en_page": "decision", "corps": MODELE_DECISION},
]

VERSION_BANQUE = 2

PARAMETRES_DEFAUT = {
    "signataires": SIGNATAIRES_DEFAUT,
    "service_defaut": "DGA",
    "delai_defaut_h": 48,
    "delai_urgent_defaut_h": 24,
    "relance_intervalle_h": 24,
    # Références imprimées, au format des modèles Huri Money.
    # Variables : {annee} {numero} {signataire} {service}
    "format_reference": "N°{annee}-{numero}/{signataire}/HM",
    "format_reference_decision": "{annee}-{numero}/{signataire}/H.M",
    "pied_de_page": ("Huri Money S.A. — Rond-point Ambassadeur, B.P. 222, Moroni, "
                     "Union des Comores — Tél. : +269 773 14 75 — contact@wakati.km"),
    "version_banque": 0,
    "ia": {
        "actif": True,
        "fournisseur": "groq",
        "base_url": FOURNISSEURS_IA["groq"]["url"],
        "modele": "openai/gpt-oss-120b",
        "temperature": 0.3,
        "max_tokens": 3000,
        "effort_raisonnement": "low",
        "delai_s": 60,
        "anonymiser": True,
        "autoriser_confidentiels": False,
    },
}


# ---------------------------------------------------------------------------
# Persistance
# ---------------------------------------------------------------------------
def _lire(fichier, defaut):
    if not fichier.exists():
        return copy.deepcopy(defaut)
    try:
        return json.loads(fichier.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logging.error("Lecture impossible : %s", fichier)
        return copy.deepcopy(defaut)


def _ecrire(fichier, donnees):
    """Écriture atomique : un plantage en cours d'écriture ne laisse jamais
    un fichier tronqué."""
    with _verrou:
        fichier.parent.mkdir(parents=True, exist_ok=True)
        tmp = fichier.with_suffix(fichier.suffix + ".tmp")
        tmp.write_text(json.dumps(donnees, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, fichier)


def _maintenant():
    return datetime.now().isoformat(timespec="seconds")


def _id_court(prefixe):
    return datetime.now().strftime(f"{prefixe}-%Y%m%d-%H%M%S-%f")[:-3]


def _slug(texte):
    texte = unicodedata.normalize("NFKD", texte or "")
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", texte.lower()).strip("-")[:40] or "x"


def _normaliser(texte):
    texte = unicodedata.normalize("NFKD", (texte or "").lower())
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    texte = texte.replace("’", "'")
    return re.sub(r"[^a-z0-9]+", " ", texte).strip()


def assurer_banque():
    """Crée les fichiers de démarrage au premier lancement. Ne réécrit
    jamais un fichier existant."""
    with _verrou:
        DONNEES.mkdir(parents=True, exist_ok=True)
        DOSSIER_TEXTES.mkdir(exist_ok=True)
        DOSSIER_PROJETS.mkdir(exist_ok=True)

        if not F_CATEGORIES.exists():
            _ecrire(F_CATEGORIES, CATEGORIES_DEFAUT)

        if not F_CORRESPONDANTS.exists():
            fiches = []
            for c in CORRESPONDANTS_DEFAUT:
                fiche = _fiche_vide()
                fiche.update(c)
                fiche["id"] = _slug(c["organisation"])
                fiche["a_verifier"] = True
                fiches.append(fiche)
            _ecrire(F_CORRESPONDANTS, fiches)

        if not F_MODELES.exists():
            modeles = []
            for nature, nom, corps in MODELES_DEFAUT:
                modeles.append({
                    "id": f"m-{nature.lower()}-generique",
                    "nom": f"{nom} — modèle de démarrage",
                    "nature": nature,
                    "categorie": "*",
                    "correspondant_id": "",
                    "corps": corps,
                    "statut": "a_valider",
                    "valide_par": "",
                    "valide_le": "",
                    "version": 1,
                    "cree_le": _maintenant(),
                    "maj_le": _maintenant(),
                })
            _ecrire(F_MODELES, modeles)

        if not F_PARAMETRES.exists():
            _ecrire(F_PARAMETRES, PARAMETRES_DEFAUT)

        _migrer_banque()


def _migrer_banque():
    """Installe, une seule fois, les modèles ajoutés après la création de la
    banque (modèles officiels de la Direction). Un modèle supprimé ensuite par
    la responsable GED n'est jamais réinstallé."""
    p = _lire(F_PARAMETRES, PARAMETRES_DEFAUT)
    if int(p.get("version_banque", 0)) >= VERSION_BANQUE:
        return
    liste = _lire(F_MODELES, [])
    presents = {m.get("id") for m in liste}
    for officiel in MODELES_OFFICIELS:
        if officiel["id"] in presents:
            continue
        liste.append({
            **officiel,
            "categorie": "*",
            "correspondant_id": "",
            "statut": "valide",
            "valide_par": "Direction Générale (modèle officiel)",
            "valide_le": _maintenant(),
            "version": 1,
            "cree_le": _maintenant(),
            "maj_le": _maintenant(),
        })
    _ecrire(F_MODELES, liste)
    p["version_banque"] = VERSION_BANQUE
    _ecrire(F_PARAMETRES, p)


def _fiche_vide():
    return {
        "id": "", "organisation": "", "nom": "", "fonction": "",
        "civilite": "", "formule_appel": "", "adresse": "", "email": "",
        "categorie": "", "signataire": "", "copies": "", "alias": "",
        "confidentiel": False, "a_verifier": False, "notes": "",
    }


def categories():
    return _lire(F_CATEGORIES, CATEGORIES_DEFAUT)


def categorie(code):
    for c in categories():
        if c.get("code") == code:
            return c
    return None


def correspondants():
    liste = _lire(F_CORRESPONDANTS, [])
    liste.sort(key=lambda f: (f.get("categorie", ""), f.get("organisation", "")))
    return liste


def correspondant(cid):
    if not cid:
        return None
    for f in correspondants():
        if f.get("id") == cid:
            return f
    return None


def modeles():
    return _lire(F_MODELES, [])


def modele(mid):
    for m in modeles():
        if m.get("id") == mid:
            return m
    return None


def parametres():
    p = _lire(F_PARAMETRES, PARAMETRES_DEFAUT)
    # Complète les clés ajoutées dans une version ultérieure.
    for cle, val in PARAMETRES_DEFAUT.items():
        if cle not in p:
            p[cle] = copy.deepcopy(val)
    for cle, val in PARAMETRES_DEFAUT["ia"].items():
        p["ia"].setdefault(cle, val)
    return p


def signataire(code):
    for s in parametres().get("signataires", []):
        if s.get("code") == code:
            return s
    return None


def codes_signataire_pour(email):
    """Codes de signataire rattachés à une adresse (utilisé par visa.py)."""
    if not email:
        return set()
    email = email.strip().lower()
    return {s["code"] for s in parametres().get("signataires", [])
            if (s.get("email") or "").strip().lower() == email}


def reponses():
    return _lire(F_REPONSES, {})


def entrants():
    return _lire(F_ENTRANTS, {})


def _visas():
    if not FICHIER_VISAS.exists():
        return {}
    try:
        return json.loads(FICHIER_VISAS.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Rôles
# ---------------------------------------------------------------------------
def _auth_liste(nom):
    try:
        import auth_config  # noqa: PLC0415
        valeur = getattr(auth_config, nom, None)
    except ImportError:
        return None
    if valeur is None:
        return None
    return {a.strip().lower() for a in valeur}


def utilisateur_courant():
    if not has_request_context():
        return "GED"
    return session.get("nom") or session.get("email") or "Service GED"


def est_responsable_ged():
    if not has_request_context():
        return True
    email = (session.get("email") or "").lower()
    if not email:
        return True  # local, sans authentification
    responsables = _auth_liste("RESPONSABLES_GED")
    if responsables is None:
        return True
    dg = _auth_liste("UTILISATEURS_DG") or set()
    return email in responsables or email in dg


def _reserve_responsable(vue):
    @functools.wraps(vue)
    def enveloppe(*args, **kwargs):
        if not est_responsable_ged():
            flash("Écran réservé à la responsable GED et à la Direction.", "error")
            return redirect(url_for("courrier.liste"))
        return vue(*args, **kwargs)
    return enveloppe


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
def date_longue(valeur):
    """2026-10-16 -> 16 octobre 2026. Laisse intact ce qui n'est pas une date."""
    if not valeur:
        return ""
    try:
        d = datetime.fromisoformat(str(valeur)[:19])
    except ValueError:
        return str(valeur)
    jour = "1er" if d.day == 1 else str(d.day)
    return f"{jour} {MOIS[d.month - 1]} {d.year}"


def date_courte(valeur):
    if not valeur:
        return ""
    try:
        d = datetime.fromisoformat(str(valeur)[:19])
    except ValueError:
        return str(valeur)
    if len(str(valeur)) > 10:
        return d.strftime("%d/%m/%Y %H:%M")
    return d.strftime("%d/%m/%Y")


def _delais(code_categorie):
    p = parametres()
    cat = categorie(code_categorie) or {}
    normal = int(cat.get("delai_h") or p.get("delai_defaut_h", 48))
    urgent = int(cat.get("delai_urgent_h") or p.get("delai_urgent_defaut_h", 24))
    return normal, urgent


def calculer_echeance(recu_le, urgence, code_categorie=""):
    if urgence == "aucune":
        return ""
    normal, urgent = _delais(code_categorie)
    heures = urgent if urgence == "urgente" else normal
    try:
        base = datetime.fromisoformat(recu_le)
    except (ValueError, TypeError):
        base = datetime.now()
    return (base + timedelta(hours=heures)).isoformat(timespec="minutes")


# ---------------------------------------------------------------------------
# Extraction du texte des courriers entrants (PDF numérique ou scan)
# ---------------------------------------------------------------------------
_CANDIDATS_TESSERACT = [r"C:\Program Files\Tesseract-OCR\tesseract.exe"]
_CANDIDATS_PDFTOPPM = [r"C:\poppler\Library\bin\pdftoppm.exe"]


def _outil(nom, candidats):
    trouve = shutil.which(nom)
    if trouve:
        return trouve
    for c in candidats:
        if Path(c).exists():
            return c
    return None


def extraire_texte(chemin_pdf, pages_max=3):
    """Texte du PDF ; OCR (Tesseract) si le PDF est un scan sans texte."""
    texte = ""
    try:
        from pypdf import PdfReader  # noqa: PLC0415
        lecteur = PdfReader(str(chemin_pdf))
        for page in lecteur.pages[:pages_max]:
            texte += (page.extract_text() or "") + "\n"
    except Exception:  # noqa: BLE001
        logging.error("Extraction pypdf impossible : %s", chemin_pdf)

    if len(texte.strip()) >= 120:
        return texte.strip()

    pdftoppm = _outil("pdftoppm", _CANDIDATS_PDFTOPPM)
    tesseract = _outil("tesseract", _CANDIDATS_TESSERACT)
    if not (pdftoppm and tesseract):
        return texte.strip()

    try:
        with tempfile.TemporaryDirectory() as tmp:
            prefixe = Path(tmp) / "p"
            subprocess.run([pdftoppm, "-r", "200", "-f", "1", "-l", str(pages_max),
                            "-png", str(chemin_pdf), str(prefixe)],
                           check=True, timeout=120, capture_output=True)
            morceaux = []
            for image in sorted(Path(tmp).glob("p*.png")):
                res = subprocess.run([tesseract, str(image), "stdout", "-l", "fra"],
                                     timeout=120, capture_output=True, text=True)
                if res.returncode != 0:  # langue fra absente : repli
                    res = subprocess.run([tesseract, str(image), "stdout"],
                                         timeout=120, capture_output=True, text=True)
                morceaux.append(res.stdout)
            ocr = "\n".join(morceaux).strip()
            return ocr if len(ocr) > len(texte.strip()) else texte.strip()
    except Exception:  # noqa: BLE001
        logging.error("OCR impossible : %s", chemin_pdf)
        return texte.strip()


def texte_entrant(entrant_id):
    f = DOSSIER_TEXTES / f"{_slug(entrant_id)}.txt"
    if f.exists():
        try:
            return f.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _extraire_en_tache(entrant_id, copie_pdf):
    def tache():
        try:
            texte = extraire_texte(copie_pdf)
            (DOSSIER_TEXTES / f"{_slug(entrant_id)}.txt").write_text(
                texte, encoding="utf-8")
            with _verrou:
                donnees = entrants()
                e = donnees.get(entrant_id)
                if e is not None:
                    e["texte_dispo"] = bool(texte)
                    if not e.get("correspondant_id"):
                        cid, _ = reconnaitre(e.get("correspondant", ""), texte)
                        if cid:
                            e["correspondant_id"] = cid
                            e["echeance"] = calculer_echeance(
                                e["recu_le"], e.get("urgence", "normale"),
                                (correspondant(cid) or {}).get("categorie", ""))
                    _ecrire(F_ENTRANTS, donnees)
        except Exception:  # noqa: BLE001
            logging.error("Extraction du texte de %s impossible", entrant_id)
        finally:
            try:
                Path(copie_pdf).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=tache, daemon=True).start()


# ---------------------------------------------------------------------------
# Reconnaissance du correspondant
# ---------------------------------------------------------------------------
def _termes(fiche):
    termes = [fiche.get("organisation", ""), fiche.get("nom", "")]
    termes += [a for a in (fiche.get("alias") or "").split(",")]
    return [t for t in (_normaliser(t) for t in termes) if len(t) >= 2]


def suggestions(saisie, texte_pdf="", limite=3):
    """Fiches de l'annuaire les plus probables, avec un score."""
    saisie_n = f" {_normaliser(saisie)} "
    pdf_n = f" {_normaliser((texte_pdf or '')[:2500])} "
    scores = []
    for fiche in correspondants():
        score = 0
        for t in _termes(fiche):
            motif = f" {t} "
            if saisie_n.strip() and motif in saisie_n:
                score = max(score, 10 + len(t))
            elif saisie_n.strip() and len(saisie_n.strip()) >= 4 \
                    and saisie_n.strip() in t:
                score = max(score, 6 + len(saisie_n.strip()))
            elif motif in pdf_n:
                score = max(score, 3 + min(len(t), 20) // 4)
        if score:
            scores.append((score, fiche))
    scores.sort(key=lambda x: x[0], reverse=True)
    return [{"id": f["id"], "organisation": f["organisation"], "score": s}
            for s, f in scores[:limite]]


def reconnaitre(saisie, texte_pdf=""):
    res = suggestions(saisie, texte_pdf, 1)
    if res and res[0]["score"] >= 5:
        return res[0]["id"], res[0]["score"]
    return "", 0


# ---------------------------------------------------------------------------
# Suivi des courriers entrants — API appelée par app.py
# ---------------------------------------------------------------------------
def enregistrer_entrant(entrant_id, meta, chemin_pdf=None, source="visa",
                        urgence="normale"):
    """Ouvre le suivi de réponse d'un courrier entrant.

    meta : service, type_doc, date_doc, objet, correspondant,
           et pour un archivage direct : nom_archive, lien.
    """
    try:
        assurer_banque()
        urgence = urgence if urgence in URGENCES else "normale"
        recu_le = _maintenant()
        cid, _ = reconnaitre(meta.get("correspondant", ""))
        code_cat = (correspondant(cid) or {}).get("categorie", "")
        suivi = {
            "id": entrant_id,
            "source": source,
            "visa_ref": entrant_id if source == "visa" else "",
            "nom_archive": meta.get("nom_archive", ""),
            "lien": meta.get("lien", ""),
            "objet": meta.get("objet", ""),
            "correspondant": meta.get("correspondant", ""),
            "correspondant_id": cid,
            "service": meta.get("service", ""),
            "type_doc": meta.get("type_doc", ""),
            "date_doc": meta.get("date_doc", ""),
            "recu_le": recu_le,
            "urgence": urgence,
            "echeance": calculer_echeance(recu_le, urgence, code_cat),
            "statut_reponse": "sans_objet" if urgence == "aucune" else "a_preparer",
            "reponse_ref": "",
            "derniere_relance": "",
            "texte_dispo": False,
        }
        with _verrou:
            donnees = entrants()
            donnees[entrant_id] = suivi
            _ecrire(F_ENTRANTS, donnees)

        if chemin_pdf and Path(chemin_pdf).exists():
            copie = DOSSIER_TEXTES / f"{_slug(entrant_id)}.pdf"
            shutil.copy2(chemin_pdf, copie)
            _extraire_en_tache(entrant_id, copie)
        return suivi
    except Exception:  # noqa: BLE001
        logging.error("Suivi de l'entrant %s impossible", entrant_id, exc_info=True)
        return None


def entrant_depuis_visa(visa_ref):
    """Ouvre après coup le suivi d'un entrant déposé avant l'installation du
    module (ou sans suivi). Le PDF est relu s'il est encore sur le poste."""
    if visa_ref in entrants():
        return entrants()[visa_ref]
    d = _visas().get(visa_ref)
    if not d or d.get("sens") != "entrant":
        return None
    chemin = None
    for dossier in ("a_viser", "a_classer"):
        for nom in (d.get("fichier", ""),
                    d.get("fichier", "").replace("AVISER_", "ASIGNER_", 1)):
            candidat = RACINE / dossier / nom
            if nom and candidat.exists():
                chemin = candidat
    suivi = enregistrer_entrant(visa_ref, {
        "service": d.get("service", ""), "type_doc": d.get("type_doc", ""),
        "date_doc": d.get("date_doc", ""), "objet": d.get("objet", ""),
        "correspondant": d.get("correspondant", ""),
    }, chemin, source="visa")
    if suivi and d.get("depose_le"):
        with _verrou:
            ents = entrants()
            e = ents[visa_ref]
            e["recu_le"] = d["depose_le"]
            e["echeance"] = calculer_echeance(
                e["recu_le"], e["urgence"],
                (correspondant(e.get("correspondant_id")) or {}).get("categorie", ""))
            _ecrire(F_ENTRANTS, ents)
            suivi = e
    return suivi


def _visa_par_fichier(fichier_local):
    """Retrouve l'entrée de visas.json correspondant à un fichier de a_classer."""
    if not fichier_local:
        return None, None
    for ref, d in _visas().items():
        nom = d.get("fichier", "")
        if nom == fichier_local or nom.replace("AVISER_", "ASIGNER_", 1) == fichier_local:
            return ref, d
    return None, None


def infos_a_classer(fichier_local):
    """Métadonnées complémentaires pour l'écran d'archivage : sens,
    correspondant et numéro réservé d'une réponse."""
    ref, d = _visa_par_fichier(fichier_local)
    if not d:
        return {}
    infos = {"sens": d.get("sens", ""), "correspondant": d.get("correspondant", "")}
    rep_ref = d.get("reponse_ref")
    if rep_ref:
        rep = reponses().get(rep_ref)
        if rep:
            infos["numero"] = rep.get("numero", "")
            infos["date_doc"] = rep.get("date_courrier", "")
            infos["service"] = rep.get("service", "")
            infos["type_doc"] = type_document(rep)
    return infos


def lier_archive(fichier_local, nouveau_nom, lien, sens, meta=None,
                 urgence="normale"):
    """Appelé après chaque archivage réussi. Retourne les valeurs des trois
    colonnes du registre : En réponse à, Statut de la réponse, Échéance.

    - courrier venu du circuit de visa : on retrouve l'entrant ou la réponse
    - entrant archivé directement : on ouvre son suivi
    """
    colonnes = {"En réponse à": "", "Statut de la réponse": "", "Échéance": ""}
    maj_entrant = None
    try:
        visa_ref, d = _visa_par_fichier(fichier_local)

        if d and d.get("reponse_ref"):
            with _verrou:
                reps = reponses()
                rep = reps.get(d["reponse_ref"])
                if rep:
                    rep["statut"] = "archive"
                    rep["nom_archive"] = nouveau_nom
                    rep["lien_archive"] = lien
                    _historiser(rep, "Archivé", nouveau_nom)
                    _ecrire(F_REPONSES, reps)
                    ents = entrants()
                    e = ents.get(rep.get("entrant_id", ""))
                    if e:
                        e["statut_reponse"] = "repondu"
                        _ecrire(F_ENTRANTS, ents)
                        colonnes["Échéance"] = date_courte(e.get("echeance"))
                        if e.get("nom_archive"):
                            maj_entrant = (e["nom_archive"], {
                                "Statut de la réponse": f"Répondu — {nouveau_nom}"})
                    colonnes["En réponse à"] = (
                        (e or {}).get("nom_archive") or rep.get("ref_entrant", ""))
                    colonnes["Statut de la réponse"] = "Archivé"
            return colonnes, maj_entrant

        if d and d.get("sens") == "entrant":
            with _verrou:
                ents = entrants()
                e = ents.get(visa_ref)
                if e:
                    e["nom_archive"] = nouveau_nom
                    e["lien"] = lien
                    _ecrire(F_ENTRANTS, ents)
                    colonnes["Statut de la réponse"] = STATUTS_ENTRANT.get(
                        e.get("statut_reponse"), "")
                    colonnes["Échéance"] = date_courte(e.get("echeance"))
            return colonnes, None

        if not fichier_local and sens == "entrant":
            m = dict(meta or {})
            m.update({"nom_archive": nouveau_nom, "lien": lien})
            e = enregistrer_entrant(f"ARC-{_slug(nouveau_nom)}", m,
                                    m.pop("chemin_pdf", None), source="archive",
                                    urgence=urgence)
            if e:
                colonnes["Statut de la réponse"] = STATUTS_ENTRANT.get(
                    e["statut_reponse"], "")
                colonnes["Échéance"] = date_courte(e.get("echeance"))
    except Exception:  # noqa: BLE001
        logging.error("Lien archive/courrier impossible pour %s", nouveau_nom,
                      exc_info=True)
    return colonnes, maj_entrant


# ---------------------------------------------------------------------------
# Projets de réponse
# ---------------------------------------------------------------------------
def _historiser(rep, action, detail=""):
    rep.setdefault("historique", []).append({
        "le": _maintenant(),
        "par": _qui(),
        "action": action,
        "detail": detail,
    })
    rep["maj_le"] = _maintenant()


def _qui():
    return utilisateur_courant()


def choisir_modele(nature, code_categorie="", correspondant_id="",
                   inclure_non_valides=False):
    """Le modèle le plus spécifique : correspondant > catégorie > générique."""
    meilleur, meilleur_score = None, -1
    for m in modeles():
        if m.get("nature") != nature:
            continue
        if m.get("statut") != "valide" and not inclure_non_valides:
            continue
        if m.get("correspondant_id"):
            if m["correspondant_id"] != correspondant_id:
                continue
            score = 3
        elif m.get("categorie") not in ("*", "", None):
            if m["categorie"] != code_categorie:
                continue
            score = 2
        else:
            score = 1
        if m.get("statut") == "valide":
            score += 0.5
        if score > meilleur_score:
            meilleur, meilleur_score = m, score
    return meilleur


def modeles_disponibles(inclure_non_valides=False):
    return [m for m in modeles()
            if m.get("statut") == "valide" or inclure_non_valides]


def variables_reponse(rep):
    """Valeurs des variables de fusion pour un projet."""
    fiche = correspondant(rep.get("correspondant_id")) or {}
    cat = categorie(fiche.get("categorie") or rep.get("categorie", "")) or {}
    sig = signataire(rep.get("signataire", "")) or {}
    appel = fiche.get("formule_appel") or "Madame, Monsieur"
    return {
        "civilite": fiche.get("civilite", ""),
        "formule_appel": appel,
        "organisation": fiche.get("organisation") or rep.get("correspondant_libre", ""),
        "ref_entrant": rep.get("ref_entrant", ""),
        "date_entrant": date_longue(rep.get("date_entrant", "")),
        "date_reception": date_longue(rep.get("date_reception", "")),
        "objet_entrant": rep.get("objet_entrant", ""),
        "objet": rep.get("objet", ""),
        "ref_sortant": rep.get("ref_sortant", ""),
        "signataire": sig.get("nom", ""),
        "fonction_signataire": sig.get("fonction", ""),
        "formule_politesse": cat.get("formule_politesse") or _POL_DISTINGUEE,
        "paragraphe_central": "",
        "date_jour": date_longue(datetime.now().date().isoformat()),
    }


def fusionner(corps, valeurs):
    """Remplace les {{variables}}. Une valeur absente devient un
    [à compléter : …] visible, jamais une invention.

    Exception : la référence et la date du courrier reçu sont des
    compléments facultatifs de la phrase d'accroche. Absentes, on retire
    le complément (« votre courrier, reçu le … ») plutôt que de bloquer la
    soumission sur une référence que l'expéditeur n'a souvent pas donnée."""
    texte = corps or ""
    if not valeurs.get("ref_entrant"):
        texte = re.sub(r"\s+référencé\s+\{\{\s*ref_entrant\s*\}\}", "", texte)
    if not valeurs.get("date_entrant"):
        texte = re.sub(r"\s+du\s+\{\{\s*date_entrant\s*\}\}", "", texte)

    def remplacer(m):
        nom = m.group(1).strip()
        val = valeurs.get(nom, "")
        if val:
            return str(val)
        if nom == "paragraphe_central":
            return PC_VIDE
        return f"[à compléter : {LIBELLES_VARIABLES.get(nom, nom)}]"

    for _ in range(3):  # la formule de politesse contient {{formule_appel}}
        nouveau = re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", remplacer, texte)
        if nouveau == texte:
            break
        texte = nouveau
    return texte


def compter_a_completer(texte):
    return len(MOTIF_A_COMPLETER.findall(texte or ""))


def zones_a_completer(*textes):
    """Libellés des zones [à compléter : …] restantes, dans l'ordre."""
    zones = []
    for t in textes:
        for m in MOTIF_A_COMPLETER.finditer(t or ""):
            lib = m.group(0)[1:-1].split(":", 1)
            zones.append(lib[1].strip() if len(lib) > 1 else "à compléter")
    return zones


def reappliquer_variables(rep):
    """Après une modification des champs (V/Réf., date, destinataire…),
    remplace dans le texte les zones qui attendaient ces valeurs.
    Sans cela, remplir le champ « V/Réf. » laissait la zone
    « [à compléter : référence du courrier reçu] » dans le corps, et la
    soumission restait bloquée sans que l'on comprenne pourquoi."""
    valeurs = variables_reponse(rep)
    n = 0
    # Projets créés avant ce correctif : la référence ou la date du courrier
    # reçu absente laissait une zone au milieu de la phrase d'accroche.
    for nom, prefixe in (("ref_entrant", "référencé"), ("date_entrant", "du")):
        if not valeurs.get(nom):
            zone = re.escape(f"[à compléter : {LIBELLES_VARIABLES[nom]}]")
            avant = rep.get("corps") or ""
            rep["corps"] = re.sub(rf"\s+{prefixe}\s+{zone}", "", avant)
            n += avant != rep["corps"]
    for nom, libelle in LIBELLES_VARIABLES.items():
        val = valeurs.get(nom, "")
        if not val or nom == "paragraphe_central":
            continue
        zone = f"[à compléter : {libelle}]"
        for champ in ("corps", "objet"):
            if zone in (rep.get(champ) or ""):
                n += rep[champ].count(zone)
                rep[champ] = rep[champ].replace(zone, str(val))
    return n


# « Réf. : BM/DG/2026-047 », « N/Réf : 123/DG », « Référence : AB-12 »
_MOTIF_REF = re.compile(
    r"(?:N/|V/|Notre\s+)?R[ée]f(?:[ée]rence)?\.?\s*(?:n[°o]\s*)?[:：]\s*"
    r"([A-Z0-9][A-Z0-9/\-_.]{2,40}[A-Z0-9])", re.IGNORECASE)


def extraire_reference(texte):
    """Référence de l'expéditeur lue dans le texte du courrier reçu, ou ""."""
    for m in _MOTIF_REF.finditer((texte or "")[:1500]):
        ref = m.group(1).strip(" .")
        if any(c.isdigit() for c in ref):
            return ref
    return ""


def creer_reponse(champs, entrant_id=""):
    """Crée un brouillon et le remplit depuis le modèle choisi."""
    assurer_banque()
    p = parametres()
    fiche = correspondant(champs.get("correspondant_id")) or {}
    code_cat = fiche.get("categorie") or champs.get("categorie", "")
    cat = categorie(code_cat) or {}

    rep = {
        "ref": _id_court("REP"),
        "cree_le": _maintenant(),
        "cree_par": _qui(),
        "maj_le": _maintenant(),
        "entrant_id": entrant_id,
        "correspondant_id": fiche.get("id", ""),
        "correspondant_libre": champs.get("correspondant_libre", "").strip(),
        "categorie": code_cat,
        "nature": champs.get("nature", "AR"),
        "modele_id": champs.get("modele_id", ""),
        "service": champs.get("service") or p.get("service_defaut", "DGA"),
        "objet": champs.get("objet", "").strip(),
        "ref_entrant": champs.get("ref_entrant", "").strip(),
        "date_entrant": champs.get("date_entrant", ""),
        "date_reception": champs.get("date_reception", ""),
        "objet_entrant": champs.get("objet_entrant", "").strip(),
        "consignes": champs.get("consignes", "").strip(),
        "signataire": (champs.get("signataire") or fiche.get("signataire")
                       or cat.get("signataire") or "DG"),
        "copies": champs.get("copies", fiche.get("copies", "")).strip(),
        "corps": "",
        "statut": "brouillon",
        "visa_ref": "",
        "numero": "",
        "ref_sortant": "",
        "date_courrier": "",
        "ia": {"utilisee": False},
        "historique": [],
    }

    m = modele(rep["modele_id"]) if rep["modele_id"] else None
    if m is None:
        m = choisir_modele(rep["nature"], code_cat, rep["correspondant_id"],
                           inclure_non_valides=est_responsable_ged())
    if m:
        rep["modele_id"] = m["id"]
        rep["corps"] = fusionner(m["corps"], variables_reponse(rep))
    else:
        rep["corps"] = fusionner(MODELES_DEFAUT[-1][2], variables_reponse(rep))

    _historiser(rep, "Projet créé",
                f"Modèle : {m['nom']}" if m else "Sans modèle (courrier libre)")

    with _verrou:
        reps = reponses()
        reps[rep["ref"]] = rep
        _ecrire(F_REPONSES, reps)
        if entrant_id:
            ents = entrants()
            e = ents.get(entrant_id)
            if e:
                e["statut_reponse"] = "en_cours"
                e["reponse_ref"] = rep["ref"]
                if not e.get("correspondant_id") and rep["correspondant_id"]:
                    e["correspondant_id"] = rep["correspondant_id"]
                _ecrire(F_ENTRANTS, ents)
    return rep


_CORRESPONDANCE_VISA = {
    "a_viser": "a_valider",
    "attente": "a_valider",
    "retourne": "retourne",
    "approuve": "en_signature",
    "vise": "signe",
}


def synchroniser():
    """Aligne le statut des projets sur le circuit de visa."""
    visas = _visas()
    with _verrou:
        reps = reponses()
        change = False
        for rep in reps.values():
            if rep.get("statut") in ("brouillon", "archive", "abandonne"):
                continue
            d = visas.get(rep.get("visa_ref", ""))
            if not d:
                continue
            nouveau = _CORRESPONDANCE_VISA.get(d.get("statut"), rep["statut"])
            if nouveau != rep["statut"]:
                rep["statut"] = nouveau
                detail = d.get("observations", "") if nouveau == "retourne" else ""
                if nouveau == "retourne":
                    rep["motif_retour"] = detail
                _historiser(rep, STATUTS.get(nouveau, nouveau), detail)
                change = True
        if change:
            _ecrire(F_REPONSES, reps)
    return reps


def type_document(rep):
    """Type GED du courrier produit : DEC pour une décision, CSOR sinon."""
    return "DEC" if mise_en_page(rep) == "decision" else "CSOR"


def _reserver_numero(rep):
    """Numéro d'ordre CSOR : suite du registre, en tenant compte des numéros
    déjà réservés par d'autres projets. La responsable GED peut toujours le
    modifier à l'archivage."""
    if rep.get("numero"):
        return rep["numero"]
    service = rep.get("service", "DGA")
    type_doc = type_document(rep)
    dernier = 0
    fn = _CTX.get("dernier_numero")
    if fn:
        try:
            dernier = fn(service, type_doc) or 0
        except Exception:  # noqa: BLE001
            dernier = 0
    annee = datetime.now().strftime("%Y")
    for autre in reponses().values():
        if (autre.get("service") == service and autre.get("numero")
                and type_document(autre) == type_doc
                and str(autre.get("date_courrier", "")).startswith(annee)):
            try:
                dernier = max(dernier, int(autre["numero"]))
            except ValueError:
                pass
    rep["numero"] = f"{dernier + 1:03d}"
    return rep["numero"]


def _ref_sortant(rep):
    date = (rep.get("date_courrier") or datetime.now().date().isoformat())
    return (f"HM26-{rep.get('service', 'DGA')}-{type_document(rep)}_"
            f"{date.replace('-', '')}_{rep.get('numero', '000')}")


# ---------------------------------------------------------------------------
# PDF sur papier à en-tête Huri Money
# ---------------------------------------------------------------------------
# Reproduit les modèles officiels transmis par la Direction (octobre 2026) :
#   - Modele_Standard_Lettre_Huri_Money.docx  -> mise en page « lettre »
#   - Modele_Decision_Vierge.docx             -> mise en page « decision »
# Papier commun : logo WAKATI en haut à gauche, logo Huri Money en haut à
# droite, grand W WAKATI en filigrane au centre, pied de page avec l'icône
# Huri Money et l'adresse. Les images sont dans le dossier entete/.
#
# Mesures relevées dans les fichiers Word (A4, marges 21,2 mm à gauche et à
# droite, 24,7 mm en haut et en bas, en-tête à 12,5 mm du bord).
DOSSIER_ENTETE = RACINE / "entete"

NAVY = (11, 31, 58)        # #0B1F3A — titres de la décision
ROUGE = (140, 22, 33)      # #8C1621 — numéros d'article
GRIS = (102, 107, 114)     # #666B72 — mentions et pied de page
TEXTE = (34, 34, 34)       # #222222
FILET = (200, 203, 208)

MARGE_G = 21.2
MARGE_D = 21.2
LARGEUR_UTILE = 210 - MARGE_G - MARGE_D

# (regulier, gras, italique, gras italique) par famille — Liberation a les métriques de
# Times New Roman et d'Arial, les polices des modèles Word.
_POLICES = {
    "serif": [
        ("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSerif-BoldItalic.ttf"),
        (r"C:\Windows\Fonts\times.ttf", r"C:\Windows\Fonts\timesbd.ttf",
         r"C:\Windows\Fonts\timesi.ttf", r"C:\Windows\Fonts\timesbi.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf"),
    ],
    "sans": [
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf"),
        (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf",
         r"C:\Windows\Fonts\ariali.ttf", r"C:\Windows\Fonts\arialbi.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf"),
    ],
}


def _police(famille):
    for jeu in _POLICES[famille]:
        if all(Path(f).exists() for f in jeu):
            return jeu
    return None


def _latin1(texte):
    """Repli sans police TrueType : les polices de base ne couvrent que le
    latin-1."""
    remplacements = {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-",
                     "—": "-", "…": "...", "œ": "oe", "Œ": "OE", "€": "EUR",
                     "·": "-", "\u00a0": " ", "\u202f": " "}
    for a, b in remplacements.items():
        texte = texte.replace(a, b)
    return texte.encode("latin-1", "replace").decode("latin-1")


def _sans_article(fonction):
    """« Le Directeur Général » -> « Directeur Général » (sous la signature)."""
    return re.sub(r"^(le|la|les|l')\s*", "", (fonction or "").strip(),
                  flags=re.I).strip()


def mise_en_page(rep):
    """« decision » pour une décision du DG, « lettre » sinon."""
    m = modele(rep.get("modele_id", "")) or {}
    if m.get("mise_en_page"):
        return m["mise_en_page"]
    return "decision" if rep.get("nature") == "DEC" else "lettre"


def reference_imprimee(rep):
    """Référence telle qu'elle figure sur le courrier, au format Huri Money
    (paramétrable). Exemple : N°2026-014/DG/HM."""
    if not rep.get("numero"):
        return ""
    p = parametres()
    cle = "format_reference_decision" if mise_en_page(rep) == "decision" \
        else "format_reference"
    gabarit = p.get(cle) or PARAMETRES_DEFAUT[cle]
    date = rep.get("date_courrier") or datetime.now().date().isoformat()
    try:
        return gabarit.format(annee=date[:4], numero=rep.get("numero", ""),
                              signataire=rep.get("signataire", "DG"),
                              service=rep.get("service", ""))
    except (KeyError, IndexError, ValueError):
        return f"N°{date[:4]}-{rep.get('numero', '')}/{rep.get('signataire', 'DG')}/HM"


def _papier(famille_police, filigrane_projet):
    """Classe FPDF portant le papier à en-tête commun aux deux mises en page."""
    from fpdf import FPDF  # noqa: PLC0415

    polices = _police(famille_police)
    nom_police = "Corps" if polices else ("Times" if famille_police == "serif"
                                          else "Helvetica")
    t = (lambda s: s or "") if polices else (lambda s: _latin1(s or ""))
    pied = parametres().get("pied_de_page") or PARAMETRES_DEFAUT["pied_de_page"]

    def image(nom):
        chemin = DOSSIER_ENTETE / nom
        return str(chemin) if chemin.exists() else None

    class Papier(FPDF):
        police = nom_police

        def header(self):
            # Filigrane WAKATI centré sur la page, derrière le texte. L'image
            # porte déjà sa transparence (comme dans le modèle Word).
            fil = image("filigrane.png")
            if fil:
                self.image(fil, x=(210 - 85.5) / 2, y=(297 - 85.5) / 2, w=85.5)
            if filigrane_projet:
                with self.local_context(fill_opacity=0.10, text_color=(140, 22, 33)):
                    self.set_font(nom_police, "B", 80)
                    with self.rotation(35, 105, 150):
                        self.text(45, 180, "PROJET")
            logo = image("logo_wakati.png")
            if logo:
                self.image(logo, x=MARGE_G + 2.8, y=15.1, w=11.1)
            hm = image("logo_hurimoney.png")
            if hm:
                self.image(hm, x=210 - MARGE_D - 45.5, y=17.8, w=45.5)
            self.set_text_color(*TEXTE)
            self.set_y(34)

        def footer(self):
            icone = image("icone_hurimoney.png")
            if icone:
                self.image(icone, x=(210 - 9.5) / 2, y=297 - 23.5, w=9.5)
            self.set_y(-12.5)
            self.set_font(nom_police, "", 7.5 if famille_police == "sans" else 9)
            self.set_text_color(*GRIS)
            self.cell(0, 4, t(pied), align="C")
            self.set_text_color(*TEXTE)

    pdf = Papier(format="A4")
    if polices:
        pdf.add_font("Corps", "", polices[0])
        pdf.add_font("Corps", "B", polices[1])
        pdf.add_font("Corps", "I", polices[2])
        pdf.add_font("Corps", "BI", polices[3])
    pdf.set_margins(MARGE_G, 34, MARGE_D)
    pdf.set_auto_page_break(True, margin=30)
    return pdf, t


def generer_pdf(rep, chemin, filigrane=False):
    """Génère le courrier sur papier à en-tête Huri Money."""
    if mise_en_page(rep) == "decision":
        pdf = _pdf_decision(rep, filigrane)
    else:
        pdf = _pdf_lettre(rep, filigrane)
    Path(chemin).parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(chemin))
    return chemin


def _paragraphes(texte):
    return [p.strip() for p in re.split(r"\n\s*\n", (texte or "").strip()) if p.strip()]


def _pdf_lettre(rep, filigrane):
    """Modèle standard de lettre : Times 11, blocs alignés à gauche,
    signature à droite."""
    pdf, t = _papier("serif", filigrane)
    pdf.add_page()
    f = pdf.police
    fiche = correspondant(rep.get("correspondant_id")) or {}
    sig = signataire(rep.get("signataire", "")) or {}
    L = 5.2  # interligne Times 11

    # Fonction de l'émetteur
    pdf.set_font(f, "B", 11)
    pdf.multi_cell(0, L, t(sig.get("fonction") or "[à compléter : signataire]"),
                   new_x="LMARGIN", new_y="NEXT")
    pdf.ln(7)

    # Lieu et date
    pdf.set_font(f, "", 11)
    date_c = rep.get("date_courrier") or datetime.now().date().isoformat()
    pdf.cell(0, L, t(f"Moroni, le {date_longue(date_c)}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(7)

    # Destinataire
    nom = fiche.get("civilite") or fiche.get("nom") or ""
    lignes = []
    if fiche.get("fonction") and not fiche.get("civilite"):
        lignes.append(fiche["fonction"])
    org = fiche.get("organisation") or rep.get("correspondant_libre", "")
    if org and org != nom:
        lignes.append(org)
    lignes += [x for x in (fiche.get("adresse") or "").splitlines() if x.strip()]
    if not nom and lignes:
        nom, lignes = lignes[0], lignes[1:]
    pdf.cell(0, L, t("À l'attention de"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(f, "B", 11)
    pdf.multi_cell(0, L, t(nom or "[à compléter : destinataire]"),
                   new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(f, "", 11)
    for ligne in lignes:
        pdf.multi_cell(0, L, t(ligne), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # Références
    ref = reference_imprimee(rep) or "N°…… (attribué à la validation)"
    pdf.set_font(f, "U", 11)
    pdf.write(L, t("Réf. :"))
    pdf.set_font(f, "", 11)
    pdf.write(L, t(f" {ref}"))
    pdf.ln(L)
    if rep.get("ref_entrant"):
        vref = rep["ref_entrant"]
        if rep.get("date_entrant"):
            vref += f" du {date_courte(rep['date_entrant'])}"
        pdf.set_font(f, "U", 11)
        pdf.write(L, t("V/Réf. :"))
        pdf.set_font(f, "", 11)
        pdf.write(L, t(f" {vref}"))
        pdf.ln(L)
    pdf.ln(5)

    # Objet
    pdf.set_font(f, "BU", 11)
    pdf.write(L, t("Objet :"))
    pdf.set_font(f, "B", 11)
    pdf.write(L, t(f" {rep.get('objet') or '[à compléter : objet]'}"))
    pdf.ln(L)
    pdf.ln(5)

    # Corps
    pdf.set_font(f, "", 11)
    for paragraphe in _paragraphes(rep.get("corps")):
        pdf.multi_cell(0, L, t(paragraphe), align="J", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(5)

    # Signature : nom en gras, espace de signature, fonction
    if pdf.get_y() > 220:
        pdf.add_page()
    pdf.ln(10)
    pdf.set_font(f, "B", 11)
    pdf.cell(0, L, t(sig.get("nom") or ""), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(28)
    pdf.set_font(f, "", 11)
    pdf.cell(0, L, t(_sans_article(sig.get("fonction")) or ""), align="R",
             new_x="LMARGIN", new_y="NEXT")

    # Copies
    copies = [c.strip() for c in re.split(r"[;\n]", rep.get("copies") or "") if c.strip()]
    if copies:
        pdf.ln(8)
        pdf.set_font(f, "U", 10)
        pdf.cell(0, 5, t("Copie :"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(f, "", 10)
        for c in copies:
            pdf.cell(0, 4.8, t(f"- {c}"), new_x="LMARGIN", new_y="NEXT")
    return pdf


_MOTIF_ARTICLE = re.compile(r"^\s*article\s+(\S+)\s*[·:.\-–—]?\s*(.*)$", re.I)
_MOTIF_DECIDE = re.compile(r"^\s*d\s*[ée]\s*c\s*i\s*d\s*e\s*:?\s*$", re.I)


def _blocs_decision(corps):
    """Découpe le corps d'une décision en blocs typés.

    Conventions de saisie (dans la Banque de courriers) :
      Vu …                  une ligne par visa
      DÉCIDE                le bandeau
      Article 1 · Titre     un article
      - texte               une puce
      | a | b | c |         une ligne de tableau (la première = en-têtes)
      autre ligne           un paragraphe
    """
    blocs, tableau = [], None
    for ligne in (corps or "").splitlines():
        brut = ligne.strip()
        if brut.startswith("|"):
            cellules = [c.strip() for c in brut.strip("|").split("|")]
            if all(re.fullmatch(r"-{2,}", c) for c in cellules if c):
                continue  # ligne de séparation façon Markdown
            if tableau is None:
                tableau = []
                blocs.append(("tableau", tableau))
            tableau.append(cellules)
            continue
        tableau = None
        if not brut:
            continue
        if _MOTIF_DECIDE.match(brut):
            blocs.append(("decide", ""))
        elif re.match(r"^vu\b", brut, re.I):
            blocs.append(("vu", re.sub(r"^vu\s*", "", brut, flags=re.I)))
        elif _MOTIF_ARTICLE.match(brut):
            num, titre = _MOTIF_ARTICLE.match(brut).groups()
            blocs.append(("article", (num, titre)))
        elif brut.startswith(("- ", "• ", "* ")):
            blocs.append(("puce", brut[2:].strip()))
        else:
            blocs.append(("texte", brut))
    return blocs


def _pdf_decision(rep, filigrane):
    """Modèle de décision du Directeur Général : Arial, cartouche de titre,
    visas, bandeau DÉCIDE, articles numérotés."""
    pdf, t = _papier("sans", filigrane)
    pdf.add_page()
    f = pdf.police
    sig = signataire(rep.get("signataire", "")) or {}
    fonction = _sans_article(sig.get("fonction")) or "Directeur Général"

    # Filet sous les logos
    pdf.set_draw_color(*NAVY)
    pdf.set_line_width(0.5)
    pdf.line(MARGE_G, 33, 210 - MARGE_D, 33)
    pdf.set_y(38)

    # Cartouche : nature, objet, référence, date. Le fond est tracé d'abord,
    # à la hauteur mesurée du titre, pour rester sous le texte.
    y0 = pdf.get_y()
    pdf.set_font(f, "B", 14)
    nb_lignes = len(pdf.multi_cell(LARGEUR_UTILE - 10, 7,
                                   t(rep.get("objet") or "[à compléter : objet]"),
                                   dry_run=True, output="LINES"))
    hauteur = 5 + 7 * nb_lignes + 1 + 2 * 4.5 + 6
    pdf.set_fill_color(246, 247, 249)
    pdf.set_draw_color(*FILET)
    pdf.set_line_width(0.2)
    pdf.rect(MARGE_G, y0 - 3, LARGEUR_UTILE, hauteur, "DF")
    pdf.set_y(y0)
    pdf.set_x(MARGE_G + 5)
    pdf.set_font(f, "", 8.5)
    pdf.set_text_color(*GRIS)
    pdf.cell(0, 5, t(f"Décision du {fonction}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(MARGE_G + 5)
    pdf.set_font(f, "B", 14)
    pdf.set_text_color(*NAVY)
    pdf.multi_cell(LARGEUR_UTILE - 10, 7, t(rep.get("objet") or "[à compléter : objet]"),
                   new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    for libelle, valeur in (("Référence", reference_imprimee(rep) or "attribuée à la validation"),
                            ("Date", date_longue(rep.get("date_courrier")
                                                 or datetime.now().date().isoformat()))):
        pdf.set_x(MARGE_G + 5)
        pdf.set_font(f, "", 8.5)
        pdf.set_text_color(*GRIS)
        pdf.write(4.5, t(f"{libelle}  "))
        pdf.set_font(f, "B", 8.5)
        pdf.set_text_color(*NAVY)
        pdf.write(4.5, t(valeur))
        pdf.ln(4.5)
    pdf.set_y(y0 - 3 + hauteur + 6)

    # « Le Directeur Général, »
    pdf.set_font(f, "B", 11.5)
    pdf.set_text_color(*NAVY)
    titre = (sig.get("fonction") or "").strip()
    if not re.match(r"(?i)^(le|la|l')\b", titre):
        titre = f"Le {fonction}"
    pdf.cell(0, 6, t(f"{titre},"), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_text_color(*TEXTE)

    retrait = 8
    for genre, contenu in _blocs_decision(rep.get("corps")):
        if genre == "vu":
            pdf.set_x(MARGE_G + retrait)
            pdf.set_font(f, "BI", 9.5)
            pdf.set_text_color(*NAVY)
            pdf.write(4.8, "Vu ")
            pdf.set_font(f, "", 9.5)
            pdf.set_text_color(*TEXTE)
            pdf.set_left_margin(MARGE_G + retrait)
            pdf.write(4.8, t(contenu))
            pdf.set_left_margin(MARGE_G)
            pdf.ln(4.8)
            pdf.ln(1.8)
        elif genre == "decide":
            pdf.ln(4)
            pdf.set_fill_color(*NAVY)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font(f, "B", 11.5)
            pdf.cell(0, 9, t("D É C I D E"), align="C", fill=True,
                     new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(*TEXTE)
            pdf.ln(3)
        elif genre == "article":
            num, titre = contenu
            if pdf.get_y() > 250:
                pdf.add_page()
            pdf.ln(3.5)
            pdf.set_font(f, "B", 10)
            pdf.set_text_color(*ROUGE)
            pdf.write(5.5, t(f"Article {num}"))
            pdf.set_text_color(*GRIS)
            pdf.write(5.5, "   ·   ")
            pdf.set_text_color(*NAVY)
            pdf.write(5.5, t(titre))
            pdf.ln(6)
            pdf.set_draw_color(*FILET)
            pdf.set_line_width(0.2)
            pdf.line(MARGE_G, pdf.get_y(), 210 - MARGE_D, pdf.get_y())
            pdf.ln(1.5)
            pdf.set_text_color(*TEXTE)
        elif genre == "puce":
            pdf.set_font(f, "", 9.5)
            pdf.set_x(MARGE_G + 6)
            pdf.cell(5, 4.8, t("•"))
            pdf.multi_cell(LARGEUR_UTILE - 11, 4.8, t(contenu), align="J",
                           new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1.2)
        elif genre == "tableau":
            pdf.set_font(f, "", 9)
            pdf.set_draw_color(*FILET)
            # Le bandeau DÉCIDE laisse le remplissage en navy : sans cette
            # remise à zéro, les lignes du tableau seraient navy sur navy.
            pdf.set_fill_color(255, 255, 255)
            pdf.set_text_color(*TEXTE)
            nb = max(len(r) for r in contenu)
            lignes = [r + [""] * (nb - len(r)) for r in contenu]
            with pdf.table(
                    width=LARGEUR_UTILE, line_height=5.5, text_align="LEFT",
                    headings_style=_style_entete_tableau(f),
                    borders_layout="HORIZONTAL_LINES",
                    cell_fill_mode="NONE") as tab:
                for r in lignes:
                    ligne = tab.row()
                    for c in r:
                        ligne.cell(t(c))
            pdf.ln(3)
        else:
            pdf.set_font(f, "", 9.5)
            pdf.multi_cell(0, 4.8, t(contenu), align="J", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2.5)

    # Copies
    copies = [c.strip() for c in re.split(r"[;\n]", rep.get("copies") or "") if c.strip()]
    if pdf.get_y() > 225:
        pdf.add_page()
    pdf.ln(8)
    pdf.set_draw_color(*FILET)
    pdf.line(MARGE_G, pdf.get_y(), 210 - MARGE_D, pdf.get_y())
    pdf.ln(3)
    if copies:
        pdf.set_font(f, "I", 8)
        pdf.set_text_color(*GRIS)
        pdf.multi_cell(0, 4.2, t("Copies : " + "  —  ".join(copies)),
                       new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # Signature
    pdf.set_font(f, "B", 10.5)
    pdf.set_text_color(*NAVY)
    pdf.cell(0, 5.5, t(sig.get("nom") or ""), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(26)
    pdf.set_font(f, "I", 8.5)
    pdf.set_text_color(*GRIS)
    pdf.cell(0, 5, t(f"{fonction}, Huri Money S.A."), align="R",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*TEXTE)
    return pdf


def _style_entete_tableau(police):
    from fpdf.fonts import FontFace  # noqa: PLC0415
    return FontFace(family=police, emphasis="BOLD", color=(255, 255, 255),
                    fill_color=NAVY)


# ---------------------------------------------------------------------------
# Assistance IA
# ---------------------------------------------------------------------------
def _cle_ia():
    if F_SECRET_IA.exists():
        try:
            cle = json.loads(F_SECRET_IA.read_text(encoding="utf-8")).get("cle", "")
            if cle:
                return cle
        except Exception:  # noqa: BLE001
            pass
    try:
        import notifier_config  # noqa: PLC0415
        cle = getattr(notifier_config, "IA_API_KEY", "")
        if cle:
            return cle
    except ImportError:
        pass
    return os.getenv("IA_API_KEY") or os.getenv("GROQ_API_KEY") or ""


def enregistrer_cle_ia(cle):
    with _verrou:
        DONNEES.mkdir(parents=True, exist_ok=True)
        if cle:
            F_SECRET_IA.write_text(json.dumps({"cle": cle}), encoding="utf-8")
            try:
                os.chmod(F_SECRET_IA, 0o600)
            except Exception:  # noqa: BLE001
                pass
        else:
            F_SECRET_IA.unlink(missing_ok=True)


def cle_ia_masquee():
    cle = _cle_ia()
    if not cle:
        return ""
    return f"…{cle[-4:]}"


def ia_autorisee(rep):
    """(autorisée, raison). Le refus est toujours expliqué à l'agent."""
    ia = parametres()["ia"]
    if not ia.get("actif"):
        return False, "Assistance IA désactivée dans les paramètres."
    if not _cle_ia():
        return False, "Aucune clé d'API configurée (Paramètres du courrier)."
    if not ia.get("modele"):
        return False, "Aucun modèle renseigné dans les paramètres."
    fiche = correspondant(rep.get("correspondant_id")) or {}
    code = fiche.get("categorie") or rep.get("categorie", "")
    cat = categorie(code) or {}
    if cat and not cat.get("ia_autorisee", True):
        return False, (f"IA non autorisée pour la catégorie « {cat.get('nom')} » "
                       "(décision de la Direction, réglable dans les paramètres).")
    confidentiel = fiche.get("confidentiel") or cat.get("confidentiel")
    if confidentiel and not ia.get("autoriser_confidentiels"):
        return False, ("Correspondant marqué confidentiel : l'IA est désactivée "
                       "pour ce courrier.")
    return True, ""


_MASQUES = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[courriel]"),
    (re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b"), "[compte]"),
    (re.compile(r"(?:\+|00)\s?269[\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}"), "[téléphone]"),
    (re.compile(r"\b\d{3}[\s.-]\d{2}[\s.-]\d{2}\b"), "[téléphone]"),
    (re.compile(r"\b\d{10,}\b"), "[numéro]"),
]


def anonymiser(texte):
    for motif, rempl in _MASQUES:
        texte = motif.sub(rempl, texte)
    return texte


_SYSTEME = """Tu rédiges pour Huri Money SA (WAKATI Mobile Money, Union des Comores) le PARAGRAPHE CENTRAL d'un courrier administratif en français.

Règles absolues :
- Écris uniquement le paragraphe central (1 à 3 paragraphes courts) : pas d'en-tête, pas de formule d'appel, pas de formule de politesse, pas de signature, pas de titre.
- Réponds aux points soulevés dans le courrier reçu, en suivant les consignes de l'agent.
- N'invente JAMAIS un montant, une date, un délai, un nom, une référence ou un engagement. Toute information nécessaire mais absente s'écrit exactement [à compléter : description courte].
- Ne promets rien que les consignes ne prévoient pas.
- Respecte le registre indiqué. Pas de Markdown, pas de listes à puces sauf si la nature du courrier l'exige (pièces demandées, documents transmis) : utilise alors des tirets simples.
- Texte brut uniquement."""


def _nettoyer_sortie(texte, valeurs):
    texte = (texte or "").strip()
    texte = re.sub(r"^\s*#+\s*", "", texte, flags=re.M)
    texte = texte.replace("**", "").replace("__", "")
    texte = re.sub(r"^\s*[•*]\s+", "- ", texte, flags=re.M)
    texte = re.sub(r"^(paragraphe central|réponse)\s*:\s*", "", texte, flags=re.I)
    # Formule d'appel ou de politesse ajoutées malgré la consigne
    appel = (valeurs.get("formule_appel") or "").strip()
    lignes = texte.splitlines()
    if lignes and (
            (appel and _normaliser(lignes[0]).startswith(_normaliser(appel)))
            or re.match(r"^\s*(madame|monsieur|mesdames|messieurs|cher|chère)\b.{0,80},\s*$",
                        lignes[0], re.I)):
        lignes = lignes[1:]
    while lignes and re.search(r"(veuillez agréer|nous vous prions d'agréer|"
                               r"cordialement|salutations)", lignes[-1], re.I):
        lignes = lignes[:-1]
    return "\n".join(lignes).strip()


def _alertes(sortie, sources):
    """Chiffres et dates de la proposition absents des sources : à vérifier."""
    src = _normaliser(" ".join(sources))
    alertes = []
    for nombre in set(re.findall(r"\d[\d\s.,]*\d|\d", sortie)):
        n = _normaliser(nombre)
        if n and n not in src:
            alertes.append(f"« {nombre.strip()} » n'apparaît ni dans le courrier "
                           "reçu ni dans vos consignes : à vérifier.")
    for mois in MOIS:
        if re.search(rf"\b{mois}\b", sortie, re.I) and mois not in src:
            alertes.append(f"Date en « {mois} » absente des sources : à vérifier.")
    return alertes[:8]


def appeler_ia(messages, ia=None, cle=None):
    """Appel compatible OpenAI. Retourne (ok, texte_ou_erreur)."""
    ia = ia or parametres()["ia"]
    cle = cle or _cle_ia()
    base = (ia.get("base_url") or FOURNISSEURS_IA.get(ia.get("fournisseur"), {})
            .get("url", "")).rstrip("/")
    if not base:
        return False, "URL du fournisseur non renseignée."
    corps = {
        "model": ia.get("modele"),
        "messages": messages,
        "temperature": float(ia.get("temperature", 0.3)),
        "max_tokens": int(ia.get("max_tokens", 3000)),
    }
    if "gpt-oss" in str(ia.get("modele")) and ia.get("effort_raisonnement"):
        corps["reasoning_effort"] = ia["effort_raisonnement"]

    def envoyer(payload):
        return requests.post(f"{base}/chat/completions",
                             headers={"Authorization": f"Bearer {cle}",
                                      "Content-Type": "application/json"},
                             json=payload, timeout=int(ia.get("delai_s", 60)))

    try:
        resp = envoyer(corps)
        if resp.status_code == 400 and "reasoning" in resp.text.lower():
            corps.pop("reasoning_effort", None)
            resp = envoyer(corps)
    except requests.Timeout:
        return False, "Le fournisseur n'a pas répondu à temps. Réessayez."
    except Exception as exc:  # noqa: BLE001
        return False, f"Fournisseur injoignable : {exc}"

    if resp.status_code == 429:
        return False, "Quota du fournisseur atteint. Réessayez dans quelques minutes."
    if resp.status_code == 401:
        return False, "Clé d'API refusée par le fournisseur."
    if resp.status_code == 404 or "model_not_found" in resp.text \
            or "does not exist" in resp.text:
        return False, (f"Le modèle « {ia.get('modele')} » n'existe plus chez ce "
                       "fournisseur. Changez-le dans les paramètres.")
    if resp.status_code != 200:
        return False, f"Erreur du fournisseur (HTTP {resp.status_code}) : {resp.text[:200]}"

    try:
        message = resp.json()["choices"][0]["message"]
        texte = message.get("content") or ""
    except Exception:  # noqa: BLE001
        return False, "Réponse du fournisseur illisible."
    if not texte.strip():
        return False, ("Réponse vide : augmentez « jetons maximum » dans les "
                       "paramètres (le modèle a épuisé son budget en réflexion).")
    return True, texte


def rediger_paragraphe(rep, consignes_sup=""):
    ok, raison = ia_autorisee(rep)
    if not ok:
        return False, raison, []

    ia = parametres()["ia"]
    fiche = correspondant(rep.get("correspondant_id")) or {}
    cat = categorie(fiche.get("categorie") or rep.get("categorie", "")) or {}
    texte_recu = texte_entrant(rep.get("entrant_id", "")) if rep.get("entrant_id") else ""
    consignes = "\n".join(c for c in (rep.get("consignes", ""), consignes_sup) if c)

    if not texte_recu.strip() and not consignes.strip():
        return False, ("Rien à exploiter : le texte du courrier reçu n'est pas "
                       "disponible (scan illisible ?). Saisissez les points à "
                       "traiter dans « Consignes » puis relancez."), []

    recu = texte_recu[:7000]
    if ia.get("anonymiser", True):
        recu = anonymiser(recu)
        consignes_envoyees = anonymiser(consignes)
    else:
        consignes_envoyees = consignes

    utilisateur = (
        f"Catégorie du destinataire : {cat.get('nom', 'non précisée')}\n"
        f"Registre attendu : {cat.get('registre', 'formel')}\n"
        f"Nature de la réponse : {NATURES.get(rep.get('nature'), rep.get('nature'))}\n"
        f"Objet : {rep.get('objet') or rep.get('objet_entrant') or 'non précisé'}\n\n"
        f"Consignes de l'agent :\n{consignes_envoyees or '(aucune)'}\n\n"
        f"Texte du courrier reçu :\n{recu or '(non disponible)'}\n\n"
        "Rédige le paragraphe central."
    )
    ok, texte = appeler_ia([{"role": "system", "content": _SYSTEME},
                            {"role": "user", "content": utilisateur}], ia)
    if not ok:
        return False, texte, []

    sortie = _nettoyer_sortie(texte, variables_reponse(rep))
    alertes = _alertes(sortie, [texte_recu, consignes, rep.get("objet", ""),
                                rep.get("objet_entrant", "")])
    with _verrou:
        reps = reponses()
        r = reps.get(rep["ref"])
        if r:
            r["ia"] = {"utilisee": True, "le": _maintenant(),
                       "modele": ia.get("modele"),
                       "fournisseur": ia.get("fournisseur"),
                       "alertes": alertes}
            _historiser(r, "Paragraphe proposé par l'IA",
                        f"{ia.get('fournisseur')} / {ia.get('modele')}")
            _ecrire(F_REPONSES, reps)
    return True, sortie, alertes


# ---------------------------------------------------------------------------
# Notifications et relances
# ---------------------------------------------------------------------------
def _cliq(titre, lignes, url=""):
    notif = _CTX.get("notifier")
    if not notif:
        return
    try:
        notif({
            "titre": titre,
            "numero": lignes.get("Statut", ""),
            "nom_fichier": lignes.get("Objet", ""),
            "service": lignes.get("Service", "-"),
            "type_doc": lignes.get("Nature", "Courrier"),
            "deposant": lignes.get("Correspondant", "-"),
            "libelle_deposant": "Correspondant",
            "date": lignes.get("Échéance", lignes.get("Date", "-")),
            "workdrive_url": url,
        })
    except Exception:  # noqa: BLE001
        logging.error("Notification Cliq courrier impossible", exc_info=True)


def relancer_echeances():
    """Relance dans #ged-notifications les entrants dont l'échéance est
    dépassée sans réponse. Une relance par intervalle (24 h par défaut)."""
    try:
        p = parametres()
        intervalle = timedelta(hours=int(p.get("relance_intervalle_h", 24)))
        maintenant = datetime.now()
        a_relancer = []
        with _verrou:
            ents = entrants()
            for e in ents.values():
                if e.get("statut_reponse") not in ("a_preparer", "en_cours"):
                    continue
                if not e.get("echeance"):
                    continue
                try:
                    if datetime.fromisoformat(e["echeance"]) > maintenant:
                        continue
                    derniere = e.get("derniere_relance")
                    if derniere and datetime.fromisoformat(derniere) + intervalle > maintenant:
                        continue
                except ValueError:
                    continue
                e["derniere_relance"] = maintenant.isoformat(timespec="seconds")
                a_relancer.append(dict(e))
            if a_relancer:
                _ecrire(F_ENTRANTS, ents)
        for e in a_relancer:
            fiche = correspondant(e.get("correspondant_id")) or {}
            _cliq("⏰ Courrier sans réponse — échéance dépassée", {
                "Statut": STATUTS_ENTRANT.get(e["statut_reponse"], ""),
                "Objet": e.get("objet", ""),
                "Service": e.get("service", "-"),
                "Nature": "Courrier entrant",
                "Correspondant": fiche.get("organisation") or e.get("correspondant", "-"),
                "Échéance": date_courte(e.get("echeance")),
            }, e.get("lien", ""))
        return len(a_relancer)
    except Exception:  # noqa: BLE001
        logging.error("Relance des échéances impossible", exc_info=True)
        return 0


def compteurs():
    """Chiffres pour la barre latérale et le tableau de bord."""
    try:
        maintenant = datetime.now()
        ouverts = [e for e in entrants().values()
                   if e.get("statut_reponse") in ("a_preparer", "en_cours")]
        retard = 0
        for e in ouverts:
            try:
                if e.get("echeance") and datetime.fromisoformat(e["echeance"]) < maintenant:
                    retard += 1
            except ValueError:
                pass
        reps = reponses().values()
        return {
            "a_repondre": len(ouverts),
            "en_retard": retard,
            "brouillons": sum(1 for r in reps if r.get("statut") in ("brouillon", "retourne")),
            "a_valider": sum(1 for r in reps if r.get("statut") == "a_valider"),
            "en_signature": sum(1 for r in reps if r.get("statut") == "en_signature"),
        }
    except Exception:  # noqa: BLE001
        return {"a_repondre": 0, "en_retard": 0, "brouillons": 0,
                "a_valider": 0, "en_signature": 0}


# ---------------------------------------------------------------------------
# Présentation
# ---------------------------------------------------------------------------
def _entrant_affiche(e):
    fiche = correspondant(e.get("correspondant_id")) or {}
    try:
        retard = bool(e.get("echeance")) and \
            datetime.fromisoformat(e["echeance"]) < datetime.now()
        reste_h = ((datetime.fromisoformat(e["echeance"]) - datetime.now())
                   .total_seconds() / 3600) if e.get("echeance") else None
    except ValueError:
        retard, reste_h = False, None
    return {
        **e,
        "organisation": fiche.get("organisation") or e.get("correspondant") or "—",
        "categorie_nom": (categorie(fiche.get("categorie", "")) or {}).get("nom", ""),
        "echeance_affiche": date_courte(e.get("echeance")),
        "recu_affiche": date_courte(e.get("recu_le")),
        "retard": retard and e.get("statut_reponse") in ("a_preparer", "en_cours"),
        "reste_h": reste_h,
        "statut_lib": STATUTS_ENTRANT.get(e.get("statut_reponse"), ""),
        "urgence_lib": URGENCES.get(e.get("urgence"), ""),
    }


def _reponse_affichee(r):
    fiche = correspondant(r.get("correspondant_id")) or {}
    return {
        **r,
        "organisation": fiche.get("organisation") or r.get("correspondant_libre") or "—",
        "statut_lib": STATUTS.get(r.get("statut"), r.get("statut")),
        "nature_lib": NATURES.get(r.get("nature"), r.get("nature")),
        "maj_affiche": date_courte(r.get("maj_le")),
        "nb_a_completer": compter_a_completer(r.get("corps")),
    }


def _contexte_commun():
    return {
        "natures": NATURES,
        "mises_en_page": MISES_EN_PAGE,
        "statuts": STATUTS,
        "urgences": URGENCES,
        "categories": categories(),
        "signataires": parametres().get("signataires", []),
        "services": _CTX.get("services", {}),
        "responsable": est_responsable_ged(),
        "cpt": compteurs(),
    }


# ---------------------------------------------------------------------------
# Routes — liste et préparation
# ---------------------------------------------------------------------------
@bp_courrier.route("/")
def liste():
    assurer_banque()
    reps = synchroniser()
    ents = sorted((_entrant_affiche(e) for e in entrants().values()
                   if e.get("statut_reponse") in ("a_preparer", "en_cours")),
                  key=lambda e: e.get("echeance") or "9999")
    en_cours = sorted((_reponse_affichee(r) for r in reps.values()
                       if r.get("statut") not in ("archive", "abandonne")),
                      key=lambda r: r.get("maj_le", ""), reverse=True)
    recents = sorted((_reponse_affichee(r) for r in reps.values()
                      if r.get("statut") in ("archive", "abandonne")),
                     key=lambda r: r.get("maj_le", ""), reverse=True)[:10]
    clos = sorted((_entrant_affiche(e) for e in entrants().values()
                   if e.get("statut_reponse") == "sans_objet"),
                  key=lambda e: e.get("recu_le", ""), reverse=True)[:10]
    return render_template("courrier.html", page="courrier",
                           entrants=ents, en_cours=en_cours, recents=recents,
                           clos=clos, **_contexte_commun())


def _champs_preparation(entrant_id):
    """Valeurs initiales du formulaire de préparation."""
    p = parametres()
    champs = {"nature": "AR", "service": p.get("service_defaut", "DGA"),
              "date_reception": datetime.now().date().isoformat()}
    contexte = {"entrant": None, "texte": "", "observations": "", "suggestions": []}
    if not entrant_id:
        return champs, contexte

    e = entrants().get(entrant_id)
    if not e:
        return champs, contexte
    texte = texte_entrant(entrant_id)
    visa = _visas().get(e.get("visa_ref", ""), {})
    champs.update({
        "correspondant_id": e.get("correspondant_id", ""),
        "correspondant_libre": "" if e.get("correspondant_id") else e.get("correspondant", ""),
        "objet_entrant": e.get("objet", ""),
        "objet": f"Réponse à votre courrier — {e.get('objet', '')}".strip(" —"),
        "ref_entrant": extraire_reference(texte),
        "date_entrant": e.get("date_doc", ""),
        "date_reception": (e.get("recu_le") or "")[:10],
        "service": e.get("service") or champs["service"],
        "consignes": visa.get("observations", ""),
    })
    contexte.update({
        "entrant": _entrant_affiche(e),
        "texte": texte,
        "observations": visa.get("observations", ""),
        "suggestions": suggestions(e.get("correspondant", ""), texte),
    })
    return champs, contexte


@bp_courrier.route("/preparer", methods=["GET", "POST"])
def preparer():
    assurer_banque()
    entrant_id = request.values.get("entrant", "")
    if entrant_id and entrant_id not in entrants():
        if not entrant_depuis_visa(entrant_id):
            entrant_id = ""
    if request.method == "POST":
        champs = {k: request.form.get(k, "") for k in (
            "correspondant_id", "correspondant_libre", "categorie", "nature",
            "modele_id", "service", "objet", "ref_entrant", "date_entrant",
            "date_reception", "objet_entrant", "consignes", "signataire", "copies")}
        if (champs["nature"] != "DEC" and not champs["correspondant_id"]
                and not champs["correspondant_libre"].strip()):
            flash("Choisissez un destinataire dans l'annuaire ou saisissez-le.", "error")
            return redirect(url_for("courrier.preparer", entrant=entrant_id))
        rep = creer_reponse(champs, entrant_id)
        flash("Projet de réponse créé. Relisez-le, complétez les zones "
              "[à compléter], puis soumettez-le à validation.", "success")
        return redirect(url_for("courrier.edition", ref=rep["ref"]))

    champs, contexte = _champs_preparation(entrant_id)
    if entrant_id and contexte["entrant"] and contexte["entrant"].get("reponse_ref"):
        rep = reponses().get(contexte["entrant"]["reponse_ref"])
        if rep and rep.get("statut") not in ("abandonne",):
            flash("Une réponse est déjà en préparation pour ce courrier.", "success")
            return redirect(url_for("courrier.edition", ref=rep["ref"]))

    return render_template("courrier_preparer.html", page="courrier",
                           champs=champs, ctx=contexte, entrant_id=entrant_id,
                           annuaire=correspondants(),
                           modeles=modeles_disponibles(est_responsable_ged()),
                           **_contexte_commun())


# ---------------------------------------------------------------------------
# Routes — édition d'un projet
# ---------------------------------------------------------------------------
def _projet(ref):
    synchroniser()
    rep = reponses().get(ref)
    if not rep:
        abort(404)
    return rep


@bp_courrier.route("/<ref>")
def edition(ref):
    rep = _projet(ref)
    entrant = entrants().get(rep.get("entrant_id", "")) if rep.get("entrant_id") else None
    ia_ok, ia_raison = ia_autorisee(rep)
    return render_template(
        "courrier_edition.html", page="courrier",
        rep=_reponse_affichee(rep),
        decision=mise_en_page(rep) == "decision",
        entrant=_entrant_affiche(entrant) if entrant else None,
        texte=texte_entrant(rep.get("entrant_id", "")) if entrant else "",
        annuaire=correspondants(),
        modeles=modeles_disponibles(est_responsable_ged()),
        modifiable=rep.get("statut") in ("brouillon", "retourne"),
        ia_ok=ia_ok, ia_raison=ia_raison, ia_modele=parametres()["ia"].get("modele"),
        pc_vide=PC_VIDE,
        **_contexte_commun())


_CHAMPS_EDITABLES = ("objet", "ref_entrant", "date_entrant", "date_reception",
                     "objet_entrant", "consignes", "signataire", "copies",
                     "service", "corps", "correspondant_id", "correspondant_libre")


@bp_courrier.route("/<ref>/enregistrer", methods=["POST"])
def enregistrer(ref):
    with _verrou:
        reps = reponses()
        rep = reps.get(ref)
        if not rep:
            abort(404)
        if rep.get("statut") not in ("brouillon", "retourne"):
            flash("Ce projet n'est plus modifiable : il est dans le circuit de "
                  "validation.", "error")
            return redirect(url_for("courrier.edition", ref=ref))
        for champ in _CHAMPS_EDITABLES:
            if champ in request.form:
                rep[champ] = request.form.get(champ, "").replace("\r\n", "\n")
        fiche = correspondant(rep.get("correspondant_id"))
        if fiche:
            rep["categorie"] = fiche.get("categorie", rep.get("categorie", ""))
        remplies = reappliquer_variables(rep)
        _historiser(rep, "Modifié",
                    f"{remplies} zone(s) complétée(s) depuis les champs" if remplies else "")
        _ecrire(F_REPONSES, reps)

    action = request.form.get("action", "")
    if action == "soumettre":
        return soumettre(ref)
    flash("Projet enregistré.", "success")
    return redirect(url_for("courrier.edition", ref=ref))


@bp_courrier.route("/<ref>/regenerer", methods=["POST"])
def regenerer(ref):
    """Recharge le corps depuis le modèle (en écrasant les modifications)."""
    with _verrou:
        reps = reponses()
        rep = reps.get(ref)
        if not rep or rep.get("statut") not in ("brouillon", "retourne"):
            abort(404)
        mid = request.form.get("modele_id") or rep.get("modele_id")
        m = modele(mid)
        if not m:
            flash("Modèle introuvable.", "error")
            return redirect(url_for("courrier.edition", ref=ref))
        rep["modele_id"] = m["id"]
        rep["nature"] = m.get("nature", rep.get("nature"))
        rep["corps"] = fusionner(m["corps"], variables_reponse(rep))
        _historiser(rep, "Corps rechargé depuis le modèle", m["nom"])
        _ecrire(F_REPONSES, reps)
    flash("Corps du courrier rechargé depuis le modèle.", "success")
    return redirect(url_for("courrier.edition", ref=ref))


@bp_courrier.route("/<ref>/ia", methods=["POST"])
def ia(ref):
    rep = _projet(ref)
    if rep.get("statut") not in ("brouillon", "retourne"):
        return jsonify({"ok": False, "erreur": "Projet non modifiable."}), 400
    consignes = request.form.get("consignes", "")
    if consignes and consignes != rep.get("consignes"):
        with _verrou:
            reps = reponses()
            reps[ref]["consignes"] = consignes
            _ecrire(F_REPONSES, reps)
        rep["consignes"] = consignes
    ok, texte, alertes = rediger_paragraphe(rep)
    if not ok:
        return jsonify({"ok": False, "erreur": texte})
    return jsonify({"ok": True, "texte": texte, "alertes": alertes,
                    "pc_vide": PC_VIDE})


@bp_courrier.route("/<ref>/apercu.pdf")
def apercu(ref):
    rep = _projet(ref)
    chemin = DOSSIER_PROJETS / f"{ref}_apercu.pdf"
    apercu_rep = dict(rep)
    if not apercu_rep.get("ref_sortant"):
        apercu_rep["ref_sortant"] = "attribuée à la validation"
    generer_pdf(apercu_rep, chemin,
                filigrane=rep.get("statut") in ("brouillon", "retourne"))
    return send_file(str(chemin), mimetype="application/pdf",
                     download_name=f"{ref}.pdf")


@bp_courrier.route("/<ref>/soumettre", methods=["POST"])
def soumettre(ref):
    """Envoie le projet dans le circuit de validation (visa du signataire)."""
    rep = _projet(ref)
    if rep.get("statut") not in ("brouillon", "retourne"):
        flash("Ce projet est déjà dans le circuit de validation.", "error")
        return redirect(url_for("courrier.edition", ref=ref))

    zones = zones_a_completer(rep.get("objet"), rep.get("corps"))
    if zones:
        liste = " ; ".join(f"« {z} »" for z in zones[:6])
        if len(zones) > 6:
            liste += f" … (+{len(zones) - 6})"
        flash(f"Soumission impossible : il reste {len(zones)} zone(s) à compléter "
              f"dans le texte du courrier — {liste}. Cliquez sur chaque zone "
              "listée sous le texte pour la retrouver, remplacez-la ou supprimez-la.",
              "error")
        return redirect(url_for("courrier.edition", ref=ref))
    if not rep.get("objet", "").strip():
        flash("L'objet du courrier est obligatoire.", "error")
        return redirect(url_for("courrier.edition", ref=ref))

    try:
        import visa  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        flash("Le circuit de visa est indisponible.", "error")
        return redirect(url_for("courrier.edition", ref=ref))

    with _verrou:
        reps = reponses()
        rep = reps[ref]
        rep["date_courrier"] = datetime.now().date().isoformat()
        _reserver_numero(rep)
        rep["ref_sortant"] = _ref_sortant(rep)
        rep["ref_imprimee"] = reference_imprimee(rep)
        chemin = DOSSIER_PROJETS / f"{ref}.pdf"
        generer_pdf(rep, chemin, filigrane=False)

        fiche = correspondant(rep.get("correspondant_id")) or {}
        organisation = (fiche.get("organisation") or rep.get("correspondant_libre", "")
                        or ("Diffusion interne" if type_document(rep) == "DEC" else ""))
        premiere = not rep.get("visa_ref")
        try:
            if premiere:
                rep["visa_ref"] = visa.deposer_pour_visa(chemin, {
                    "service": rep.get("service", "DGA"),
                    "type_doc": type_document(rep),
                    "date_doc": rep["date_courrier"],
                    "objet": rep.get("objet", ""),
                    "sens": "sortant",
                    "correspondant": organisation,
                    "deposant": _qui(),
                    "signataire": rep.get("signataire", "DG"),
                    "reponse_ref": ref,
                })
            else:
                visa.resoumettre_document(rep["visa_ref"], chemin, {
                    "objet": rep.get("objet", ""),
                    "date_doc": rep["date_courrier"],
                    "signataire": rep.get("signataire", "DG"),
                })
        except Exception as exc:  # noqa: BLE001
            logging.error("Soumission du projet %s", ref, exc_info=True)
            flash(f"Soumission impossible : {exc}", "error")
            return redirect(url_for("courrier.edition", ref=ref))

        rep["statut"] = "a_valider"
        rep.pop("motif_retour", None)
        _historiser(rep, "Soumis à validation" if premiere else "Resoumis à validation",
                    rep.get("ref_imprimee") or rep["ref_sortant"])
        _ecrire(F_REPONSES, reps)

    sig = signataire(rep.get("signataire", "")) or {}
    _cliq("✉ Projet de réponse à valider", {
        "Statut": "À valider",
        "Objet": rep.get("objet", ""),
        "Service": _CTX["services"].get(rep.get("service"), rep.get("service", "-")),
        "Nature": NATURES.get(rep.get("nature"), "Courrier"),
        "Correspondant": organisation,
        "Date": sig.get("fonction", rep.get("signataire", "")),
    })
    flash(f"Projet transmis pour validation ({sig.get('fonction') or rep.get('signataire')}). "
          f"Référence : {rep.get('ref_imprimee') or rep['ref_sortant']}", "success")
    return redirect(url_for("courrier.edition", ref=ref))


@bp_courrier.route("/<ref>/abandonner", methods=["POST"])
def abandonner(ref):
    with _verrou:
        reps = reponses()
        rep = reps.get(ref)
        if not rep or rep.get("statut") not in ("brouillon", "retourne"):
            abort(404)
        rep["statut"] = "abandonne"
        _historiser(rep, "Abandonné", request.form.get("motif", ""))
        _ecrire(F_REPONSES, reps)
        ents = entrants()
        e = ents.get(rep.get("entrant_id", ""))
        if e and e.get("reponse_ref") == ref:
            e["statut_reponse"] = "a_preparer"
            e["reponse_ref"] = ""
            _ecrire(F_ENTRANTS, ents)
    flash("Projet abandonné.", "success")
    return redirect(url_for("courrier.liste"))


@bp_courrier.route("/entrant/<eid>/statut", methods=["POST"])
def statut_entrant(eid):
    """Marque un entrant « sans réponse attendue », le rouvre, ou change
    son urgence (recalcule l'échéance)."""
    action = request.form.get("action", "")
    with _verrou:
        ents = entrants()
        e = ents.get(eid)
        if not e:
            abort(404)
        if action == "sans_objet":
            e["statut_reponse"] = "sans_objet"
        elif action == "rouvrir":
            e["statut_reponse"] = "en_cours" if e.get("reponse_ref") else "a_preparer"
            if e.get("urgence") == "aucune":
                e["urgence"] = "normale"
            e["echeance"] = calculer_echeance(
                e["recu_le"], e["urgence"],
                (correspondant(e.get("correspondant_id")) or {}).get("categorie", ""))
        elif action in ("normale", "urgente"):
            e["urgence"] = action
            e["echeance"] = calculer_echeance(
                e["recu_le"], action,
                (correspondant(e.get("correspondant_id")) or {}).get("categorie", ""))
        _ecrire(F_ENTRANTS, ents)
    flash("Suivi du courrier mis à jour.", "success")
    return redirect(url_for("courrier.liste"))


# ---------------------------------------------------------------------------
# Routes — banque (annuaire, modèles, catégories)
# ---------------------------------------------------------------------------
@bp_courrier.route("/banque")
@_reserve_responsable
def banque():
    assurer_banque()
    onglet = request.args.get("onglet", "annuaire")
    edite_c = correspondant(request.args.get("correspondant", ""))
    edite_m = modele(request.args.get("modele", "")) if request.args.get("modele") else None
    nouveau_m = request.args.get("modele") == "nouveau"
    return render_template("courrier_banque.html", page="banque",
                           onglet=onglet, annuaire=correspondants(),
                           liste_modeles=modeles(), edite_c=edite_c,
                           edite_m=edite_m, nouveau_m=nouveau_m,
                           nouveau_c=request.args.get("correspondant") == "nouveau",
                           variables=LIBELLES_VARIABLES,
                           **_contexte_commun())


@bp_courrier.route("/banque/correspondant", methods=["POST"])
@_reserve_responsable
def sauver_correspondant():
    f = request.form
    with _verrou:
        liste = _lire(F_CORRESPONDANTS, [])
        cid = f.get("id", "")
        fiche = next((x for x in liste if x.get("id") == cid), None)
        if fiche is None:
            fiche = _fiche_vide()
            base = _slug(f.get("organisation", "") or f.get("nom", ""))
            cid, n = base, 2
            while any(x.get("id") == cid for x in liste):
                cid, n = f"{base}-{n}", n + 1
            fiche["id"] = cid
            liste.append(fiche)
        for champ in ("organisation", "nom", "fonction", "civilite", "formule_appel",
                      "adresse", "email", "categorie", "signataire", "copies",
                      "alias", "notes"):
            fiche[champ] = f.get(champ, "").replace("\r\n", "\n").strip()
        fiche["confidentiel"] = f.get("confidentiel") == "on"
        fiche["a_verifier"] = f.get("a_verifier") == "on"
        if not fiche["organisation"] and not fiche["nom"]:
            flash("Indiquez au moins l'organisation ou le nom.", "error")
            return redirect(url_for("courrier.banque", correspondant="nouveau"))
        _ecrire(F_CORRESPONDANTS, liste)
    flash(f"Fiche « {fiche['organisation'] or fiche['nom']} » enregistrée.", "success")
    return redirect(url_for("courrier.banque", onglet="annuaire"))


@bp_courrier.route("/banque/correspondant/<cid>/supprimer", methods=["POST"])
@_reserve_responsable
def supprimer_correspondant(cid):
    with _verrou:
        liste = [x for x in _lire(F_CORRESPONDANTS, []) if x.get("id") != cid]
        _ecrire(F_CORRESPONDANTS, liste)
    flash("Fiche supprimée.", "success")
    return redirect(url_for("courrier.banque", onglet="annuaire"))


@bp_courrier.route("/banque/modele", methods=["POST"])
@_reserve_responsable
def sauver_modele():
    f = request.form
    with _verrou:
        liste = modeles()
        mid = f.get("id", "")
        m = next((x for x in liste if x.get("id") == mid), None)
        corps = f.get("corps", "").replace("\r\n", "\n").strip()
        if m is None:
            base = f"m-{f.get('nature', 'x').lower()}-{_slug(f.get('nom', ''))}"
            mid, n = base, 2
            while any(x.get("id") == mid for x in liste):
                mid, n = f"{base}-{n}", n + 1
            m = {"id": mid, "version": 0, "cree_le": _maintenant()}
            liste.append(m)
        elif m.get("corps") != corps:
            # Tout changement de texte repasse le modèle en validation.
            m["statut"] = "a_valider"
        m.update({
            "nom": f.get("nom", "").strip() or NATURES.get(f.get("nature"), "Modèle"),
            "nature": f.get("nature", "AR"),
            "categorie": f.get("categorie", "*") or "*",
            "correspondant_id": f.get("correspondant_id", ""),
            "mise_en_page": f.get("mise_en_page") or (
                "decision" if f.get("nature") == "DEC" else "lettre"),
            "corps": corps,
            "maj_le": _maintenant(),
        })
        m.setdefault("statut", "a_valider")
        m["version"] = int(m.get("version", 0)) + 1
        if f.get("valider") == "on":
            m["statut"] = "valide"
            m["valide_par"] = _qui()
            m["valide_le"] = _maintenant()
        _ecrire(F_MODELES, liste)
    flash(f"Modèle « {m['nom']} » enregistré"
          f"{' et validé' if m['statut'] == 'valide' else ' (à valider)'}.", "success")
    return redirect(url_for("courrier.banque", onglet="modeles"))


@bp_courrier.route("/banque/modele/<mid>/<action>", methods=["POST"])
@_reserve_responsable
def action_modele(mid, action):
    with _verrou:
        liste = modeles()
        m = next((x for x in liste if x.get("id") == mid), None)
        if not m:
            abort(404)
        if action == "valider":
            m.update({"statut": "valide", "valide_par": _qui(),
                      "valide_le": _maintenant()})
            message = "Modèle validé : il est désormais proposé aux rédacteurs."
        elif action == "retirer":
            m["statut"] = "a_valider"
            message = "Modèle retiré : il n'est plus proposé aux rédacteurs."
        elif action == "supprimer":
            liste = [x for x in liste if x.get("id") != mid]
            message = "Modèle supprimé."
        elif action == "dupliquer":
            copie = copy.deepcopy(m)
            copie.update({"id": f"{mid}-copie-{datetime.now():%H%M%S}",
                          "nom": f"{m['nom']} (copie)", "statut": "a_valider",
                          "version": 1, "cree_le": _maintenant(),
                          "maj_le": _maintenant()})
            liste.append(copie)
            message = "Modèle dupliqué."
        else:
            abort(400)
        _ecrire(F_MODELES, liste)
    flash(message, "success")
    return redirect(url_for("courrier.banque", onglet="modeles"))


@bp_courrier.route("/banque/categories", methods=["POST"])
@_reserve_responsable
def sauver_categories():
    f = request.form
    with _verrou:
        liste = categories()
        for c in liste:
            code = c["code"]
            for champ in ("nom", "registre", "signataire", "formule_politesse", "exemples"):
                cle = f"{code}__{champ}"
                if cle in f:
                    c[champ] = f.get(cle, "").strip()
            for champ in ("delai_h", "delai_urgent_h"):
                cle = f"{code}__{champ}"
                if cle in f:
                    try:
                        c[champ] = max(1, int(f.get(cle)))
                    except (TypeError, ValueError):
                        pass
            c["ia_autorisee"] = f.get(f"{code}__ia_autorisee") == "on"
            c["confidentiel"] = f.get(f"{code}__confidentiel") == "on"
        _ecrire(F_CATEGORIES, liste)
    _recalculer_echeances()
    flash("Catégories enregistrées. Les échéances ouvertes ont été recalculées.",
          "success")
    return redirect(url_for("courrier.banque", onglet="categories"))


def _recalculer_echeances():
    with _verrou:
        ents = entrants()
        for e in ents.values():
            if e.get("statut_reponse") in ("a_preparer", "en_cours"):
                e["echeance"] = calculer_echeance(
                    e["recu_le"], e.get("urgence", "normale"),
                    (correspondant(e.get("correspondant_id")) or {}).get("categorie", ""))
        _ecrire(F_ENTRANTS, ents)


@bp_courrier.route("/banque/export.json")
@_reserve_responsable
def exporter():
    contenu = {
        "exporte_le": _maintenant(),
        "categories": categories(),
        "correspondants": _lire(F_CORRESPONDANTS, []),
        "modeles": modeles(),
    }
    return Response(json.dumps(contenu, indent=2, ensure_ascii=False),
                    mimetype="application/json",
                    headers={"Content-Disposition":
                             f"attachment; filename=banque_courriers_{datetime.now():%Y%m%d}.json"})


@bp_courrier.route("/banque/import", methods=["POST"])
@_reserve_responsable
def importer():
    fichier = request.files.get("fichier")
    if not fichier:
        flash("Choisissez un fichier d'export.", "error")
        return redirect(url_for("courrier.banque", onglet="sauvegarde"))
    try:
        contenu = json.loads(fichier.read().decode("utf-8"))
        assert isinstance(contenu.get("correspondants"), list)
        assert isinstance(contenu.get("modeles"), list)
        assert isinstance(contenu.get("categories"), list)
    except Exception:  # noqa: BLE001
        flash("Fichier illisible : ce n'est pas un export de la banque.", "error")
        return redirect(url_for("courrier.banque", onglet="sauvegarde"))
    with _verrou:
        _ecrire(F_CATEGORIES, contenu["categories"])
        _ecrire(F_CORRESPONDANTS, contenu["correspondants"])
        _ecrire(F_MODELES, contenu["modeles"])
    flash("Banque restaurée depuis la sauvegarde.", "success")
    return redirect(url_for("courrier.banque"))


# ---------------------------------------------------------------------------
# Routes — paramètres
# ---------------------------------------------------------------------------
@bp_courrier.route("/parametres", methods=["GET", "POST"])
@_reserve_responsable
def ecran_parametres():
    assurer_banque()
    if request.method == "POST":
        f = request.form
        with _verrou:
            p = parametres()
            ia_p = p["ia"]
            ia_p["actif"] = f.get("ia_actif") == "on"
            ia_p["fournisseur"] = f.get("ia_fournisseur", "groq")
            url = f.get("ia_base_url", "").strip()
            ia_p["base_url"] = url or FOURNISSEURS_IA.get(ia_p["fournisseur"], {}).get("url", "")
            ia_p["modele"] = f.get("ia_modele", "").strip()
            ia_p["anonymiser"] = f.get("ia_anonymiser") == "on"
            ia_p["autoriser_confidentiels"] = f.get("ia_confidentiels") == "on"
            ia_p["effort_raisonnement"] = f.get("ia_effort", "low")
            for champ, conv in (("temperature", float), ("max_tokens", int),
                                ("delai_s", int)):
                try:
                    ia_p[champ] = conv(f.get(f"ia_{champ}"))
                except (TypeError, ValueError):
                    pass
            for champ in ("delai_defaut_h", "delai_urgent_defaut_h",
                          "relance_intervalle_h"):
                try:
                    p[champ] = max(1, int(f.get(champ)))
                except (TypeError, ValueError):
                    pass
            p["service_defaut"] = f.get("service_defaut", p.get("service_defaut", "DGA"))
            for champ in ("format_reference", "format_reference_decision", "pied_de_page"):
                valeur = f.get(champ, "").strip()
                if valeur:
                    p[champ] = valeur
            sigs = []
            for i in range(int(f.get("nb_signataires", 0)) + 1):
                code = f.get(f"sig_{i}_code", "").strip().upper()
                if not code:
                    continue
                sigs.append({"code": code,
                             "fonction": f.get(f"sig_{i}_fonction", "").strip(),
                             "nom": f.get(f"sig_{i}_nom", "").strip(),
                             "email": f.get(f"sig_{i}_email", "").strip().lower()})
            if sigs:
                p["signataires"] = sigs
            _ecrire(F_PARAMETRES, p)
        cle = f.get("ia_cle", "").strip()
        if cle:
            enregistrer_cle_ia(cle)
        if f.get("ia_effacer_cle") == "on":
            enregistrer_cle_ia("")
        _recalculer_echeances()
        flash("Paramètres enregistrés.", "success")
        return redirect(url_for("courrier.ecran_parametres"))

    return render_template("courrier_parametres.html", page="parametres",
                           p=parametres(), fournisseurs=FOURNISSEURS_IA,
                           cle=cle_ia_masquee(), **_contexte_commun())


@bp_courrier.route("/parametres/tester-ia", methods=["POST"])
@_reserve_responsable
def tester_ia():
    if not _cle_ia():
        return jsonify({"ok": False, "erreur": "Aucune clé d'API configurée."})
    ok, texte = appeler_ia([
        {"role": "system", "content": "Réponds en une phrase courte, en français."},
        {"role": "user", "content": "Confirme que tu es opérationnel."}])
    if ok:
        return jsonify({"ok": True, "texte": texte.strip()[:300],
                        "modele": parametres()["ia"].get("modele")})
    return jsonify({"ok": False, "erreur": texte})


# ---------------------------------------------------------------------------
# API JSON — formulaire de préparation
# ---------------------------------------------------------------------------
@bp_courrier.route("/api/fiche/<cid>")
def api_fiche(cid):
    fiche = correspondant(cid)
    if not fiche:
        return jsonify({}), 404
    cat = categorie(fiche.get("categorie", "")) or {}
    return jsonify({**fiche, "categorie_nom": cat.get("nom", ""),
                    "signataire_defaut": fiche.get("signataire") or cat.get("signataire", "DG"),
                    "registre": cat.get("registre", "")})


@bp_courrier.route("/api/modele")
def api_modele():
    m = choisir_modele(request.args.get("nature", ""),
                       request.args.get("categorie", ""),
                       request.args.get("correspondant", ""),
                       inclure_non_valides=est_responsable_ged())
    return jsonify({"id": m["id"], "nom": m["nom"],
                    "statut": m.get("statut")} if m else {})


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------
def init_courrier(app, protection=None, services=None, types=None,
                  dernier_numero=None, notifier=None):
    """Enregistre le module. À appeler APRÈS app.register_blueprint(bp_courrier)."""
    _CTX.update({"services": services or {}, "types": types or {},
                 "dernier_numero": dernier_numero, "notifier": notifier})
    if protection is not None:
        for nom, vue in list(app.view_functions.items()):
            if nom.startswith("courrier."):
                app.view_functions[nom] = protection(vue)

    app.jinja_env.filters.setdefault("date_longue", date_longue)
    app.jinja_env.filters.setdefault("date_courte", date_courte)

    assurer_banque()
    nb_valides = sum(1 for m in modeles() if m.get("statut") == "valide")
    print(f"[COURRIER] Module actif — {len(correspondants())} correspondant(s), "
          f"{len(modeles())} modèle(s) dont {nb_valides} validé(s), "
          f"IA {'prête' if _cle_ia() else 'sans clé'} "
          f"({parametres()['ia'].get('modele')})")
