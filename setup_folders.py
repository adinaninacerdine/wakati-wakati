"""
setup_folders.py — Arborescence WorkDrive de la GED et folders_map.json
=======================================================================
Crée, pour chaque service, un sous-dossier par type de document, et
enregistre leurs identifiants dans folders_map.json.

Peut être relancé sans risque : les dossiers et identifiants déjà présents
dans folders_map.json sont conservés tels quels (y compris ceux ajoutés à
la main, comme Mandat). Seuls les manquants sont cherchés puis créés.

Usage :
    python setup_folders.py            # aperçu : ce qui serait créé
    python setup_folders.py --confirm  # crée les dossiers manquants

Sur le VPS, folders_map.json est monté en lecture seule dans le conteneur :
lancer ce script sur l'hôte (ou sur le poste), puis
    docker compose restart ged
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zoho_connector import ZohoConnector  # noqa: E402
from zoho_config import WORKDRIVE_ROOT_ID  # noqa: E402

ROOT_DIR = Path(__file__).resolve().parent
MAP_FILE = ROOT_DIR / "folders_map.json"

SERVICES = {
    "FIN": "Finances", "RH": "Ressources Humaines", "DGA": "Direction / Admin",
    "JUR": "Juridique", "COM": "Commercial", "EXP": "Exploitation"
}
TYPES = {
    "FAC": "Facture", "CNT": "Contrat", "NOT": "Note de service", "VIR": "Virement",
    "DEM": "Demande", "ODM": "Ordre de mission", "ETA": "État / Relevé", "ACC": "Accord",
    "ATT": "Attestation", "BOR": "Bordereau", "CENT": "Courrier Entrant",
    "CSOR": "Courrier Sortant", "PV": "Procès-verbal", "DOC": "Document divers",
    "MAN": "Mandat", "DEC": "Décision",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm", action="store_true",
                        help="crée réellement les dossiers manquants")
    args = parser.parse_args()

    folder_map = {"services": {}}
    if MAP_FILE.exists():
        folder_map = json.loads(MAP_FILE.read_text(encoding="utf-8"))
        folder_map.setdefault("services", {})

    manquants = [(s, t) for s in SERVICES for t in TYPES
                 if t not in folder_map["services"].get(s, {}).get("types", {})]
    services_manquants = [s for s in SERVICES if s not in folder_map["services"]]

    print(f"Racine WorkDrive : {WORKDRIVE_ROOT_ID}")
    print(f"Services absents du mappage : {services_manquants or 'aucun'}")
    print(f"Dossiers de type à ajouter  : {len(manquants)}")
    for s, t in manquants:
        print(f"  - {SERVICES[s]} / {TYPES[t]}")

    if not manquants:
        print("\nRien à faire : le mappage est complet.")
        return
    if not args.confirm:
        print("\nAperçu uniquement. Relancer avec --confirm pour créer.")
        return

    z = ZohoConnector()
    crees, erreurs = 0, 0
    for code_ser, nom_ser in SERVICES.items():
        besoin = [t for s, t in manquants if s == code_ser]
        if not besoin:
            continue
        entree = folder_map["services"].get(code_ser)
        if not entree:
            ser_id = z.workdrive_find_or_create_folder(nom_ser, WORKDRIVE_ROOT_ID)
            if not ser_id:
                print(f"ERREUR : service {nom_ser} introuvable et non créé")
                erreurs += 1
                continue
            entree = {"id": ser_id, "types": {}}
            folder_map["services"][code_ser] = entree
        for code_typ in besoin:
            try:
                typ_id = z.workdrive_find_or_create_folder(TYPES[code_typ], entree["id"])
                entree.setdefault("types", {})[code_typ] = typ_id
                crees += 1
                print(f"  OK  {nom_ser} / {TYPES[code_typ]}")
            except Exception as exc:  # noqa: BLE001
                erreurs += 1
                print(f"  KO  {nom_ser} / {TYPES[code_typ]} : {exc}")

    MAP_FILE.write_text(json.dumps(folder_map, ensure_ascii=False, indent=4),
                        encoding="utf-8")
    print(f"\nBilan : {crees} dossier(s) ajouté(s), {erreurs} erreur(s). "
          f"Mappage enregistré dans {MAP_FILE}")


if __name__ == '__main__':
    main()
