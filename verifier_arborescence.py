"""
verifier_arborescence.py — Contrôle en LECTURE SEULE de l'arborescence WorkDrive
=================================================================================
Compare folders_map.json à ce qui existe réellement dans WorkDrive, sans rien
créer, déplacer ni supprimer.

Signale :
  - les DOUBLONS : deux dossiers du même nom au même endroit (racine ou service) ;
  - les dossiers du mappage introuvables dans WorkDrive ;
  - les dossiers présents dans WorkDrive mais absents du mappage (ossature
    existante que la GED n'utilise pas) ;
  - les dossiers ajoutés depuis la sauvegarde (folders_map.json.avant-notes).

Usage :
    python3 verifier_arborescence.py
    python3 verifier_arborescence.py --avant folders_map.json.avant-notes
"""
import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zoho_connector import ZohoConnector  # noqa: E402
from zoho_config import WORKDRIVE_API, WORKDRIVE_ROOT_ID  # noqa: E402

ROOT_DIR = Path(__file__).resolve().parent
MAP_FILE = ROOT_DIR / "folders_map.json"


def lister(z, parent_id):
    """Tous les éléments d'un dossier (pagination WorkDrive par 50)."""
    elements, offset = [], 0
    while True:
        r = requests.get(f"{WORKDRIVE_API}/files/{parent_id}/files",
                         headers=z._headers(),
                         params={"page[limit]": 50, "page[offset]": offset},
                         timeout=30)
        r.raise_for_status()
        lot = r.json().get("data", [])
        elements.extend(lot)
        if len(lot) < 50:
            return elements
        offset += 50
        time.sleep(0.3)


def dossiers(elements):
    res = []
    for it in elements:
        a = it.get("attributes", {})
        if a.get("is_folder", a.get("type") == "folder"):
            res.append((a.get("name", "?"), it["id"], a.get("created_time", "")))
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--avant", help="ancienne version de folders_map.json à comparer")
    args = ap.parse_args()

    carte = json.loads(MAP_FILE.read_text(encoding="utf-8")).get("services", {})
    z = ZohoConnector()
    alertes = 0

    # --- Ajouts depuis la sauvegarde -------------------------------------
    if args.avant and Path(args.avant).exists():
        avant = json.loads(Path(args.avant).read_text(encoding="utf-8")).get("services", {})
        modifies = []
        for s, e in avant.items():
            if s not in carte:
                modifies.append(f"service {s} DISPARU du mappage")
                continue
            if e.get("id") != carte[s].get("id"):
                modifies.append(f"service {s} : identifiant CHANGÉ")
            for t, tid in e.get("types", {}).items():
                if carte[s].get("types", {}).get(t) != tid:
                    modifies.append(f"{s}/{t} : identifiant CHANGÉ ou supprimé")
        print("=== Comparaison avec la sauvegarde ===")
        if modifies:
            alertes += len(modifies)
            for m in modifies:
                print(f"  !! {m}")
        else:
            print("  Aucun identifiant existant modifié : uniquement des ajouts.")
        ajouts = [f"{s}/{t}" for s, e in carte.items() for t in e.get("types", {})
                  if t not in avant.get(s, {}).get("types", {})]
        print(f"  {len(ajouts)} entrée(s) ajoutée(s).\n")

    # --- Racine ------------------------------------------------------------
    print("=== Racine GED ===")
    racine = dossiers(lister(z, WORKDRIVE_ROOT_ID))
    ids_services = {e.get("id"): s for s, e in carte.items()}
    for nom, n in Counter(n for n, _, _ in racine).items():
        if n > 1:
            alertes += 1
            print(f"  !! DOUBLON à la racine : « {nom} » x{n}")
    for nom, fid, cree in sorted(racine):
        statut = f"utilisé par la GED ({ids_services[fid]})" if fid in ids_services \
            else "hors mappage (non utilisé par la GED)"
        print(f"  {nom:35} {statut}  créé {cree}")

    # --- Chaque service -----------------------------------------------------
    for code, e in carte.items():
        print(f"\n=== {code} ===")
        try:
            sous = dossiers(lister(z, e["id"]))
        except Exception as exc:  # noqa: BLE001
            alertes += 1
            print(f"  !! dossier service illisible : {exc}")
            continue
        noms = Counter(n for n, _, _ in sous)
        for nom, n in noms.items():
            if n > 1:
                alertes += 1
                print(f"  !! DOUBLON : « {nom} » x{n}")
        ids = {fid for _, fid, _ in sous}
        for t, tid in e.get("types", {}).items():
            if tid not in ids:
                alertes += 1
                print(f"  !! type {t} : dossier du mappage introuvable dans le service")
        utilises = set(e.get("types", {}).values())
        hors = [n for n, fid, _ in sous if fid not in utilises]
        print(f"  {len(sous)} sous-dossier(s), {len(utilises)} dans le mappage"
              + (f", hors mappage : {', '.join(sorted(hors))}" if hors else ""))
        time.sleep(0.3)

    print(f"\nBilan : {alertes} alerte(s). Rien n'a été modifié.")


if __name__ == "__main__":
    main()
