#!/usr/bin/env python3
"""
nettoyer_demo.py — efface les traces d'une démonstration de la GED
==================================================================
Une démonstration fait passer un courrier FICTIF dans le vrai circuit :
visa du DG, projet de réponse, numéro de courrier sortant réservé, PDF
générés. Ce script retire tout ce qui se rattache à ce courrier, pour que
la démo ne laisse ni numéro réservé, ni ligne « en attente », ni fichier.

Il ne touche NI au registre Zoho Sheet NI à WorkDrive : la démo s'arrête
avant l'archivage. Si un document de démo a été archivé quand même,
supprime-le à la main dans WorkDrive et dans le registre.

USAGE (dans le conteneur, sur le serveur)
-----------------------------------------
  docker exec ged-wakati python /app/nettoyer_demo.py --mot Mdjihari
  docker exec ged-wakati python /app/nettoyer_demo.py --mot Mdjihari --confirm

Sur le poste : ./venv/bin/python nettoyer_demo.py --mot Mdjihari [--confirm]

Sans --confirm : aperçu, rien n'est modifié.
--mot : texte cherché (sans accents ni majuscules) dans l'objet, le
correspondant ou l'organisation. Mets un mot propre au courrier fictif.
"""

import argparse
import json
import sys
import unicodedata
from pathlib import Path

RACINE = Path(__file__).resolve().parent
sys.path.insert(0, str(RACINE))

import courrier  # noqa: E402
import visa  # noqa: E402


def norm(texte):
    t = unicodedata.normalize("NFKD", str(texte or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def concerne(fiche, mot, champs):
    return any(mot in norm(fiche.get(c, "")) for c in champs)


def main():
    p = argparse.ArgumentParser(description="Efface les traces d'une démonstration.")
    p.add_argument("--mot", required=True, help="mot propre au courrier fictif (ex. Mdjihari)")
    p.add_argument("--confirm", action="store_true", help="appliquer (sinon aperçu)")
    args = p.parse_args()

    mot = norm(args.mot).strip()
    if len(mot) < 4:
        print("ERREUR : --mot doit faire au moins 4 caractères, pour ne rien effacer par erreur.")
        sys.exit(1)

    visas = visa._charger()
    reps = courrier.reponses()
    ents = courrier.entrants()

    # 1. Ce qui contient le mot
    r_visas = {r for r, d in visas.items()
               if concerne(d, mot, ("objet", "correspondant", "fichier"))}
    r_ents = {r for r, e in ents.items()
              if concerne(e, mot, ("objet", "correspondant", "nom_archive"))}
    r_reps = {r for r, x in reps.items()
              if concerne(x, mot, ("organisation", "objet", "objet_entrant", "destinataire"))}

    # 2. Ce qui s'y rattache (projet <-> entrant <-> visa)
    for r in list(r_reps):
        x = reps[r]
        if x.get("visa_ref"):
            r_visas.add(x["visa_ref"])
        if x.get("entrant_id"):
            r_ents.add(x["entrant_id"])
    for r in list(r_ents):
        e = ents.get(r, {})
        if e.get("visa_ref"):
            r_visas.add(e["visa_ref"])
        if e.get("reponse_ref"):
            r_reps.add(e["reponse_ref"])
    for r in list(r_visas):
        d = visas.get(r, {})
        if d.get("reponse_ref"):
            r_reps.add(d["reponse_ref"])
    r_visas &= set(visas)
    r_ents &= set(ents)
    r_reps &= set(reps)

    archives = [r for r in r_reps if reps[r].get("statut") == "archive"] + \
               [r for r in r_ents if ents[r].get("nom_archive")]

    # 3. Fichiers
    fichiers = []
    for r in r_visas:
        nom = visas[r].get("fichier", "")
        if nom:
            fichiers.append(visa.DOSSIER_A_VISER / nom)
            fichiers.append(visa.DOSSIER_A_CLASSER / nom.replace("AVISER_", "ASIGNER_", 1))
            fichiers.append(visa.DOSSIER_A_CLASSER / nom)
    for r in r_reps:
        fichiers += [courrier.DOSSIER_PROJETS / f"{r}.pdf",
                     courrier.DOSSIER_PROJETS / f"{r}_apercu.pdf"]
    for r in r_ents:
        slug = courrier._slug(r)
        fichiers += [courrier.DOSSIER_TEXTES / f"{slug}.pdf",
                     courrier.DOSSIER_TEXTES / f"{slug}.txt"]
    fichiers = sorted({f for f in fichiers if f.exists()})

    print(f"\n=== NETTOYAGE DE LA DÉMO « {args.mot} » ===\n")
    print(f"  passages au visa    : {len(r_visas)}")
    for r in sorted(r_visas):
        d = visas[r]
        print(f"    {r}  {d.get('sens', ''):8} {d.get('statut', ''):10} {d.get('objet', '')[:60]}")
    print(f"  courriers entrants  : {len(r_ents)}")
    for r in sorted(r_ents):
        print(f"    {r}  {ents[r].get('objet', '')[:70]}")
    print(f"  projets de réponse  : {len(r_reps)}")
    for r in sorted(r_reps):
        x = reps[r]
        print(f"    {r}  {x.get('statut', ''):12} n° {x.get('numero') or '—':4} "
              f"{x.get('organisation', '')[:40]}")
    print(f"  fichiers            : {len(fichiers)}")
    for f in fichiers:
        print(f"    {f.relative_to(RACINE) if RACINE in f.parents else f}")

    if archives:
        print("\n  (!) Des éléments ont été ARCHIVÉS : ils sont aussi dans WorkDrive et le")
        print("      registre. Ce script ne les y retire pas : fais-le à la main.")

    if not (r_visas or r_ents or r_reps or fichiers):
        print("\nRien à nettoyer.")
        return

    if not args.confirm:
        print("\nAperçu seulement. Relance avec --confirm pour effacer.")
        return

    with courrier._verrou:
        reps = courrier.reponses()
        ents = courrier.entrants()
        for r in r_reps:
            reps.pop(r, None)
        for r in r_ents:
            ents.pop(r, None)
        courrier._ecrire(courrier.F_REPONSES, reps)
        courrier._ecrire(courrier.F_ENTRANTS, ents)

    visas = visa._charger()
    for r in r_visas:
        visas.pop(r, None)
    visa._sauver(visas)

    n = 0
    for f in fichiers:
        try:
            f.unlink()
            n += 1
        except OSError as exc:
            print(f"  impossible d'effacer {f.name} : {exc}")

    print(f"\nEffacé : {len(r_visas)} visa(s), {len(r_ents)} entrant(s), "
          f"{len(r_reps)} projet(s), {n} fichier(s).")
    print("Le numéro de courrier sortant réservé par la démo est libéré.")


if __name__ == "__main__":
    main()
