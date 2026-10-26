#!/usr/bin/env python3
"""
recherche.py — Moteur de recherche de la GED WAKATI
===================================================
Retrouver un document sans ouvrir le registre Zoho Sheet : on tape quelques
mots (« banque centrale agrément », « Janus facture mars »), la GED cherche
dans le nom du fichier, l'objet, le correspondant, l'ancien nom et le chemin
des dossiers d'origine, et dans le TEXTE des PDF (OCR des scans compris).
Accents et majuscules indifférents, mots partiels acceptés.

D'OÙ VIENNENT LES DOCUMENTS INDEXÉS
-----------------------------------
  1. Chaque archivage fait dans l'application : indexé automatiquement,
     texte compris (extraction en tâche de fond).
  2. Le registre : écran Recherche › « Réindexer depuis le registre », ou
     en ligne de commande (--depuis-sheet, --depuis-registre).
  3. Les archives migrées par migrer_archives.py : indexées au fil de
     --appliquer, ou d'un coup avec --indexer-recherche.

L'index est une base SQLite (recherche plein texte FTS5) rangée avec les
données du module Courrier : courrier/recherche.sqlite. Elle se reconstruit
à tout moment, ce n'est pas une donnée de référence — le registre et
WorkDrive le restent.

LIGNE DE COMMANDE (depuis la racine du projet)
----------------------------------------------
  ./venv/bin/python recherche.py --stats
  ./venv/bin/python recherche.py --chercher "banque centrale agrément"
  ./venv/bin/python recherche.py --depuis-registre            # registre_2026.csv
  ./venv/bin/python recherche.py --depuis-sheet               # registre Zoho Sheet
  ./venv/bin/python recherche.py --archives-sheet "<lien du classeur>" --libelle "2021-2025" --apercu
  ./venv/bin/python recherche.py --archives-sheet "<lien du classeur>" --libelle "2021-2025"
      Autre classeur (archives des années antérieures). Colonnes reconnues par
      leur intitulé ; toutes les feuilles par défaut (--feuilles "2021,2022").
  ./venv/bin/python recherche.py --indexer-dossier ~/Documents/GED [--sans-texte]
  ./venv/bin/python recherche.py --exporter index_ged.jsonl
  ./venv/bin/python recherche.py --importer index_ged.jsonl [--synchroniser]
  ./venv/bin/python recherche.py --supprimer "HM26-…pdf"

TRANSFÉRER L'INDEX DU POSTE VERS LE SERVEUR
-------------------------------------------
L'OCR des archives se fait sur le poste (là où sont les fichiers). On
exporte, on copie dans le dossier courrier/ du serveur, on importe :

  ./venv/bin/python recherche.py --exporter index_ged.jsonl
  scp index_ged.jsonl root@<vps>:/opt/ged-wakati/courrier/
  docker exec ged-wakati python /app/recherche.py --importer /app/courrier/index_ged.jsonl

L'import fusionne : il n'efface rien, il complète et met à jour.
Avec --synchroniser, il retire en plus les fiches « migration des archives »
absentes du fichier (noms corrigés depuis). Les archivages faits dans
l'application et les fiches du registre ne sont jamais touchés.
"""

import csv
import functools
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

RACINE = Path(__file__).resolve().parent
BASE = Path(os.getenv("GED_DONNEES", str(RACINE))) / "courrier"
F_INDEX = BASE / "recherche.sqlite"
REGISTRE_CSV = RACINE / "registre_2026.csv"

WORKBOOK_ID = "avaji4d47ec1cbffc4f5184dc8b3127b83ae7"
FEUILLE = "DATA"

_verrou = threading.RLock()

# Libellés par défaut (mêmes que app.py) : un nom HM26 lu en ligne de
# commande donne « Direction / Admin », comme un archivage fait dans l'écran.
SERVICES = {
    "FIN": "Finances", "RH": "Ressources Humaines", "DGA": "Direction / Admin",
    "JUR": "Juridique", "COM": "Commercial", "EXP": "Exploitation",
}
TYPES = {
    "FAC": "Facture", "CNT": "Contrat", "NOT": "Note de service", "VIR": "Virement",
    "DEM": "Demande", "ODM": "Ordre de mission", "ETA": "État / Relevé", "ACC": "Accord",
    "ATT": "Attestation", "BOR": "Bordereau", "CENT": "Courrier Entrant",
    "CSOR": "Courrier Sortant", "PV": "Procès-verbal", "DOC": "Document divers",
    "MAN": "Mandat", "DEC": "Décision", "COR": "Courrier", "INV": "Invitation",
    "REG": "Règlement intérieur", "DOS": "Dossier individuel", "PAI": "Paie",
}
_CTX = {"services": dict(SERVICES), "types": dict(TYPES)}

COLONNES = ["nom", "service", "type_doc", "date", "annee", "sens",
            "correspondant", "objet", "lien", "source", "origine", "chemin",
            "md5", "texte", "mots_cles", "indexe_le", "texte_le"]

# Poids de pertinence (bm25) : un mot trouvé dans l'objet ou le
# correspondant compte plus que le même mot perdu dans le corps du texte.
# Ordre des colonnes de la table FTS : nom, correspondant, objet, mots_cles, texte
POIDS = "3.0, 4.0, 5.0, 2.0, 1.0"

MOTIF_HM = re.compile(
    r"^HM(?P<annee>\d{2})-(?P<service>[A-Z]+)-(?P<type>[A-Z]+)"
    r"_(?P<date>\d{8})_(?P<numero>[A-Z0-9]+)_(?P<objet>.*?)\.pdf$", re.I)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
def _normaliser(texte):
    t = unicodedata.normalize("NFKD", texte or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", t)).strip()


@functools.lru_cache(maxsize=1)
def fts5_disponible():
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        c.close()
        return True
    except sqlite3.OperationalError:
        return False


def connexion():
    BASE.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(F_INDEX), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(f"""CREATE TABLE IF NOT EXISTS documents (
        {', '.join(c_ + (' TEXT PRIMARY KEY' if c_ == 'nom' else ' TEXT') for c_ in COLONNES)},
        recherche TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS i_md5 ON documents(md5)")
    if fts5_disponible():
        c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
            nom, correspondant, objet, mots_cles, texte,
            tokenize="unicode61 remove_diacritics 2")""")
    return c


def _depuis_nom(nom):
    """Métadonnées lisibles dans un nom HM26 (ou HM25…)."""
    m = MOTIF_HM.match(nom or "")
    if not m:
        return {}
    d = m.group("date")
    date = f"{d[:4]}-{d[4:6]}-{d[6:]}" if d[4:] != "0000" else d[:4]
    return {
        "service": _CTX["services"].get(m.group("service").upper(), m.group("service").upper()),
        "type_doc": _CTX["types"].get(m.group("type").upper(), m.group("type").upper()),
        "date": date,
        "annee": d[:4],
        "objet": m.group("objet").replace("_", " "),
    }


def indexer(doc):
    """Ajoute ou met à jour un document. Ne remplace jamais une valeur
    connue par une valeur vide (un réimport du registre n'efface pas le
    texte OCR obtenu par ailleurs)."""
    nom = (doc.get("nom") or "").strip()
    if not nom:
        return False
    complet = {k: "" for k in COLONNES}
    complet.update({k: v for k, v in _depuis_nom(nom).items() if v})
    complet.update({k: (str(v) if v is not None else "") for k, v in doc.items()
                    if k in COLONNES and v not in (None, "")})
    complet["nom"] = nom
    if complet["date"] and not complet["annee"]:
        complet["annee"] = complet["date"][:4]

    with _verrou:
        c = connexion()
        try:
            ancien = c.execute("SELECT * FROM documents WHERE nom=?", (nom,)).fetchone()
            if ancien:
                for k in COLONNES:
                    if not complet.get(k) and ancien[k]:
                        complet[k] = ancien[k]
            complet["indexe_le"] = datetime.now().isoformat(timespec="seconds")
            complet["recherche"] = _normaliser(" ".join(
                complet[k] for k in ("nom", "correspondant", "objet", "mots_cles",
                                     "texte", "service", "type_doc")))
            champs = COLONNES + ["recherche"]
            c.execute(f"INSERT OR REPLACE INTO documents ({', '.join(champs)}) "
                      f"VALUES ({', '.join('?' for _ in champs)})",
                      [complet[k] for k in champs])
            if fts5_disponible():
                c.execute("DELETE FROM documents_fts WHERE nom=?", (nom,))
                c.execute("INSERT INTO documents_fts (nom, correspondant, objet, mots_cles, texte) "
                          "VALUES (?,?,?,?,?)",
                          (nom, complet["correspondant"], complet["objet"],
                           complet["mots_cles"], complet["texte"]))
            c.commit()
        finally:
            c.close()
    return True


# ---------------------------------------------------------------------------
# Texte des PDF
# ---------------------------------------------------------------------------
def extraire_texte(chemin_pdf, pages_max=3):
    """Texte du PDF, OCR si c'est un scan. Réutilise l'extraction du module
    Courrier (pypdf puis Tesseract), avec repli sur pypdf seul."""
    try:
        from courrier import extraire_texte as _extraire  # noqa: PLC0415
        return _extraire(chemin_pdf, pages_max=pages_max)
    except ImportError:
        pass
    try:
        from pypdf import PdfReader  # noqa: PLC0415
        lecteur = PdfReader(str(chemin_pdf))
        return "\n".join((p.extract_text() or "") for p in lecteur.pages[:pages_max]).strip()
    except Exception:  # noqa: BLE001
        return ""


def indexer_fichier(doc, chemin_pdf=None, en_tache=True, pages_max=3):
    """Indexe les métadonnées tout de suite, puis le texte du PDF.

    en_tache=True (application web) : le texte est extrait en arrière-plan
    sur une copie, l'archivage n'attend pas l'OCR.
    """
    indexer(doc)
    if not chemin_pdf or not Path(chemin_pdf).exists():
        return

    def extraire(chemin, supprimer):
        try:
            texte = extraire_texte(chemin, pages_max=pages_max)
            if texte:
                indexer({"nom": doc["nom"], "texte": texte[:60000],
                         "texte_le": datetime.now().isoformat(timespec="seconds")})
        except Exception:  # noqa: BLE001
            logging.error("Indexation du texte de %s impossible", doc.get("nom"),
                          exc_info=True)
        finally:
            if supprimer:
                try:
                    Path(chemin).unlink(missing_ok=True)
                except OSError:
                    pass

    if en_tache:
        fd, copie = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        shutil.copy2(chemin_pdf, copie)
        threading.Thread(target=extraire, args=(copie, True), daemon=True).start()
    else:
        extraire(chemin_pdf, False)


# ---------------------------------------------------------------------------
# Recherche
# ---------------------------------------------------------------------------
def _requete_fts(saisie):
    """« Banque centrale "mise en place" » -> "banque"* "centrale"* "mise en place"."""
    morceaux = []
    for phrase in re.findall(r'"([^"]+)"', saisie or ""):
        mots = re.findall(r"\w+", phrase)
        if mots:
            morceaux.append('"' + " ".join(mots) + '"')
    reste = re.sub(r'"[^"]*"', " ", saisie or "")
    for mot in re.findall(r"\w+", reste):
        if len(mot) >= 2 or mot.isdigit():
            morceaux.append(f'"{mot}"*')
    return " ".join(morceaux)


def rechercher(saisie="", service="", type_doc="", sens="", annee="",
               limite=25, decalage=0):
    """Retourne (total, résultats). Sans mots, liste les plus récents."""
    filtres, valeurs = [], []
    for col, val in (("d.service", service), ("d.type_doc", type_doc),
                     ("d.sens", sens), ("d.annee", annee)):
        if val:
            filtres.append(f"{col} = ?")
            valeurs.append(val)

    c = connexion()
    try:
        requete = _requete_fts(saisie)
        if requete and fts5_disponible():
            ou = " AND ".join(["documents_fts MATCH ?"] + filtres)
            base = (f"FROM documents_fts JOIN documents d ON d.nom = documents_fts.nom "
                    f"WHERE {ou}")
            params = [requete] + valeurs
            try:
                total = c.execute(f"SELECT count(*) {base}", params).fetchone()[0]
                lignes = c.execute(
                    f"SELECT d.*, bm25(documents_fts, {POIDS}) AS score, "
                    f"snippet(documents_fts, 4, '\x02', '\x03', ' … ', 14) AS extrait, "
                    f"snippet(documents_fts, 2, '\x02', '\x03', ' … ', 14) AS extrait_objet "
                    f"{base} ORDER BY score LIMIT ? OFFSET ?",
                    params + [limite, decalage]).fetchall()
            except sqlite3.OperationalError:
                return 0, []
        elif requete:
            # Repli sans FTS5 : chaque mot doit figurer dans le texte normalisé.
            mots = [_normaliser(m) for m in re.findall(r"\w+", saisie) if m]
            conds = filtres + ["d.recherche LIKE ?" for _ in mots]
            params = valeurs + [f"%{m}%" for m in mots]
            ou = " AND ".join(conds) or "1=1"
            total = c.execute(f"SELECT count(*) FROM documents d WHERE {ou}", params).fetchone()[0]
            lignes = c.execute(f"SELECT d.*, '' AS extrait, '' AS extrait_objet "
                               f"FROM documents d WHERE {ou} ORDER BY d.date DESC "
                               f"LIMIT ? OFFSET ?", params + [limite, decalage]).fetchall()
        else:
            ou = " AND ".join(filtres) or "1=1"
            total = c.execute(f"SELECT count(*) FROM documents d WHERE {ou}", valeurs).fetchone()[0]
            lignes = c.execute(f"SELECT d.*, '' AS extrait, '' AS extrait_objet "
                               f"FROM documents d WHERE {ou} ORDER BY d.date DESC, d.nom "
                               f"LIMIT ? OFFSET ?", valeurs + [limite, decalage]).fetchall()
        return total, [dict(l) for l in lignes]
    finally:
        c.close()


def fichier_local(doc):
    """Chemin du PDF sur cette machine, ou None.

    Le chemin enregistre a l'indexation peut avoir change depuis :
    --appliquer renomme le fichier en HM26-… dans le meme dossier. On essaie
    donc le chemin enregistre, puis le nom HM, puis le nom d'origine, dans
    ce meme dossier. Seuls des PDF sont servis.
    """
    chemin = doc.get("chemin") or ""
    if not chemin:
        return None
    base = Path(chemin)
    for candidat in (base, base.parent / (doc.get("nom") or ""),
                     base.parent / (doc.get("origine") or "")):
        try:
            if candidat.name and candidat.suffix.lower() == ".pdf" and candidat.is_file():
                return candidat
        except OSError:
            continue
    return None


def statistiques():
    c = connexion()
    try:
        total = c.execute("SELECT count(*) FROM documents").fetchone()[0]
        avec_texte = c.execute("SELECT count(*) FROM documents WHERE texte <> ''").fetchone()[0]
        def valeurs(col):
            return [r[0] for r in c.execute(
                f"SELECT DISTINCT {col} FROM documents WHERE {col} <> '' ORDER BY {col}")]
        return {"total": total, "avec_texte": avec_texte,
                "services": valeurs("service"), "types": valeurs("type_doc"),
                "annees": sorted(valeurs("annee"), reverse=True),
                "sens": valeurs("sens"), "fts5": fts5_disponible()}
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Sources : registre CSV, Zoho Sheet, dossier local, JSONL
# ---------------------------------------------------------------------------
_SYNONYMES = {
    "nom": ["nom du fichier", "nom fichier", "fichier", "nom du document", "nom document",
            "document", "nom du fichier hm", "nom hm", "nom hm26"],
    "origine": ["nom d origine", "nom origine", "ancien nom", "nom initial"],
    "date_exacte": ["date exacte", "date complete", "date precise"],
    "service": ["service", "direction", "departement"],
    "type_doc": ["type", "type de document", "nature", "categorie", "nature du document"],
    "date": ["date", "date du document", "date du courrier", "date courrier",
             "date d arrivee", "date de reception", "date d envoi", "date de depart",
             "date d enregistrement", "annee", "exercice"],
    "objet": ["objet", "objet du courrier", "intitule", "libelle", "titre", "sujet",
              "description"],
    "lien": ["lien zoho", "lien", "url", "lien workdrive", "lien du document",
             "lien fichier", "lien du fichier", "ouvrir", "acces"],
    "sens": ["sens", "flux", "type de courrier", "entrant sortant", "arrivee depart"],
    "correspondant": ["correspondant", "organisme", "structure", "tiers", "partenaire",
                      "provenance", "expediteur destinataire"],
    "expediteur": ["expediteur", "emetteur"],
    "destinataire": ["destinataire"],
    "numero": ["n", "no", "num", "numero", "n d ordre", "numero d ordre",
               "n d enregistrement", "numero d enregistrement", "reference", "ref"],
    "observations": ["observations", "observation", "remarques", "commentaire",
                     "suites a donner"],
    "dossier": ["dossier", "chemin", "arborescence", "emplacement", "chemin du dossier",
                "dossier parent"],
}


def _date_iso(valeur):
    """« 12/03/2023 », « 12-03-23 », « 2023-03-12 », « 2023 » -> ISO (ou "")."""
    v = str(valeur or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", v)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", v)
    if m:
        a = int(m.group(3))
        a = a + 2000 if a < 100 else a
        if 1 <= int(m.group(2)) <= 12:
            return f"{a:04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.match(r"^(19|20)\d{2}$", v)
    return m.group(0) if m else ""


def _sens_lisible(valeur):
    v = _normaliser(valeur)
    if v.startswith(("entr", "arriv", "recu")):
        return "Entrant"
    if v.startswith(("sort", "depart", "envoy", "emis")):
        return "Sortant"
    return str(valeur or "").strip()


def _depuis_ligne_registre(ligne, source):
    """Ligne du registre (dict en-tête -> valeur) -> document indexable."""
    inverse = {}
    for champ, variantes in _SYNONYMES.items():
        for v in variantes:
            inverse[v] = champ
    d = {}
    for entete, valeur in ligne.items():
        champ = inverse.get(_normaliser(entete))
        if champ and valeur not in (None, ""):
            d[champ] = str(valeur).strip()
    if not d.get("nom"):
        return None
    correspondant = d.get("correspondant") or d.get("expediteur") or d.get("destinataire") or ""
    lien = d.get("lien", "")
    if lien and not lien.startswith("http"):
        lien = ""
    return {"nom": d["nom"], "service": d.get("service", ""),
            "type_doc": d.get("type_doc", ""), "date": d.get("date", ""),
            "sens": d.get("sens", ""), "correspondant": correspondant,
            "objet": d.get("objet", ""), "lien": lien, "source": source,
            "mots_cles": d.get("observations", "")}


# ---------------------------------------------------------------------------
# Autres classeurs Zoho Sheet (archives 2021-2025…)
# ---------------------------------------------------------------------------
F_SOURCES = BASE / "sources_sheet.json"
_MOTIF_ID_SHEET = re.compile(r"(?:/open/|/sheet/|/spreadsheet/|^)([A-Za-z0-9]{25,})")


def id_classeur(ref):
    """Identifiant d'un classeur Zoho Sheet depuis son lien ou son id."""
    m = _MOTIF_ID_SHEET.search(str(ref or "").strip())
    return m.group(1) if m else ""


def _connecteur():
    sys.path.insert(0, str(RACINE))
    from zoho_connector import ZohoConnector  # noqa: PLC0415
    return ZohoConnector()


def _appel_sheet(connecteur, classeur, donnees):
    import requests  # noqa: PLC0415
    r = requests.post(f"https://sheet.zoho.com/api/v2/{classeur}",
                      headers=connecteur._headers(
                          {"Content-Type": "application/x-www-form-urlencoded"}),
                      data=donnees, timeout=60)
    r.raise_for_status()
    corps = r.json()
    if corps.get("status") == "failure":
        raise RuntimeError(f"Zoho Sheet : {corps}")
    return corps


def feuilles_classeur(connecteur, classeur):
    corps = _appel_sheet(connecteur, classeur, {"method": "worksheet.list"})
    noms = []
    for f in corps.get("worksheet_names") or corps.get("worksheets") or []:
        nom = f.get("worksheet_name") if isinstance(f, dict) else f
        if nom:
            noms.append(str(nom))
    return noms


def _score_entetes(entetes):
    """Nombre d'intitulés de colonnes reconnus (nom, date, objet, lien…)."""
    connus = {v for vs in _SYNONYMES.values() for v in vs}
    connus |= {"chemin", "dossier", "taille", "extension", "emplacement"}
    return sum(1 for e in entetes if _normaliser(e) in connus)


def ligne_entete(connecteur, classeur, feuille, essais=10):
    """Ligne des intitulés de colonnes : la première, ou plus bas quand la
    feuille commence par un titre (« INDEX ARCHIVAGE 2021-2025 … »)."""
    meilleure, score_max = 1, -1
    for h in range(1, essais + 1):
        try:
            corps = _appel_sheet(connecteur, classeur, {
                "method": "worksheet.records.fetch", "worksheet_name": feuille,
                "header_row": h, "records_start_index": 1, "count": 3})
        except Exception:  # noqa: BLE001
            continue
        lot = corps.get("records") or []
        if not lot:
            continue
        score = _score_entetes([k for k in lot[0] if k != "row_index"])
        if score > score_max:
            meilleure, score_max = h, score
        if score >= 3:
            break
    return meilleure, max(score_max, 0)


# --- Lecture brute des cellules (repli quand l'API « records » ne trouve
#     pas les intitulés, par exemple sous un titre fusionné) -----------------
def _cellules(connecteur, classeur, feuille, debut, fin, nb_col=30):
    """{n° de ligne: [valeurs]} pour les lignes debut..fin."""
    corps = _appel_sheet(connecteur, classeur, {
        "method": "worksheet.content.get", "worksheet_name": feuille,
        "start_row": debut, "start_column": 1, "end_row": fin, "end_column": nb_col})
    lignes = {}
    for r in corps.get("range_details") or corps.get("data") or []:
        idx = int(r.get("row_index") or r.get("row") or 0)
        cases = [""] * nb_col
        for c in r.get("row_details") or r.get("columns") or []:
            j = int(c.get("column_index") or c.get("column") or 0) - 1
            if 0 <= j < nb_col:
                val = c.get("content")
                if val in (None, ""):
                    val = c.get("value", "")
                formule = str(c.get("formula") or "")
                m = re.search(r'HYPERLINK\(\s*"([^"]+)"', formule, re.I)
                if m:  # lien caché derrière un libellé
                    val = f"{val} {m.group(1)}".strip()
                cases[j] = "" if val is None else str(val)
        if idx:
            lignes[idx] = cases
    return lignes


def lignes_feuille_brutes(connecteur, classeur, feuille, taille_lot=400, nb_col=30):
    """Même sortie que lignes_feuille, en lisant les cellules : la ligne
    d'intitulés est cherchée dans les 15 premières lignes."""
    tete = _cellules(connecteur, classeur, feuille, 1, 15, nb_col)
    h, score_max = 1, -1
    for idx in sorted(tete):
        score = _score_entetes([v for v in tete[idx] if v])
        if score > score_max:
            h, score_max = idx, score
    entetes = [re.sub(r"\s*https?://\S+", "", v).strip() or f"Colonne {j + 1}"
               for j, v in enumerate(tete.get(h, []))]
    debut, vides = h + 1, 0
    while vides < 2:
        bloc = _cellules(connecteur, classeur, feuille, debut, debut + taille_lot - 1, nb_col)
        if not any(any(v for v in l) for l in bloc.values()):
            vides += 1
        for idx in sorted(bloc):
            valeurs = bloc[idx]
            if any(valeurs):
                yield {e: v for e, v in zip(entetes, valeurs) if not e.startswith("Colonne ") or v}
        debut += taille_lot


def diagnostic_feuille(ref, feuille, afficher=print):
    """Montre ce que Zoho renvoie pour une feuille : pour régler l'import."""
    classeur, zc = id_classeur(ref), _connecteur()
    afficher(f"=== Diagnostic de « {feuille} » ===")
    for h in range(1, 7):
        try:
            corps = _appel_sheet(zc, classeur, {
                "method": "worksheet.records.fetch", "worksheet_name": feuille,
                "header_row": h, "records_start_index": 1, "count": 2})
            lot = corps.get("records") or []
            afficher(f"records, en-tête ligne {h} : {list(lot[0].keys())[:10] if lot else '(vide)'}")
        except Exception as exc:  # noqa: BLE001
            afficher(f"records, en-tête ligne {h} : ERREUR {str(exc)[:150]}")
    try:
        brut = _cellules(zc, classeur, feuille, 1, 8, 12)
        for idx in sorted(brut):
            afficher(f"cellules ligne {idx} : {[v[:25] for v in brut[idx] if v]}")
        corps = _appel_sheet(zc, classeur, {
            "method": "worksheet.content.get", "worksheet_name": feuille,
            "start_row": 5, "start_column": 1, "end_row": 5, "end_column": 12})
        afficher("cellules ligne 5 (brut) : " + json.dumps(corps, ensure_ascii=False)[:1200])
        if not brut:
            corps = _appel_sheet(zc, classeur, {
                "method": "worksheet.content.get", "worksheet_name": feuille,
                "start_row": 1, "start_column": 1, "end_row": 3, "end_column": 6})
            afficher("réponse brute : " + json.dumps(corps, ensure_ascii=False)[:800])
    except Exception as exc:  # noqa: BLE001
        afficher(f"cellules : ERREUR {str(exc)[:300]}")


def lignes_feuille(connecteur, classeur, feuille, taille_lot=500, entete=1):
    debut = 1
    while True:
        corps = _appel_sheet(connecteur, classeur, {
            "method": "worksheet.records.fetch", "worksheet_name": feuille,
            "header_row": entete, "records_start_index": debut, "count": taille_lot})
        lot = corps.get("records") or []
        for ligne in lot:
            yield ligne
        if len(lot) < taille_lot:
            return
        debut += taille_lot


def _depuis_ligne_archive(ligne, source, feuille, rang):
    """Ligne d'un registre d'archives aux colonnes libres -> document.

    Colonnes reconnues par leur intitulé (synonymes) ; les autres colonnes
    deviennent des mots-clés, donc restent cherchables. Sans colonne « nom
    du fichier », un nom stable est construit : feuille + numéro + objet."""
    inverse = {}
    for champ, variantes in _SYNONYMES.items():
        for v in variantes:
            inverse.setdefault(v, champ)
    d, autres, liens = {}, [], []
    for entete, valeur in ligne.items():
        if valeur in (None, "") or entete == "row_index":
            continue
        valeur = str(valeur).strip()
        liens += re.findall(r"https?://[^\s\"'<>]+", valeur)
        champ = inverse.get(_normaliser(entete))
        if champ and champ not in d:
            d[champ] = valeur
        elif not valeur.startswith("http"):
            autres.append(valeur)
    if not any(d.get(k) for k in ("nom", "objet", "correspondant", "expediteur",
                                  "destinataire", "numero")):
        return None  # ligne vide ou de sous-total
    # La date la plus précise l'emporte : « Date exacte » avant « Année ».
    date = _date_iso(d.get("date_exacte")) or _date_iso(d.get("date"))
    if not date and re.match(r"^(19|20)\d{2}$", str(feuille).strip()):
        date = str(feuille).strip()
    nom = d.get("nom", "")
    if not nom or nom.startswith("http"):
        num = d.get("numero") or f"{rang:04d}"
        nom = f"ARCHIVE {feuille} n°{num} — {(d.get('objet') or '')[:70]}".strip(" —")
    urls_lien = re.findall(r"https?://[^\s\"'<>]+", str(d.get("lien", "")))
    lien = urls_lien[0] if urls_lien else (liens[0] if liens else "")
    correspondant = d.get("correspondant") or d.get("expediteur") or d.get("destinataire") or ""
    parties = [p.strip() for p in re.split(r"[/\\>]", d.get("dossier", "")) if p.strip()]
    if not correspondant and parties and not re.search(r"\.[a-z0-9]{2,4}$", parties[-1], re.I):
        # Chemin de DOSSIER (pas de fichier en bout) : le dernier dossier
        # porte souvent le nom du correspondant (…/Courriers sortants/BCC) ;
        # on saute années et mois (« 2025 », « 08_Aout »).
        for part in reversed(parties):
            n = re.sub(r"^\d+\s*", "", _normaliser(part))
            if n and not re.fullmatch(r"(19|20)\d{2}|\d+|janvier|fevrier|mars|avril|mai|juin|"
                                      r"juillet|aout|septembre|octobre|novembre|decembre", n):
                correspondant = part
                break
    # Une colonne « Type » qui donne l'extension (pdf, docx…) n'est pas le
    # type de document : on laisse le nom HM26 le fournir (DOC, ACC, PV…).
    type_doc = d.get("type_doc", "")
    if re.fullmatch(r"\.?(pdf|docx?|xlsx?|jpe?g|png|tiff?|odt|ods|txt|zip)", type_doc.strip(), re.I):
        type_doc = ""
    return {"nom": nom, "service": d.get("service", ""), "type_doc": type_doc,
            "date": date, "annee": date[:4], "sens": _sens_lisible(d.get("sens", "")),
            "correspondant": correspondant, "objet": d.get("objet", ""), "lien": lien,
            "source": source, "origine": d.get("origine", ""),
            "mots_cles": " ".join([str(feuille), d.get("numero", ""), d.get("origine", ""),
                                   " ".join(parties), d.get("observations", "")] + autres)}


def depuis_sheet_externe(ref, feuilles=None, libelle="", apercu=False, afficher=print):
    """Indexe un autre classeur Zoho Sheet (archives d'années antérieures…).

    ref : lien ou id du classeur. feuilles : liste de noms, None = toutes.
    apercu=True : n'indexe rien, montre les colonnes reconnues et 3 exemples.
    Retourne le nombre de documents indexés (ou lus, en aperçu)."""
    classeur = id_classeur(ref)
    if not classeur:
        raise ValueError(f"Lien ou identifiant de classeur Zoho Sheet invalide : {ref}")
    zc = _connecteur()
    feuilles = feuilles or feuilles_classeur(zc, classeur)
    source = f"archives {libelle}".strip() if libelle else "archives Zoho Sheet"
    n = 0
    for feuille in feuilles:
        nb_feuille, exemples, entetes = 0, [], []
        h, score = ligne_entete(zc, classeur, feuille)
        if score >= 2:
            source_lignes = lignes_feuille(zc, classeur, feuille, entete=h)
            mode = f"intitulés de colonnes en ligne {h}"
        else:
            source_lignes = lignes_feuille_brutes(zc, classeur, feuille)
            mode = "lecture des cellules"
        for rang, ligne in enumerate(source_lignes, 1):
            if not entetes:
                entetes = [k for k in ligne.keys() if k != "row_index"]
            doc = _depuis_ligne_archive(ligne, source, feuille, rang)
            if not doc:
                continue
            nb_feuille += 1
            if apercu:
                if len(exemples) < 3:
                    exemples.append(doc)
            elif indexer(doc):
                n += 1
        if apercu:
            n += nb_feuille
            afficher(f"\n--- Feuille « {feuille} » : {nb_feuille} document(s) ({mode})")
            inverse = {v: c for c, vs in _SYNONYMES.items() for v in vs}
            for e in entetes:
                afficher(f"    {str(e)[:32]:32} -> {inverse.get(_normaliser(e), '(mot-clé)')}")
            for doc in exemples:
                afficher(f"    · {doc['nom'][:70]} | {doc['date'] or '—'} | "
                         f"{doc['correspondant'][:25] or '—'} | lien : {'oui' if doc['lien'] else 'non'}")
        elif afficher:
            afficher(f"  « {feuille} » : {nb_feuille} document(s)")
    return n


def sources_memorisees():
    try:
        return json.loads(F_SOURCES.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []


def memoriser_source(ref, feuilles=None, libelle=""):
    """Le bouton « Réindexer » de l'écran Recherche relira aussi ce classeur."""
    sources = [s for s in sources_memorisees() if s.get("id") != id_classeur(ref)]
    sources.append({"id": id_classeur(ref), "feuilles": feuilles or None, "libelle": libelle})
    BASE.mkdir(parents=True, exist_ok=True)
    F_SOURCES.write_text(json.dumps(sources, indent=2, ensure_ascii=False), encoding="utf-8")


def depuis_sources_memorisees(afficher=None):
    n = 0
    for s in sources_memorisees():
        try:
            n += depuis_sheet_externe(s["id"], s.get("feuilles"), s.get("libelle", ""),
                                      afficher=afficher)
        except Exception:  # noqa: BLE001
            logging.error("Réindexation de la source %s impossible", s.get("id"), exc_info=True)
    return n


def depuis_registre_csv(chemin=None):
    chemin = Path(chemin or REGISTRE_CSV)
    if not chemin.exists():
        raise FileNotFoundError(f"{chemin} introuvable")
    n = 0
    with open(chemin, "r", encoding="utf-8-sig") as f:
        for ligne in csv.DictReader(f, delimiter=";"):
            doc = _depuis_ligne_registre(ligne, "registre local")
            if doc and indexer(doc):
                n += 1
    return n


def depuis_sheet(connecteur=None, taille_lot=500):
    """Relit le registre Zoho Sheet (API tabulaire, scope dataAPI.READ)."""
    import requests  # noqa: PLC0415
    if connecteur is None:
        sys.path.insert(0, str(RACINE))
        from zoho_connector import ZohoConnector  # noqa: PLC0415
        connecteur = ZohoConnector()
    url = f"https://sheet.zoho.com/api/v2/{WORKBOOK_ID}"
    n, debut = 0, 1
    while True:
        r = requests.post(url, headers=connecteur._headers(
            {"Content-Type": "application/x-www-form-urlencoded"}),
            data={"method": "worksheet.records.fetch", "worksheet_name": FEUILLE,
                  "header_row": 1, "records_start_index": debut, "count": taille_lot},
            timeout=60)
        r.raise_for_status()
        corps = r.json()
        if corps.get("status") == "failure":
            raise RuntimeError(f"Zoho Sheet : {corps}")
        lot = corps.get("records") or []
        for ligne in lot:
            doc = _depuis_ligne_registre(ligne, "registre Zoho Sheet")
            if doc and indexer(doc):
                n += 1
        if len(lot) < taille_lot:
            break
        debut += taille_lot
    return n


def depuis_dossier(dossier, avec_texte=True, afficher=print):
    """Indexe tous les PDF d'une arborescence locale. Le chemin des dossiers
    et le nom d'origine deviennent des mots-clés ; le dernier dossier est
    proposé comme correspondant quand le nom ne dit rien."""
    racine = Path(dossier).expanduser().resolve()
    fichiers = sorted(p for p in racine.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
    deja = set()
    if avec_texte:
        c = connexion()
        deja = {r[0] for r in c.execute("SELECT chemin FROM documents WHERE texte <> ''")}
        c.close()
    n = 0
    for i, p in enumerate(fichiers, 1):
        relatif = p.parent.relative_to(racine)
        doc = {"nom": p.name, "source": "dossier local", "chemin": str(p),
               "mots_cles": " ".join([p.stem] + list(relatif.parts)),
               "origine": p.name}
        if relatif.parts and not MOTIF_HM.match(p.name):
            doc["correspondant"] = relatif.parts[-1]
        texte_requis = avec_texte and str(p) not in deja
        indexer_fichier(doc, p if texte_requis else None, en_tache=False)
        n += 1
        if afficher and (i % 10 == 0 or i == len(fichiers)):
            afficher(f"  {i}/{len(fichiers)}  {p.name[:70]}")
    return n


def exporter(chemin):
    c = connexion()
    n = 0
    try:
        with open(chemin, "w", encoding="utf-8") as f:
            for r in c.execute(f"SELECT {', '.join(COLONNES)} FROM documents"):
                f.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
                n += 1
    finally:
        c.close()
    return n


def supprimer(noms):
    """Retire des documents de l'index (nom corrige, envoi annule)."""
    n = 0
    with _verrou:
        c = connexion()
        try:
            for nom in noms:
                cur = c.execute("DELETE FROM documents WHERE nom=?", (nom,))
                n += max(cur.rowcount, 0)
                if fts5_disponible():
                    c.execute("DELETE FROM documents_fts WHERE nom=?", (nom,))
            c.commit()
        finally:
            c.close()
    return n


def purger_source(source, garder):
    """Retire les documents d'une source qui ne figurent plus dans `garder`.
    Sert a la migration des archives : un nom corrige remplace l'ancien."""
    c = connexion()
    try:
        noms = [r[0] for r in c.execute("SELECT nom FROM documents WHERE source=?", (source,))]
    finally:
        c.close()
    return supprimer([n for n in noms if n not in garder])


# Seule source synchronisee a l'import : elle vient entierement du poste.
# Les archivages faits dans l'application et la reindexation du registre
# vivent sur le serveur et ne sont jamais retires par un import.
SOURCE_MIGRATION = "migration des archives"


def importer(chemin, synchroniser=False):
    """Fusionne un export. synchroniser=True retire en plus les fiches de la
    migration des archives absentes du fichier (noms corriges depuis).
    Retourne (importes, retires)."""
    n = 0
    noms_migration = set()
    with open(chemin, "r", encoding="utf-8") as f:
        for ligne in f:
            ligne = ligne.strip()
            if not ligne:
                continue
            doc = json.loads(ligne)
            if doc.get("source") == SOURCE_MIGRATION:
                noms_migration.add(doc.get("nom"))
            if indexer(doc):
                n += 1
    retires = purger_source(SOURCE_MIGRATION, noms_migration) if synchroniser else 0
    return n, retires


# ---------------------------------------------------------------------------
# Écran de recherche
# ---------------------------------------------------------------------------
try:
    from flask import (Blueprint, abort, flash, redirect, render_template,
                       request, send_file, session, url_for)
    from markupsafe import Markup, escape

    bp_recherche = Blueprint("recherche", __name__, url_prefix="/recherche")

    def _surligner(texte):
        """Le surlignage FTS (\\x02…\\x03) devient <mark>, le reste est échappé."""
        if not texte:
            return Markup("")
        sur = str(escape(texte)).replace("\x02", "<mark>").replace("\x03", "</mark>")
        return Markup(sur)

    def _peut_reindexer():
        email = (session.get("email") or "").lower()
        if not email:
            return True
        try:
            import courrier  # noqa: PLC0415
            return courrier.est_responsable_ged()
        except Exception:  # noqa: BLE001
            return True

    @bp_recherche.route("/")
    def ecran():
        q = (request.args.get("q") or "").strip()
        filtres = {k: request.args.get(k, "") for k in ("service", "type_doc", "sens", "annee")}
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1
        par_page = 25
        total, resultats = rechercher(q, limite=par_page, decalage=(page - 1) * par_page,
                                      **filtres)
        for r in resultats:
            # Le chemin complet est celui du poste qui a indexe : seul le
            # dossier parle a l'utilisateur (« Exim bank », « BCC »).
            r["dossier"] = Path(r["chemin"]).parent.name if r.get("chemin") else ""
            # Fichier present sur CETTE machine (poste de la responsable GED,
            # ou serveur pour les archivages faits dans l'application) :
            # on propose de l'ouvrir directement.
            r["fichier_dispo"] = fichier_local(r) is not None
            r["extrait_html"] = _surligner(r.get("extrait") or "")
            r["objet_html"] = _surligner(r.get("extrait_objet") or "") \
                if "\x02" in (r.get("extrait_objet") or "") else escape(r.get("objet") or "")
        return render_template("recherche.html", page="recherche",
                               q=q, filtres=filtres, total=total,
                               resultats=resultats, num_page=page,
                               nb_pages=max(1, -(-total // par_page)),
                               stats=statistiques(), reindexer=_peut_reindexer())

    @bp_recherche.route("/document/<path:nom>")
    def document(nom):
        """Ouvre le PDF d'un document indexe, s'il est sur cette machine."""
        c = connexion()
        try:
            ligne = c.execute("SELECT * FROM documents WHERE nom=?", (nom,)).fetchone()
        finally:
            c.close()
        if not ligne:
            abort(404)
        chemin = fichier_local(dict(ligne))
        if chemin is None:
            flash("Ce document n'est pas disponible sur ce poste : ouvrez-le depuis "
                  "WorkDrive ou depuis le dossier d'archives.", "error")
            return redirect(url_for("recherche.ecran", q=request.args.get("q", "")))
        return send_file(str(chemin), mimetype="application/pdf",
                         download_name=chemin.name, as_attachment=False)

    @bp_recherche.route("/reindexer", methods=["POST"])
    def reindexer():
        if not _peut_reindexer():
            flash("Réindexation réservée à la responsable GED.", "error")
            return redirect(url_for("recherche.ecran"))
        source = request.form.get("source", "sheet")

        def tache():
            try:
                n = depuis_sheet() if source == "sheet" else depuis_registre_csv()
                if source == "sheet":
                    n += depuis_sources_memorisees()
                print(f"[RECHERCHE] Réindexation ({source}) : {n} document(s)")
            except Exception:  # noqa: BLE001
                logging.error("Réindexation %s impossible", source, exc_info=True)
                print(f"[RECHERCHE] Réindexation ({source}) en échec — voir le journal")

        threading.Thread(target=tache, daemon=True).start()
        flash("Réindexation lancée en arrière-plan. Rechargez la page dans une "
              "minute pour voir le nouveau total.", "success")
        return redirect(url_for("recherche.ecran"))

except ImportError:  # utilisation en ligne de commande sans Flask
    bp_recherche = None


def init_recherche(app, protection=None, services=None, types=None):
    """À appeler APRÈS app.register_blueprint(bp_recherche)."""
    _CTX["services"].update(services or {})
    _CTX["types"].update(types or {})
    if protection is not None:
        for nom, vue in list(app.view_functions.items()):
            if nom.startswith("recherche."):
                app.view_functions[nom] = protection(vue)
    s = statistiques()
    print(f"[RECHERCHE] Index : {s['total']} document(s), dont {s['avec_texte']} "
          f"avec texte — {'FTS5' if s['fts5'] else 'mode simple (FTS5 absent)'}")


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------
def _option(args, nom, defaut=None):
    if nom in args:
        i = args.index(nom)
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            return args[i + 1]
    return defaut


def main():
    args = sys.argv[1:]
    if not args or "-h" in args or "--aide" in args:
        print(__doc__)
        return
    if "--stats" in args:
        s = statistiques()
        print(f"Index : {F_INDEX}")
        print(f"  documents      : {s['total']}")
        print(f"  avec texte     : {s['avec_texte']}")
        print(f"  années         : {', '.join(s['annees']) or '—'}")
        print(f"  plein texte    : {'FTS5' if s['fts5'] else 'mode simple'}")
    elif "--chercher" in args:
        saisie = _option(args, "--chercher", "")
        total, res = rechercher(saisie, limite=15)
        print(f"{total} résultat(s) pour « {saisie} »")
        for r in res:
            extrait = (r.get("extrait") or "").replace("\x02", "[").replace("\x03", "]")
            print(f"\n  {r['nom']}\n    {r['date']} · {r['correspondant'] or '—'} · {r['objet'][:70]}")
            if extrait:
                print(f"    … {extrait[:160]}")
    elif "--depuis-registre" in args:
        print(f"{depuis_registre_csv(_option(args, '--depuis-registre'))} document(s) indexé(s).")
    elif "--archives-sheet" in args:
        ref = _option(args, "--archives-sheet")
        feuilles = [f.strip() for f in (_option(args, "--feuilles", "") or "").split(",") if f.strip()]
        libelle = _option(args, "--libelle", "") or ""
        if "--diagnostic" in args:
            for f in feuilles or feuilles_classeur(_connecteur(), id_classeur(ref)):
                diagnostic_feuille(ref, f)
        elif "--apercu" in args:
            n = depuis_sheet_externe(ref, feuilles or None, libelle, apercu=True)
            print(f"\nAperçu : {n} document(s) seraient indexés. Rien n'a été écrit.")
        else:
            n = depuis_sheet_externe(ref, feuilles or None, libelle)
            memoriser_source(ref, feuilles or None, libelle)
            print(f"{n} document(s) d'archives indexé(s). Source mémorisée : le bouton "
                  "« Réindexer » de l'écran Recherche la relira.")
    elif "--depuis-sheet" in args:
        print(f"{depuis_sheet()} document(s) indexé(s) depuis Zoho Sheet.")
    elif "--indexer-dossier" in args:
        dossier = _option(args, "--indexer-dossier")
        if not dossier:
            print("ERREUR : --indexer-dossier <dossier>")
            return
        n = depuis_dossier(dossier, avec_texte="--sans-texte" not in args)
        print(f"{n} fichier(s) indexé(s).")
    elif "--exporter" in args:
        chemin = _option(args, "--exporter", "index_ged.jsonl")
        print(f"{exporter(chemin)} document(s) exporté(s) dans {chemin}")
    elif "--importer" in args:
        chemin = _option(args, "--importer")
        n, retires = importer(chemin, synchroniser="--synchroniser" in args)
        print(f"{n} document(s) importé(s)."
              + (f" {retires} ancienne(s) fiche(s) de migration retirée(s)." if retires else ""))
    elif "--supprimer" in args:
        nom = _option(args, "--supprimer")
        print(f"{supprimer([nom]) if nom else 0} document(s) retiré(s).")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
