#!/usr/bin/env python3
"""
purger_visas.py — désengorge la file « à viser » du DG avant la mise en production
=================================================================================
Les documents en attente de visa déposés AVANT une date donnée quittent
l'écran Visa Direction et passent dans la section « Signés, à archiver » de
l'écran Courriers. La responsable GED les y classe comme d'habitude
(bouton Archiver : numéro, dossier WorkDrive, registre). Les plus récents
restent chez le DG.

Rien n'est détruit ni archivé automatiquement :
  - le PDF passe de a_viser/ à a_classer/ (préfixe ASIGNER_, celui que
    l'écran de classement reconnaît), une copie est gardée en sauvegarde ;
  - la fiche de visas.json est conservée avec le statut « retire_file » :
    l'écran d'archivage retrouve ainsi le sens et le correspondant ;
  - un projet de réponse qui attendait la validation du DG redevient
    « brouillon », son numéro de courrier sortant est libéré ;
  - --restaurer remet les documents non encore classés dans la file du DG.

Le courrier fictif de la démonstration se retire à part, entièrement :
  docker exec ged-wakati python /app/nettoyer_demo.py --mot Mdjihari --confirm

USAGE (dans le conteneur, sur le serveur)
-----------------------------------------
  # aperçu : garde chez le DG ce qui a été déposé depuis le 21 octobre 2026
  docker exec ged-wakati python /app/purger_visas.py --garder-depuis 2026-10-21

  # application
  docker exec ged-wakati python /app/purger_visas.py --garder-depuis 2026-10-21 --confirm

  # retour arrière (documents pas encore archivés)
  docker exec ged-wakati python /app/purger_visas.py --restaurer /app/a_viser/_retires_20261023-1405

OPTIONS
-------
  --garder-depuis AAAA-MM-JJ   date de dépôt à partir de laquelle on garde (obligatoire)
  --exclure X [X …]            garde chez le DG ces documents même s'ils sont anciens.
                               X = une référence VISA-… lue dans l'aperçu, ou un mot de
                               l'objet ou du correspondant (ex. BCC). Répétable.
  --avec-retournes             déplace aussi les documents « retournés au service »
                               antérieurs à la date (par défaut : seulement « à viser »)
  --avec-signature             retire aussi les anciennes entrées « En attente de
                               signature » (en_attente.json) antérieures à la date
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


def exclu(ref, d, exclusions):
    """Vrai si le document est désigné par --exclure (référence ou mot)."""
    for x in exclusions:
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


# ---------------------------------------------------------------------------
def selectionner(visas, limite, avec_retournes, exclusions=()):
    statuts = STATUTS_A_VISER + (("retourne",) if avec_retournes else ())
    retires, gardes, sans_date, exclus = {}, {}, {}, {}
    for ref, d in visas.items():
        if d.get("statut") not in statuts:
            continue
        j = date_depot(d)
        if j is None:
            sans_date[ref] = d
        elif j < limite:
            if exclu(ref, d, exclusions):
                exclus[ref] = d
            else:
                retires[ref] = d
        else:
            gardes[ref] = d
    return retires, gardes, sans_date, exclus


def afficher(titre, fiches):
    print(f"\n{titre} : {len(fiches)}")
    for ref, d in sorted(fiches.items(), key=lambda x: str(x[1].get("depose_le", ""))):
        sens = "reçu" if d.get("sens") == "entrant" else "émis"
        print(f"  {ref:22} {str(d.get('depose_le', ''))[:10]}  {sens:5} {d.get('statut', ''):9} "
              f"{(d.get('correspondant') or '—')[:20]:20} {(d.get('objet') or '')[:44]}")


def purger(args):
    try:
        limite = date.fromisoformat(args.garder_depuis)
    except ValueError:
        print("ERREUR : --garder-depuis attend une date AAAA-MM-JJ.")
        sys.exit(1)

    visas = visa._charger()
    retires, gardes, sans_date, exclus = selectionner(visas, limite, args.avec_retournes,
                                                       args.exclure or [])
    inconnues = [x for x in (args.exclure or []) if x.upper().startswith("VISA-")
                 and x.upper() not in {r.upper() for r in visas}]

    attente = lire_json(FICHIER_ATTENTE, {}) if args.avec_signature else {}
    attente_retires = {r: m for r, m in attente.items()
                       if (jour(m.get("soumis_le")) or limite) < limite}

    reponses = courrier.reponses() if courrier else {}
    reps_liees = [k for k, x in reponses.items()
                  if x.get("visa_ref") in retires and x.get("statut") == "a_valider"]

    total = sum(1 for d in visas.values() if d.get("statut") in STATUTS_A_VISER)
    print(f"\n=== FILE DE VISA DU DG — {total} document(s) à viser au total ===")
    print(f"Restent chez le DG : les dépôts depuis le {limite.strftime('%d/%m/%Y')} inclus.")
    afficher("PASSENT DANS « SIGNÉS, À ARCHIVER » (classement par la responsable GED)", retires)
    afficher("RESTENT CHEZ LE DG", gardes)
    if args.exclure:
        afficher("RESTENT CHEZ LE DG PAR --exclure", exclus)
        if inconnues:
            print(f"\n(!) Référence(s) introuvable(s) dans la file : {', '.join(inconnues)}")
    if sans_date:
        afficher("SANS DATE DE DÉPÔT (restent chez le DG, à voir à la main)", sans_date)
    if not args.avec_retournes:
        vieux_retours = sum(1 for d in visas.values() if d.get("statut") == "retourne"
                            and (date_depot(d) or limite) < limite)
        if vieux_retours:
            print(f"\n(i) {vieux_retours} document(s) « retourné(s) au service » antérieur(s) "
                  "à la date restent en place. Ajoute --avec-retournes pour les déplacer aussi.")
    if args.avec_signature:
        print(f"\n« En attente de signature » à retirer : {len(attente_retires)} sur {len(attente)}")
    if reps_liees:
        print(f"\n{len(reps_liees)} projet(s) de réponse redeviendront « brouillon ».")

    if not retires and not attente_retires:
        print("\nRien à déplacer.")
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
    if courrier and courrier.F_REPONSES.exists():
        shutil.copy2(courrier.F_REPONSES, dossier / "reponses_avant.json")
    originaux = copy.deepcopy(retires)

    # --- PDF : a_viser/ -> a_classer/ (copie de sauvegarde gardée) -------
    visa.DOSSIER_A_CLASSER.mkdir(exist_ok=True)
    deplaces, manquants = 0, []
    for ref, d in retires.items():
        src = visa.DOSSIER_A_VISER / d.get("fichier", "")
        if not d.get("fichier") or not src.exists():
            manquants.append(ref)
            continue
        shutil.copy2(src, dossier / "pdf" / src.name)
        shutil.move(str(src), str(visa.DOSSIER_A_CLASSER / nom_a_classer(src.name)))
        deplaces += 1

    # --- visas.json : la fiche reste, hors de la file du DG --------------
    maintenant = datetime.now().isoformat(timespec="seconds")
    visas = visa._charger()
    for ref in retires:
        d = visas.get(ref)
        if d:
            d["statut_avant"] = d.get("statut", "")
            d["statut"] = STATUT_RETIRE
            d["retire_le"] = maintenant
    visa._sauver(visas)

    # --- en_attente.json -------------------------------------------------
    if attente_retires:
        attente = lire_json(FICHIER_ATTENTE, {})
        for r in attente_retires:
            attente.pop(r, None)
        ecrire_json(FICHIER_ATTENTE, attente)

    # --- Projets de réponse qui attendaient le DG ------------------------
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
                    "action": "Retiré de la file du DG",
                    "detail": "Remis en brouillon, numéro libéré : à resoumettre si besoin."})
            courrier._ecrire(courrier.F_REPONSES, reps)

    ecrire_json(dossier / "retires.json", {
        "le": maintenant,
        "garder_depuis": args.garder_depuis,
        "visas": originaux,
        "en_attente": attente_retires,
        "reponses_en_brouillon": reps_liees,
    })

    print(f"\nFait : {deplaces} document(s) passé(s) dans « Signés, à archiver », "
          f"{len(attente_retires)} « en attente de signature » retiré(s).")
    if manquants:
        print(f"(!) PDF introuvable pour : {', '.join(manquants)} — fiche marquée retirée quand même.")
    print("La responsable GED les classe depuis l'écran Courriers, bouton Archiver.")
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
    print(f"Remis dans la file du DG : {remis} document(s).")
    if deja_classes:
        print(f"Déjà archivés, laissés tels quels : {', '.join(deja_classes)}")


def main():
    p = argparse.ArgumentParser(description="Désengorge la file de visa du DG.")
    p.add_argument("--garder-depuis", help="date AAAA-MM-JJ : restent chez le DG les dépôts depuis")
    p.add_argument("--exclure", nargs="+", metavar="X",
                   help="références VISA-… ou mots à laisser chez le DG malgré leur date")
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
