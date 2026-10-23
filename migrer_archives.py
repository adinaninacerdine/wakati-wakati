#!/usr/bin/env python3
"""
migrer_archives.py — migration d'une arborescence existante vers la norme HM26
===============================================================================
Reprend une arborescence constituee avant HM26 (clef USB, dossier reseau),
en deduit les metadonnees, propose un nom normalise, puis — apres validation
humaine — renomme, depose dans WorkDrive et inscrit au registre.

TROIS PASSES. Les deux premieres n'ecrivent rien d'irreversible.

  PASSE 1 — CARTOGRAPHIE
  ./venv/bin/python migrer_archives.py --cartographier --source ~/Documents/GED

      Lit l'arborescence, propose un couple service/type par dossier,
      et ecrit carte_import.json. Les types proposes sont CONTRAINTS a
      ceux reellement declares dans folders_map.json : un type inconnu
      sort en "a_decider" avec la liste des types disponibles.

      -> Tu relis carte_import.json (une quarantaine de lignes, pas 367)
         et tu corriges ce qui doit l'etre.

  PASSE 2 — PROPOSITIONS
  ./venv/bin/python migrer_archives.py --proposer --source ~/Documents/GED
      Ajoute --numeroter pour que le script PROPOSE un numero aux series
      qui n'en ont pas, en reprenant apres le plus grand numero deja
      utilise (noms de fichiers + registre). La proposition reste a
      valider dans le CSV : attribuer un numero d'enregistrement est un
      acte administratif, le script epargne seulement la frappe.

      Pour chaque fichier : extrait le numero et la date du nom, deduit
      le tiers du sous-dossier, nettoie l'objet, et propose un nom HM26.
      Ecrit propositions.csv et doublons.csv.

      -> La responsable GED relit propositions.csv. Les numeros sont son
         acte administratif, pas celui du script. Elle corrige la colonne
         "Action" et la colonne "Nom HM26" la ou il faut.

  PASSE 3 — APPLICATION
  ./venv/bin/python migrer_archives.py --appliquer --source ~/Documents/GED

      Applique propositions.csv ligne par ligne, selon la colonne Action :
        indexer   renomme le fichier local, uploade, inscrit au registre
        renommer  renomme le fichier local SEULEMENT (pas d'upload,
                  pas de registre) — pour les documents encore en cours
        ignorer   ne touche a rien
        a_decider refuse de traiter la ligne, et le dit

      Chaque renommage est consigne dans renommages.csv, ce qui rend
      l'operation reversible :

  ANNULATION DU RENOMMAGE
  ./venv/bin/python migrer_archives.py --annuler-renommage

  ANNULATION D'UN PASSAGE --appliquer (envoi compris)
  ./venv/bin/python migrer_archives.py --annuler-envoi
  ./venv/bin/python migrer_archives.py --annuler-envoi "2026-10-20 14:27:01"

      Sans valeur : liste les passages (horodatage du journal). Avec un
      horodatage : met les fichiers envoyes a la corbeille WorkDrive
      (restaurables 30 jours), retire leurs lignes du registre Zoho Sheet
      et de registre_2026.csv, remet les anciens noms sur le poste et
      retire les fiches du moteur de recherche. Confirmation demandee.

  MOTEUR DE RECHERCHE (sans upload ni registre)
  ./venv/bin/python migrer_archives.py --indexer-recherche --source ~/Documents/GED

      Verse dans le moteur de recherche de la GED toutes les lignes
      « indexer » et « renommer » de propositions.csv, texte des PDF compris
      (OCR des scans). Le fichier est retrouve sous son nom HM ou sous son
      nom d'origine. Relancable : un document deja lu n'est pas relu.
      --tout : ajoute aussi les lignes « a_decider », sous leur nom
      d'origine, pour que toute l'archive soit cherchable des maintenant.
      --appliquer le fait aussi, au fil de l'eau, pour ce qu'il traite.

NOUVEAUTES (octobre 2026)
-------------------------
  - Prefixe selon l'annee du document : HM25-… pour 2025, HM26-… pour 2026.
    Un document d'une autre annee n'est plus bloque « hors perimetre », et
    la numerotation se fait par annee : le 009 de 2025 et le 009 de 2026 ne
    sont plus en conflit.
  - Correspondant lu sur plusieurs niveaux : le DERNIER dossier pertinent
    sous la regle (…/Courrier entrants/ACTIC -> ACTIC ;
    …/Contrats Fournisseur/ECO-cuisine/FACTURE -> ECO-cuisine). Les dossiers
    de rangement (mois, annees, « Facture », « Scan »…) sont sautes.
    carte_import.json porte un champ « tiers » par dossier pour forcer la
    valeur a la main ; « tiers_propose » montre ce que le script a lu.
  - Indexation dans le moteur de recherche : nom, objet, correspondant,
    ancien nom, chemin des dossiers et texte des PDF. --sans-texte pour
    sauter l'OCR (plus rapide).

CORRECTIFS (20 octobre 2026)
----------------------------
  - Un fichier DEJA nomme au format HM (renomme lors d'un passage
    precedent) garde son nom, son numero et sa date. Avant, il etait
    renomme une seconde fois : HM26-…_054_HM26_DGA_CSOR_…_008_Objet.
    --appliquer refuse aussi une telle ligne si propositions.csv est perime.
  - Numero lu apres le destinataire dans un courrier sortant
    (« CS-BCC-010-… », « CS-Min des Postes -009-… ») au lieu d'en generer un.
  - Dates lues n'importe ou dans le nom : 31012025, 20260318, 11-09-2026,
    0426 (avril 2026) — et retirees de l'objet.
  - « vœux » -> « voeux », apostrophes typographiques conservees, restes
    d'extension (« M28pdf ») et apostrophes orphelines retires.
  - Registre : une date incomplete (2026-01-00) n'est plus ecrite dans la
    colonne Date ; elle reste dans le nom du fichier.

OPTIONS
-------
  --source <dossier>   arborescence a migrer. Obligatoire sauf annulation.
  --numeroter          (avec --proposer) propose les numeros manquants.
  --limite N           ne traiter que les N premieres lignes eligibles.
  --sans-renommer      uploader et indexer sans renommer les fichiers locaux.
  --sans-upload        renommer et indexer sans uploader.
  --sans-index         renommer et uploader sans ecrire au registre.
  --ecraser            remplacer un fichier WorkDrive de meme nom.
  --forcer             pas de confirmation interactive.
  --sans-texte         (avec --appliquer ou --indexer-recherche) indexer la
                       recherche sans lire le texte des PDF.

NORME PRODUITE
--------------
    HM{AA}-{SERVICE}-{TYPE}_{AAAAMMJJ}_{NNN}_{OBJET}.pdf

AA = deux derniers chiffres de l'annee du document (HM26 si l'annee est
inconnue).

Quand la date est partiellement inconnue, les positions manquantes sont
a zero (20260800 pour « aout 2026 »), ce que le registre accepte deja.
Quand aucun numero n'est extractible, le nom porte XXX et la ligne sort
en "a_decider" : le script n'invente pas un numero d'enregistrement.

SECURITE
--------
Aucun fichier n'est supprime, jamais. Les renommages sont consignes et
reversibles. --cartographier et --proposer n'ecrivent que leurs propres
rapports. Seul --appliquer touche a WorkDrive et au registre.
"""

import csv
import hashlib
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Racine du projet
# ---------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent

ROOT_DIR = None
for parent in [CURRENT_DIR] + list(CURRENT_DIR.parents):
    if (parent / "zoho_config.py").exists():
        ROOT_DIR = parent
        break

if not ROOT_DIR:
    print("ERREUR : zoho_config.py introuvable.")
    print("Lance ce script depuis le dossier du projet.")
    sys.exit(1)

sys.path.insert(0, str(ROOT_DIR))

MAP_FILE = ROOT_DIR / "folders_map.json"
REGISTRE = ROOT_DIR / "registre_2026.csv"
CARTE_IMPORT = ROOT_DIR / "carte_import.json"
PROPOSITIONS = ROOT_DIR / "propositions.csv"
DOUBLONS = ROOT_DIR / "doublons.csv"
RENOMMAGES = ROOT_DIR / "renommages.csv"
JOURNAL = ROOT_DIR / "migration_journal.csv"

WORKBOOK_ID = "avaji4d47ec1cbffc4f5184dc8b3127b83ae7"
FEUILLE = "DATA"
ANNEE = "26"

EP_FICHIERS = "/files/{fid}/files"

PAUSE_UPLOAD = 0.5
PAUSE_SHEET = 0.4

SERVICES = {
    "FIN": "Finances", "RH": "Ressources Humaines", "DGA": "Direction / Admin",
    "JUR": "Juridique", "COM": "Commercial", "EXP": "Exploitation",
}
TYPES = {
    "FAC": "Facture", "CNT": "Contrat", "NOT": "Note de service", "VIR": "Virement",
    "DEM": "Demande", "ODM": "Ordre de mission", "ETA": "État / Relevé",
    "ACC": "Accord", "ATT": "Attestation", "BOR": "Bordereau",
    "CENT": "Courrier Entrant", "CSOR": "Courrier Sortant",
    "PV": "Procès-verbal", "DOC": "Document divers", "COR": "Courrier",
    "MAN": "Mandat de paiement",
    # Types proposes par les heuristiques mais pas forcement declares
    # dans folders_map.json : la cartographie le signale.
    "DEC": "Décision", "INV": "Invitation", "REG": "Règlement intérieur",
    "DOS": "Dossier individuel", "PAI": "Paie",
}

BRANCHES = {
    "CENT": "Courriers", "CSOR": "Courriers", "COR": "Courriers",
    "NOT": "Courriers", "PV": "Direction", "DEC": "Direction / Admin",
    "MAN": "Finances",
}
BRANCHES_SERVICE = {
    "RH": "Ressources Humaines", "FIN": "Finances", "JUR": "Juridique",
    "DGA": "Direction / Admin", "COM": "Commercial", "EXP": "Exploitation",
}

MOTIF_HM26 = re.compile(
    r"^HM(?P<annee>\d{2})-(?P<service>[A-Z]+)-(?P<type>[A-Z]+)"
    r"_(?P<date>\d{8})_(?P<numero>[A-Z0-9]+)_(?P<objet>.*?)\.pdf$",
    re.IGNORECASE,
)


# ===========================================================================
# Outils de texte
# ===========================================================================
# Ligatures et signes que NFKD ne decompose pas : sans eux « vœux » devient
# « v_ux » et l'apostrophe typographique colle deux mots.
TRANSLITTERATION = str.maketrans({
    "œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE", "ß": "ss",
    "’": "'", "‘": "'", "«": " ", "»": " ", "•": " ", "–": "-", "—": "-",
})


def sans_accent(texte):
    t = unicodedata.normalize("NFKD", (texte or "").translate(TRANSLITTERATION))
    return "".join(c for c in t if not unicodedata.combining(c))


def normaliser(texte):
    """Minuscules, sans accent, separateurs unifies en espace."""
    t = sans_accent(texte or "").lower()
    t = re.sub(r"[_\-/\\.]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def chemin_normalise(relatif):
    """Chemin de dossier normalise, separateurs conserves."""
    morceaux = [normaliser(m) for m in Path(relatif).parts if m not in (".", "")]
    return "/".join(morceaux)


# ===========================================================================
# Heuristiques de classement — ordre du plus specifique au plus general
# ===========================================================================
# (fragments de chemin, service, type, sens, action, tiers_depuis_sous_dossier)
REGLES = [
    (("admnistration/courrier entrant", "administration/courrier entrant"),
     "DGA", "CENT", "entrant", "indexer", True),
    (("admnistration/courrier sotant", "administration/courrier sortant",
      "admnistration/courrier sortant"),
     "DGA", "CSOR", "sortant", "indexer", True),
    (("appui telecom",), "DGA", "CSOR", "sortant", "indexer", False),

    (("admnistration/note interne", "administration/note interne"),
     "DGA", "NOT", "", "indexer", False),
    (("admnistration/decisions", "administration/decisions"),
     "DGA", "DEC", "", "indexer", False),
    (("admnistration/invitation", "administration/invitation"),
     "DGA", "INV", "entrant", "indexer", False),
    (("admnistration/reglements interieurs", "administration/reglements interieurs"),
     "DGA", "REG", "", "indexer", False),
    (("admnistration/correspondances", "administration/correspondances"),
     "DGA", "COR", "", "indexer", False),

    (("direction/correspondances dg",), "DGA", "COR", "", "indexer", False),
    (("direction/reunion pv",), "DGA", "PV", "", "indexer", False),
    (("direction/missions",), "DGA", "ODM", "", "indexer", False),
    (("direction/partenariats",), "DGA", "ACC", "", "indexer", False),
    (("direction/dossiers institutionnels",), "DGA", "DOC", "", "indexer", False),
    (("direction/instuction", "instruiction dg", "direction/instruction"),
     "DGA", "NOT", "", "indexer", False),

    (("finance/mandats paiement",), "FIN", "MAN", "", "indexer", False),
    (("finance/facture",), "FIN", "FAC", "", "indexer", True),
    (("finance/quittances",), "FIN", "ETA", "", "indexer", False),
    (("finance/documents bancaires",), "FIN", "ETA", "", "indexer", False),
    (("finance/comptabilite",), "FIN", "ETA", "", "indexer", False),
    (("finance/fiscalite",), "FIN", "ETA", "", "indexer", False),
    (("finance/paiements",), "FIN", "VIR", "", "indexer", False),
    (("finance/pieces justificatives",), "FIN", "DOC", "", "indexer", False),
    (("finance/virement projet",), "FIN", "VIR", "", "indexer", False),

    (("rh/contrat de travail",), "RH", "CNT", "", "indexer", False),
    (("rh/accords de confidentialite",), "RH", "ACC", "", "indexer", False),
    (("rh/ac 2025",), "RH", "ACC", "", "a_decider", False),
    (("rh/attestations",), "RH", "ATT", "", "indexer", False),
    (("rh/conventions stage",), "RH", "CNT", "", "indexer", False),
    (("rh/conges et absences",), "RH", "DEM", "", "indexer", False),
    (("rh/evaluations et formations",), "RH", "DOC", "", "indexer", False),
    (("rh/fiches de paie", "rh/paie"), "RH", "PAI", "", "indexer", False),
    (("rh/dossiers du personnel",), "RH", "DOS", "", "indexer", True),

    (("juridique/contrats fournisseur",), "JUR", "CNT", "", "indexer", True),
    (("juridique/accords",), "JUR", "ACC", "", "indexer", False),
    (("juridique/conventions",), "JUR", "CNT", "", "indexer", False),
    (("juridique/statuts",), "JUR", "DOC", "", "indexer", False),
    (("juridique/mandats juridiques",), "JUR", "DOC", "", "indexer", False),
    (("juridique/documents reglementaires",), "JUR", "DOC", "", "indexer", False),
    (("juridique/propriete intellectuelle",), "JUR", "DOC", "", "indexer", False),

    (("technique it",), "EXP", "DOC", "", "indexer", False),
    (("commercial",), "COM", "DOC", "", "indexer", False),
    (("regulateurs",), "JUR", "DOC", "", "indexer", True),
    (("conformite",), "JUR", "DOC", "", "indexer", False),
    (("projet",), "EXP", "DOC", "", "indexer", False),

    # Fourre-tout : volontairement en a_decider, ils demandent un oeil humain.
    (("archives",), "DGA", "DOC", "", "a_decider", False),
    (("dossiers en cours",), "DGA", "DOC", "", "renommer", False),
    (("model",), "", "", "", "ignorer", False),
    (("archivage",), "", "", "", "ignorer", False),

    # Derniers recours par service racine.
    (("finance",), "FIN", "DOC", "", "a_decider", False),
    (("juridique",), "JUR", "DOC", "", "a_decider", False),
    (("rh",), "RH", "DOC", "", "a_decider", False),
    (("admnistration", "administration"), "DGA", "DOC", "", "a_decider", False),
    (("direction",), "DGA", "DOC", "", "a_decider", False),
]


def deviner(relatif):
    """Applique les regles a un chemin de dossier relatif.

    Retourne aussi `profondeur` : le nombre de segments consommes par la
    regle. Le tiers se lit au segment suivant, jamais plus haut — c'est ce
    qui distingue FINANCE/Facture/Janus (tiers = Janus) de FINANCE/Facture
    (pas de tiers) ou de FINANCE/Mandats_Paiement/Mandat 2026 (pas un tiers).
    """
    chemin = chemin_normalise(relatif)
    segments = [s for s in chemin.split("/") if s]

    for fragments, service, type_doc, sens, action, tiers in REGLES:
        for frag in fragments:
            frag_segs = [s for s in frag.split("/") if s]
            n = len(frag_segs)
            for i in range(len(segments) - n + 1):
                # Prefixe et non egalite : « Courrier entrants » (pluriel dans
                # l'arborescence) doit matcher « courrier entrant ».
                if all(segments[i + k].startswith(frag_segs[k])
                       for k in range(n)):
                    return {
                        "service": service, "type": type_doc, "sens": sens,
                        "action": action, "tiers_depuis_sous_dossier": tiers,
                        "profondeur": i + n,
                    }
    return {"service": "", "type": "", "sens": "", "action": "a_decider",
            "tiers_depuis_sous_dossier": False, "profondeur": 0}


# ===========================================================================
# Extraction du numero
# ===========================================================================
MOTIFS_NUMERO = [
    (r"mandat\s*(?:n[°o]\s*)?0*(\d{1,4})", "Mandat NNN"),
    (r"\bcs\s*ref\s*0*(\d{1,4})", "CS-REF-NNN"),
    (r"\bce\s*ref\s*0*(\d{1,4})", "CE-REF-NNN"),
    (r"\bref\s*0*(\d{1,4})", "Ref NNN"),
    (r"\bcs\s*0*(\d{1,3})(?=\D|$)", "CS-NN"),
    (r"\bce\s*0*(\d{1,3})(?=\D|$)", "CE-NN"),
    (r"\bn[°o]\s*0*(\d{1,4})", "N° NNN"),
    (r"^(?:decision|note|pv|bordereau|attestation|facture)\s*n?[°o]?\s*0*(\d{1,4})\b",
     "Serie NNN"),
    # Courrier sortant dont le numero suit le destinataire :
    # « CS-BCC-010-Reponse… », « CS-Min des Postes -009-Remerciement… ».
    # Limite aux CS : dans un CE, un numero isole est souvent la reference
    # de l'expediteur, pas la notre.
    (r"^cs\s.*?(?:^|\s)(0\d{2})(?=\s|$)", "CS-…-NNN"),
]


def extraire_numero(nom):
    """Retourne (numero sur 3 chiffres, motif d'origine) ou ("", "")."""
    base = normaliser(nom)
    for motif, etiquette in MOTIFS_NUMERO:
        m = re.search(motif, base)
        if not m:
            continue
        brut = m.group(1)
        # Un nombre a 4 chiffres dans la plage des annees n'est pas un numero.
        if len(brut) == 4 and 2000 <= int(brut) <= 2099:
            continue
        return f"{int(brut):03d}", etiquette
    return "", ""


# ===========================================================================
# Extraction de la date
# ===========================================================================
MOIS = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10,
    "novembre": 11, "decembre": 12,
}


def _annee_pleine(valeur):
    n = int(valeur)
    if n >= 1000:
        return n
    return 2000 + n if n < 50 else 1900 + n


def extraire_date(nom, relatif):
    """Retourne (aaaa, mm, jj, source). Les inconnues valent 0."""
    base = normaliser(nom)
    base = re.sub(r"(?:\s?pdf)+$", "", base).strip()

    # 0a. AAAAMMJJ accole, n'importe ou : MAPA_20260318_…
    for m in re.finditer(r"(?:^|\s)(20\d{2})(\d{2})(\d{2})(?=\s|$)", base):
        aaaa, mm, jj = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mm <= 12 and 1 <= jj <= 31:
            return aaaa, mm, jj, "nom AAAAMMJJ"

    # 0b. JJMMAAAA accole, n'importe ou : -31012025, - 25062026 -
    for m in re.finditer(r"(?:^|\s)(\d{2})(\d{2})(20\d{2})(?=\s|$)", base):
        jj, mm, aaaa = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mm <= 12 and 1 <= jj <= 31:
            return aaaa, mm, jj, "nom JJMMAAAA"

    # 0c. JJ MM AAAA separes, n'importe ou : -11-09-2026, 22-07-2026
    for m in re.finditer(r"(?:^|\s)(\d{1,2})\s(\d{1,2})\s(20\d{2})(?=\s|$)", base):
        jj, mm, aaaa = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mm <= 12 and 1 <= jj <= 31:
            return aaaa, mm, jj, "nom JJ-MM-AAAA"

    # 1. JJMMAA accole en fin : -120426
    m = re.search(r"(?:^|\s)(\d{2})(\d{2})(\d{2})(?=\D*$)", base)
    if m:
        jj, mm, aa = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= jj <= 31 and 1 <= mm <= 12:
            return _annee_pleine(aa), mm, jj, "nom JJMMAA"

    # 2. JJ MM AAAA ou JJ MM AA separes
    m = re.search(r"(?:^|\s)(\d{1,2})\s(\d{1,2})\s(\d{2,4})(?=\D*$)", base)
    if m:
        jj, mm, aa = int(m.group(1)), int(m.group(2)), m.group(3)
        if 1 <= jj <= 31 and 1 <= mm <= 12:
            return _annee_pleine(aa), mm, jj, "nom JJ-MM-AA"

    # 3. MM AA en fin : -03-26
    m = re.search(r"(?:^|\s)(\d{1,2})\s(\d{2})(?=\D*$)", base)
    if m:
        mm, aa = int(m.group(1)), int(m.group(2))
        if 1 <= mm <= 12 and 20 <= aa <= 40:
            return _annee_pleine(aa), mm, 0, "nom MM-AA"

    # 3b. MMAA accole en fin : -0426
    m = re.search(r"(?:^|\s)(0[1-9]|1[0-2])(\d{2})(?=\D*$)", base)
    if m:
        mm, aa = int(m.group(1)), int(m.group(2))
        if 20 <= aa <= 40:
            return _annee_pleine(aa), mm, 0, "nom MMAA"

    # 4. Nom de mois, dans le nom puis dans le chemin
    for source_texte, etiquette in ((base, "nom mois"),
                                    (chemin_normalise(relatif), "dossier mois")):
        for libelle, numero in MOIS.items():
            if libelle not in source_texte:
                continue
            m = re.search(r"(20\d{2}|\b\d{2}\b)", source_texte.split(libelle, 1)[1])
            if m:
                return _annee_pleine(m.group(1)), numero, 0, etiquette
            m = re.search(r"(20\d{2})", source_texte)
            if m:
                return int(m.group(1)), numero, 0, etiquette
            return 2000 + int(ANNEE), numero, 0, etiquette + " (annee supposee)"

    # 5. Annee seule, dans le nom puis dans le chemin
    for source_texte, etiquette in ((base, "nom annee"),
                                    (chemin_normalise(relatif), "dossier annee")):
        m = re.search(r"\b(20\d{2})\b", source_texte)
        if m:
            return int(m.group(1)), 0, 0, etiquette

    return 0, 0, 0, ""


# ===========================================================================
# Tiers et objet
# ===========================================================================
# Les separateurs sont unifies en espace AVANT ces motifs, donc \s suffit.
PREFIXES_A_RETIRER = [
    r"^cs\s*ref\s*\d+\s*(?:ct\s+)?",
    r"^ce\s*ref\s*\d+\s*(?:ct\s+)?",
    r"^cs\s*\d{1,3}\s+", r"^ce\s*\d{1,3}\s+",
    r"^cs\s+", r"^ce\s+",
    r"^mandat\s*\d*\s*",
    r"^ac\s*conf\s*rh\s*(?:\d{4}\s*)?",
    r"^contrat\s*rh\s*(?:\d{4}\s*)?",
    r"^ref\s*\d+\s*", r"^n[°o]\s*\d+\s*",
    r"^(decision|note|pv|bordereau|attestation|facture)\s*n?[°o]?\s*\d{1,3}\s+",
]

MOTIF_MOIS = "|".join(MOIS)


def _tiers_plausible(candidat):
    """Ecarte un segment de chemin qui n'est pas un tiers : mois, annee, vide."""
    n = normaliser(candidat)
    if not n or len(n) < 2:
        return False
    if n in MOIS:
        return False
    if re.search(r"\b(19|20)\d{2}\b", n):
        return False
    if re.fullmatch(r"\d+", n):
        return False
    if re.search(rf"\b({MOTIF_MOIS})\b", n):
        return False
    return True


# Dossiers de rangement : ils classent, ils ne nomment pas un correspondant.
DOSSIERS_DE_RANGEMENT = {
    "facture", "factures", "contrat", "contrats", "courrier", "courriers",
    "scan", "scans", "scanne", "pdf", "copie", "copies", "original", "originaux",
    "annexe", "annexes", "piece jointe", "pieces jointes", "divers", "autres",
    "archive", "archives", "ancien", "anciens", "nouveau", "dossier", "dossiers",
    "mandat", "mandats", "situation", "recu", "recus", "envoye", "envoyes",
    "signe", "signes", "valide", "a signer", "brouillon", "brouillons", "test",
    "prestation de service", "prestations de service", "prestations",
}


def _dossier_de_rangement(candidat):
    n = normaliser(candidat)
    return n in DOSSIERS_DE_RANGEMENT or any(
        n.startswith(m + " ") and len(n.split()) <= 2 for m in ("scan", "copie", "annexe"))


def tiers_du_dossier(sous_dossier, regle):
    """Dernier dossier pertinent SOUS celui consomme par la regle.

    Le nom du correspondant est souvent le niveau le plus profond
    (…/Courrier entrants/ACTIC). On remonte depuis le fond en sautant mois,
    annees et dossiers de rangement :
    …/Contrats Fournisseur/ECO-cuisine/FACTURE -> ECO-cuisine.
    """
    parts = [p for p in Path(sous_dossier).parts if p not in (".", "")]
    profondeur = regle.get("profondeur", 0)
    if not profondeur or len(parts) <= profondeur:
        return ""
    for candidat in reversed(parts[profondeur:]):
        if _tiers_plausible(candidat) and not _dossier_de_rangement(candidat):
            return candidat.strip()
    return ""


def extraire_tiers(nom, sous_dossier, regle):
    """Expediteur, destinataire ou titulaire du dossier.

    Ordre : valeur forcee dans carte_import.json (« tiers »), puis dernier
    dossier pertinent sous la regle, puis indices dans le nom du fichier.
    """
    if (regle.get("tiers") or "").strip():
        return regle["tiers"].strip()

    if regle.get("tiers_depuis_sous_dossier"):
        trouve = tiers_du_dossier(sous_dossier, regle)
        if trouve:
            return trouve

    if "appui telecom" in chemin_normalise(sous_dossier):
        return "Comores Telecom"

    # CE-DGI-objet  ->  DGI
    m = re.match(r"^(?:CE|CS)[-_\s]+([A-Z][A-Za-z0-9&'\. ]{1,24}?)[-_]", nom)
    if m and _tiers_plausible(m.group(1)):
        return m.group(1).strip()

    return ""


def nettoyer_objet(nom, numero, date_tuple, tiers, sens=""):
    """Objet lisible, debarrasse des prefixes de serie, numeros et dates.

    Quand le tiers n'a pas de colonne au registre (sens vide : dossier du
    personnel, contrat fournisseur, facture), il est place en tete de l'objet
    plutot que perdu.
    """
    stem = nom
    while stem.lower().endswith(".pdf"):
        stem = stem[:-4]

    t = sans_accent(stem)
    # Extension restee dans le nom : « M28pdf », « HM5 pdf », « .pdf (2) »
    t = re.sub(r"\.?pdf\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"[_\-–—]+", " ", t)
    t = re.sub(r"[()\[\]{}]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    for motif in PREFIXES_A_RETIRER:
        nouveau = re.sub(motif, "", t, flags=re.IGNORECASE).strip()
        if nouveau != t:
            t = nouveau
            break

    # Le numero a sa place dans le nom : on ne le laisse pas en plus dans
    # l'objet (« CS-BCC-010-Reponse » -> objet « Reponse »).
    if numero:
        t = re.sub(rf"(?:^|\s){numero}(?=\s|$)", " ", t, count=1).strip()

    # Suffixe de scanner : « _0001 », « 001 »
    t = re.sub(r"\s0{2,}\d\s*$", "", t)

    # Dates completes, ou qu'elles soient : 31012025, 20260318, 11 09 2026
    def _date_valide(m):
        a, b, c = (int(x) for x in m.groups())
        return ((1 <= b <= 12 and 1 <= c <= 31 and 2000 <= a <= 2099)
                or (1 <= b <= 12 and 1 <= a <= 31 and 2000 <= c <= 2099))
    t = re.sub(r"(?:^|\s)(\d{4})(\d{2})(\d{2})(?=\s|$)",
               lambda m: " " if _date_valide(m) else m.group(0), t)
    t = re.sub(r"(?:^|\s)(\d{2})(\d{2})(\d{4})(?=\s|$)",
               lambda m: " " if _date_valide(m) else m.group(0), t)
    t = re.sub(r"(?:^|\s)(\d{1,2})\s(\d{1,2})\s(20\d{2})(?=\s|$)",
               lambda m: " " if _date_valide(m) else m.group(0), t)
    t = re.sub(r"\s+", " ", t).strip()

    # Dates en fin de chaine, du plus precis au plus vague
    t = re.sub(r"\s\d{2}\s?\d{2}\s?\d{2}\s*$", "", t)
    t = re.sub(r"\s(0[1-9]|1[0-2])(2\d|3\d)\s*$", "", t)
    t = re.sub(r"\s\d{1,2}\s\d{2,4}\s*$", "", t)
    t = re.sub(rf"\s({MOTIF_MOIS})(\s\d{{2,4}})?\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s(19|20)\d{2}\s*$", "", t)

    # Le tiers a sa propre colonne (ou passera en tete) : on evite de le
    # repeter dans l'objet. D'abord la locution complete, puis mot a mot
    # avec tolerance de prefixe — « Janus » doit aussi attraper « janusz ».
    if tiers:
        t = re.sub(rf"\b{re.escape(sans_accent(tiers))}\b", " ", t,
                   flags=re.IGNORECASE)
        cles = [normaliser(mot) for mot in re.split(r"[\s.]+", tiers) if mot]
        cles = [c for c in cles if len(c) >= 3]
        if cles:
            gardes = []
            for mot in t.split():
                nm = normaliser(mot)
                if any(nm == c or (len(c) >= 4 and nm.startswith(c)) for c in cles):
                    continue
                gardes.append(mot)
            t = " ".join(gardes)

    t = re.sub(r"[^A-Za-z0-9 ']+", " ", t)
    t = re.sub(r"(?:^|\s)'+|'+(?=\s|$)", " ", t)   # apostrophes orphelines
    t = re.sub(r"\s+", " ", t).strip()

    if tiers and not sens:
        t = f"{sans_accent(tiers)} {t}".strip()
        t = re.sub(r"[^A-Za-z0-9 ']+", " ", t)
        t = re.sub(r"\s+", " ", t).strip()

    if not t:
        t = "document"

    return "_".join(t.split())[:70].strip("_") or "document"


def annee_courte(date_tuple):
    """« 25 » pour un document de 2025 ; ANNEE si l'annee est inconnue."""
    aaaa = date_tuple[0] if date_tuple else 0
    return f"{aaaa % 100:02d}" if aaaa else ANNEE


def nom_hm26(service, type_doc, date_tuple, numero, objet):
    """Nom normalise. Le prefixe suit l'annee du document : HM25, HM26…"""
    aaaa, mm, jj = date_tuple
    jeton = f"{aaaa or 2000 + int(ANNEE):04d}{mm:02d}{jj:02d}"
    return (f"HM{annee_courte(date_tuple)}-{service}-{type_doc}_{jeton}_"
            f"{numero or 'XXX'}_{objet}.pdf")


# ===========================================================================
# folders_map.json
# ===========================================================================
def charger_carte_workdrive():
    if not MAP_FILE.exists():
        print(f"ERREUR : {MAP_FILE.name} introuvable.")
        sys.exit(1)
    carte = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    dispo = {}
    for service, bloc in (carte.get("services") or {}).items():
        types = {t: fid for t, fid in (bloc.get("types") or {}).items() if fid}
        if types:
            dispo[service.upper()] = types
    return dispo


# ===========================================================================
# Inventaire local
# ===========================================================================
def empreinte(chemin):
    h = hashlib.md5()
    with open(chemin, "rb") as f:
        for bloc in iter(lambda: f.read(1024 * 256), b""):
            h.update(bloc)
    return h.hexdigest()


def inventaire(source):
    racine = Path(source).expanduser().resolve()
    if not racine.is_dir():
        print(f"ERREUR : {racine} n'est pas un dossier.")
        sys.exit(1)

    fichiers = []
    for chemin in sorted(racine.rglob("*")):
        if not chemin.is_file() or chemin.suffix.lower() != ".pdf":
            continue
        try:
            tete = chemin.open("rb").read(5)
        except OSError as exc:
            print(f"  illisible : {chemin.name} ({exc})")
            continue
        fichiers.append({
            "chemin": chemin,
            "nom": chemin.name,
            "sous_dossier": str(chemin.parent.relative_to(racine)) or ".",
            "taille": chemin.stat().st_size,
            "md5": empreinte(chemin),
            "pdf_valide": tete.startswith(b"%PDF"),
        })
    return racine, fichiers


# ===========================================================================
# PASSE 1 — cartographie
# ===========================================================================
def cartographier(source):
    racine, fichiers = inventaire(source)
    dispo = charger_carte_workdrive()

    print(f"\n=== PASSE 1 — CARTOGRAPHIE ===\n")
    print(f"Source : {racine}")
    print(f"Fichiers PDF : {len(fichiers)}")
    print(f"Couples service/type declares dans folders_map.json : "
          f"{sum(len(t) for t in dispo.values())}")

    comptes = defaultdict(int)
    for f in fichiers:
        comptes[f["sous_dossier"]] += 1

    dossiers = {}
    inconnus = defaultdict(list)

    for relatif in sorted(comptes):
        regle = deviner(relatif)
        service = regle["service"]
        type_doc = regle["type"]
        note = ""

        if service and service not in dispo:
            note = f"service {service} absent de folders_map.json"
            regle["action"] = "a_decider"
        elif service and type_doc and type_doc not in dispo.get(service, {}):
            note = (f"type {type_doc} non declare pour {service} — "
                    f"choisis parmi : {' '.join(sorted(dispo[service]))}")
            regle["action"] = "a_decider"
            inconnus[service].append(type_doc)

        dossiers[relatif] = {
            "fichiers": comptes[relatif],
            "service": service,
            "type": type_doc,
            "sens": regle["sens"],
            "action": regle["action"],
            "tiers_depuis_sous_dossier": regle["tiers_depuis_sous_dossier"],
            "profondeur": regle.get("profondeur", 0),
            "tiers_propose": tiers_du_dossier(relatif, regle)
                             if regle["tiers_depuis_sous_dossier"] else "",
            "tiers": "",
            "note": note,
        }

    sortie = {
        "_lisez_moi": [
            "Un dossier = une ligne. Corrige service, type, sens et action.",
            "action : indexer | renommer | ignorer | a_decider",
            "  indexer   renomme, uploade, inscrit au registre",
            "  renommer  renomme le fichier local seulement",
            "  ignorer   ne touche a rien",
            "  a_decider bloque la ligne : le script refusera de la traiter",
            "Les lignes en a_decider avec une note sont celles a trancher.",
            "tiers : laisse vide pour garder tiers_propose (lu dans le dernier",
            "  dossier pertinent) ; renseigne-le pour forcer le correspondant.",
        ],
        "_services_et_types_disponibles": {s: sorted(t) for s, t in sorted(dispo.items())},
        "dossiers": dossiers,
    }

    CARTE_IMPORT.write_text(
        json.dumps(sortie, ensure_ascii=False, indent=2), encoding="utf-8")

    par_action = defaultdict(int)
    par_action_fichiers = defaultdict(int)
    for info in dossiers.values():
        par_action[info["action"]] += 1
        par_action_fichiers[info["action"]] += info["fichiers"]

    print("\n--- REPARTITION ---")
    for action in sorted(par_action):
        print(f"  {action:12} {par_action[action]:>3} dossier(s), "
              f"{par_action_fichiers[action]:>3} fichier(s)")

    a_trancher = [(r, i) for r, i in dossiers.items() if i["note"]]
    if a_trancher:
        print(f"\n--- {len(a_trancher)} DOSSIER(S) A TRANCHER ---")
        for relatif, info in a_trancher[:15]:
            print(f"  {relatif}  ({info['fichiers']} fic.)")
            print(f"      {info['note']}")
        if len(a_trancher) > 15:
            print(f"  … et {len(a_trancher) - 15} autre(s), voir le fichier")

    if inconnus:
        print("\n--- TYPES PROPOSES MAIS NON DECLARES ---")
        for service, types in sorted(inconnus.items()):
            print(f"  {service} : {' '.join(sorted(set(types)))}")
        print("  Soit tu les remplaces par un type existant dans carte_import.json,")
        print("  soit tu crees le dossier WorkDrive et tu l'ajoutes a folders_map.json.")

    print(f"\n{CARTE_IMPORT.name} ecrit. Relis-le, puis lance --proposer.")
    print("Rien d'autre n'a ete ecrit.")


# ===========================================================================
# PASSE 2 — propositions
# ===========================================================================
COLONNES_PROPOSITIONS = [
    "Action", "Nom HM26", "Fichier origine", "Sous-dossier", "Service", "Type",
    "Sens", "Date", "Date source", "Numero", "Numero source", "Tiers", "Objet",
    "Dossier WorkDrive", "MD5", "Taille Ko", "Remarque",
]


def numeroter_series(lignes, noms_registre):
    """Propose un numero aux lignes qui n'en ont pas, par serie service/type.

    La serie reprend apres le plus grand numero deja utilise — dans les noms
    de fichiers d'origine ET dans le registre existant. Rien n'est ecrit au
    registre ici : c'est une PROPOSITION dans le CSV, que la responsable GED
    accepte ou corrige. L'attribution d'un numero d'enregistrement reste son
    acte, le script ne fait que lui epargner la frappe.
    """
    # Une serie = une annee x un service x un type : le registre repart a 001
    # chaque annee (HM25-DGA-CSOR 009 et HM26-DGA-CSOR 009 coexistent).
    maxima = defaultdict(int)

    for nom in noms_registre:
        m = MOTIF_HM26.match(nom)
        if not m:
            continue
        chiffres = re.sub(r"\D", "", m.group("numero"))
        if chiffres:
            cle = (m.group("annee"), m.group("service").upper(), m.group("type").upper())
            maxima[cle] = max(maxima[cle], int(chiffres))

    for ligne in lignes:
        if ligne["Numero"] and ligne["Service"] and ligne["Type"]:
            cle = (annee_courte(ligne["_date"]), ligne["Service"], ligne["Type"])
            maxima[cle] = max(maxima[cle], int(ligne["Numero"]))

    a_combler = [l for l in lignes
                 if l["_manque_numero"] and l["Service"] and l["Type"]
                 and l["Action"] not in ("ignorer",)]
    a_combler.sort(key=lambda l: (l["Service"], l["Type"], l["Date"] or "9999",
                                  l["Fichier origine"]))

    attribues = 0
    for ligne in a_combler:
        cle = (annee_courte(ligne["_date"]), ligne["Service"], ligne["Type"])
        maxima[cle] += 1
        ligne["Numero"] = f"{maxima[cle]:03d}"
        ligne["Numero source"] = "genere"
        ligne["Remarque"] = (ligne["Remarque"] + " ; " if ligne["Remarque"] else "") \
            + "numero propose par le script — A VALIDER"
        attribues += 1

    return attribues


def proposer(source, numeroter=False):
    racine, fichiers = inventaire(source)
    dispo = charger_carte_workdrive()

    if not CARTE_IMPORT.exists():
        print(f"ERREUR : {CARTE_IMPORT.name} introuvable. Lance --cartographier.")
        sys.exit(1)

    carte = json.loads(CARTE_IMPORT.read_text(encoding="utf-8"))
    dossiers = carte.get("dossiers") or {}

    print(f"\n=== PASSE 2 — PROPOSITIONS ===\n")
    print(f"Source : {racine}")
    print(f"Fichiers PDF : {len(fichiers)}")

    noms_registre = charger_noms_registre()
    print(f"Deja au registre : {len(noms_registre)}")

    lignes = []

    for f in fichiers:
        info = dossiers.get(f["sous_dossier"])
        if info is None:
            info = {"service": "", "type": "", "sens": "", "action": "a_decider",
                    "tiers_depuis_sous_dossier": False, "profondeur": 0,
                    "note": "dossier absent de carte_import.json"}
        elif "profondeur" not in info:
            # carte_import.json ecrit par une version anterieure
            info = dict(info, profondeur=deviner(f["sous_dossier"]).get("profondeur", 0))

        action = info.get("action", "a_decider")
        service = (info.get("service") or "").upper()
        type_doc = (info.get("type") or "").upper()
        remarques = []
        if info.get("note"):
            remarques.append(info["note"])

        if not f["pdf_valide"]:
            action = "a_decider"
            remarques.append("l'entete du fichier n'est pas %PDF")

        if f["nom"].lower().endswith(".pdf.pdf"):
            remarques.append("extension doublee, corrigee par le renommage")

        # Fichier DEJA au format HM (renomme par un passage precedent, ou par
        # l'application) : son nom fait foi. On le relit au lieu de le
        # renommer une seconde fois — sinon « HM26-…_008_Objet » devient
        # « HM26-…_054_HM26_DGA_CSOR_…_008_Objet ».
        deja_hm = MOTIF_HM26.match(f["nom"])
        tiers = extraire_tiers(f["nom"], f["sous_dossier"], info)
        if deja_hm:
            d = deja_hm.group("date")
            aaaa, mm, jj = int(d[:4]), int(d[4:6]), int(d[6:])
            src_date = "nom HM existant" if aaaa else ""
            numero = deja_hm.group("numero").upper()
            src_num = "nom HM existant" if numero != "XXX" else ""
            if numero == "XXX":
                numero = ""
            objet = deja_hm.group("objet")
            nom_service = deja_hm.group("service").upper()
            nom_type = deja_hm.group("type").upper()
            if (service, type_doc) != (nom_service, nom_type) and service and type_doc:
                remarques.append(f"le nom dit {nom_service}-{nom_type}, le dossier "
                                 f"{service}-{type_doc} : le nom est garde")
            service, type_doc = nom_service, nom_type
            remarques.append("deja au format HM — nom conserve")
        else:
            numero, src_num = extraire_numero(f["nom"])
            aaaa, mm, jj, src_date = extraire_date(f["nom"], f["sous_dossier"])
            objet = nettoyer_objet(f["nom"], numero, (aaaa, mm, jj), tiers,
                                   sens=info.get("sens", ""))

        if aaaa and aaaa != 2000 + int(ANNEE) and not deja_hm:
            if src_date == "nom annee":
                # Une annee nue dans un nom de fichier appartient souvent a
                # l'objet, pas a la date : « campagne QoS 2025 » n'est pas un
                # document de 2025. On ne la retient pas comme date.
                remarques.append(
                    f"annee {aaaa} trouvee dans le nom mais probablement dans "
                    f"l'objet — date laissee vide")
                aaaa = mm = jj = 0
                src_date = ""
                objet = nettoyer_objet(f["nom"], numero, (0, 0, 0), tiers,
                                       sens=info.get("sens", ""))
            else:
                # L'annee entre dans le prefixe (HM25-…) et dans la serie de
                # numerotation : plus de blocage « hors perimetre ».
                remarques.append(f"annee {aaaa} -> prefixe HM{aaaa % 100:02d}")

        # Le verdict sur l'absence de numero est rendu APRES la boucle :
        # --numeroter peut encore combler le trou, et il lui faut d'abord
        # connaitre le plus grand numero de chaque serie.
        manque_numero = not numero

        if not src_date and action == "indexer":
            remarques.append("aucune date extraite")

        fid = ""
        if service and type_doc:
            fid = dispo.get(service, {}).get(type_doc, "")
            if not fid and action == "indexer":
                remarques.append(f"{service}-{type_doc} sans dossier WorkDrive")
                action = "a_decider"

        # Nom fige : un nom HM complet (numero compris) se garde tel quel.
        nom_fige = f["nom"] if deja_hm and numero else ""

        propose = nom_fige
        if service and type_doc and not propose:
            propose = nom_hm26(service, type_doc, (aaaa, mm, jj), numero, objet)

        lignes.append({
            "Action": action,
            "Nom HM26": propose,
            "Fichier origine": f["nom"],
            "Sous-dossier": f["sous_dossier"],
            "Service": service,
            "Type": type_doc,
            "Sens": info.get("sens", ""),
            "Date": f"{aaaa:04d}-{mm:02d}-{jj:02d}" if aaaa else "",
            "Date source": src_date,
            "Numero": numero,
            "Numero source": src_num,
            "Tiers": tiers,
            "Objet": objet.replace("_", " "),
            "Dossier WorkDrive": fid,
            "MD5": f["md5"],
            "Taille Ko": f"{f['taille'] / 1024:.1f}",
            "Remarque": " ; ".join(remarques),
            "_manque_numero": manque_numero,
            "_date": (aaaa, mm, jj),
            "_objet": objet,
            "_nom_fige": nom_fige,
        })

    # --- Numerotation des series incompletes ----------------------------
    if numeroter:
        attribues = numeroter_series(lignes, noms_registre)
        print(f"\n--- NUMEROTATION ---\n  {attribues} numero(s) proposes")

    # --- Verdict sur les numeros manquants ------------------------------
    noms_proposes = defaultdict(list)
    for ligne in lignes:
        if ligne["_manque_numero"] and not ligne["Numero"]:
            ligne["Remarque"] = (ligne["Remarque"] + " ; " if ligne["Remarque"] else "") \
                + "aucun numero extrait — a saisir"
            if ligne["Action"] == "indexer":
                ligne["Action"] = "a_decider"
        if ligne["Service"] and ligne["Type"]:
            ligne["Nom HM26"] = ligne["_nom_fige"] or nom_hm26(
                ligne["Service"], ligne["Type"], ligne["_date"], ligne["Numero"],
                ligne["_objet"])
            noms_proposes[ligne["Nom HM26"]].append(
                f"{ligne['Sous-dossier']}/{ligne['Fichier origine']}")
        if ligne["Nom HM26"] and ligne["Nom HM26"] in noms_registre:
            ligne["Remarque"] = (ligne["Remarque"] + " ; " if ligne["Remarque"] else "") \
                + "ce nom existe deja au registre"
            if ligne["Action"] == "indexer":
                ligne["Action"] = "a_decider"

    # Collisions de noms proposes. On nomme les concurrents : sans eux, la
    # ligne est inarbitrable — on ne sait pas si c'est une piece jointe,
    # un doublon, ou deux actes portant le meme numero.
    for propose, origines in noms_proposes.items():
        if len(origines) > 1:
            for ligne in lignes:
                if ligne["Nom HM26"] != propose:
                    continue
                moi = f"{ligne['Sous-dossier']}/{ligne['Fichier origine']}"
                autres = [o for o in origines if o != moi]
                detail = " | ".join(a[:60] for a in autres[:3])
                if len(autres) > 3:
                    detail += f" | +{len(autres) - 3}"
                ligne["Remarque"] = (ligne["Remarque"] + " ; " if ligne["Remarque"] else "") \
                    + f"collision avec : {detail}"
                if ligne["Action"] == "indexer":
                    ligne["Action"] = "a_decider"

    # Conflits de numerotation : deux documents distincts portant le meme
    # numero dans la meme serie. Les noms proposes peuvent differer (objets
    # differents) et echapper a la detection de collision ci-dessus, alors
    # que c'est l'atteinte la plus grave a l'integrite du registre.
    # On ne tranche pas : lettre + annexe partagent legitimement un numero,
    # deux actes distincts non. Seul un humain fait la difference.
    par_serie = defaultdict(list)
    for ligne in lignes:
        if (ligne["Numero"] and ligne["Service"] and ligne["Type"]
                and ligne["Numero source"] != "genere"
                and ligne["Action"] != "ignorer"):
            par_serie[(annee_courte(ligne["_date"]), ligne["Service"], ligne["Type"],
                       ligne["Numero"])].append(ligne)

    conflits_numero = 0
    for (annee_serie, service, type_doc, numero), groupe in par_serie.items():
        if len(groupe) < 2:
            continue
        conflits_numero += 1
        for ligne in groupe:
            autres = [f"{a['Sous-dossier']}/{a['Fichier origine']}"
                      for a in groupe if a is not ligne]
            detail = " | ".join(a[:55] for a in autres[:3])
            if len(autres) > 3:
                detail += f" | +{len(autres) - 3}"
            ligne["Remarque"] = (ligne["Remarque"] + " ; " if ligne["Remarque"] else "") \
                + f"NUMERO HM{annee_serie}-{service}-{type_doc} {numero} partage avec : {detail}"
            if ligne["Action"] == "indexer":
                ligne["Action"] = "a_decider"

    doublons = detecter_doublons(fichiers)
    for d in doublons:
        if d["niveau"] in ("1-CONTENU", "2-NOM"):
            for nom in d["ecartes"]:
                for ligne in lignes:
                    if ligne["Fichier origine"] == nom and \
                            ligne["Sous-dossier"] == d["sous_dossier_ecarte"].get(nom, ""):
                        ligne["Action"] = "ignorer"
                        ligne["Remarque"] = (ligne["Remarque"] + " ; " if ligne["Remarque"] else "") \
                            + f"doublon {d['niveau']} de {d['conserve']}"

    with open(PROPOSITIONS, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLONNES_PROPOSITIONS, delimiter=";",
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(lignes)

    with open(DOUBLONS, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Niveau", "Cle", "Conserve", "Ecartes", "Decision"])
        for d in doublons:
            w.writerow([d["niveau"], d["cle"], d["conserve"],
                        " | ".join(d["ecartes"]), d["decision"]])

    par_action = defaultdict(int)
    for ligne in lignes:
        par_action[ligne["Action"]] += 1

    print("\n--- PROPOSITIONS ---")
    for action in sorted(par_action):
        print(f"  {action:12} {par_action[action]:>4}")

    sources_num = defaultdict(int)
    for ligne in lignes:
        if ligne["Numero source"]:
            sources_num[ligne["Numero source"]] += 1
    if sources_num:
        print("\n--- NUMEROS EXTRAITS ---")
        for src in sorted(sources_num):
            print(f"  {src:14} {sources_num[src]:>4}")

    print(f"\n  {PROPOSITIONS.name}  {len(lignes)} ligne(s)")
    print(f"  {DOUBLONS.name}  {len(doublons)} anomalie(s)")
    if conflits_numero:
        print(f"\n  (!) {conflits_numero} numero(s) porte(s) par plusieurs documents.")
        print("      Cherche NUMERO dans la colonne Remarque : piece jointe")
        print("      legitime, ou deux actes a renumeroter ?")

    print("\n--- CORRESPONDANCE DES COLONNES DU REGISTRE ---")
    mapping = mapper_colonnes(verbeux=True)
    if not mapping:
        print("  (registre_2026.csv absent : la passe 3 utilisera le schema par defaut)")

    print(f"\nOuvre {PROPOSITIONS.name} dans LibreOffice (separateur point-virgule).")
    print("Corrige la colonne Action et la colonne Nom HM26, puis --appliquer.")
    print("Rien n'a ete envoye ni renomme.")


def detecter_doublons(fichiers):
    """Doublons MD5 et nom dans le lot local."""
    rapport = []
    par_md5 = defaultdict(list)
    par_nom = defaultdict(list)

    for f in fichiers:
        par_md5[f["md5"]].append(f)
        par_nom[f["nom"]].append(f)

    deja = set()

    for md5, groupe in par_md5.items():
        if len(groupe) > 1:
            conserve = groupe[0]
            ecartes = groupe[1:]
            for f in ecartes:
                deja.add((f["nom"], f["sous_dossier"]))
            rapport.append({
                "niveau": "1-CONTENU", "cle": md5[:12],
                "conserve": f"{conserve['sous_dossier']}/{conserve['nom']}",
                "ecartes": [f["nom"] for f in ecartes],
                "sous_dossier_ecarte": {f["nom"]: f["sous_dossier"] for f in ecartes},
                "decision": "Premier conserve",
            })

    for nom, groupe in par_nom.items():
        if len(groupe) > 1:
            conserve = groupe[0]
            ecartes = [f for f in groupe[1:]
                       if (f["nom"], f["sous_dossier"]) not in deja]
            if not ecartes:
                continue
            rapport.append({
                "niveau": "2-NOM", "cle": nom,
                "conserve": f"{conserve['sous_dossier']}/{conserve['nom']}",
                "ecartes": [f["nom"] for f in ecartes],
                "sous_dossier_ecarte": {f["nom"]: f["sous_dossier"] for f in ecartes},
                "decision": "Premier conserve",
            })

    return rapport


# ===========================================================================
# Registre
# ===========================================================================
SYNONYMES = {
    "nom_fichier": ["nom du fichier", "nom fichier", "fichier", "document"],
    "service": ["service", "direction", "entite"],
    "type_doc": ["type", "type de document", "nature"],
    "date": ["date", "date du document", "date document"],
    "ile": ["ile"],
    "objet": ["objet", "sujet", "intitule", "libelle"],
    "branche": ["branche ged", "branche"],
    "emplacement": ["emplacement physique", "emplacement"],
    "statut": ["statut", "etat"],
    "lien": ["lien zoho", "lien", "url", "permalink"],
    "numero": ["n", "no", "num", "numero", "numero d enregistrement"],
    "sens": ["sens", "flux"],
    "expediteur": ["expediteur", "emetteur", "provenance"],
    "destinataire": ["destinataire"],
    "deposant": ["deposant", "depose par", "agent"],
    "observations": ["observations", "observation", "remarques", "suites a donner"],
}

SCHEMA_DEFAUT = [
    ("Nom du fichier", "nom_fichier"), ("Service", "service"), ("Type", "type_doc"),
    ("Date", "date"), ("Île", "ile"), ("Objet", "objet"),
    ("Branche GED", "branche"), ("Emplacement physique", "emplacement"),
    ("Statut", "statut"), ("Lien Zoho", "lien"),
]


def mapper_colonnes(verbeux=False):
    """Associe les en-tetes reels du registre aux champs du script."""
    if not REGISTRE.exists():
        return []

    with open(REGISTRE, "r", encoding="utf-8-sig") as f:
        entetes = next(csv.reader(f, delimiter=";"), [])

    if not entetes:
        return []

    inverse = {}
    for champ, variantes in SYNONYMES.items():
        for v in variantes:
            inverse[v] = champ

    mapping = []
    for entete in entetes:
        cle = normaliser(entete)
        champ = inverse.get(cle, "")
        mapping.append((entete, champ))
        if verbeux:
            etat = champ or "(non renseigne)"
            print(f"  {entete:28} -> {etat}")

    return mapping


def charger_noms_registre():
    if not REGISTRE.exists():
        return set()
    noms = set()
    with open(REGISTRE, "r", encoding="utf-8-sig") as f:
        for i, ligne in enumerate(csv.reader(f, delimiter=";")):
            if i == 0 or not ligne or not ligne[0]:
                continue
            noms.add(ligne[0].strip())
    return noms


def valeurs_registre(ligne, rid, deposant):
    """Construit le dict de la ligne de registre d'apres les en-tetes reels."""
    service = ligne["Service"]
    type_doc = ligne["Type"]
    sens = ligne.get("Sens", "")
    tiers = ligne.get("Tiers", "")

    champs = {
        "nom_fichier": ligne["Nom HM26"],
        "service": SERVICES.get(service, service),
        "type_doc": TYPES.get(type_doc, type_doc),
        # Une date incomplete (2026-01-00) n'est pas une date pour le
        # tableur : elle reste lisible dans le nom du fichier.
        "date": "" if (ligne.get("Date", "") or "").endswith("-00") else ligne.get("Date", ""),
        "ile": "",
        "objet": ligne.get("Objet", ""),
        "branche": BRANCHES.get(type_doc) or BRANCHES_SERVICE.get(service, "Divers"),
        "emplacement": "Zoho WorkDrive",
        "statut": "Classé",
        "lien": f"https://workdrive.zoho.com/file/{rid}" if rid else "",
        "numero": ligne.get("Numero", ""),
        "sens": sens,
        "expediteur": tiers if sens == "entrant" else "",
        "destinataire": tiers if sens == "sortant" else "",
        "deposant": deposant,
        "observations": "Import de masse — archives anterieures",
    }

    mapping = mapper_colonnes()
    if not mapping:
        mapping = SCHEMA_DEFAUT

    return {entete: champs.get(champ, "") for entete, champ in mapping}


# ===========================================================================
# WorkDrive
# ===========================================================================
def connecteur():
    from zoho_connector import ZohoConnector
    return ZohoConnector()


def entetes(zc):
    if hasattr(zc, "_headers") and callable(zc._headers):
        try:
            return zc._headers()
        except Exception:  # noqa: BLE001
            pass
    return {"Authorization": f"Zoho-oauthtoken {zc._token()}"}


def base_workdrive():
    try:
        from zoho_config import WORKDRIVE_API
        return str(WORKDRIVE_API).rstrip("/")
    except ImportError:
        return "https://www.zohoapis.com/workdrive/api/v1"


def lister_dossier(zc, folder_id):
    """{nom: resource_id} des fichiers d'un dossier WorkDrive, et un booleen
    qui dit si le listage est complet."""
    noms = {}
    offset = 0
    limite = 50
    while True:
        url = base_workdrive() + EP_FICHIERS.format(fid=folder_id)
        try:
            r = requests.get(url, headers=entetes(zc),
                             params={"page[limit]": limite, "page[offset]": offset},
                             timeout=40)
        except Exception:  # noqa: BLE001
            return noms, False
        if r.status_code != 200:
            return noms, False
        try:
            lot = r.json().get("data", [])
        except ValueError:
            return noms, False
        if not lot:
            break
        for item in lot:
            attrs = item.get("attributes", {}) or {}
            nom = attrs.get("name") or attrs.get("FileName") or ""
            if nom:
                noms[nom] = item.get("id") or attrs.get("resource_id") or ""
        if len(lot) < limite:
            break
        offset += limite
        time.sleep(0.2)
    return noms, True


def uploader(zc, chemin, nom, folder_id, ecraser=False):
    """POST direct : workdrive_upload du connecteur ne passe pas
    override-name-exist, et WorkDrive renomme alors en 'fichier-1.pdf'.
    Son raise_for_status() avale aussi le corps de la reponse d'erreur."""
    url = base_workdrive() + "/upload"
    try:
        with open(chemin, "rb") as flux:
            r = requests.post(
                url,
                headers=entetes(zc),
                files={"content": (nom, flux, "application/pdf")},
                data={
                    "filename": nom,
                    "parent_id": folder_id,
                    "override-name-exist": "true" if ecraser else "false",
                },
                timeout=180,
            )
    except Exception as exc:  # noqa: BLE001
        return False, "", f"appel impossible : {exc}"

    if r.status_code not in (200, 201):
        return False, "", f"HTTP {r.status_code} {r.text[:300]}"

    try:
        payload = r.json()
    except ValueError:
        return False, "", f"reponse illisible : {r.text[:200]}"

    try:
        data = payload.get("data")
        premier = data[0] if isinstance(data, list) and data else data or {}
        attrs = premier.get("attributes", premier) or {}
        rid = (attrs.get("resource_id") or attrs.get("ResourceId")
               or premier.get("id") or "")
        nom_reel = attrs.get("FileName") or attrs.get("name") or nom
    except (AttributeError, IndexError, TypeError):
        return False, "", f"reponse inattendue : {str(payload)[:250]}"

    if not rid:
        return False, "", f"resource_id absent : {str(payload)[:250]}"
    if nom_reel != nom:
        return True, rid, f"ATTENTION WorkDrive a renomme en {nom_reel}"
    return True, rid, ""


# ===========================================================================
# Moteur de recherche de la GED
# ===========================================================================
SOURCE_RECHERCHE = "migration des archives"


def module_recherche():
    """recherche.py (a cote de app.py), ou None s'il est absent."""
    try:
        import recherche  # noqa: PLC0415
        return recherche
    except Exception as exc:  # noqa: BLE001
        print(f"  (moteur de recherche indisponible : {exc})")
        return None


def _date_lisible(date):
    """2026-05-00 -> 2026-05 ; 2026-00-00 -> 2026."""
    if not date:
        return ""
    if date.endswith("-00-00"):
        return date[:4]
    if date.endswith("-00"):
        return date[:7]
    return date


def document_recherche(ligne, chemin, rid=""):
    """Fiche du moteur de recherche pour une ligne de propositions.csv.

    L'ancien nom et le chemin des dossiers deviennent des mots-cles : on
    retrouve ainsi un document par ce que l'equipe connait deja
    (« CS-REF-009 », « Courrier sotants/Exim bank »).
    """
    service, type_doc = ligne.get("Service", ""), ligne.get("Type", "")
    date = _date_lisible(ligne.get("Date", ""))
    return {
        "nom": ligne.get("Nom HM26") or ligne.get("Fichier origine"),
        "service": SERVICES.get(service, service),
        "type_doc": TYPES.get(type_doc, type_doc),
        "date": date,
        "annee": date[:4],
        "sens": {"entrant": "Entrant", "sortant": "Sortant"}.get(ligne.get("Sens", ""), ""),
        "correspondant": ligne.get("Tiers", ""),
        "objet": ligne.get("Objet", ""),
        "lien": f"https://workdrive.zoho.com/file/{rid}" if rid else "",
        "source": SOURCE_RECHERCHE,
        "origine": ligne.get("Fichier origine", ""),
        "chemin": str(chemin),
        "md5": ligne.get("MD5", ""),
        "mots_cles": " ".join([Path(ligne.get("Fichier origine", "")).stem,
                               ligne.get("Sous-dossier", "").replace("/", " ")]),
    }


def _textes_deja_lus(rech):
    """Noms deja indexes AVEC leur texte : l'OCR ne sera pas refait."""
    try:
        c = rech.connexion()
        noms = {r[0] for r in c.execute("SELECT nom FROM documents WHERE texte <> ''")}
        c.close()
        return noms
    except Exception:  # noqa: BLE001
        return set()


def _liens_journal():
    """resource_id WorkDrive deja obtenus, par fichier d'origine.
    Les envois annules (--annuler-envoi) sont ecartes : leur fichier est a la
    corbeille de WorkDrive."""
    liens, annules = {}, set()
    if JOURNAL.exists():
        with open(JOURNAL, "r", encoding="utf-8-sig") as f:
            lignes = list(csv.DictReader(f, delimiter=";"))
        annules = {l["resource_id"] for l in lignes
                   if l.get("Action") == "annule" and l.get("resource_id")}
        for l in lignes:
            rid = l.get("resource_id")
            if rid and l.get("Action") != "annule" and rid not in annules:
                liens[l.get("Fichier origine", "")] = rid
    return liens


def indexer_recherche(source, args):
    """Verse propositions.csv dans le moteur de recherche, sans rien uploader
    ni ecrire au registre."""
    racine = Path(source).expanduser().resolve()
    if not PROPOSITIONS.exists():
        print(f"ERREUR : {PROPOSITIONS.name} introuvable. Lance --proposer.")
        return
    rech = module_recherche()
    if rech is None:
        return
    avec_texte = "--sans-texte" not in args
    limite = valeur_option(args, "--limite")
    limite = int(limite) if limite and limite.isdigit() else None

    # --tout : les lignes « a_decider » aussi. Elles entrent dans la
    # recherche sous leur nom d'origine (leur nom HM n'est pas encore valide),
    # marquees « pas encore en ligne ».
    actions = ("indexer", "renommer", "a_decider") if "--tout" in args \
        else ("indexer", "renommer")
    with open(PROPOSITIONS, "r", encoding="utf-8-sig") as f:
        lignes = [l for l in csv.DictReader(f, delimiter=";")
                  if (l.get("Action") or "").strip().lower() in actions]
    if limite:
        lignes = lignes[:limite]

    deja = _textes_deja_lus(rech) if avec_texte else set()
    liens = _liens_journal()
    print(f"\n=== MOTEUR DE RECHERCHE ===\n")
    print(f"Lignes retenues ({', '.join(actions)}) : {len(lignes)}")
    print(f"Texte des PDF : {'oui (OCR des scans, patience)' if avec_texte else 'non'}")
    print(f"Index : {rech.F_INDEX}\n")

    n_ok = n_absent = 0
    for i, ligne in enumerate(lignes, 1):
        dossier = racine / ligne.get("Sous-dossier", "")
        chemin = dossier / (ligne.get("Nom HM26") or "")
        if not chemin.is_file():
            chemin = dossier / ligne.get("Fichier origine", "")
        if not chemin.is_file():
            n_absent += 1
            continue
        doc = document_recherche(ligne, chemin, liens.get(ligne.get("Fichier origine", ""), ""))
        if (ligne.get("Action") or "").strip().lower() == "a_decider":
            doc["nom"] = ligne.get("Fichier origine", doc["nom"])
            doc["mots_cles"] += " " + (ligne.get("Nom HM26") or "")
        lire = avec_texte and doc["nom"] not in deja
        rech.indexer_fichier(doc, chemin if lire else None, en_tache=False)
        n_ok += 1
        print(f"  [{i}/{len(lignes)}] {'T ' if lire else '  '}{doc['nom'][:70]}")

    # Fiches de migration qui ne correspondent plus a aucune ligne de
    # propositions.csv (nom corrige depuis) : on les retire de l'index.
    n_purge = 0
    if not limite and hasattr(rech, "purger_source"):
        with open(PROPOSITIONS, "r", encoding="utf-8-sig") as f:
            tous = list(csv.DictReader(f, delimiter=";"))
        valides = {l.get("Nom HM26") for l in tous} | {l.get("Fichier origine") for l in tous}
        n_purge = rech.purger_source(SOURCE_RECHERCHE, valides - {"", None})

    print(f"\n  indexes      : {n_ok}")
    print(f"  introuvables : {n_absent}")
    if n_purge:
        print(f"  anciennes fiches retirees : {n_purge}")
    print("\nPour l'envoyer sur le serveur :")
    print("  ./venv/bin/python recherche.py --exporter index_ged.jsonl")


# ===========================================================================
# PASSE 3 — application
# ===========================================================================
# Coupures reseau : on attend et on reessaie, puis on arrete le passage
# proprement plutot que d'enchainer des dizaines d'echecs.
PAUSE_RESEAU = 20
ESSAIS_RESEAU = 3
COUPURES_AVANT_ARRET = 3


def erreur_reseau(message):
    m = str(message).lower()
    return any(k in m for k in ("appel impossible", "nameresolution", "failed to resolve",
                                "connection aborted", "connectionerror", "max retries",
                                "timed out", "temporary failure", "remote end closed",
                                "network is unreachable"))


def envoyer_avec_reprise(zc, chemin, nom, fid, ecraser, distant, prefixe):
    """Upload avec reprise. Apres une coupure, le dossier est relu avant de
    renvoyer : un envoi parti malgre le timeout n'est pas refait (sinon
    WorkDrive creerait « nom(1).pdf »)."""
    succes, rid, msg = uploader(zc, chemin, nom, fid, ecraser=ecraser)
    essai = 1
    while not succes and erreur_reseau(msg) and essai < ESSAIS_RESEAU:
        print(f"{prefixe} reseau coupe, nouvel essai dans {PAUSE_RESEAU} s…")
        time.sleep(PAUSE_RESEAU)
        essai += 1
        noms, ok = lister_dossier(zc, fid)
        if ok:
            distant[fid] = noms
            if nom in noms:
                return True, noms[nom], "envoye malgre la coupure (retrouve dans WorkDrive)"
        succes, rid, msg = uploader(zc, chemin, nom, fid, ecraser=ecraser)
    return succes, rid, msg


def inscrire_avec_reprise(zc, donnees, prefixe):
    """Ligne de registre avec reprise sur coupure. Leve la derniere erreur."""
    for essai in range(1, ESSAIS_RESEAU + 1):
        try:
            return zc.sheet_add_row(WORKBOOK_ID, FEUILLE, donnees)
        except Exception as exc:  # noqa: BLE001
            if essai == ESSAIS_RESEAU or not erreur_reseau(exc):
                raise
            print(f"{prefixe} registre injoignable, nouvel essai dans {PAUSE_RESEAU} s…")
            time.sleep(PAUSE_RESEAU)


class TraceCSV(list):
    """Liste dont chaque ajout est aussitot ecrit en fin de fichier CSV."""

    def __init__(self, chemin, entete):
        super().__init__()
        self.chemin, self.entete = chemin, entete

    def append(self, ligne):
        super().append(ligne)
        nouveau = not self.chemin.exists()
        with open(self.chemin, "a", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh, delimiter=";")
            if nouveau:
                w.writerow(self.entete)
            w.writerow(ligne)


def appliquer(source, args):
    racine = Path(source).expanduser().resolve()

    if not PROPOSITIONS.exists():
        print(f"ERREUR : {PROPOSITIONS.name} introuvable. Lance --proposer.")
        sys.exit(1)

    limite = valeur_option(args, "--limite")
    limite = int(limite) if limite and limite.isdigit() else None
    forcer = "--forcer" in args
    ecraser = "--ecraser" in args
    sans_renommer = "--sans-renommer" in args
    sans_upload = "--sans-upload" in args
    sans_index = "--sans-index" in args
    sans_texte = "--sans-texte" in args
    deposant = valeur_option(args, "--deposant", "Import de masse")
    rech = module_recherche()

    with open(PROPOSITIONS, "r", encoding="utf-8-sig") as f:
        lignes = list(csv.DictReader(f, delimiter=";"))

    print(f"\n=== PASSE 3 — APPLICATION ===\n")
    print(f"Source : {racine}")
    print(f"{PROPOSITIONS.name} : {len(lignes)} ligne(s)")

    a_indexer, a_renommer, refusees = [], [], []
    deja_faits = 0
    noms_registre = charger_noms_registre()
    liens = _liens_journal()

    for ligne in lignes:
        action = (ligne.get("Action") or "").strip().lower()
        if action == "ignorer":
            continue
        if action == "a_decider":
            refusees.append(ligne)
            continue

        chemin = racine / ligne["Sous-dossier"] / ligne["Fichier origine"]
        nom_hm = (ligne.get("Nom HM26") or "").strip()
        if not chemin.exists() and nom_hm and nom_hm != ligne["Fichier origine"] \
                and (racine / ligne["Sous-dossier"] / nom_hm).exists():
            # Deja renomme par un passage precedent (eventuellement coupe).
            if nom_hm in noms_registre or action != "indexer":
                deja_faits += 1
                continue
            # Renomme et envoye, mais la ligne de registre a echoue : on
            # la rattrape, sans renvoyer le fichier.
            ligne["_chemin"] = racine / ligne["Sous-dossier"] / nom_hm
            ligne["_cible"] = nom_hm
            ligne["_rid"] = liens.get(ligne["Fichier origine"], "")
            ligne["_rattrapage"] = True
            a_indexer.append(ligne)
            continue
        if not chemin.exists():
            ligne["_erreur"] = "fichier introuvable sur le disque"
            refusees.append(ligne)
            continue

        nom_cible = (ligne.get("Nom HM26") or "").strip()
        if MOTIF_HM26.match(ligne["Fichier origine"]) and nom_cible != ligne["Fichier origine"]:
            ligne["_erreur"] = ("deja au format HM, nom propose different : "
                                "propositions.csv perime, relance --proposer")
            refusees.append(ligne)
            continue
        if not MOTIF_HM26.match(nom_cible):
            ligne["_erreur"] = f"nom non conforme a la norme : {nom_cible}"
            refusees.append(ligne)
            continue
        if "_XXX_" in nom_cible:
            ligne["_erreur"] = "numero XXX non remplace"
            refusees.append(ligne)
            continue

        ligne["_chemin"] = chemin
        ligne["_cible"] = nom_cible

        if action == "indexer":
            a_indexer.append(ligne)
        elif action == "renommer":
            a_renommer.append(ligne)
        else:
            ligne["_erreur"] = f"action inconnue : {action}"
            refusees.append(ligne)

    if limite:
        a_indexer = a_indexer[:limite]
        a_renommer = a_renommer[:limite]

    n_rattrapage = sum(1 for l in a_indexer if l.get("_rattrapage"))
    print(f"\n  a indexer   {len(a_indexer) - n_rattrapage:>4}")
    if n_rattrapage:
        print(f"  a completer {n_rattrapage:>4}   (deja envoyes, ligne de registre manquante)")
    print(f"  a renommer  {len(a_renommer):>4}")
    if deja_faits:
        print(f"  deja faits  {deja_faits:>4}   (renommes et envoyes par un passage precedent)")
    print(f"  refusees    {len(refusees):>4}")

    if refusees:
        print("\n--- LIGNES REFUSEES (echantillon) ---")
        for ligne in refusees[:8]:
            motif = ligne.get("_erreur") or ligne.get("Remarque") or "action a_decider"
            print(f"  {ligne['Fichier origine'][:48]:48} {motif[:60]}")
        if len(refusees) > 8:
            print(f"  … et {len(refusees) - 8} autre(s)")

    if not a_indexer and not a_renommer:
        print("\nRien a appliquer.")
        return

    # Controle des doublons cote WorkDrive
    index_distant = {}
    if a_indexer and not sans_upload:
        try:
            zc = connecteur()
        except Exception as exc:  # noqa: BLE001
            print(f"\nERREUR de connexion Zoho : {exc}")
            return
        dossiers = {l["Dossier WorkDrive"] for l in a_indexer if l["Dossier WorkDrive"]}
        print(f"\nRelevé de {len(dossiers)} dossier(s) WorkDrive")
        echec_listage = False
        for fid in sorted(dossiers):
            noms, ok = lister_dossier(zc, fid)
            index_distant[fid] = noms
            if not ok:
                echec_listage = True
            print(f"  {fid[:18]}… {len(noms)} fichier(s) {'' if ok else '(LISTAGE ECHOUE)'}")
        if echec_listage and not forcer:
            print("\nREFUS : listage WorkDrive en echec. Sans lui, WorkDrive")
            print("creera des 'fichier-1.pdf' sans le dire. Corrige l'endpoint,")
            print("ou --forcer en connaissance de cause.")
            return
    else:
        zc = None
        if a_indexer and not sans_index:
            try:
                zc = connecteur()
            except Exception as exc:  # noqa: BLE001
                print(f"\nERREUR de connexion Zoho : {exc}")
                return

    print(f"\n  renommage local : {'non' if sans_renommer else 'oui'}")
    print(f"  upload          : {'non' if sans_upload else 'oui'}")
    print(f"  indexation      : {'non' if sans_index else 'oui'}")
    print(f"  ecrasement      : {'OUI' if ecraser else 'non'}")
    print(f"  recherche       : {'non (recherche.py absent)' if rech is None else ('oui, sans texte' if sans_texte else 'oui, texte compris')}")

    if not forcer:
        total = len(a_indexer) + len(a_renommer)
        rep = input(f"\nAppliquer sur {total} fichier(s) ? (oui/non) ").strip().lower()
        if rep not in ("oui", "o", "yes", "y"):
            print("Annule. Rien n'a ete fait.")
            return

    horodatage = time.strftime("%Y-%m-%d %H:%M:%S")
    # Ecrits ligne par ligne : une coupure (reseau, Ctrl+C, batterie) au
    # 60e fichier ne fait pas perdre la trace des 59 premiers.
    journal = TraceCSV(JOURNAL, ["Horodatage", "Fichier origine", "Action",
                                 "resource_id", "Upload", "Index", "Message"])
    renommages = TraceCSV(RENOMMAGES, ["Horodatage", "Ancien chemin", "Nouveau chemin"])
    n_renomme = n_upload = n_index = n_echec = 0
    coupures = 0
    arret_reseau = False

    # --- Renommage seul -------------------------------------------------
    for i, ligne in enumerate(a_renommer, 1):
        chemin = ligne["_chemin"]
        cible = chemin.parent / ligne["_cible"]
        if cible.exists():
            print(f"  [R{i}] collision, saute : {ligne['_cible'][:55]}")
            journal.append([horodatage, ligne["Fichier origine"], "renommer", "",
                            "ECHEC", "", "nom cible deja present"])
            n_echec += 1
            continue
        try:
            chemin.rename(cible)
            renommages.append([horodatage, str(chemin), str(cible)])
            n_renomme += 1
            print(f"  [R{i}/{len(a_renommer)}] {ligne['_cible'][:62]}")
            if rech is not None:
                try:
                    rech.indexer_fichier(document_recherche(ligne, cible),
                                         None if sans_texte else cible, en_tache=False)
                except Exception as exc:  # noqa: BLE001
                    print(f"      (recherche : {exc})")
        except OSError as exc:
            n_echec += 1
            print(f"  [R{i}] ECHEC renommage : {exc}")
            journal.append([horodatage, ligne["Fichier origine"], "renommer", "",
                            "ECHEC", "", str(exc)[:120]])

    # --- Indexation complete --------------------------------------------
    for i, ligne in enumerate(a_indexer, 1):
        prefixe = f"  [{i}/{len(a_indexer)}]"
        chemin = ligne["_chemin"]
        cible_nom = ligne["_cible"]
        fid = ligne["Dossier WorkDrive"]
        rid = ""
        msg = ""
        etat_up = etat_idx = "saute"

        distant = index_distant.get(fid, {})
        if ligne.get("_rattrapage"):
            # Fichier deja envoye : on retrouve son lien, on ne le renvoie pas.
            rid = ligne.get("_rid") or distant.get(cible_nom, "")
            etat_up = "deja"
        elif not sans_upload:
            if cible_nom in distant and not ecraser:
                # Envoye par un passage coupe avant d'avoir tout consigne :
                # on reprend son lien et on termine le travail.
                rid = distant.get(cible_nom, "")
                etat_up = "deja"
                msg = "deja dans WorkDrive, lien repris"
            else:
                succes, rid, msg = envoyer_avec_reprise(zc, chemin, cible_nom, fid,
                                                        ecraser, index_distant, prefixe)
                etat_up = "ok" if succes else "ECHEC"
                if not succes:
                    n_echec += 1
                    print(f"{prefixe} ECHEC upload {cible_nom[:42]} — {msg}")
                    journal.append([horodatage, ligne["Fichier origine"], "indexer", "",
                                    etat_up, etat_idx, msg])
                    time.sleep(PAUSE_UPLOAD)
                    if erreur_reseau(msg):
                        coupures += 1
                        if coupures >= COUPURES_AVANT_ARRET:
                            arret_reseau = True
                            break
                    continue
                n_upload += 1
                coupures = 0
                time.sleep(PAUSE_UPLOAD)

        if not sans_index and cible_nom in noms_registre:
            etat_idx = "deja"
        elif not sans_index:
            donnees = valeurs_registre(ligne, rid, deposant)
            try:
                inscrire_avec_reprise(zc, donnees, prefixe)
                etat_idx = "ok"
                n_index += 1
                noms_registre.add(cible_nom)
            except Exception as exc:  # noqa: BLE001
                etat_idx = "ECHEC"
                n_echec += 1
                msg = (msg + " / " + str(exc)[:150]).strip(" /")
                print(f"{prefixe} ECHEC registre {cible_nom[:42]} — {exc}")
                # Fichier envoye mais pas inscrit : on garde son ancien nom,
                # la prochaine relance reprendra son lien et l'inscrira.
                journal.append([horodatage, ligne["Fichier origine"], "indexer", rid,
                                etat_up, etat_idx, msg])
                if erreur_reseau(exc):
                    coupures += 1
                    if coupures >= COUPURES_AVANT_ARRET:
                        arret_reseau = True
                        break
                continue
            else:
                try:
                    nouveau = not REGISTRE.exists()
                    with open(REGISTRE, "a", encoding="utf-8-sig", newline="") as fh:
                        w = csv.writer(fh, delimiter=";")
                        if nouveau:
                            w.writerow(list(donnees.keys()))
                        w.writerow(list(donnees.values()))
                except OSError as exc:
                    msg = (msg + f" / miroir CSV : {exc}").strip(" /")
            time.sleep(PAUSE_SHEET)

        if not sans_renommer:
            cible = chemin.parent / cible_nom
            if not cible.exists():
                try:
                    chemin.rename(cible)
                    renommages.append([horodatage, str(chemin), str(cible)])
                    n_renomme += 1
                except OSError as exc:
                    msg = (msg + f" / renommage : {exc}").strip(" /")

        if rech is not None:
            emplacement = chemin.parent / cible_nom
            if not emplacement.exists():
                emplacement = chemin
            try:
                rech.indexer_fichier(document_recherche(ligne, emplacement, rid),
                                     None if sans_texte else emplacement, en_tache=False)
            except Exception as exc:  # noqa: BLE001
                msg = (msg + f" / recherche : {exc}").strip(" /")

        print(f"{prefixe} {cible_nom[:58]}" + (f" — {msg}" if msg else ""))
        journal.append([horodatage, ligne["Fichier origine"], "indexer", rid,
                        etat_up, etat_idx, msg])

    print("\n" + "=" * 52)
    if arret_reseau:
        print("  ARRET : connexion perdue plusieurs fois de suite.")
        print("  Rien n'est perdu. Relance la meme commande quand le reseau revient :")
        print("  elle reprendra la ou elle s'est arretee.\n")
    print(f"  renommages    : {n_renomme}")
    print(f"  uploads       : {n_upload}")
    print(f"  lignes ajoutees au registre : {n_index}")
    print(f"  echecs        : {n_echec}")
    print(f"  journal       : {JOURNAL.name}")
    if renommages:
        print(f"  renommages consignes dans {RENOMMAGES.name}")
        print(f"  annulation : --annuler-renommage")


# ===========================================================================
# Annulation du renommage
# ===========================================================================
def annuler_renommage():
    if not RENOMMAGES.exists():
        print(f"{RENOMMAGES.name} introuvable : aucun renommage a annuler.")
        return

    with open(RENOMMAGES, "r", encoding="utf-8-sig") as f:
        lignes = [l for l in csv.reader(f, delimiter=";")][1:]

    print(f"\n{len(lignes)} renommage(s) consigne(s).")
    rep = input("Tout remettre aux noms d'origine ? (oui/non) ").strip().lower()
    if rep not in ("oui", "o"):
        print("Annule.")
        return

    ok = echec = 0
    for ligne in reversed(lignes):
        if len(ligne) < 3:
            continue
        ancien, nouveau = Path(ligne[1]), Path(ligne[2])
        if not nouveau.exists():
            echec += 1
            continue
        if ancien.exists():
            echec += 1
            print(f"  l'ancien nom est repris, saute : {ancien.name[:55]}")
            continue
        try:
            nouveau.rename(ancien)
            ok += 1
        except OSError as exc:
            echec += 1
            print(f"  ECHEC : {exc}")

    print(f"\n  restaures : {ok}")
    print(f"  non traites : {echec}")
    print("  Les lignes du registre et les fichiers WorkDrive ne sont PAS touches.")


# ===========================================================================
# Annulation d'un passage complet (envoi WorkDrive + registre + renommage)
# ===========================================================================
def _passages():
    """Horodatages des passages --appliquer, avec leur nombre d'envois."""
    if not JOURNAL.exists():
        return []
    with open(JOURNAL, "r", encoding="utf-8-sig") as f:
        lignes = list(csv.DictReader(f, delimiter=";"))
    annules = {l["resource_id"] for l in lignes if l.get("Action") == "annule"}
    compte = defaultdict(int)
    for l in lignes:
        if l.get("Action") != "annule" and l.get("resource_id") \
                and l["resource_id"] not in annules:
            compte[l["Horodatage"]] += 1
    return sorted(compte.items())


def corbeille_workdrive(zc, rid):
    """Envoie un fichier WorkDrive a la corbeille (restaurable 30 jours)."""
    url = f"{base_workdrive()}/files/{rid}"
    corps = {"data": {"attributes": {"status": "51"}, "type": "files"}}
    h = dict(entetes(zc))
    h.update({"Content-Type": "application/json", "Accept": "application/vnd.api+json"})
    try:
        r = requests.patch(url, headers=h, json=corps, timeout=40)
    except Exception as exc:  # noqa: BLE001
        return False, f"appel impossible : {exc}"
    if r.status_code in (200, 202, 204):
        return True, ""
    return False, f"HTTP {r.status_code} {r.text[:200]}"


def supprimer_ligne_sheet(zc, colonne, valeur):
    """Supprime la ligne du registre Zoho Sheet dont `colonne` vaut `valeur`."""
    url = (f"https://sheet.zoho.com/api/v2/{WORKBOOK_ID}"
           f"?method=worksheet.records.delete")
    h = dict(entetes(zc))
    h["Content-Type"] = "application/x-www-form-urlencoded"
    donnees = {
        "worksheet_name": FEUILLE,
        "criteria": f'"{colonne}"="{valeur}"',
        "first_match_only": "true",
        "delete_rows": "true",
    }
    try:
        r = requests.post(url, headers=h, data=donnees, timeout=40)
        corps = r.json()
    except Exception as exc:  # noqa: BLE001
        return False, f"appel impossible : {exc}"
    if r.status_code == 200 and corps.get("status") == "success":
        return True, ""
    return False, str(corps)[:200]


def annuler_envoi(horodatage, args):
    """Defait un passage --appliquer : fichiers WorkDrive a la corbeille,
    lignes retirees du registre (Sheet et CSV), anciens noms remis sur le
    poste, fiches retirees du moteur de recherche."""
    if not horodatage:
        passages = _passages()
        print("Passages --appliquer enregistres (horodatage, envois) :")
        for h, n in passages:
            print(f"  {h}   {n} envoi(s)")
        if passages:
            print(f'\nExemple : --annuler-envoi "{passages[-1][0]}"')
        return

    with open(JOURNAL, "r", encoding="utf-8-sig") as f:
        journal = list(csv.DictReader(f, delimiter=";"))
    deja = {l["resource_id"] for l in journal if l.get("Action") == "annule"}
    envois = [l for l in journal if l.get("Horodatage") == horodatage
              and l.get("Action") != "annule" and l.get("resource_id")
              and l["resource_id"] not in deja]

    renoms = []
    if RENOMMAGES.exists():
        with open(RENOMMAGES, "r", encoding="utf-8-sig") as f:
            renoms = [l for l in csv.reader(f, delimiter=";")][1:]
    a_restaurer = [l for l in renoms if len(l) >= 3 and l[0] == horodatage]

    if not envois and not a_restaurer:
        print(f"Rien a annuler pour {horodatage}.")
        print("Lance --annuler-envoi sans valeur pour voir les passages.")
        return

    # Noms publies : ceux du registre local pour ces resource_id
    rids = {l["resource_id"] for l in envois}
    noms_publies = set()
    lignes_registre, entete = [], []
    if REGISTRE.exists():
        with open(REGISTRE, "r", encoding="utf-8-sig") as f:
            tout = list(csv.reader(f, delimiter=";"))
        entete, lignes_registre = (tout[0], tout[1:]) if tout else ([], [])
        for l in lignes_registre:
            if any(rid in ";".join(l) for rid in rids):
                noms_publies.add(l[0])
    noms_publies |= {Path(l[2]).name for l in a_restaurer}

    mapping = mapper_colonnes() or SCHEMA_DEFAUT
    col_lien = next((e for e, c in mapping if c == "lien"), "Lien Zoho")

    print(f"\n=== ANNULATION DU PASSAGE {horodatage} ===\n")
    print(f"  fichiers WorkDrive a mettre a la corbeille : {len(envois)}")
    print(f"  lignes du registre a retirer               : {len(envois)}")
    print(f"  fichiers a remettre a leur ancien nom      : {len(a_restaurer)}")
    for nom in sorted(noms_publies):
        print(f"    - {nom[:90]}")

    if "--forcer" not in args:
        rep = input("\nAnnuler ce passage ? (oui/non) ").strip().lower()
        if rep not in ("oui", "o", "yes", "y"):
            print("Abandon. Rien n'a ete touche.")
            return

    zc = connecteur() if envois else None
    rapport = []
    a_la_main = []
    for l in envois:
        rid = l["resource_id"]
        ok_wd, msg_wd = corbeille_workdrive(zc, rid)
        time.sleep(PAUSE_UPLOAD)
        ok_sh, msg_sh = supprimer_ligne_sheet(
            zc, col_lien, f"https://workdrive.zoho.com/file/{rid}")
        time.sleep(PAUSE_SHEET)
        etat = f"WorkDrive {'ok' if ok_wd else 'ECHEC'} / registre {'ok' if ok_sh else 'ECHEC'}"
        print(f"  {l['Fichier origine'][:55]:55} {etat}")
        if not ok_wd:
            print(f"      WorkDrive : {msg_wd}")
        if not ok_sh:
            print(f"      Sheet : {msg_sh}")
            a_la_main.append(rid)
        rapport.append([time.strftime("%Y-%m-%d %H:%M:%S"), l["Fichier origine"],
                        "annule", rid, "corbeille" if ok_wd else "ECHEC",
                        "retire" if ok_sh else "ECHEC",
                        f"annulation du passage {horodatage}"])

    # Registre local : on retire les lignes de ces envois
    if lignes_registre and rids:
        gardees = [l for l in lignes_registre if not any(rid in ";".join(l) for rid in rids)]
        if len(gardees) != len(lignes_registre):
            with open(REGISTRE, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(entete)
                w.writerows(gardees)

    # Anciens noms sur le poste
    restaures = 0
    restes = []
    for l in reversed(a_restaurer):
        ancien, nouveau = Path(l[1]), Path(l[2])
        if nouveau.exists() and not ancien.exists():
            try:
                nouveau.rename(ancien)
                restaures += 1
                continue
            except OSError as exc:
                print(f"  ECHEC renommage : {exc}")
        restes.append(l)
    if a_restaurer:
        autres = [l for l in renoms if l not in a_restaurer] + list(reversed(restes))
        with open(RENOMMAGES, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Horodatage", "Ancien chemin", "Nouveau chemin"])
            w.writerows(autres)

    # Moteur de recherche
    retirees = 0
    rech = module_recherche()
    if rech is not None and hasattr(rech, "supprimer"):
        retirees = rech.supprimer(sorted(noms_publies))

    if rapport:
        with open(JOURNAL, "a", encoding="utf-8-sig", newline="") as f:
            csv.writer(f, delimiter=";").writerows(rapport)

    print("\n" + "=" * 52)
    print(f"  a la corbeille WorkDrive : {sum(1 for r in rapport if r[4] == 'corbeille')}")
    print(f"  retirees du registre     : {sum(1 for r in rapport if r[5] == 'retire')}")
    print(f"  anciens noms remis       : {restaures}")
    print(f"  fiches de recherche      : {retirees} retiree(s)")
    if a_la_main:
        print("\n  A supprimer A LA MAIN dans le registre Zoho Sheet (colonne "
              f"« {col_lien} ») :")
        for rid in a_la_main:
            print(f"    https://workdrive.zoho.com/file/{rid}")


# ===========================================================================
# Programme principal
# ===========================================================================
def valeur_option(args, nom, defaut=None):
    if nom in args:
        i = args.index(nom)
        if i + 1 < len(args):
            return args[i + 1]
    return defaut


def main():
    args = sys.argv[1:]

    if not args or "--aide" in args or "-h" in args:
        print(__doc__)
        return

    if "--annuler-renommage" in args:
        annuler_renommage()
        return

    if "--annuler-envoi" in args:
        h = valeur_option(args, "--annuler-envoi")
        annuler_envoi(h if h and not h.startswith("--") else None, args)
        return

    source = valeur_option(args, "--source")
    if not source:
        print("ERREUR : --source <dossier> est obligatoire.")
        return

    if "--indexer-recherche" in args:
        indexer_recherche(source, args)
    elif "--cartographier" in args:
        cartographier(source)
    elif "--proposer" in args:
        proposer(source, numeroter="--numeroter" in args)
    elif "--appliquer" in args:
        appliquer(source, args)
    else:
        print(__doc__)
        print("Choisis --cartographier, --proposer, --appliquer ou --indexer-recherche.")


if __name__ == "__main__":
    main()
