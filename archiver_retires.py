#!/usr/bin/env python3
"""
archiver_retires.py — archive dans la GED les documents sortis de la file du DG
==============================================================================
Après purger_visas.py --confirm, les documents retirés de la file de visa
sont rangés dans a_viser/_retires_AAAAMMJJ-HHMM/. Ce script les archive
comme le ferait l'écran « Archiver un document » :

  - nom HM26 : HM26-{SERVICE}-{TYPE}_{AAAAMMJJ}_{NNN}_{OBJET}.pdf, avec le
    numéro suivant de la série. Le dernier numéro est lu dans le registre
    Zoho Sheet ET dans le registre local, plus les numéros réservés par le
    module Courrier : pas de doublon avec les archives migrées ;
  - dépôt dans le dossier WorkDrive du service et du type ;
  - ligne au registre (Zoho Sheet + registre_2026.csv), statut
    « Archivé sans visa » : on sait ensuite que la pièce n'a pas reçu le
    visa du DG dans l'application ;
  - fiche dans le moteur de recherche, texte du PDF compris.

Relançable : un document déjà archivé (consigné dans archives.json du
dossier) n'est pas archivé deux fois. Une coupure réseau arrête le passage
proprement ; relance la même commande.

USAGE (dans le conteneur, sur le serveur)
-----------------------------------------
  docker exec ged-wakati python /app/archiver_retires.py /app/a_viser/_retires_20261023-1445
  docker exec ged-wakati python /app/archiver_retires.py /app/a_viser/_retires_20261023-1445 --confirm

OPTIONS
-------
  --confirm           archive (sinon : aperçu des noms, rien n'est envoyé)
  --exclure X [X …]   ne pas archiver ces documents (référence VISA-… ou mot de
                      l'objet / du correspondant)
  --statut "…"        statut écrit au registre (défaut : « Archivé sans visa »)
"""

import argparse
import csv
import json
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from datetime import datetime
from pathlib import Path

RACINE = Path(__file__).resolve().parent
sys.path.insert(0, str(RACINE))

import requests  # noqa: E402

MAP_FILE = RACINE / "folders_map.json"
REGISTRE = RACINE / "registre_2026.csv"
WORKBOOK_ID = "avaji4d47ec1cbffc4f5184dc8b3127b83ae7"
FEUILLE = "DATA"
DOSSIERS_DEFAUT = {"entrant": "avajid1a38194ab8f48baa462286eef5a1315",
                   "sortant": "5wh2ha7a8ad89bbac457a90d68d9d4d175f97"}

SERVICES = {"FIN": "Finances", "RH": "Ressources Humaines", "DGA": "Direction / Admin",
            "JUR": "Juridique", "COM": "Commercial", "EXP": "Exploitation"}
TYPES = {"FAC": "Facture", "CNT": "Contrat", "NOT": "Note de service", "VIR": "Virement",
         "DEM": "Demande", "ODM": "Ordre de mission", "ETA": "État / Relevé", "ACC": "Accord",
         "ATT": "Attestation", "BOR": "Bordereau", "CENT": "Courrier Entrant",
         "CSOR": "Courrier Sortant", "PV": "Procès-verbal", "DOC": "Document divers",
         "MAN": "Mandat", "DEC": "Décision"}
SENS = {"entrant": "Entrant", "sortant": "Sortant"}

ESSAIS = 3
PAUSE_RESEAU = 20


# ---------------------------------------------------------------------------
def norm(texte):
    t = unicodedata.normalize("NFKD", str(texte or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def nettoyer_texte(texte):
    """Même règle que l'écran d'archivage (app.py)."""
    if not texte:
        return "SANS_OBJET"
    texte = texte.upper()
    for a, b in {'É': 'E', 'È': 'E', 'Ê': 'E', 'Ë': 'E', 'À': 'A', 'Â': 'A', 'Ä': 'A',
                 'Ô': 'O', 'Ö': 'O', 'Î': 'I', 'Ï': 'I', 'Ç': 'C', 'Ù': 'U', 'Ú': 'U',
                 'Û': 'U', 'Ü': 'U', '’': "'"}.items():
        texte = texte.replace(a, b)
    texte = texte.replace(" ", "_")
    texte = re.sub(r"[^A-Z0-9_\-]", "", texte)
    return texte[:60]


def lire_json(chemin, defaut):
    try:
        return json.loads(Path(chemin).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return defaut


def ecrire_json(chemin, donnees):
    Path(chemin).write_text(json.dumps(donnees, indent=2, ensure_ascii=False), encoding="utf-8")


def reseau(exc):
    m = str(exc).lower()
    return any(k in m for k in ("resolve", "connection", "timed out", "timeout",
                                "max retries", "temporary failure", "unreachable"))


def exclu(ref, d, exclusions):
    for x in exclusions:
        if x.upper().startswith("VISA-"):
            if ref.upper() == x.upper():
                return True
        elif norm(x) in norm(d.get("objet")) or norm(x) in norm(d.get("correspondant")):
            return True
    return False


# ---------------------------------------------------------------------------
# Numérotation : registre Sheet + registre local + réservations du Courrier
# ---------------------------------------------------------------------------
MOTIF_NOM = re.compile(r"^HM(\d{2})-([A-Z]+)-([A-Z]+)_\d{8}_(\d{3})_")


def noms_sheet(zc):
    noms, debut, lot = [], 1, 500
    url = f"https://sheet.zoho.com/api/v2/{WORKBOOK_ID}"
    while True:
        r = requests.post(url, headers=zc._headers(
            {"Content-Type": "application/x-www-form-urlencoded"}),
            data={"method": "worksheet.records.fetch", "worksheet_name": FEUILLE,
                  "header_row": 1, "records_start_index": debut, "count": lot},
            timeout=60)
        r.raise_for_status()
        corps = r.json()
        if corps.get("status") == "failure":
            raise RuntimeError(f"Zoho Sheet : {corps}")
        lignes = corps.get("records") or []
        for ligne in lignes:
            nom = ligne.get("Nom du fichier") or next(iter(ligne.values()), "")
            noms.append(str(nom))
        if len(lignes) < lot:
            return noms
        debut += lot


def noms_csv():
    if not REGISTRE.exists():
        return []
    with open(REGISTRE, encoding="utf-8-sig") as f:
        return [l[0] for l in csv.reader(f, delimiter=";") if l]


def maxima(noms):
    mx = {}
    for nom in noms:
        m = MOTIF_NOM.match(nom.strip())
        if m and m.group(1) == "26":
            cle = (m.group(2), m.group(3))
            mx[cle] = max(mx.get(cle, 0), int(m.group(4)))
    try:
        import courrier  # noqa: PLC0415
        for x in courrier.reponses().values():
            if x.get("numero") and x.get("statut") != "abandonne":
                cle = (x.get("service", "DGA"), courrier.type_document(x))
                mx[cle] = max(mx.get(cle, 0), int(x["numero"]))
    except Exception:  # noqa: BLE001
        pass
    return mx


def dossier_workdrive(service, type_doc, sens):
    carte = lire_json(MAP_FILE, {})
    try:
        fid = carte["services"][service]["types"][type_doc]
        if fid:
            return fid
    except (KeyError, TypeError):
        pass
    return DOSSIERS_DEFAUT["sortant" if type_doc in ("CSOR", "DEC") or sens == "sortant"
                           else "entrant"]


# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Archive les documents retirés de la file du DG.")
    p.add_argument("dossier", help="dossier a_viser/_retires_… créé par purger_visas.py")
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--exclure", nargs="+", default=[], metavar="X")
    p.add_argument("--statut", default="Archivé sans visa")
    args = p.parse_args()

    dossier = Path(args.dossier)
    infos = lire_json(dossier / "retires.json", None)
    if not infos:
        print(f"ERREUR : {dossier}/retires.json introuvable — est-ce bien un dossier de purge ?")
        sys.exit(1)
    deja = lire_json(dossier / "archives.json", {})

    docs = []
    for ref, d in sorted((infos.get("visas") or {}).items(),
                         key=lambda x: (str(x[1].get("date_doc", "")), x[0])):
        if ref in deja:
            continue
        pdf = dossier / "pdf" / d.get("fichier", "")
        if exclu(ref, d, args.exclure):
            continue
        docs.append((ref, d, pdf))

    print(f"\n=== ARCHIVAGE DES DOCUMENTS RETIRÉS — {dossier.name} ===")
    print(f"  déjà archivés : {len(deja)}   à archiver : {len(docs)}")
    if not docs:
        print("\nRien à archiver.")
        return

    from zoho_connector import ZohoConnector  # noqa: PLC0415
    zc = ZohoConnector()
    try:
        noms = noms_sheet(zc)
        source = f"registre Zoho Sheet ({len(noms)} lignes) + registre local"
    except Exception as exc:  # noqa: BLE001
        noms = []
        source = f"registre local SEULEMENT — Sheet illisible ({exc})"
        if args.confirm:
            print(f"\nREFUS : sans le registre Zoho Sheet, les numéros risquent de doublonner "
                  f"les archives migrées.\n{exc}\nRelance quand la connexion est revenue.")
            sys.exit(1)
    mx = maxima(noms + noms_csv())
    print(f"  numérotation lue dans : {source}\n")

    plan = []
    for ref, d, pdf in docs:
        service = (d.get("service") or "DGA").upper()
        type_doc = (d.get("type_doc") or ("CENT" if d.get("sens") == "entrant" else "CSOR")).upper()
        date_doc = str(d.get("date_doc") or str(d.get("depose_le", ""))[:10])
        cle = (service, type_doc)
        mx[cle] = mx.get(cle, 0) + 1
        nom = (f"HM26-{service}-{type_doc}_{date_doc.replace('-', '')}_"
               f"{mx[cle]:03d}_{nettoyer_texte(d.get('objet'))}.pdf")
        manque = "" if pdf.exists() else "  (!) PDF ABSENT"
        plan.append((ref, d, pdf, service, type_doc, date_doc, nom))
        print(f"  {ref:22} -> {nom[:78]}{manque}")

    if not args.confirm:
        print(f"\nStatut au registre : « {args.statut} ».")
        print("Aperçu seulement. Relance avec --confirm pour archiver.")
        return

    try:
        import recherche  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        recherche = None

    n_ok = n_echec = coupures = 0
    tmp = Path(tempfile.mkdtemp(prefix="archivage_"))
    for ref, d, pdf, service, type_doc, date_doc, nom in plan:
        if not pdf.exists():
            print(f"  {ref}  PDF absent, ignoré")
            n_echec += 1
            continue
        sens = d.get("sens") or ("entrant" if type_doc == "CENT" else "sortant")
        copie = tmp / nom
        shutil.copy2(pdf, copie)
        lien = ""
        for essai in range(1, ESSAIS + 1):
            try:
                resp = zc.workdrive_upload(str(copie), dossier_workdrive(service, type_doc, sens))
                data = resp.get("data", {})
                data = data[0] if isinstance(data, list) and data else data
                attrs = (data or {}).get("attributes", {}) or {}
                rid = attrs.get("resource_id") or (data or {}).get("id") or ""
                lien = f"https://workdrive.zoho.com/file/{rid}" if rid else ""
                break
            except Exception as exc:  # noqa: BLE001
                if essai < ESSAIS and reseau(exc):
                    print(f"  {ref}  réseau coupé, nouvel essai dans {PAUSE_RESEAU} s…")
                    time.sleep(PAUSE_RESEAU)
                    continue
                print(f"  {ref}  ÉCHEC envoi WorkDrive : {exc}")
                break
        if not lien:
            n_echec += 1
            coupures += 1
            if coupures >= 3:
                print("\nARRÊT : connexion perdue. Relance la même commande, elle reprendra.")
                break
            continue
        coupures = 0

        correspondant = d.get("correspondant", "")
        ligne = {
            "Nom du fichier": nom, "Service": SERVICES.get(service, service),
            "Type": TYPES.get(type_doc, type_doc), "Date": date_doc, "Île": "",
            "Objet": d.get("objet", ""), "Branche GED": "Courriers",
            "Emplacement physique": "Zoho WorkDrive", "Statut": args.statut,
            "Lien Zoho": lien, "Sens": SENS.get(sens, ""), "Correspondant": correspondant,
        }
        with open(REGISTRE, "a", encoding="utf-8-sig", newline="") as f:
            csv.writer(f, delimiter=";").writerow(list(ligne.values()))
        registre_ok = False
        for essai in range(1, ESSAIS + 1):
            try:
                zc.sheet_add_row(WORKBOOK_ID, FEUILLE, ligne)
                registre_ok = True
                break
            except Exception as exc:  # noqa: BLE001
                if essai < ESSAIS and reseau(exc):
                    time.sleep(PAUSE_RESEAU)
                    continue
                print(f"  {ref}  (!) registre Sheet en échec : {exc} — ligne à ajouter à la main")

        if recherche is not None:
            try:
                recherche.indexer_fichier({
                    "nom": nom, "service": SERVICES.get(service, service),
                    "type_doc": TYPES.get(type_doc, type_doc), "date": date_doc,
                    "sens": SENS.get(sens, ""), "correspondant": correspondant,
                    "objet": d.get("objet", ""), "lien": lien,
                    "source": "archivage GED", "origine": d.get("fichier", ""),
                }, copie, en_tache=False)
            except Exception as exc:  # noqa: BLE001
                print(f"  {ref}  (recherche : {exc})")

        deja[ref] = {"nom": nom, "lien": lien, "registre_sheet": registre_ok,
                     "le": datetime.now().isoformat(timespec="seconds")}
        ecrire_json(dossier / "archives.json", deja)
        n_ok += 1
        print(f"  {ref}  archivé : {nom[:70]}")
        time.sleep(0.5)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nArchivés : {n_ok}   échecs : {n_echec}")
    print(f"Suivi : {dossier / 'archives.json'}")


if __name__ == "__main__":
    main()
