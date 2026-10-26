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

LECTURE DE L'AGENDA
-------------------
lister_evenements(uid, du, au) lit les rendez-vous d'un agenda (écran Agenda
de la GED, rappels, programme du jour). Les rendez-vous répétitifs sont
renvoyés occurrence par occurrence.

LIGNE DE COMMANDE (tests)
-------------------------
  python calendrier.py --tester                 # jeton + liste des agendas visibles
  python calendrier.py --evenement-test <UID>   # crée un RDV de test dans 1 h (30 min)
  python calendrier.py --semaine <UID>          # rendez-vous des 7 prochains jours
  docker exec ged-wakati python /app/calendrier.py --tester
"""

import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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


# ---------------------------------------------------------------------------
# Lecture des rendez-vous
# ---------------------------------------------------------------------------
_MOTIF_DT = re.compile(r"^(\d{8})(?:T(\d{4,6}))?(Z|[+-]\d{4})?$")


def _lire_dt(valeur):
    """« 20261026T100000+0300 », « 20261026T070000Z », « 20261026T100000 »
    ou « 20261026 » -> (datetime naïf heure des Comores, journée_entière)."""
    v = str(valeur or "").strip()
    m = _MOTIF_DT.match(v)
    if not m:
        return None, False
    jour, heure, decalage = m.groups()
    if not heure:
        return datetime.strptime(jour, "%Y%m%d"), True
    heure = heure.ljust(6, "0")
    d = datetime.strptime(jour + heure, "%Y%m%d%H%M%S")
    if decalage:
        if decalage == "Z":
            d = d.replace(tzinfo=ZoneInfo("UTC"))
        else:
            signe = 1 if decalage[0] == "+" else -1
            delta = timedelta(hours=int(decalage[1:3]), minutes=int(decalage[3:5])) * signe
            from datetime import timezone  # noqa: PLC0415
            d = d.replace(tzinfo=timezone(delta))
        d = d.astimezone(ZoneInfo(FUSEAU)).replace(tzinfo=None)
    return d, False


def _evenements_bruts(calendrier_uid, du, au, par_occurrence):
    params = {"range": json.dumps({"start": du.strftime("%Y%m%d"),
                                   "end": au.strftime("%Y%m%d")}),
              "timezone": FUSEAU}
    if par_occurrence:
        params["byinstance"] = "true"
    entetes = dict(_entetes(), Accept="application/json+large")
    r = requests.get(f"{_config()['api']}/calendars/{calendrier_uid}/events",
                     headers=entetes, params=params, timeout=30)
    try:
        data = r.json()
    except ValueError:
        data = {"brut": r.text[:300]}
    if r.status_code != 200 or not isinstance(data, dict) or "events" not in data:
        # Zoho répond {"error": ...} ou parfois rien quand la période est vide
        if r.status_code == 200 and isinstance(data, dict) and not data.get("error"):
            return []
        raise RuntimeError(f"Lecture de l'agenda : HTTP {r.status_code} {str(data)[:300]}")
    return data.get("events") or []


def _normaliser(e, complet=None):
    """Événement Zoho -> dict GED. `complet` : la version détaillée du même
    événement (titre, lieu… manquent dans les occurrences)."""
    c = complet or {}
    # Les dates viennent toujours de `e` : pour un RDV répétitif, c'est
    # l'occurrence de la semaine affichée, pas la première séance.
    dt = e.get("dateandtime") or {}
    debut, journee = _lire_dt(dt.get("start") or e.get("start"))
    fin, _ = _lire_dt(dt.get("end") or e.get("end"))
    if debut is None:  # occurrence sans dates exploitables : version complète
        dt = c.get("dateandtime") or {}
        debut, journee = _lire_dt(dt.get("start") or c.get("start"))
        fin, _ = _lire_dt(dt.get("end") or c.get("end"))
    if debut is None:
        return None
    journee = journee or bool(e.get("isallday") or c.get("isallday"))
    if fin is None:
        fin = debut + timedelta(hours=1)
    invites = []
    for a in (e.get("attendees") or c.get("attendees") or []):
        if isinstance(a, dict) and a.get("email"):
            invites.append({"email": a["email"].lower(), "statut": a.get("status", "")})
    v = lambda k: e.get(k) or c.get(k) or ""  # noqa: E731
    return {
        "uid": v("uid"),
        "titre": v("title") or "(sans titre)",
        "debut": debut, "fin": fin, "journee": journee,
        "lieu": v("location"),
        "description": v("description"),
        "lien": v("viewEventURL") or v("view_event_url"),
        "organisateur": str(v("organizer")).lower(),
        "invites": invites,
        "prive": bool(e.get("isprivate") or c.get("isprivate")),
    }


def lister_evenements(calendrier_uid, du, au):
    """Rendez-vous de l'agenda entre deux dates (incluses), triés.

    du, au : date. Zoho limite une lecture à 31 jours : on découpe."""
    if isinstance(du, datetime):
        du = du.date()
    if isinstance(au, datetime):
        au = au.date()
    res, vus = [], set()
    debut = du
    while debut <= au:
        fin = min(au, debut + timedelta(days=30))
        complets = {x.get("uid"): x for x in _evenements_bruts(calendrier_uid, debut, fin, False)}
        try:
            occurrences = _evenements_bruts(calendrier_uid, debut, fin, True)
        except Exception:  # noqa: BLE001
            occurrences = list(complets.values())
        for e in occurrences:
            n = _normaliser(e, complets.get(e.get("uid")))
            if not n:
                continue
            cle = (n["uid"], n["debut"].isoformat())
            if cle in vus:
                continue
            vus.add(cle)
            if n["fin"].date() < du or n["debut"].date() > au:
                continue
            res.append(n)
        debut = fin + timedelta(days=1)
    res.sort(key=lambda x: (x["debut"], not x["journee"]))
    return res


def heure_courte(d):
    """datetime -> « 9 h », « 9 h 30 »."""
    return f"{d.hour} h" + (f" {d.minute:02d}" if d.minute else "")


def plage(evt):
    """« Toute la journée », « 9 h – 10 h 30 »."""
    if evt.get("journee"):
        return "Toute la journée"
    return f"{heure_courte(evt['debut'])} – {heure_courte(evt['fin'])}"


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


def jour_lettre(d):
    """date -> « lundi 26 octobre 2026 »."""
    jour = "1er" if d.day == 1 else str(d.day)
    return f"{JOURS[d.weekday()]} {jour} {MOIS[d.month - 1]} {d.year}"


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
    elif "--semaine" in args:
        uid = args[args.index("--semaine") + 1] if len(args) > args.index("--semaine") + 1 else ""
        auj = date.today()
        evts = lister_evenements(uid, auj, auj + timedelta(days=6))
        print(f"{len(evts)} rendez-vous du {auj:%d/%m} au {auj + timedelta(days=6):%d/%m} :")
        for e in evts:
            print(f"  {e['debut']:%a %d/%m} {plage(e):22} {e['titre'][:50]}"
                  + (f"  [{e['lieu'][:30]}]" if e["lieu"] else ""))
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
