#!/usr/bin/env python3
"""
rappels_agenda.py — programme du jour envoyé automatiquement aux directeurs
==========================================================================
Chaque matin, chaque directeur dont l'agenda est relié à la GED reçoit son
programme de la journée (message privé Zoho Cliq + e-mail), comme le bouton
« Envoyer le programme » de l'écran Agenda.

Sans --confirm : aperçu seulement, rien n'est envoyé.

USAGE
-----
  python rappels_agenda.py                         # aperçu, programme d'aujourd'hui, DG
  python rappels_agenda.py --confirm               # envoie
  python rappels_agenda.py --jour demain --confirm # programme de demain (envoi la veille au soir)
  python rappels_agenda.py --directeurs DG DAF     # plusieurs directeurs
  python rappels_agenda.py --tous --confirm        # tous les signataires dont l'agenda est relié
  python rappels_agenda.py --canal cliq            # cliq | email | les-deux (défaut)
  python rappels_agenda.py --meme-vide             # envoie aussi « Aucun rendez-vous prévu »
  python rappels_agenda.py --sans-week-end         # ne fait rien le samedi et le dimanche

CRON SUR LE VPS (crontab -e, heure du serveur = heure des Comores ?)
-------------------------------------------------------------------
  # Programme du jour à 7 h 30, du lundi au vendredi
  30 7 * * 1-5  docker exec ged-wakati python /app/rappels_agenda.py --confirm >> /var/log/ged-rappels.log 2>&1
  # Programme du lendemain à 18 h, du dimanche au jeudi
  0 18 * * 0-4  docker exec ged-wakati python /app/rappels_agenda.py --jour demain --confirm >> /var/log/ged-rappels.log 2>&1
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

RACINE = Path(__file__).resolve().parent
sys.path.insert(0, str(RACINE))


def main():
    ap = argparse.ArgumentParser(description="Programme du jour des directeurs (Cliq + e-mail).")
    ap.add_argument("--jour", default="aujourdhui",
                    help="aujourdhui | demain | AAAA-MM-JJ (défaut : aujourdhui)")
    ap.add_argument("--directeurs", nargs="*", default=["DG"], help="codes signataires (défaut : DG)")
    ap.add_argument("--tous", action="store_true", help="tous les signataires dont l'agenda est relié")
    ap.add_argument("--canal", default="les-deux", choices=["cliq", "email", "les-deux"])
    ap.add_argument("--meme-vide", action="store_true", help="envoyer aussi si aucun rendez-vous")
    ap.add_argument("--sans-week-end", action="store_true", help="ne rien faire samedi et dimanche")
    ap.add_argument("--confirm", action="store_true", help="envoyer pour de vrai")
    a = ap.parse_args()

    if a.jour in ("aujourdhui", "aujourd'hui", "today"):
        jour = date.today()
    elif a.jour in ("demain", "tomorrow"):
        jour = date.today() + timedelta(days=1)
    else:
        try:
            jour = date.fromisoformat(a.jour)
        except ValueError:
            sys.exit(f"Date invalide : {a.jour}")
    if a.sans_week_end and jour.weekday() >= 5:
        print(f"{jour:%d/%m/%Y} est un week-end : rien à envoyer.")
        return

    import calendrier  # noqa: PLC0415
    import courrier  # noqa: PLC0415

    ok_cal, raison = calendrier.disponible()
    if not ok_cal:
        sys.exit(raison)
    canaux = ("cliq", "email") if a.canal == "les-deux" else (a.canal,)
    sigs = courrier.parametres().get("signataires", [])
    if a.tous:
        cibles = [s for s in sigs if courrier._agenda_de(s)[0] and s.get("email")]
    else:
        cibles = [s for s in sigs if s.get("code") in a.directeurs]
        inconnus = set(a.directeurs) - {s.get("code") for s in cibles}
        for code in sorted(inconnus):
            print(f"  ? {code} : signataire inconnu")

    print(f"Programme du {calendrier.jour_lettre(jour)} — "
          f"{'ENVOI' if a.confirm else 'APERÇU (ajouter --confirm pour envoyer)'}\n")
    envoyes = erreurs = sautes = 0
    for sig in cibles:
        nom = f"{sig.get('code')} ({sig.get('email') or 'sans e-mail'})"
        try:
            evts = courrier.evenements_directeur(sig, jour, jour)
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {nom} : {exc}")
            erreurs += 1
            continue
        print(f"  {nom} : {len(evts)} rendez-vous")
        for e in evts:
            print(f"      • {calendrier.plage(e):22} {e['titre'][:60]}"
                  + (f" ({e['lieu'][:30]})" if e.get("lieu") else ""))
        if not evts and not a.meme_vide:
            print("      (rien à envoyer — utiliser --meme-vide pour prévenir quand même)")
            sautes += 1
            continue
        if not a.confirm:
            continue
        ok, details, _ = courrier.envoyer_programme(sig, jour, "GED", canaux, evts=evts)
        print(f"      {'✓' if ok else '✗'} " + " ; ".join(details))
        envoyes += ok
        erreurs += not ok

    print(f"\nBilan : {envoyes} envoyé(s), {sautes} sans rendez-vous, {erreurs} erreur(s)"
          + ("" if a.confirm else " — aperçu, rien n'a été envoyé"))
    sys.exit(1 if erreurs else 0)


if __name__ == "__main__":
    main()
