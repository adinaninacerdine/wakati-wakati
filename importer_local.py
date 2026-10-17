#!/usr/bin/env python3
"""
importer_local.py — import en masse : disque local -> WorkDrive -> registre 2026
================================================================================
Prend des fichiers qui sont SUR TON DISQUE, les depose dans le bon dossier
WorkDrive selon leur nom, puis ecrit la ligne correspondante dans le registre.

C'est l'inverse de indexer_archives.py, qui lui part de WorkDrive.
Les deux scripts cohabitent sans se gener.

CHAINE COMPLETE
---------------
    fichier local
      -> lecture du nom (service, type, date, numero, objet)
      -> resolution du dossier WorkDrive via folders_map.json
      -> controle des doublons (3 fronts : local, WorkDrive, registre)
      -> upload WorkDrive
      -> ecriture de la ligne au registre Sheet (feuille DATA)
      -> miroir dans registre_2026.csv

A LANCER DEPUIS LA RACINE DU PROJET (la ou se trouve zoho_config.py)
--------------------------------------------------------------------

  1) SONDAGE — a faire une fois, n'ecrit rien, n'uploade rien
     ./venv/bin/python importer_local.py --probe

     Verifie le token, liste les methodes d'upload disponibles sur ton
     connecteur, et teste les endpoints de listage WorkDrive. Les noms
     d'endpoints varient d'un compte a l'autre : sur ce projet, aucun
     n'a jamais correspondu a la documentation du premier coup.

  2) INVENTAIRE — lecture seule, rien n'est envoye ni ecrit
     ./venv/bin/python importer_local.py --scan --source ~/courriers_2026

     Produit deux rapports :
       import_inventaire.csv  — chaque fichier, sa destination, son statut
       import_doublons.csv    — les collisions, classees par niveau

  3) TEST SUR QUELQUES FICHIERS — upload + indexation reels, mais limites
     ./venv/bin/python importer_local.py --importer --source ~/courriers_2026 --limite 3

  4) IMPORT COMPLET
     ./venv/bin/python importer_local.py --importer --source ~/courriers_2026

OPTIONS
-------
  --source <dossier>   dossier local a importer (recursif). Obligatoire.
  --limite N           ne traiter que les N premiers fichiers eligibles.
  --deposant "Nom"     nom inscrit comme deposant. Defaut : "Import en masse".
  --sans-index         uploader sans ecrire au registre.
  --sans-upload        ecrire au registre sans uploader (fichiers deja en ligne).
  --ecraser            remplacer un fichier WorkDrive de meme nom au lieu de
                       le sauter. A n'utiliser qu'en connaissance de cause.
  --forcer             pas de confirmation interactive.
  --plat               ne pas descendre dans les sous-dossiers.

DETECTION DES DOUBLONS — TROIS FRONTS, QUATRE NIVEAUX
-----------------------------------------------------
Fronts :
  A. local contre local      deux fichiers du meme lot
  B. local contre WorkDrive  le fichier est deja en ligne
  C. local contre registre   le fichier est deja indexe

Niveaux :
  1. CONTENU  empreinte MD5 identique -> meme document, ECARTE
  2. NOM      nom de fichier identique -> ECARTE
  3. NUMERO   meme service/type/numero, fichiers differents -> ARBITRAGE
  4. OBJET    meme service/type/date, objet proche -> SIGNALE

Seuls les niveaux 1 et 2 ecartent un fichier. Les niveaux 3 et 4 sont
rapportes pour decision humaine : la numerotation du registre de courrier
est un acte administratif, pas un calcul.

SECURITE
--------
--scan n'ecrit rien, nulle part. --importer ajoute des lignes et des
fichiers, n'en modifie ni n'en supprime aucun (sauf --ecraser, explicite).
Aucun fichier local n'est deplace, renomme ou supprime.
Si le listage WorkDrive echoue, l'upload est REFUSE sans --forcer : sans
listage, on ne peut pas garantir l'absence de doublon, et WorkDrive
renomme silencieusement en "fichier-1.pdf" au lieu de refuser.
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
    print("Lance ce script depuis le dossier du projet (celui qui contient app.py).")
    sys.exit(1)

sys.path.insert(0, str(ROOT_DIR))

MAP_FILE = ROOT_DIR / "folders_map.json"
REGISTRE = ROOT_DIR / "registre_2026.csv"
RAPPORT = ROOT_DIR / "import_inventaire.csv"
RAPPORT_DOUBLONS = ROOT_DIR / "import_doublons.csv"
JOURNAL = ROOT_DIR / "import_journal.csv"

WORKBOOK_ID = "avaji4d47ec1cbffc4f5184dc8b3127b83ae7"
FEUILLE = "DATA"

# --- Endpoint de listage (a ajuster apres --probe) -------------------------
EP_FICHIERS = "/files/{fid}/files"

EP_CANDIDATS = [
    "/files/{fid}/files",
    "/files/{fid}/children",
    "/folders/{fid}/files",
]
# ---------------------------------------------------------------------------

EXTENSIONS = {".pdf"}

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
}

MOTIF_NOM = re.compile(
    r"^HM(?P<annee>\d{2})-(?P<service>[A-Z]+)-(?P<type>[A-Z]+)"
    r"_(?P<date>\d{8})"
    r"_(?P<numero>[A-Z]*\d+)"
    r"_(?P<objet>.*?)"
    r"\.(?P<ext>pdf)$",
    re.IGNORECASE,
)

PAUSE_UPLOAD = 0.5   # entre deux uploads WorkDrive
PAUSE_SHEET = 0.4    # entre deux ecritures Sheet


# ===========================================================================
# Connexion
# ===========================================================================
def connecteur():
    from zoho_connector import ZohoConnector
    return ZohoConnector()


def entetes(zc):
    """En-tetes d'authentification.

    On reutilise _headers() du connecteur quand il existe : c'est lui qui
    gere le rafraichissement du token et qui fonctionne en production.
    """
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


# ===========================================================================
# Carte des dossiers
# ===========================================================================
def charger_carte():
    if not MAP_FILE.exists():
        print(f"ERREUR : {MAP_FILE.name} introuvable dans {ROOT_DIR}.")
        sys.exit(1)
    return json.loads(MAP_FILE.read_text(encoding="utf-8"))


def resoudre_dossier(carte, service, type_doc):
    """Trouve l'identifiant du dossier WorkDrive pour un couple service/type.

    Retourne (folder_id, chemin_lisible) ou (None, raison).
    """
    services = carte.get("services") or {}

    bloc = services.get(service) or services.get(service.upper())
    if not bloc:
        return None, f"service {service} absent de folders_map.json"

    types = bloc.get("types") or {}
    fid = types.get(type_doc) or types.get(type_doc.upper())

    if not fid:
        # Repli : un dossier par defaut du service, s'il existe.
        fid = bloc.get("defaut") or bloc.get("_defaut") or bloc.get("racine")
        if fid:
            return fid, f"{service}/(defaut)"
        dispo = ", ".join(sorted(types.keys())) or "aucun"
        return None, f"type {type_doc} absent pour {service} (types connus : {dispo})"

    return fid, f"{service}/{type_doc}"


# ===========================================================================
# Lecture locale
# ===========================================================================
def empreinte(chemin):
    h = hashlib.md5()
    with open(chemin, "rb") as f:
        for bloc in iter(lambda: f.read(1024 * 256), b""):
            h.update(bloc)
    return h.hexdigest()


def analyser_nom(nom):
    """Metadonnees extraites du nom. None si le nom ne suit pas la norme."""
    m = MOTIF_NOM.match(nom)
    if not m:
        return None

    g = m.groupdict()
    brut = g["date"]

    if brut[4:6] == "00" or brut[6:] == "00":
        date_lisible = f"{brut[:4]}-00-00"
        date_valide = False
    else:
        date_lisible = f"{brut[:4]}-{brut[4:6]}-{brut[6:]}"
        date_valide = True

    return {
        "service": g["service"].upper(),
        "type_doc": g["type"].upper(),
        "date_doc": date_lisible,
        "date_valide": date_valide,
        "numero": g["numero"].upper(),
        "objet": g["objet"].replace("_", " ").strip(),
    }


def inventaire_local(source, plat=False):
    """Liste les fichiers du dossier source, avec metadonnees et empreinte."""
    racine = Path(source).expanduser().resolve()

    if not racine.exists():
        print(f"ERREUR : {racine} introuvable.")
        sys.exit(1)
    if not racine.is_dir():
        print(f"ERREUR : {racine} n'est pas un dossier.")
        sys.exit(1)

    motif = "*" if plat else "**/*"
    fichiers = []

    for chemin in sorted(racine.glob(motif)):
        if not chemin.is_file():
            continue
        if chemin.suffix.lower() not in EXTENSIONS:
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
            "meta": analyser_nom(chemin.name),
        })

    return racine, fichiers


# ===========================================================================
# Etat distant : WorkDrive
# ===========================================================================
def lister_dossier(zc, folder_id, verbeux=False):
    """Noms et empreintes des fichiers deja dans un dossier WorkDrive.

    Retourne (liste, ok). ok=False si l'endpoint n'a pas repondu.
    """
    resultat = []
    offset = 0
    limite = 50
    ok = True

    while True:
        url = base_workdrive() + EP_FICHIERS.format(fid=folder_id)
        try:
            r = requests.get(
                url,
                headers=entetes(zc),
                params={"page[limit]": limite, "page[offset]": offset},
                timeout=40,
            )
        except Exception as exc:  # noqa: BLE001
            if verbeux:
                print(f"    appel impossible : {exc}")
            return resultat, False

        if r.status_code != 200:
            if verbeux:
                print(f"    HTTP {r.status_code} {r.text[:150]}")
            return resultat, False

        try:
            lot = r.json().get("data", [])
        except ValueError:
            return resultat, False

        if not lot:
            break

        for item in lot:
            attrs = item.get("attributes", {}) or {}
            nom = (attrs.get("name") or attrs.get("display_attr_name")
                   or attrs.get("FileName") or "")
            if not nom:
                continue
            resultat.append({
                "nom": nom,
                "resource_id": item.get("id") or attrs.get("resource_id", ""),
                "md5": attrs.get("md5_checksum") or attrs.get("MD5_CHECKSUM") or "",
            })

        if len(lot) < limite:
            break
        offset += limite
        time.sleep(0.2)

    return resultat, ok


# ===========================================================================
# Etat distant : registre
# ===========================================================================
def charger_registre():
    """(noms deja indexes, cles service/type/numero deja utilisees)."""
    if not REGISTRE.exists():
        print(f"  {REGISTRE.name} absent : le registre est considere vide.")
        return set(), set()

    noms = set()
    cles = set()

    with open(REGISTRE, "r", encoding="utf-8-sig") as f:
        for i, ligne in enumerate(csv.reader(f, delimiter=";")):
            if i == 0 or not ligne or not ligne[0]:
                continue
            nom = ligne[0].strip()
            noms.add(nom)
            meta = analyser_nom(nom)
            if meta:
                cles.add((meta["service"], meta["type_doc"], meta["numero"]))

    return noms, cles


# ===========================================================================
# Doublons
# ===========================================================================
def _normaliser(texte):
    t = unicodedata.normalize("NFKD", texte or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", t.lower())


def analyser_doublons(fichiers, noms_registre, cles_registre, index_workdrive):
    """Qualifie chaque fichier et produit le rapport des collisions.

    Enrichit chaque fichier avec : statut, motif.
    Retourne la liste des anomalies.
    """
    rapport = []

    par_md5 = defaultdict(list)
    par_nom = defaultdict(list)
    par_numero = defaultdict(list)
    par_objet = defaultdict(list)

    for f in fichiers:
        par_md5[f["md5"]].append(f)
        par_nom[f["nom"]].append(f)
        m = f.get("meta")
        if m:
            par_numero[(m["service"], m["type_doc"], m["numero"])].append(f)
            par_objet[(m["service"], m["type_doc"], m["date_doc"],
                       _normaliser(m["objet"])[:24])].append(f)

    # --- Front A : local contre local ----------------------------------
    for md5, groupe in par_md5.items():
        if len(groupe) > 1:
            for f in groupe[1:]:
                f["statut"] = "doublon"
                f["motif"] = f"1-CONTENU identique a {groupe[0]['nom']}"
            rapport.append({
                "front": "A-LOCAL",
                "niveau": "1-CONTENU",
                "cle": md5[:12],
                "nombre": len(groupe),
                "details": " | ".join(f"{f['sous_dossier']}/{f['nom']}" for f in groupe),
                "decision": "Premier conserve, suivants ecartes",
            })

    for nom, groupe in par_nom.items():
        if len(groupe) > 1:
            for f in groupe[1:]:
                if f.get("statut") != "doublon":
                    f["statut"] = "doublon"
                    f["motif"] = "2-NOM deja present dans le lot"
            rapport.append({
                "front": "A-LOCAL",
                "niveau": "2-NOM",
                "cle": nom,
                "nombre": len(groupe),
                "details": " | ".join(f["sous_dossier"] for f in groupe),
                "decision": "Premier conserve, suivants ecartes",
            })

    # --- Front B : local contre WorkDrive ------------------------------
    for f in fichiers:
        if f.get("statut") == "doublon":
            continue
        distant = index_workdrive.get(f.get("folder_id") or "")
        if not distant:
            continue

        if f["md5"] and f["md5"] in distant["par_md5"]:
            f["statut"] = "deja_en_ligne"
            f["motif"] = "1-CONTENU deja dans WorkDrive"
            rapport.append({
                "front": "B-WORKDRIVE", "niveau": "1-CONTENU", "cle": f["nom"],
                "nombre": 2, "details": f["chemin_court"],
                "decision": "Non reuploade",
            })
        elif f["nom"] in distant["par_nom"]:
            f["statut"] = "deja_en_ligne"
            f["motif"] = "2-NOM deja dans WorkDrive"
            rapport.append({
                "front": "B-WORKDRIVE", "niveau": "2-NOM", "cle": f["nom"],
                "nombre": 2, "details": f["chemin_court"],
                "decision": "Non reuploade (--ecraser pour remplacer)",
            })

    # --- Front C : local contre registre -------------------------------
    for f in fichiers:
        if f.get("statut") in ("doublon",):
            continue
        if f["nom"] in noms_registre:
            f["deja_indexe"] = True
            rapport.append({
                "front": "C-REGISTRE", "niveau": "2-NOM", "cle": f["nom"],
                "nombre": 1, "details": f["chemin_court"],
                "decision": "Deja au registre, pas de nouvelle ligne",
            })

    # --- Niveau 3 : conflit de numerotation ----------------------------
    for (service, type_doc, numero), groupe in par_numero.items():
        noms = {f["nom"] for f in groupe}
        conflit_registre = (service, type_doc, numero) in cles_registre
        if len(noms) > 1 or (conflit_registre and not any(
                f["nom"] in noms_registre for f in groupe)):
            rapport.append({
                "front": "A-LOCAL" if len(noms) > 1 else "C-REGISTRE",
                "niveau": "3-NUMERO",
                "cle": f"{service}-{type_doc}_{numero}",
                "nombre": len(noms),
                "details": " | ".join(sorted(noms)),
                "decision": "ARBITRAGE REQUIS — numero d'enregistrement en conflit",
            })

    # --- Niveau 4 : objets proches -------------------------------------
    for (service, type_doc, date_doc, cle_objet), groupe in par_objet.items():
        noms = {f["nom"] for f in groupe}
        if len(noms) > 1 and cle_objet:
            rapport.append({
                "front": "A-LOCAL",
                "niveau": "4-OBJET",
                "cle": f"{service}-{type_doc} {date_doc}",
                "nombre": len(noms),
                "details": " | ".join(sorted(noms)),
                "decision": "Suspect, non ecarte — a verifier",
            })

    return rapport


# ===========================================================================
# Qualification
# ===========================================================================
def qualifier(zc, fichiers, carte, sans_upload):
    """Resout la destination de chaque fichier et releve l'etat WorkDrive."""
    index_workdrive = {}
    listage_ok = True
    dossiers_vus = set()

    for f in fichiers:
        f["chemin_court"] = f"{f['sous_dossier']}/{f['nom']}"
        f.setdefault("statut", "")
        f.setdefault("motif", "")
        f.setdefault("deja_indexe", False)
        f["folder_id"] = ""
        f["destination"] = ""

        if not f["pdf_valide"]:
            f["statut"] = "non_pdf"
            f["motif"] = "l'entete du fichier n'est pas %PDF"
            continue

        if not f["meta"]:
            f["statut"] = "nom_non_conforme"
            f["motif"] = "nom hors norme HM26-SERVICE-TYPE_DATE_NUMERO_OBJET.pdf"
            continue

        fid, info = resoudre_dossier(carte, f["meta"]["service"], f["meta"]["type_doc"])
        if not fid:
            f["statut"] = "sans_destination"
            f["motif"] = info
            continue

        f["folder_id"] = fid
        f["destination"] = info
        dossiers_vus.add(fid)

    # Un seul listage par dossier concerne.
    if not sans_upload:
        print(f"\nRelevé de {len(dossiers_vus)} dossier(s) WorkDrive concerné(s)")
        for fid in sorted(dossiers_vus):
            distant, ok = lister_dossier(zc, fid, verbeux=True)
            if not ok:
                listage_ok = False
            index_workdrive[fid] = {
                "par_nom": {d["nom"] for d in distant},
                "par_md5": {d["md5"] for d in distant if d["md5"]},
            }
            print(f"  {fid[:18]}… {len(distant)} fichier(s) {'' if ok else '(LISTAGE ECHOUE)'}")

    return index_workdrive, listage_ok


# ===========================================================================
# Rapports
# ===========================================================================
def ecrire_rapports(racine, fichiers, doublons):
    with open(RAPPORT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow([
            "Fichier", "Sous-dossier", "Taille (Ko)", "Service", "Type",
            "Date", "Date valide", "Numero", "Objet", "Destination",
            "Statut", "Motif", "Deja au registre",
        ])
        for fic in fichiers:
            m = fic.get("meta") or {}
            w.writerow([
                fic["nom"], fic["sous_dossier"], f"{fic['taille'] / 1024:.1f}",
                m.get("service", ""), m.get("type_doc", ""),
                m.get("date_doc", ""), "oui" if m.get("date_valide") else "non",
                m.get("numero", ""), m.get("objet", ""),
                fic.get("destination", ""),
                fic.get("statut") or "a importer",
                fic.get("motif", ""),
                "oui" if fic.get("deja_indexe") else "non",
            ])

    with open(RAPPORT_DOUBLONS, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Front", "Niveau", "Cle", "Nombre", "Details", "Decision"])
        for d in sorted(doublons, key=lambda x: (x["niveau"], x["front"])):
            w.writerow([d["front"], d["niveau"], d["cle"], d["nombre"],
                        d["details"], d["decision"]])

    print(f"\n  {RAPPORT.name}         {len(fichiers)} ligne(s)")
    print(f"  {RAPPORT_DOUBLONS.name}  {len(doublons)} anomalie(s)")


def resume(fichiers, doublons):
    compte = defaultdict(int)
    for f in fichiers:
        compte[f.get("statut") or "a importer"] += 1

    print("\n--- INVENTAIRE ---")
    for statut in sorted(compte):
        print(f"  {statut:22} {compte[statut]:>4}")

    niveaux = defaultdict(int)
    for d in doublons:
        niveaux[d["niveau"]] += 1
    if niveaux:
        print("\n--- ANOMALIES ---")
        for n in sorted(niveaux):
            print(f"  {n:22} {niveaux[n]:>4}")

    arbitrages = [d for d in doublons if d["niveau"] == "3-NUMERO"]
    if arbitrages:
        print(f"\n  (!) {len(arbitrages)} conflit(s) de numerotation. Relis "
              f"{RAPPORT_DOUBLONS.name} avant d'importer :")
        for d in arbitrages[:5]:
            print(f"      {d['cle']} -> {d['details'][:80]}")
        if len(arbitrages) > 5:
            print(f"      … et {len(arbitrages) - 5} autre(s)")


# ===========================================================================
# Upload
# ===========================================================================
def _methode_upload(zc):
    """Cherche une methode d'upload deja presente sur le connecteur."""
    for nom in ("workdrive_upload", "upload_file", "upload", "uploader",
                "televerser", "wd_upload"):
        if hasattr(zc, nom) and callable(getattr(zc, nom)):
            return nom
    return None


def uploader(zc, fic, ecraser=False):
    """Depose le fichier dans WorkDrive. Retourne (ok, resource_id, message).

    POST direct plutot que zc.workdrive_upload() pour trois raisons :
      - workdrive_upload ne passe pas `override-name-exist`, et WorkDrive
        renomme alors silencieusement en "fichier-1.pdf" : c'est exactement
        le doublon que tout ce script cherche a eviter.
      - workdrive_upload ne passe pas `filename` : le nom depend du chemin.
      - son raise_for_status() avale le corps de la reponse, donc le vrai
        message d'erreur de Zoho, qui est toujours le plus utile.

    La forme de l'appel reprend celle du connecteur (parent_id en form-data,
    contenu en multipart), qui est eprouvee sur ce compte.
    """
    url = base_workdrive() + "/upload"

    try:
        with open(fic["chemin"], "rb") as flux:
            r = requests.post(
                url,
                headers=entetes(zc),
                files={"content": (fic["nom"], flux, "application/pdf")},
                data={
                    "filename": fic["nom"],
                    "parent_id": fic["folder_id"],
                    "override-name-exist": "true" if ecraser else "false",
                },
                timeout=180,
            )
    except Exception as exc:  # noqa: BLE001
        return False, "", f"appel impossible : {exc}"

    if r.status_code not in (200, 201):
        # On affiche le corps : raise_for_status() l'avale et on perd le
        # vrai message de Zoho, qui est toujours le plus utile.
        return False, "", f"HTTP {r.status_code} {r.text[:300]}"

    try:
        payload = r.json()
    except ValueError:
        return False, "", f"reponse illisible : {r.text[:200]}"

    rid = ""
    try:
        data = payload.get("data")
        premier = data[0] if isinstance(data, list) and data else data or {}
        attrs = premier.get("attributes", premier) or {}
        rid = (attrs.get("resource_id") or attrs.get("ResourceId")
               or premier.get("id") or "")
        nom_reel = attrs.get("FileName") or attrs.get("name") or fic["nom"]
    except (AttributeError, IndexError, TypeError):
        nom_reel = fic["nom"]

    if not rid:
        return False, "", f"resource_id absent de la reponse : {str(payload)[:250]}"

    # WorkDrive renomme en "fichier-1.pdf" quand le nom existe deja et que
    # override est a false. On le signale : c'est exactement le doublon
    # qu'on cherche a eviter.
    if nom_reel != fic["nom"]:
        return True, rid, f"ATTENTION renomme par WorkDrive en {nom_reel}"

    return True, rid, ""


# ===========================================================================
# Indexation
# ===========================================================================
def ligne_registre(fic, rid, deposant):
    m = fic["meta"]
    lien = f"https://workdrive.zoho.com/file/{rid}" if rid else ""
    return {
        "Nom du fichier": fic["nom"],
        "Service": SERVICES.get(m["service"], m["service"]),
        "Type": TYPES.get(m["type_doc"], m["type_doc"]),
        "Date": m["date_doc"],
        "Île": "",
        "Objet": m["objet"],
        "Branche GED": "Courriers",
        "Emplacement physique": "Zoho WorkDrive",
        "Statut": "Classé",
        "Lien Zoho": lien,
    }


def indexer(zc, fic, rid, deposant):
    ligne = ligne_registre(fic, rid, deposant)

    try:
        zc.sheet_add_row(WORKBOOK_ID, FEUILLE, ligne)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]

    # Miroir local, pour que le tableau de bord reste coherent.
    try:
        nouveau = not REGISTRE.exists()
        with open(REGISTRE, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            if nouveau:
                w.writerow(list(ligne.keys()))
            w.writerow(list(ligne.values()))
    except OSError as exc:
        return True, f"Sheet ecrit mais miroir CSV en echec : {exc}"

    return True, ""


def journaliser(entrees):
    nouveau = not JOURNAL.exists()
    with open(JOURNAL, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        if nouveau:
            w.writerow(["Horodatage", "Fichier", "Destination", "resource_id",
                        "Upload", "Index", "Message"])
        w.writerows(entrees)


# ===========================================================================
# Sondage
# ===========================================================================
def sonder():
    print("\n=== SONDAGE ===\n")

    print("1) Connexion")
    try:
        zc = connecteur()
        jeton = zc._token()
        print(f"   token obtenu : {jeton[:12]}…")
    except Exception as exc:  # noqa: BLE001
        print(f"   ECHEC : {exc}")
        return
    print(f"   base WorkDrive : {base_workdrive()}")

    print("\n2) Methodes du connecteur")
    m = _methode_upload(zc)
    print(f"   methode d'upload presente : {m or 'aucune'}")
    print("   -> non utilisee : POST direct, pour passer override-name-exist")
    print("      et pour afficher le corps des reponses d'erreur.")
    print(f"   sheet_add_row present     : {hasattr(zc, 'sheet_add_row')}")
    print(f"   _headers present          : {hasattr(zc, '_headers')}")

    print("\n3) Carte des dossiers")
    carte = charger_carte()
    cibles = []
    for service, bloc in (carte.get("services") or {}).items():
        for type_doc, fid in (bloc.get("types") or {}).items():
            if fid:
                cibles.append((service, type_doc, fid))
    print(f"   {len(cibles)} couple(s) service/type declare(s)")
    for service, type_doc, fid in cibles[:8]:
        print(f"     {service}-{type_doc}  -> {fid}")
    if len(cibles) > 8:
        print(f"     … et {len(cibles) - 8} autre(s)")

    if not cibles:
        print("   Aucun dossier declare : folders_map.json est a completer.")
        return

    print("\n4) Endpoints de listage")
    fid_test = cibles[0][2]
    gagnant = None
    for ep in EP_CANDIDATS:
        url = base_workdrive() + ep.format(fid=fid_test)
        try:
            r = requests.get(url, headers=entetes(zc),
                             params={"page[limit]": 1}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            print(f"   {ep:28} injoignable ({exc})")
            continue
        marque = "OK" if r.status_code == 200 else f"HTTP {r.status_code}"
        print(f"   {ep:28} {marque} {r.text[:90] if r.status_code != 200 else ''}")
        if r.status_code == 200 and not gagnant:
            gagnant = ep

    print("\n=== A REPORTER DANS CE FICHIER ===")
    if gagnant:
        print(f'   EP_FICHIERS = "{gagnant}"')
        if gagnant == EP_FICHIERS:
            print("   (c'est deja la valeur en place : rien a changer)")
    else:
        print("   Aucun endpoint de listage n'a repondu 200.")
        print("   Sans listage, la detection des doublons cote WorkDrive est")
        print("   impossible : l'import exigera --forcer, et WorkDrive pourra")
        print("   creer des 'fichier-1.pdf'. Corrige d'abord l'endpoint.")

    print("\nAucun fichier n'a ete envoye, aucune ligne ecrite.")


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

    if "--probe" in args:
        sonder()
        return

    mode_scan = "--scan" in args
    mode_import = "--importer" in args

    if not (mode_scan or mode_import):
        print(__doc__)
        print("Choisis --probe, --scan ou --importer.")
        return

    source = valeur_option(args, "--source")
    if not source:
        print("ERREUR : --source <dossier> est obligatoire.")
        print("Exemple : --source ~/courriers_2026")
        return

    deposant = valeur_option(args, "--deposant", "Import en masse")
    limite = valeur_option(args, "--limite")
    limite = int(limite) if limite and limite.isdigit() else None

    plat = "--plat" in args
    forcer = "--forcer" in args
    ecraser = "--ecraser" in args
    sans_index = "--sans-index" in args
    sans_upload = "--sans-upload" in args

    print("\n=== IMPORT LOCAL -> WORKDRIVE -> REGISTRE ===")
    print(f"Mode       : {'IMPORT (ecriture)' if mode_import else 'SCAN (lecture seule)'}")
    print(f"Projet     : {ROOT_DIR}")

    # --- Lecture locale -------------------------------------------------
    racine, fichiers = inventaire_local(source, plat=plat)
    print(f"Source     : {racine}")
    print(f"Trouve     : {len(fichiers)} fichier(s) PDF")

    if not fichiers:
        print("\nRien a traiter. Verifie --source, et retire --plat si les")
        print("fichiers sont dans des sous-dossiers.")
        return

    carte = charger_carte()

    try:
        zc = connecteur()
    except Exception as exc:  # noqa: BLE001
        print(f"\nERREUR de connexion Zoho : {exc}")
        return

    # --- Qualification --------------------------------------------------
    index_workdrive, listage_ok = qualifier(zc, fichiers, carte, sans_upload)

    print("\nLecture du registre")
    noms_registre, cles_registre = charger_registre()
    print(f"  {len(noms_registre)} entree(s) deja au registre")

    print("\nAnalyse des doublons")
    doublons = analyser_doublons(fichiers, noms_registre, cles_registre,
                                 index_workdrive)

    resume(fichiers, doublons)
    ecrire_rapports(racine, fichiers, doublons)

    # --- Selection ------------------------------------------------------
    eligibles = [f for f in fichiers if not f.get("statut")]
    a_reindexer = [f for f in fichiers
                   if f.get("statut") == "deja_en_ligne" and not f.get("deja_indexe")]

    if sans_upload:
        eligibles = [f for f in fichiers
                     if f.get("statut") in ("", "deja_en_ligne")
                     and not f.get("deja_indexe") and f.get("meta")]

    if limite:
        eligibles = eligibles[:limite]
        print(f"\n  --limite {limite} : {len(eligibles)} fichier(s) retenu(s)")

    if mode_scan:
        print(f"\n{len(eligibles)} fichier(s) seraient importe(s).")
        if a_reindexer:
            print(f"{len(a_reindexer)} fichier(s) sont deja en ligne mais pas au")
            print("registre : relance avec --importer --sans-upload pour les indexer.")
        print(f"\nRelis {RAPPORT.name} et {RAPPORT_DOUBLONS.name}, puis --importer.")
        print("Rien n'a ete envoye ni ecrit.")
        return

    # --- Garde-fous -----------------------------------------------------
    if not eligibles:
        print("\nAucun fichier eligible. Consulte les rapports.")
        return

    if not listage_ok and not sans_upload and not forcer:
        print("\nREFUS : le listage WorkDrive a echoue sur au moins un dossier.")
        print("Sans listage, impossible de garantir l'absence de doublon, et")
        print("WorkDrive creera des 'fichier-1.pdf' silencieusement.")
        print("Lance --probe pour corriger EP_FICHIERS, ou --forcer en")
        print("connaissance de cause.")
        return

    print("\n--- A IMPORTER ---")
    for f in eligibles[:10]:
        print(f"  {f['nom'][:58]:58} -> {f['destination']}")
    if len(eligibles) > 10:
        print(f"  … et {len(eligibles) - 10} autre(s)")

    print(f"\n  upload      : {'non' if sans_upload else 'oui'}")
    print(f"  indexation  : {'non' if sans_index else 'oui'}")
    print(f"  ecrasement  : {'OUI' if ecraser else 'non'}")
    duree = len(eligibles) * (PAUSE_UPLOAD + PAUSE_SHEET) + len(eligibles) * 1.5
    print(f"  duree estimee : environ {duree / 60:.1f} min")

    if not forcer:
        rep = input(f"\nImporter {len(eligibles)} fichier(s) ? (oui/non) ").strip().lower()
        if rep not in ("oui", "o", "yes", "y"):
            print("Annule. Rien n'a ete fait.")
            return

    # --- Execution ------------------------------------------------------
    print("\n--- EXECUTION ---")
    horodatage = time.strftime("%Y-%m-%d %H:%M:%S")
    journal = []
    ok_upload = ok_index = echecs = 0

    for i, fic in enumerate(eligibles, 1):
        prefixe = f"  [{i}/{len(eligibles)}]"
        rid = ""
        msg = ""
        etat_up = "saute"
        etat_idx = "saute"

        if not sans_upload:
            succes, rid, msg = uploader(zc, fic, ecraser=ecraser)
            etat_up = "ok" if succes else "ECHEC"
            if succes:
                ok_upload += 1
            else:
                echecs += 1
                print(f"{prefixe} ECHEC upload {fic['nom'][:45]} — {msg}")
                journal.append([horodatage, fic["nom"], fic["destination"],
                                "", etat_up, etat_idx, msg])
                time.sleep(PAUSE_UPLOAD)
                continue
            time.sleep(PAUSE_UPLOAD)

        if not sans_index and not fic.get("deja_indexe"):
            succes, erreur = indexer(zc, fic, rid, deposant)
            etat_idx = "ok" if succes else "ECHEC"
            if succes:
                ok_index += 1
                if erreur:
                    msg = (msg + " / " + erreur).strip(" /")
            else:
                echecs += 1
                msg = (msg + " / " + erreur).strip(" /")
                print(f"{prefixe} ECHEC index {fic['nom'][:45]} — {erreur}")
            time.sleep(PAUSE_SHEET)

        if msg:
            print(f"{prefixe} {fic['nom'][:50]} — {msg}")
        else:
            print(f"{prefixe} {fic['nom'][:60]}")

        journal.append([horodatage, fic["nom"], fic["destination"], rid,
                        etat_up, etat_idx, msg])

    journaliser(journal)

    print("\n" + "=" * 52)
    print(f"  uploads reussis    : {ok_upload}")
    print(f"  lignes au registre : {ok_index}")
    print(f"  echecs             : {echecs}")
    print(f"  journal            : {JOURNAL.name}")
    if a_reindexer and not sans_upload:
        print(f"\n  {len(a_reindexer)} fichier(s) etaient deja en ligne sans etre")
        print("  indexes. Pour les ajouter au registre :")
        print(f"    ./venv/bin/python {Path(__file__).name} --importer "
              f"--sans-upload --source {source}")


if __name__ == "__main__":
    main()
