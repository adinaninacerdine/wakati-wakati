#!/usr/bin/env python3
"""
purger_visas.py — remet à zéro les files du DG avant la mise en production
=========================================================================
Trois nettoyages, combinables, tous avec aperçu, sauvegarde et retour arrière :

  1. --garder-depuis AAAA-MM-JJ
     Les documents « à viser » déposés AVANT cette date quittent l'écran
     Visa Direction ; les plus récents restent chez le DG.

  2. --approuves
     La section « Approuvés — en attente de signature manuscrite » est vidée.

  Dans les deux cas, les documents passent dans « Signés, à archiver »
  (écran Courriers) : la responsable GED les classe elle-même, bouton
  Archiver (numéro, dossier WorkDrive, registre). Rien n'est archivé
  automatiquement.

  2 bis. --archiver X [X …]
     Des documents précis (à viser, approuvés ou retournés), quelle que soit
     leur date, passent dans « Signés, à archiver ». X = une référence VISA-…
     ou un mot de l'objet ou du correspondant.

  3. --supprimer X [X …]
     Retire DÉFINITIVEMENT de la GED des documents de test : ni file du DG,
     ni « à archiver ». X = une référence VISA-… lue dans l'aperçu, ou un mot
     de l'objet ou du correspondant. Le PDF est gardé dans la sauvegarde.

Détails :
  - le PDF passe de a_viser/ à a_classer/ (préfixe ASIGNER_, celui que
    l'écran de classement reconnaît) ; une copie reste dans la sauvegarde ;
  - la fiche de visas.json est conservée avec le statut « retire_file » :
    l'écran d'archivage retrouve ainsi le sens et le correspondant ;
  - un projet de réponse qui attendait la validation du DG redevient
    « brouillon », son numéro de courrier sortant est libéré ;
  - --restaurer remet tout ce qui n'a pas encore été archivé.

Le courrier fictif de la démonstration se retire à part, entièrement :
  docker exec ged-wakati python /app/nettoyer_demo.py --mot Mdjihari --confirm

USAGE (dans le conteneur, sur le serveur)
-----------------------------------------
  # aperçu
  docker exec ged-wakati python /app/purger_visas.py --approuves \\
      --garder-depuis 2026-10-21 --exclure VISA-20261019-114530

  # un document précis, quelle que soit sa date
  docker exec ged-wakati python /app/purger_visas.py --archiver "Situation financière"

  # application : ajouter --confirm à la même commande
  # retour arrière
  docker exec ged-wakati python /app/purger_visas.py --restaurer /app/a_viser/_retires_20261023-1530

OPTIONS
-------
  --garder-depuis AAAA-MM-JJ   « à viser » : restent chez le DG les dépôts depuis cette date
  --exclure X [X …]            « à viser » anciens à laisser quand même chez le DG
  --approuves                  vide la section « Approuvés » vers « Signés, à archiver »
  --archiver X [X …]           documents précis vers « Signés, à archiver » (toute date)
  --supprimer X [X …]          documents de test à retirer définitivement
  --avec-retournes             déplace aussi les « retournés au service » antérieurs à la date
  --avec-signature             retire aussi les anciennes entrées « En attente de signature »
  --confirm                    applique (sinon : aperçu, rien n'est modifié)
"""

import argparse
import copy
import json
import shutil
import sys
import unicodedata
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
STATUT_RETIRE = "retire_file"


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


def norm(texte):
    t = unicodedata.normalize("NFKD", str(texte or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def designe(ref, d, liste):
    """Vrai si le document est désigné par une référence VISA-… ou un mot."""
    for x in liste or []:
        if x.upper().startswith("VISA-"):
            if ref.upper() == x.upper():
                return True
        elif norm(x) in norm(d.get("objet")) or norm(x) in norm(d.get("correspondant")):
            return True
    return False


def date_depot(d):
    """Date du dernier dépôt au visa (une resoumission remet le compteur)."""
    return jour(d.get("depose_le")) or jour(d.get("resoumis_le"))


def nom_a_classer(fichier):
    return fichier.replace("AVISER_", "ASIGNER_", 1) if fichier.startswith("AVISER_") \
        else f"ASIGNER_{fichier}"


def afficher(titre, fiches):
    print(f"\n{titre} : {len(fiches)}")
    for ref, d in sorted(fiches.items(), key=lambda x: str(x[1].get("depose_le", ""))):
        sens = "reçu" if d.get("sens") == "entrant" else "émis"
        print(f"  {ref:22} {str(d.get('depose_le', ''))[:10]}  {sens:5} {d.get('statut', ''):9} "
              f"{(d.get('correspondant') or '—')[:20]:20} {(d.get('objet') or '')[:44]}")


# ---------------------------------------------------------------------------
def purger(args):
    limite = None
    if args.garder_depuis:
        try:
            limite = date.fromisoformat(args.garder_depuis)
        except ValueError:
            print("ERREUR : --garder-depuis attend une date AAAA-MM-JJ.")
            sys.exit(1)

    visas = visa._charger()
    toutes_refs = {r.upper() for r in visas}
    inconnues = [x for x in (args.exclure or []) + (args.supprimer or []) + (args.archiver or [])
                 if x.upper().startswith("VISA-") and x.upper() not in toutes_refs]

    # --- 1. File « à viser » -------------------------------------------
    file_retires, file_gardes, sans_date, exclus = {}, {}, {}, {}
    if limite:
        statuts = STATUTS_A_VISER + (("retourne",) if args.avec_retournes else ())
        for ref, d in visas.items():
            if d.get("statut") not in statuts:
                continue
            j = date_depot(d)
            if j is None:
                sans_date[ref] = d
            elif j < limite and not designe(ref, d, args.exclure):
                file_retires[ref] = d
            elif j < limite:
                exclus[ref] = d
            else:
                file_gardes[ref] = d

    # --- 2. Section « Approuvés » --------------------------------------
    approuves = {r: d for r, d in visas.items() if d.get("statut") == "approuve"} \
        if args.approuves else {}

    # --- 3. Tests à supprimer : en circuit, ou déjà passés dans « Signés,
    #        à archiver » par un nettoyage précédent (statut retire_file) --
    en_circuit = {r: d for r, d in visas.items()
                  if d.get("statut") in STATUTS_A_VISER + ("approuve", "retourne", STATUT_RETIRE)}
    a_supprimer = {r: d for r, d in en_circuit.items() if designe(r, d, args.supprimer)}

    # --- 2 bis. Documents désignés, toute date ------------------------
    designes = {r: d for r, d in visas.items()
                if d.get("statut") in STATUTS_A_VISER + ("approuve", "retourne")
                and designe(r, d, args.archiver)} if args.archiver else {}

    a_deplacer = {r: d for r, d in {**file_retires, **approuves, **designes}.items()
                  if r not in a_supprimer}

    attente = lire_json(FICHIER_ATTENTE, {}) if args.avec_signature and limite else {}
    attente_retires = {r: m for r, m in attente.items()
                       if (jour(m.get("soumis_le")) or limite) < limite}

    concernes = set(a_deplacer) | set(a_supprimer)
    reponses = courrier.reponses() if courrier else {}
    reps_liees = [k for k, x in reponses.items() if x.get("visa_ref") in concernes
                  and x.get("statut") in ("a_valider", "en_signature")]

    # --- Aperçu ----------------------------------------------------------
    total = sum(1 for d in visas.values() if d.get("statut") in STATUTS_A_VISER)
    nb_app = sum(1 for d in visas.values() if d.get("statut") == "approuve")
    print(f"\n=== FILES DU DG — {total} à viser, {nb_app} approuvé(s) en attente de signature ===")
    if limite:
        print(f"File « à viser » : restent chez le DG les dépôts depuis le {limite.strftime('%d/%m/%Y')}.")
        afficher("À VISER ANCIENS -> « SIGNÉS, À ARCHIVER »",
                 {r: d for r, d in file_retires.items() if r not in a_supprimer})
        afficher("À VISER QUI RESTENT CHEZ LE DG", {**file_gardes, **exclus})
        if sans_date:
            afficher("SANS DATE DE DÉPÔT (restent chez le DG, à voir à la main)", sans_date)
    if args.approuves:
        afficher("APPROUVÉS -> « SIGNÉS, À ARCHIVER »",
                 {r: d for r, d in approuves.items() if r not in a_supprimer})
    if args.archiver:
        afficher("DOCUMENTS DÉSIGNÉS -> « SIGNÉS, À ARCHIVER »",
                 {r: d for r, d in designes.items() if r not in a_supprimer})
        if not designes:
            print("  (aucun document en circuit ne correspond — vérifier le mot ou la référence)")
    if args.supprimer:
        afficher("TESTS SUPPRIMÉS DÉFINITIVEMENT (copie gardée en sauvegarde)", a_supprimer)
    if inconnues:
        print(f"\n(!) Référence(s) introuvable(s) : {', '.join(inconnues)}")
    if args.avec_signature and limite:
        print(f"\n« En attente de signature » à retirer : {len(attente_retires)} sur {len(attente)}")
    if reps_liees:
        print(f"\n{len(reps_liees)} projet(s) de réponse redeviendront « brouillon ».")
    reste_app = nb_app - sum(1 for r in concernes if visas[r].get("statut") == "approuve")
    reste_file = total - sum(1 for r in concernes if visas[r].get("statut") in STATUTS_A_VISER)
    print(f"\nAprès application : {reste_file} à viser chez le DG, {reste_app} approuvé(s).")

    if not a_deplacer and not a_supprimer and not attente_retires:
        print("\nRien à faire.")
        return
    if not args.confirm:
        print("\nAperçu seulement. Relance la même commande avec --confirm pour appliquer.")
        return

    # --- Sauvegarde complète avant toute modification -------------------
    horo = datetime.now().strftime("%Y%m%d-%H%M%S")
    dossier = visa.DOSSIER_A_VISER / f"_retires_{horo}"
    (dossier / "pdf").mkdir(parents=True, exist_ok=True)
    shutil.copy2(visa.FICHIER_VISAS, dossier / "visas_avant.json")
    if FICHIER_ATTENTE.exists():
        shutil.copy2(FICHIER_ATTENTE, dossier / "en_attente_avant.json")
    if courrier and courrier.F_REPONSES.exists():
        shutil.copy2(courrier.F_REPONSES, dossier / "reponses_avant.json")
    originaux = copy.deepcopy({r: visas[r] for r in concernes})

    maintenant = datetime.now().isoformat(timespec="seconds")
    visa.DOSSIER_A_CLASSER.mkdir(exist_ok=True)
    deplaces, supprimes, manquants = 0, 0, []

    # PDF : déplacés vers a_classer/, ou rangés dans la sauvegarde (tests)
    for ref in concernes:
        d = visas[ref]
        src = visa.DOSSIER_A_VISER / d.get("fichier", "")
        if d.get("statut") == STATUT_RETIRE:
            src = visa.DOSSIER_A_CLASSER / nom_a_classer(d.get("fichier", ""))
        if not d.get("fichier") or not src.exists():
            manquants.append(ref)
            continue
        shutil.copy2(src, dossier / "pdf" / src.name)
        if ref in a_supprimer:
            src.unlink()
            supprimes += 1
        else:
            shutil.move(str(src), str(visa.DOSSIER_A_CLASSER / nom_a_classer(src.name)))
            deplaces += 1

    # visas.json : fiches déplacées gardées (hors file), tests retirés
    visas = visa._charger()
    for ref in a_deplacer:
        d = visas.get(ref)
        if d:
            d["statut_avant"] = d.get("statut", "")
            d["statut"] = STATUT_RETIRE
            d["retire_le"] = maintenant
    for ref in a_supprimer:
        visas.pop(ref, None)
    visa._sauver(visas)

    if attente_retires:
        attente = lire_json(FICHIER_ATTENTE, {})
        for r in attente_retires:
            attente.pop(r, None)
        ecrire_json(FICHIER_ATTENTE, attente)

    if courrier and reps_liees:
        with courrier._verrou:
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
                    "action": "Retiré du circuit du DG",
                    "detail": "Remis en brouillon, numéro libéré : à resoumettre si besoin."})
            courrier._ecrire(courrier.F_REPONSES, reps)

    ecrire_json(dossier / "retires.json", {
        "le": maintenant,
        "garder_depuis": args.garder_depuis or "",
        "visas": {r: originaux[r] for r in a_deplacer},
        "supprimes": {r: originaux[r] for r in a_supprimer},
        "en_attente": attente_retires,
        "reponses_en_brouillon": reps_liees,
    })

    print(f"\nFait : {deplaces} document(s) passé(s) dans « Signés, à archiver », "
          f"{supprimes} test(s) supprimé(s), {len(attente_retires)} « en attente de signature » retiré(s).")
    if manquants:
        print(f"(!) PDF introuvable pour : {', '.join(manquants)} — fiche traitée quand même.")
    print("La responsable GED classe les documents depuis l'écran Courriers, bouton Archiver.")
    print(f"Sauvegarde : {dossier}")
    print(f"Retour arrière : python purger_visas.py --restaurer {dossier}")


def restaurer(dossier):
    dossier = Path(dossier)
    infos = lire_json(dossier / "retires.json", None)
    if not infos:
        print(f"ERREUR : {dossier}/retires.json introuvable.")
        sys.exit(1)
    visas = visa._charger()
    remis, deja_classes = 0, []
    for ref, original in (infos.get("visas") or {}).items():
        d = visas.get(ref)
        if d and d.get("statut") != STATUT_RETIRE:
            continue  # traité depuis : on n'y touche pas
        a_classer = visa.DOSSIER_A_CLASSER / nom_a_classer(original.get("fichier", ""))
        cible = visa.DOSSIER_A_VISER / original.get("fichier", "")
        if a_classer.exists():
            shutil.move(str(a_classer), str(cible))
        elif not cible.exists():
            deja_classes.append(ref)  # déjà archivé par la responsable GED
            continue
        visas[ref] = original
        remis += 1
    for ref, original in (infos.get("supprimes") or {}).items():
        if ref in visas:
            continue
        sauve = dossier / "pdf" / original.get("fichier", "")
        if sauve.exists():
            shutil.copy2(sauve, visa.DOSSIER_A_VISER / sauve.name)
        visas[ref] = original
        remis += 1
    visa._sauver(visas)
    if infos.get("en_attente"):
        attente = lire_json(FICHIER_ATTENTE, {})
        for r, m in infos["en_attente"].items():
            attente.setdefault(r, m)
        ecrire_json(FICHIER_ATTENTE, attente)
    if courrier and infos.get("reponses_en_brouillon"):
        avant = lire_json(dossier / "reponses_avant.json", {})
        with courrier._verrou:
            reps = courrier.reponses()
            for k in infos["reponses_en_brouillon"]:
                if k in avant and reps.get(k, {}).get("statut") == "brouillon":
                    reps[k] = avant[k]
            courrier._ecrire(courrier.F_REPONSES, reps)
    print(f"Remis en place : {remis} document(s).")
    if deja_classes:
        print(f"Déjà archivés, laissés tels quels : {', '.join(deja_classes)}")


def main():
    p = argparse.ArgumentParser(description="Remet à zéro les files du DG.")
    p.add_argument("--garder-depuis", help="date AAAA-MM-JJ : restent chez le DG les dépôts depuis")
    p.add_argument("--exclure", nargs="+", metavar="X",
                   help="« à viser » anciens à laisser chez le DG (référence VISA-… ou mot)")
    p.add_argument("--approuves", action="store_true",
                   help="vide la section « Approuvés » vers « Signés, à archiver »")
    p.add_argument("--archiver", nargs="+", metavar="X",
                   help="documents précis vers « Signés, à archiver », toute date (référence VISA-… ou mot)")
    p.add_argument("--supprimer", nargs="+", metavar="X",
                   help="documents de test à retirer définitivement (référence VISA-… ou mot)")
    p.add_argument("--avec-retournes", action="store_true")
    p.add_argument("--avec-signature", action="store_true")
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--restaurer", metavar="DOSSIER")
    args = p.parse_args()
    if args.restaurer:
        restaurer(args.restaurer)
    elif args.garder_depuis or args.approuves or args.supprimer or args.archiver:
        purger(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
