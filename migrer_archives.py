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

NORME PRODUITE
--------------
    HM26-{SERVICE}-{TYPE}_{AAAAMMJJ}_{NNN}_{OBJET}.pdf

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
def sans_accent(texte):
    t = unicodedata.normalize("NFKD", texte or "")
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
    r"^ac\s*conf\s*rh\s*\d{4}\s*",
    r"^contrat\s*rh\s*\d{4}\s*",
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


def extraire_tiers(nom, sous_dossier, regle):
    """Expediteur, destinataire ou titulaire du dossier.

    Lu au segment qui suit immediatement celui consomme par la regle.
    """
    if regle.get("tiers_depuis_sous_dossier"):
        parts = [p for p in Path(sous_dossier).parts if p not in (".", "")]
        profondeur = regle.get("profondeur", 0)
        if profondeur and len(parts) > profondeur:
            candidat = parts[profondeur]
            if _tiers_plausible(candidat):
                return candidat.strip()

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
    t = re.sub(r"[_\-–—]+", " ", t)
    t = re.sub(r"[()\[\]{}]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    for motif in PREFIXES_A_RETIRER:
        nouveau = re.sub(motif, "", t, flags=re.IGNORECASE).strip()
        if nouveau != t:
            t = nouveau
            break

    # Suffixe de scanner : « _0001 », « 001 »
    t = re.sub(r"\s0{2,}\d\s*$", "", t)

    # Dates en fin de chaine, du plus precis au plus vague
    t = re.sub(r"\s\d{2}\s?\d{2}\s?\d{2}\s*$", "", t)
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
    t = re.sub(r"\s+", " ", t).strip()

    if tiers and not sens:
        t = f"{sans_accent(tiers)} {t}".strip()
        t = re.sub(r"[^A-Za-z0-9 ']+", " ", t)
        t = re.sub(r"\s+", " ", t).strip()

    if not t:
        t = "document"

    return "_".join(t.split())[:70].strip("_") or "document"


def nom_hm26(service, type_doc, date_tuple, numero, objet):
    aaaa, mm, jj = date_tuple
    jeton = f"{aaaa or 2000 + int(ANNEE):04d}{mm:02d}{jj:02d}"
    return f"HM{ANNEE}-{service}-{type_doc}_{jeton}_{numero or 'XXX'}_{objet}.pdf"


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
    maxima = defaultdict(int)

    for nom in noms_registre:
        m = MOTIF_HM26.match(nom)
        if not m:
            continue
        chiffres = re.sub(r"\D", "", m.group("numero"))
        if chiffres:
            cle = (m.group("service").upper(), m.group("type").upper())
            maxima[cle] = max(maxima[cle], int(chiffres))

    for ligne in lignes:
        if ligne["Numero"] and ligne["Service"] and ligne["Type"]:
            cle = (ligne["Service"], ligne["Type"])
            maxima[cle] = max(maxima[cle], int(ligne["Numero"]))

    a_combler = [l for l in lignes
                 if l["_manque_numero"] and l["Service"] and l["Type"]
                 and l["Action"] not in ("ignorer",)]
    a_combler.sort(key=lambda l: (l["Service"], l["Type"], l["Date"] or "9999",
                                  l["Fichier origine"]))

    attribues = 0
    for ligne in a_combler:
        cle = (ligne["Service"], ligne["Type"])
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

        numero, src_num = extraire_numero(f["nom"])
        aaaa, mm, jj, src_date = extraire_date(f["nom"], f["sous_dossier"])
        tiers = extraire_tiers(f["nom"], f["sous_dossier"], info)
        objet = nettoyer_objet(f["nom"], numero, (aaaa, mm, jj), tiers,
                               sens=info.get("sens", ""))

        if aaaa and aaaa != 2000 + int(ANNEE):
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
                remarques.append(f"annee {aaaa}, hors perimetre {2000 + int(ANNEE)}")
                if action == "indexer":
                    action = "a_decider"

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

        propose = ""
        if service and type_doc:
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
            ligne["Nom HM26"] = nom_hm26(ligne["Service"], ligne["Type"],
                                         ligne["_date"], ligne["Numero"],
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
            par_serie[(ligne["Service"], ligne["Type"], ligne["Numero"])].append(ligne)

    conflits_numero = 0
    for (service, type_doc, numero), groupe in par_serie.items():
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
                + f"NUMERO {service}-{type_doc} {numero} partage avec : {detail}"
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
        "date": ligne.get("Date", ""),
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
    noms = set()
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
                noms.add(nom)
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
# PASSE 3 — application
# ===========================================================================
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
    deposant = valeur_option(args, "--deposant", "Import de masse")

    with open(PROPOSITIONS, "r", encoding="utf-8-sig") as f:
        lignes = list(csv.DictReader(f, delimiter=";"))

    print(f"\n=== PASSE 3 — APPLICATION ===\n")
    print(f"Source : {racine}")
    print(f"{PROPOSITIONS.name} : {len(lignes)} ligne(s)")

    a_indexer, a_renommer, refusees = [], [], []

    for ligne in lignes:
        action = (ligne.get("Action") or "").strip().lower()
        if action == "ignorer":
            continue
        if action == "a_decider":
            refusees.append(ligne)
            continue

        chemin = racine / ligne["Sous-dossier"] / ligne["Fichier origine"]
        if not chemin.exists():
            ligne["_erreur"] = "fichier introuvable sur le disque"
            refusees.append(ligne)
            continue

        nom_cible = (ligne.get("Nom HM26") or "").strip()
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

    print(f"\n  a indexer   {len(a_indexer):>4}")
    print(f"  a renommer  {len(a_renommer):>4}")
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

    if not forcer:
        total = len(a_indexer) + len(a_renommer)
        rep = input(f"\nAppliquer sur {total} fichier(s) ? (oui/non) ").strip().lower()
        if rep not in ("oui", "o", "yes", "y"):
            print("Annule. Rien n'a ete fait.")
            return

    horodatage = time.strftime("%Y-%m-%d %H:%M:%S")
    journal, renommages = [], []
    n_renomme = n_upload = n_index = n_echec = 0

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

        if not sans_upload:
            if cible_nom in index_distant.get(fid, set()) and not ecraser:
                print(f"{prefixe} deja dans WorkDrive, saute : {cible_nom[:50]}")
                journal.append([horodatage, ligne["Fichier origine"], "indexer", "",
                                "saute", "saute", "deja dans WorkDrive"])
                continue
            succes, rid, msg = uploader(zc, chemin, cible_nom, fid, ecraser=ecraser)
            etat_up = "ok" if succes else "ECHEC"
            if not succes:
                n_echec += 1
                print(f"{prefixe} ECHEC upload {cible_nom[:42]} — {msg}")
                journal.append([horodatage, ligne["Fichier origine"], "indexer", "",
                                etat_up, etat_idx, msg])
                time.sleep(PAUSE_UPLOAD)
                continue
            n_upload += 1
            time.sleep(PAUSE_UPLOAD)

        if not sans_index:
            donnees = valeurs_registre(ligne, rid, deposant)
            try:
                zc.sheet_add_row(WORKBOOK_ID, FEUILLE, donnees)
                etat_idx = "ok"
                n_index += 1
            except Exception as exc:  # noqa: BLE001
                etat_idx = "ECHEC"
                n_echec += 1
                msg = (msg + " / " + str(exc)[:150]).strip(" /")
                print(f"{prefixe} ECHEC registre {cible_nom[:42]} — {exc}")
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

        print(f"{prefixe} {cible_nom[:58]}" + (f" — {msg}" if msg else ""))
        journal.append([horodatage, ligne["Fichier origine"], "indexer", rid,
                        etat_up, etat_idx, msg])

    # --- Traces ---------------------------------------------------------
    if journal:
        nouveau = not JOURNAL.exists()
        with open(JOURNAL, "a", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh, delimiter=";")
            if nouveau:
                w.writerow(["Horodatage", "Fichier origine", "Action",
                            "resource_id", "Upload", "Index", "Message"])
            w.writerows(journal)

    if renommages:
        nouveau = not RENOMMAGES.exists()
        with open(RENOMMAGES, "a", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh, delimiter=";")
            if nouveau:
                w.writerow(["Horodatage", "Ancien chemin", "Nouveau chemin"])
            w.writerows(renommages)

    print("\n" + "=" * 52)
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

    source = valeur_option(args, "--source")
    if not source:
        print("ERREUR : --source <dossier> est obligatoire.")
        return

    if "--cartographier" in args:
        cartographier(source)
    elif "--proposer" in args:
        proposer(source, numeroter="--numeroter" in args)
    elif "--appliquer" in args:
        appliquer(source, args)
    else:
        print(__doc__)
        print("Choisis --cartographier, --proposer ou --appliquer.")


if __name__ == "__main__":
    main()
