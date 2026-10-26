#!/usr/bin/env python3
"""
calendrier.py — rendez-vous dans les agendas Zoho Calendar des directeurs
=========================================================================
À la suite d'un courrier, la responsable GED programme un rendez-vous dans
l'agenda du DG (puis, une fois configurés, dans ceux des autres directeurs :
DAF, DT, Direction Commerciale…).

JETON SÉPARÉ — rien n'est touché dans l'existant
------------------------------------------------
Le calendrier a son propre refresh token, CALENDAR_REFRESH_TOKEN, dans
zoho_config.py (même principe que CLIQ_REFRESH_TOKEN). Le jeton principal
REFRESH_TOKEN (WorkDrive, Sheet, Sign) n'est ni lu ni modifié ici. Pour le
créer : python setup_calendar_token.py (voir ce script).

DEUX FAÇONS D'ÉCRIRE DANS L'AGENDA D'UN DIRECTEUR
-------------------------------------------------
  1. Agenda partagé (recommandé) : le directeur partage son agenda Zoho avec
     le compte qui a généré le jeton, avec le droit de modifier les
     événements. L'agenda apparaît alors dans la liste (Paramètres ›
     Signataires › Agenda) et l'événement y est créé directement.
  2. Invitation : sans agenda partagé, l'événement est créé dans l'agenda
     du compte GED et le directeur y est invité par son adresse e-mail ; il
     apparaît dans son agenda à l'acceptation.

LIGNE DE COMMANDE (tests)
-------------------------
  python calendrier.py --tester                 # jeton + liste des agendas visibles
  python calendrier.py --evenement-test <UID>   # crée un RDV de test dans 1 h (30 min)
  docker exec ged-wakati python /app/calendrier.py --tester
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

RACINE = Path(__file__).resolve().parent
sys.path.insert(0, str(RACINE))

DONNEES = Path(os.getenv("GED_DONNEES", str(RACINE))) / "courrier"
CACHE = DONNEES / ".zoho_calendar_token.json"
FUSEAU = "Indian/Comoro"
SCOPES = "ZohoCalendar.calendar.ALL,ZohoCalendar.event.ALL"


def _config():
    try:
        import zoho_config as zc  # noqa: PLC0415
    except ImportError:
        return {}
    return {
        "client_id": getattr(zc, "CLIENT_ID", ""),
        "client_secret": getattr(zc, "CLIENT_SECRET", ""),
        "refresh": getattr(zc, "CALENDAR_REFRESH_TOKEN", ""),
        "accounts": getattr(zc, "ACCOUNTS_URL", "https://accounts.zoho.com"),
        "api": getattr(zc, "CALENDAR_API", "https://calendar.zoho.com/api/v1").rstrip("/"),
    }


def disponible():
    """(True, "") si le jeton calendrier est configuré, sinon (False, raison)."""
    c = _config()
    if not c:
        return False, "zoho_config.py introuvable."
    if not c["refresh"]:
        return False, ("Agenda non relié : CALENDAR_REFRESH_TOKEN absent de zoho_config.py "
                       "(voir setup_calendar_token.py).")
    return True, ""


def _jeton():
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
        if cache.get("expire", 0) > time.time() + 60:
            return cache["access_token"]
    except Exception:  # noqa: BLE001
        pass
    c = _config()
    r = requests.post(f"{c['accounts']}/oauth/v2/token", params={
        "refresh_token": c["refresh"], "client_id": c["client_id"],
        "client_secret": c["client_secret"], "grant_type": "refresh_token"}, timeout=30)
    data = r.json()
    if "access_token" not in data:
        raise RuntimeError(f"Jeton Zoho Calendar refusé : {data}")
    try:
        DONNEES.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"access_token": data["access_token"],
                                     "expire": time.time() + data.get("expires_in", 3600)}),
                         encoding="utf-8")
    except OSError:
        pass
    return data["access_token"]


def _entetes():
    return {"Authorization": f"Zoho-oauthtoken {_jeton()}"}


CATEGORIES = ("", "shared", "subscribed", "group", "all")


def _appel_calendriers(categorie=""):
    params = {"category": categorie} if categorie else None
    r = requests.get(f"{_config()['api']}/calendars", headers=_entetes(),
                     params=params, timeout=30)
    try:
        data = r.json()
    except ValueError:
        data = {"brut": r.text[:300]}
    return r.status_code, data


def lister_calendriers(journal=None):
    """Agendas visibles par le compte du jeton : les siens, et ceux qu'on lui
    a partagés. Zoho range les agendas partagés dans une autre catégorie que
    les siens : on les interroge toutes. [{uid, nom, categorie, droit, couleur}]

    journal : liste où consigner chaque appel (diagnostic)."""
    res, vus, premiere_erreur = [], set(), None
    for cat in CATEGORIES:
        try:
            statut, data = _appel_calendriers(cat)
        except Exception as exc:  # noqa: BLE001
            statut, data = 0, {"erreur": str(exc)}
        cals = data.get("calendars") if isinstance(data, dict) else None
        if journal is not None:
            journal.append(f"category={cat or '(aucune)'} : HTTP {statut}, "
                           + (f"{len(cals)} agenda(s)" if cals is not None else str(data)[:200]))
        if statut != 200 or cals is None:
            if not cat:
                premiere_erreur = f"HTTP {statut} {str(data)[:300]}"
            continue
        for cal in cals:
            uid = cal.get("uid") or cal.get("caluid") or ""
            if not uid or uid in vus:
                continue
            vus.add(uid)
            proprio = cal.get("owner") or cal.get("owner_email") or cal.get("ownerEmail") or ""
            if isinstance(proprio, dict):
                proprio = proprio.get("email") or proprio.get("name") or ""
            res.append({
                "uid": uid,
                "nom": cal.get("name", "(sans nom)") + (f" — {proprio}" if proprio else ""),
                "categorie": cal.get("category") or cat or "",
                "droit": cal.get("privilege") or cal.get("permission") or "",
                "couleur": cal.get("color", ""),
            })
    if not res and premiere_erreur:
        raise RuntimeError(f"Liste des agendas : {premiere_erreur}")
    return res


def _format(d):
    """Heure locale des Comores, au format attendu par Zoho (le fuseau est
    donné à part, dans dateandtime.timezone)."""
    return d.strftime("%Y%m%dT%H%M%S")


def creer_evenement(calendrier_uid, titre, debut, duree_min=60, lieu="", description="",
                    invites=(), rappel_min=30):
    """Crée un événement. Retourne (True, {uid, lien}) ou (False, message).

    debut : datetime naïf, heure des Comores. invites : adresses e-mail.
    """
    fin = debut + timedelta(minutes=int(duree_min or 60))
    corps = {
        "title": titre[:250],
        "dateandtime": {"timezone": FUSEAU, "start": _format(debut), "end": _format(fin)},
        "isallday": False,
        "location": lieu,
        "description": description[:3000],
    }
    invites = [e.strip().lower() for e in invites if e and "@" in e]
    if invites:
        corps["attendees"] = [{"email": e, "permission": 1, "attendance": 1} for e in invites]
    if rappel_min:
        corps["reminders"] = [{"action": "popup", "minutes": -int(rappel_min)}]

    url = f"{_config()['api']}/calendars/{calendrier_uid}/events"
    essais = [
        # Format documenté : eventdata en paramètre
        lambda: requests.post(url, headers=_entetes(),
                              params={"eventdata": json.dumps(corps, ensure_ascii=False)},
                              timeout=30),
        # Repli : eventdata en champ de formulaire
        lambda: requests.post(url, headers=_entetes(),
                              data={"eventdata": json.dumps(corps, ensure_ascii=False)},
                              timeout=30),
    ]
    derniere = ""
    for essai in essais:
        try:
            r = essai()
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            derniere = f"Zoho Calendar injoignable : {exc}"
            continue
        evts = data.get("events") or []
        if r.status_code in (200, 201) and evts:
            e = evts[0]
            return True, {"uid": e.get("uid", ""),
                          "lien": e.get("viewEventURL") or e.get("view_event_url") or "",
                          "etag": e.get("etag", "")}
        derniere = f"HTTP {r.status_code} : {str(data)[:400]}"
        if "eventdata" not in str(data).lower():
            break  # autre erreur que le format d'envoi : inutile de réessayer
    logging.error("Création d'événement Zoho Calendar : %s", derniere)
    return False, derniere


# ---------------------------------------------------------------------------
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]


def date_heure_lettre(d):
    """datetime -> « jeudi 29 octobre 2026 à 10 h » / « … à 10 h 30 »."""
    jour = "1er" if d.day == 1 else str(d.day)
    heure = f"{d.hour} h" + (f" {d.minute:02d}" if d.minute else "")
    return f"{JOURS[d.weekday()]} {jour} {MOIS[d.month - 1]} {d.year} à {heure}"


def main():
    args = sys.argv[1:]
    ok, raison = disponible()
    if not ok:
        print(raison)
        sys.exit(1)
    if "--tester" in args:
        print("Jeton Zoho Calendar : OK")
        journal = []
        cals = lister_calendriers(journal)
        for ligne in journal:
            print(f"  [{ligne}]")
        print(f"{len(cals)} agenda(s) visible(s) :")
        for c in cals:
            print(f"  {c['uid']}  {c['nom'][:40]:40} {c['categorie']:10} {c['droit']}")
        if len(cals) <= 1:
            print("\nAgenda du DG absent ? Récupère son identifiant dans Zoho Calendar :")
            print("  agenda partagé -> ⋮ -> Paramètres de l'agenda -> « ID de l'agenda » (ou l'adresse")
            print("  iCal / XML : l'identifiant est la longue suite de lettres et chiffres).")
            print("Puis teste l'écriture : python calendrier.py --evenement-test <ID>")
    elif "--evenement-test" in args:
        uid = args[args.index("--evenement-test") + 1] if len(args) > args.index("--evenement-test") + 1 else ""
        debut = (datetime.now() + timedelta(hours=1)).replace(second=0, microsecond=0)
        ok, info = creer_evenement(uid, "Test GED — à supprimer", debut, 30,
                                   "Locaux Huri Money, Moroni",
                                   "Événement de test créé par la GED WAKATI.")
        print("Créé :" if ok else "ÉCHEC :", info)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
