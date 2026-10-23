#!/usr/bin/env python3
"""
purger_visas.py — vide la file « à viser » du DG avant la mise en production
============================================================================
Retire de l'écran Visa Direction les documents en attente de visa déposés
AVANT une date donnée, et garde les plus récents. Rien n'est détruit : les
PDF et les fiches retirés sont rangés dans un dossier daté, d'où
--restaurer les remet en place à l'identique.

Pour chaque document retiré :
  - la fiche quitte visas.json, le PDF quitte a_viser/ ;
  - un courrier reçu suivi par le module Courrier passe « sans réponse
    attendue » (il ne compte plus dans les retards) ;
  - un projet de réponse qui attendait la validation du DG redevient
    « brouillon », son numéro de courrier sortant est libéré : on pourra
    le reprendre et le resoumettre.

USAGE (dans le conteneur, sur le serveur)
-----------------------------------------
  # aperçu : garde ce qui a été déposé depuis le 21 octobre 2026 inclus
  docker exec ged-wakati python /app/purger_visas.py --garder-depuis 2026-10-21

  # application
  docker exec ged-wakati python /app/purger_visas.py --garder-depuis 2026-10-21 --confirm

  # retour arrière
  docker exec ged-wakati python /app/purger_visas.py --restaurer /app/a_viser/_retires_20261023-1405

OPTIONS
-------
  --garder-depuis AAAA-MM-JJ   date de dépôt à partir de laquelle on garde (obligatoire)
  --avec-retournes             retire aussi les documents « retournés au service »
                               antérieurs à la date (par défaut : seulement « à viser »)
  --avec-signature             retire aussi les anciennes entrées « En attente de
                               signature » (en_attente.json) antérieures à la date
  --confirm                    applique (sinon : aperçu, rien n'est modifié)
"""

import argparse
import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

RACINE = Path(__file__).resolve().parent
sys.path.insert(0, str(RACINE))

import visa  # noqa: E402

try:
    import courrier  # noqa: E402
except Exception:  # noqa: BLE001
    courrier = None

FICHIER_ATTENTE = RACINE / "en_attente.json"
STATUTS_A_VISER = ("a_viser", "attente")


def jour(iso):
    try:
        return datetime.fromisoformat(str(iso)[:19]).date()
    except (ValueError, TypeError):
        return None


def lire_json(chemin, defaut):
    try:
        return json.loads(Path(chemin).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return defaut


def ecrire_json(chemin, donnees):
    Path(chemin).write_text(json.dumps(donnees, indent=2, ensure_ascii=False), encoding="utf-8")


def date_depot(d):
    """Date du dernier dépôt au visa (une resoumission remet le compteur)."""
    return jour(d.get("depose_le")) or jour(d.get("resoumis_le"))


# ---------------------------------------------------------------------------
def selectionner(visas, limite, avec_retournes):
    statuts = STATUTS_A_VISER + (("retourne",) if avec_retournes else ())
    retires, gardes, sans_date = {}, {}, {}
    for ref, d in visas.items():
        if d.get("statut") not in statuts:
            continue
        j = date_depot(d)
        if j is None:
            sans_date[ref] = d
        elif j < limite:
            retires[ref] = d
        else:
            gardes[ref] = d
    return retires, gardes, sans_date


def afficher(titre, fiches):
    print(f"\n{titre} : {len(fiches)}")
    for ref, d in sorted(fiches.items(), key=lambda x: str(x[1].get("depose_le", ""))):
        sens = "reçu" if d.get("sens") == "entrant" else "émis"
        print(f"  {str(d.get('depose_le', ''))[:10]}  {sens:5} {d.get('statut', ''):9} "
              f"{(d.get('correspondant') or '—')[:22]:22} {(d.get('objet') or '')[:48]}")


def purger(args):
    try:
        limite = date.fromisoformat(args.garder_depuis)
    except ValueError:
        print("ERREUR : --garder-depuis attend une date AAAA-MM-JJ.")
        sys.exit(1)

    visas = visa._charger()
    retires, gardes, sans_date = selectionner(visas, limite, args.avec_retournes)

    attente = lire_json(FICHIER_ATTENTE, {}) if args.avec_signature else {}
    attente_retires = {r: m for r, m in attente.items()
                       if (jour(m.get("soumis_le")) or limite) < limite}

    entrants = courrier.entrants() if courrier else {}
    reponses = courrier.reponses() if courrier else {}
    ents_lies = [r for r in retires if r in entrants
                 and entrants[r].get("statut_reponse") not in ("repondu", "sans_objet")]
    reps_liees = [k for k, x in reponses.items() if x.get("visa_ref") in retires]

    total = sum(1 for d in visas.values() if d.get("statut") in STATUTS_A_VISER)
    print(f"\n=== FILE DE VISA DU DG — {total} document(s) à viser au total ===")
    print(f"On garde ce qui a été déposé depuis le {limite.strftime('%d/%m/%Y')} inclus.")
    afficher("À RETIRER", retires)
    afficher("À GARDER", gardes)
    if sans_date:
        afficher("SANS DATE DE DÉPÔT (gardés, à voir à la main)", sans_date)
    if not args.avec_retournes:
        vieux_retours = sum(1 for d in visas.values() if d.get("statut") == "retourne"
                            and (date_depot(d) or limite) < limite)
        if vieux_retours:
            print(f"\n(i) {vieux_retours} document(s) « retourné(s) au service » antérieur(s) "
                  "à la date restent en place. Ajoute --avec-retournes pour les retirer aussi.")
    if args.avec_signature:
        print(f"\n« En attente de signature » à retirer : {len(attente_retires)} sur {len(attente)}")
    print(f"\nEffets liés : {len(ents_lies)} courrier(s) reçu(s) passeront « sans réponse attendue », "
          f"{len(reps_liees)} projet(s) de réponse redeviendront « brouillon ».")

    if not retires and not attente_retires:
        print("\nRien à retirer.")
        return
    if not args.confirm:
        print("\nAperçu seulement. Relance avec --confirm pour appliquer.")
        return

    # --- Sauvegarde complète avant toute modification -------------------
    horo = datetime.now().strftime("%Y%m%d-%H%M")
    dossier = visa.DOSSIER_A_VISER / f"_retires_{horo}"
    (dossier / "pdf").mkdir(parents=True, exist_ok=True)
    shutil.copy2(visa.FICHIER_VISAS, dossier / "visas_avant.json")
    if FICHIER_ATTENTE.exists():
        shutil.copy2(FICHIER_ATTENTE, dossier / "en_attente_avant.json")
    if courrier:
        for f in (courrier.F_ENTRANTS, courrier.F_REPONSES):
            if f.exists():
                shutil.copy2(f, dossier / f"{f.stem}_avant.json")

    # --- PDF -------------------------------------------------------------
    deplaces = 0
    for ref, d in retires.items():
        src = visa.DOSSIER_A_VISER / d.get("fichier", "")
        if d.get("fichier") and src.exists():
            shutil.move(str(src), str(dossier / "pdf" / src.name))
            deplaces += 1

    # --- visas.json ------------------------------------------------------
    visas = visa._charger()
    for ref in retires:
        visas.pop(ref, None)
    visa._sauver(visas)

    # --- en_attente.json -------------------------------------------------
    if attente_retires:
        attente = lire_json(FICHIER_ATTENTE, {})
        for r in attente_retires:
            attente.pop(r, None)
        ecrire_json(FICHIER_ATTENTE, attente)

    # --- Module Courrier -------------------------------------------------
    if courrier and (ents_lies or reps_liees):
        maintenant = datetime.now().isoformat(timespec="seconds")
        with courrier._verrou:
            ents = courrier.entrants()
            for r in ents_lies:
                e = ents.get(r)
                if e:
                    e["statut_reponse"] = "sans_objet"
                    e["echeance"] = ""
                    e["note_purge"] = f"Retiré de la file du DG le {maintenant[:10]}"
            courrier._ecrire(courrier.F_ENTRANTS, ents)

            reps = courrier.reponses()
            for k in reps_liees:
                x = reps.get(k)
                if not x:
                    continue
                x["statut"] = "brouillon"
                x["visa_ref"] = ""
                x["numero"] = ""
                x.setdefault("historique", []).append({
                    "le": maintenant, "par": "Nettoyage de mise en production",
                    "action": "Retiré de la file du DG",
                    "detail": "Remis en brouillon, numéro libéré : à resoumettre si besoin."})
            courrier._ecrire(courrier.F_REPONSES, reps)

    ecrire_json(dossier / "retires.json", {
        "le": datetime.now().isoformat(timespec="seconds"),
        "garder_depuis": args.garder_depuis,
        "visas": retires,
        "en_attente": attente_retires,
        "entrants_sans_objet": ents_lies,
        "reponses_en_brouillon": reps_liees,
    })

    print(f"\nFait : {len(retires)} document(s) retiré(s) de la file du DG, "
          f"{deplaces} PDF rangé(s), {len(attente_retires)} « en attente de signature » retiré(s).")
    print(f"Tout est sauvegardé dans : {dossier}")
    print(f"Retour arrière : python purger_visas.py --restaurer {dossier}")


def restaurer(dossier):
    dossier = Path(dossier)
    if not (dossier / "visas_avant.json").exists():
        print(f"ERREUR : {dossier} ne contient pas de sauvegarde (visas_avant.json).")
        sys.exit(1)
    infos = lire_json(dossier / "retires.json", {})
    # Les fiches retirées reviennent ; tout ce qui a été fait depuis est conservé.
    visas = visa._charger()
    for ref, d in (infos.get("visas") or {}).items():
        visas.setdefault(ref, d)
    visa._sauver(visas)
    n = 0
    for pdf in (dossier / "pdf").glob("*.pdf"):
        cible = visa.DOSSIER_A_VISER / pdf.name
        if not cible.exists():
            shutil.move(str(pdf), str(cible))
            n += 1
    if infos.get("en_attente"):
        attente = lire_json(FICHIER_ATTENTE, {})
        for r, m in infos["en_attente"].items():
            attente.setdefault(r, m)
        ecrire_json(FICHIER_ATTENTE, attente)
    if courrier:
        avant_e = lire_json(dossier / "entrants_avant.json", {})
        avant_r = lire_json(dossier / "reponses_avant.json", {})
        with courrier._verrou:
            ents = courrier.entrants()
            for r in infos.get("entrants_sans_objet") or []:
                if r in avant_e:
                    ents[r] = avant_e[r]
            courrier._ecrire(courrier.F_ENTRANTS, ents)
            reps = courrier.reponses()
            for k in infos.get("reponses_en_brouillon") or []:
                if k in avant_r and reps.get(k, {}).get("statut") == "brouillon":
                    reps[k] = avant_r[k]
            courrier._ecrire(courrier.F_REPONSES, reps)
    print(f"Restauré : {len(infos.get('visas') or {})} fiche(s), {n} PDF remis dans a_viser/.")


def main():
    p = argparse.ArgumentParser(description="Vide la file de visa du DG avant une date.")
    p.add_argument("--garder-depuis", help="date AAAA-MM-JJ : on garde ce qui a été déposé depuis")
    p.add_argument("--avec-retournes", action="store_true")
    p.add_argument("--avec-signature", action="store_true")
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--restaurer", metavar="DOSSIER")
    args = p.parse_args()
    if args.restaurer:
        restaurer(args.restaurer)
    elif args.garder_depuis:
        purger(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
