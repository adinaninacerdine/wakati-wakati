"""
routeur.py — Moteur de notifications de la GED (Cliq et e-mail)
================================================================
Chaque événement a une NATURE, et une nature = un canal :

  action      une personne doit agir (viser, signer, traiter, corriger)
              -> message privé Cliq à elle seule ; e-mail seulement si Cliq
                 échoue ou si l'événement est prioritaire/urgent.
  info        une personne est informée (demande traitée, note à lire)
              -> message privé Cliq ; e-mail seulement en secours.
  suivi       traçabilité de l'équipe courrier (archivé, remis, visa, RDV)
              -> canal #ged-courrier, jamais d'e-mail.
  direction   ce que la Direction doit voir (échéance dépassée, remise
              échouée, note bloquée) -> canal #ged-direction ; si urgent,
              e-mail aux destinataires Direction en plus.
  escalade    relance après délai -> message privé + e-mail, copie hiérarchie.

Anti-surcharge :
  - plage de silence (19 h – 7 h 30 et dimanche par défaut) : les messages
    attendent le matin ; les urgences passent ;
  - regroupement : plusieurs messages pour un même destinataire en quelques
    instants partent en un seul ; plus de 3 cartes pour un canal deviennent
    un résumé ;
  - dédoublonnage : une même clé (ex. relance d'une note) n'est envoyée qu'une
    fois par 24 h ;
  - récapitulatif quotidien par e-mail (17 h, du lundi au samedi) à la
    Direction et à la responsable GED, au lieu d'un e-mail par document.

La boucle de fond (un thread, un seul worker gunicorn) vide la file, lance
les relances automatiques et le récapitulatif.

Données : courrier/notifications.json (file d'attente, journal, réglages).
"""

import logging
import threading
import time
from datetime import datetime, timedelta

from flask import (Blueprint, flash, has_request_context, redirect,
                   render_template, request, url_for)

import courrier as C

bp_notifications = Blueprint("notifications", __name__, url_prefix="/notifications")

F_ETAT = C.DONNEES / "notifications.json"

NATURES = {
    "action": "Action attendue (privé)",
    "info": "Information (privé)",
    "suivi": "Suivi courrier (#ged-courrier)",
    "direction": "Direction (#ged-direction)",
    "escalade": "Escalade (privé + e-mail)",
    "recap": "Récapitulatif quotidien",
}

REGLAGES_DEFAUT = {
    "canal_courrier": "",          # nom unique Cliq ; vide = CLIQ_CHANNEL de notifier_config
    "canal_direction": "",         # vide = même canal que le courrier
    "emails_direction": "",        # vide = DG + UTILISATEURS_DG + responsables GED
    "silence_actif": True,
    "silence_debut": "19:00",
    "silence_fin": "07:30",
    "silence_dimanche": True,
    "regroupement_s": 90,
    "recap_actif": True,
    "recap_heure": "17:00",
    "delai_rappel_h": 24,
    "adresse_ged": "",             # ex. https://ged.wakati.km (liens des relances automatiques)
}

_CTX = {"lances": False, "rappels": [], "racine": ""}
_HOOKS = {}          # nom -> fonction(item, ok, detail) appelée après envoi différé


# ---------------------------------------------------------------------------
# État
# ---------------------------------------------------------------------------
def _etat():
    e = C._lire(F_ETAT, {})
    e.setdefault("attente", [])
    e.setdefault("journal", [])
    e.setdefault("cles", {})
    e.setdefault("jour", {"date": "", "evenements": []})
    e.setdefault("reglages", {})
    return e


def _sauver(e):
    e["journal"] = e["journal"][-400:]
    limite = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    e["cles"] = {k: v for k, v in e["cles"].items() if v >= limite}
    C._ecrire(F_ETAT, e)


def reglages():
    r = dict(REGLAGES_DEFAUT)
    r.update(_etat().get("reglages", {}))
    return r


def _canal(nature):
    r = reglages()
    try:
        import notifier  # noqa: PLC0415
        defaut = notifier._get("CLIQ_CHANNEL", "")
    except Exception:  # noqa: BLE001
        defaut = ""
    courrier_ = (r.get("canal_courrier") or defaut).strip().lstrip("#")
    if nature == "direction":
        return (r.get("canal_direction") or courrier_).strip().lstrip("#")
    return courrier_


def emails_direction():
    r = reglages()
    saisis = [x.strip().lower() for x in (r.get("emails_direction") or "").replace(";", ",").split(",")
              if "@" in x]
    if saisis:
        return saisis
    liste = set()
    for nom in ("UTILISATEURS_DG", "RESPONSABLES_GED"):
        liste |= set(C._auth_liste(nom) or set())
    dg = C.signataire("DG") or {}
    if dg.get("email"):
        liste.add(dg["email"].lower())
    return sorted(liste)


def _maintenant():
    return datetime.now()


def _hm(texte, defaut):
    try:
        h, m = (int(x) for x in str(texte).split(":")[:2])
        return h * 60 + m
    except (ValueError, TypeError):
        return defaut


def en_silence(quand=None):
    r = reglages()
    if not r.get("silence_actif", True):
        return False
    quand = quand or _maintenant()
    if r.get("silence_dimanche", True) and quand.weekday() == 6:
        return True
    minutes = quand.hour * 60 + quand.minute
    debut, fin = _hm(r.get("silence_debut"), 19 * 60), _hm(r.get("silence_fin"), 7 * 60 + 30)
    if debut > fin:
        return minutes >= debut or minutes < fin
    return debut <= minutes < fin


def fin_de_silence(quand=None):
    """Premier instant hors plage de silence."""
    quand = quand or _maintenant()
    t = quand.replace(second=0, microsecond=0)
    for _ in range(60 * 24 * 3):
        if not en_silence(t):
            return t
        t += timedelta(minutes=1)
    return quand


# ---------------------------------------------------------------------------
# Envoi effectif
# ---------------------------------------------------------------------------
def _journaliser(e, item, ok, detail):
    e["journal"].append({"le": _maintenant().isoformat(timespec="seconds"),
                         "nature": item.get("nature"), "canal": item.get("canal"),
                         "dest": item.get("dest", ""), "titre": item.get("titre", "")[:160],
                         "ok": bool(ok), "detail": str(detail)[:200],
                         "urgent": bool(item.get("urgent"))})


def _envoyer_un(item):
    """Envoie un élément. Retourne (ok, détail). Ne lève jamais."""
    try:
        import notifier  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return False, f"notifier indisponible : {exc}"
    try:
        canal = item.get("canal")
        if canal == "cliq_prive":
            ok, detail = notifier.message_cliq_personne(item["dest"], item["texte"])
            if not ok and item.get("secours_email"):
                ok2, detail2 = notifier.envoyer_email([item["dest"]], item.get("sujet") or item["titre"],
                                                      item.get("texte_mail") or item["texte"])
                return ok2, f"Cliq KO ({detail}) → e-mail : {detail2}"
            return ok, detail
        if canal == "cliq_canal":
            if item.get("carte"):
                return notifier.notify_cliq(item["carte"], channel=item["dest"])
            return notifier.message_cliq_canal(item["texte"], channel=item["dest"])
        if canal == "email":
            dest = item["dest"] if isinstance(item["dest"], list) else [item["dest"]]
            return notifier.envoyer_email(dest, item.get("sujet") or item["titre"],
                                          item.get("texte_mail") or item["texte"])
        return False, f"canal inconnu : {canal}"
    except Exception as exc:  # noqa: BLE001
        logging.error("Notification impossible", exc_info=True)
        return False, str(exc)


def _apres_envoi(item, ok, detail):
    hook = _HOOKS.get(item.get("hook") or "")
    if hook:
        try:
            hook(item, ok, detail)
        except Exception:  # noqa: BLE001
            logging.error("Suite d'envoi %s", item.get("hook"), exc_info=True)


def _deposer(items, immediat=False):
    """Envoie tout de suite ce qui doit partir, met le reste en file.
    Retourne (ok, détail) du premier élément (pour l'appelant)."""
    retour = (True, "rien à envoyer")
    maintenant = _maintenant()
    silence = en_silence(maintenant)
    r = reglages()
    a_envoyer = []
    with C._verrou:
        e = _etat()
        for i, item in enumerate(items):
            item.setdefault("cree_le", maintenant.isoformat(timespec="seconds"))
            if item.get("urgent") or immediat and not silence:
                a_envoyer.append(item)
                continue
            if silence:
                item["apres"] = fin_de_silence(maintenant).isoformat(timespec="seconds")
                detail = f"programmé le {C.date_courte(item['apres'][:10])} à {item['apres'][11:16]} (plage de silence)"
            else:
                item["apres"] = (maintenant + timedelta(seconds=int(r.get("regroupement_s", 90)))
                                 ).isoformat(timespec="seconds")
                detail = "en file (regroupement)"
            e["attente"].append(item)
            if i == 0:
                retour = ("attente", detail)
        _sauver(e)
    for i, item in enumerate(a_envoyer):
        ok, detail = _envoyer_un(item)
        with C._verrou:
            e = _etat()
            _journaliser(e, item, ok, detail)
            _sauver(e)
        _apres_envoi(item, ok, detail)
        if item is items[0]:
            retour = (ok, detail)
    return retour


def _deja(cle, heures=24):
    """Dédoublonnage : True si la clé a été émise dans les `heures` dernières."""
    if not cle:
        return False
    with C._verrou:
        e = _etat()
        der = e["cles"].get(cle)
        if der and datetime.fromisoformat(der) + timedelta(hours=heures) > _maintenant():
            return True
        e["cles"][cle] = _maintenant().isoformat(timespec="seconds")
        _sauver(e)
    return False


def _noter_jour(nature, titre, lien="", detail=""):
    """Événements du jour pour le récapitulatif."""
    with C._verrou:
        e = _etat()
        jour = _maintenant().strftime("%Y-%m-%d")
        if e["jour"].get("date") != jour:
            e["jour"] = {"date": jour, "evenements": []}
        e["jour"]["evenements"].append({"le": _maintenant().strftime("%H:%M"), "nature": nature,
                                        "titre": titre[:200], "lien": lien, "detail": detail[:200]})
        e["jour"]["evenements"] = e["jour"]["evenements"][-500:]
        _sauver(e)


# ---------------------------------------------------------------------------
# API pour les modules
# ---------------------------------------------------------------------------
def personne(email, titre, texte, *, nature="action", urgent=False, cle=None,
             sujet=None, texte_mail=None, hook=None, contexte=None, immediat=False):
    """Message privé à une personne. `urgent` : e-mail en plus et pas de
    plage de silence. Retourne (ok | "attente", détail)."""
    email = (email or "").strip().lower()
    if not email:
        return False, "adresse e-mail absente"
    if _deja(cle):
        return True, "déjà envoyé dans les dernières 24 h"
    base = {"nature": nature, "titre": titre, "texte": texte, "sujet": sujet or titre,
            "texte_mail": texte_mail or texte, "urgent": urgent, "hook": hook,
            "contexte": contexte or {}}
    items = [{**base, "canal": "cliq_prive", "dest": email, "secours_email": True}]
    if urgent or nature == "escalade":
        items.append({**base, "canal": "email", "dest": email, "hook": None})
    return _deposer(items, immediat=immediat)


def email(destinataires, titre, texte, *, nature="info", urgent=False, cle=None):
    """E-mail seul (copie à la hiérarchie, secours). Retourne (ok | "attente", détail)."""
    dest = [d.strip().lower() for d in (destinataires or []) if d and "@" in d]
    if not dest:
        return False, "aucun destinataire"
    if _deja(cle):
        return True, "déjà envoyé dans les dernières 24 h"
    return _deposer([{"nature": nature, "canal": "email", "dest": dest, "titre": titre,
                      "sujet": titre, "texte": texte, "urgent": urgent}])


def canal(nature, carte=None, texte="", *, titre="", urgent=False, cle=None, lien=""):
    """Publication dans #ged-courrier (suivi) ou #ged-direction (direction).
    `carte` : dictionnaire au format notifier.notify_cliq."""
    titre = titre or (carte or {}).get("titre") or texte[:80]
    _noter_jour(nature, titre, lien or (carte or {}).get("workdrive_url", ""))
    if _deja(cle):
        return True, "déjà publié dans les dernières 24 h"
    dest = _canal(nature)
    if not dest:
        return False, "canal Cliq non configuré"
    items = [{"nature": nature, "canal": "cliq_canal", "dest": dest, "carte": carte,
              "texte": texte or titre, "titre": titre, "urgent": urgent}]
    if nature == "direction" and urgent:
        corps = texte or "\n".join(f"{c} : {v}" for c, v in _lignes_carte(carte))
        items.append({"nature": nature, "canal": "email", "dest": emails_direction(),
                      "titre": titre, "sujet": f"[GED — urgent] {titre}",
                      "texte": corps + (f"\n\n{lien}" if lien else ""), "urgent": True})
    return _deposer(items)


def _lignes_carte(carte):
    if not carte:
        return []
    if carte.get("lignes"):
        return [(c, v) for c, v in carte["lignes"]]
    return [(k, carte.get(k)) for k in ("nom_fichier", "service", "type_doc", "deposant", "date")
            if carte.get(k)]


def carte(doc):
    """Adaptateur pour courrier._cliq / visa : `doc` porte « nature »
    (suivi par défaut) et « urgent »."""
    doc = dict(doc)
    nature = doc.pop("nature", "suivi")
    urgent = bool(doc.pop("urgent", False))
    cle = doc.pop("cle", None)
    titre = " — ".join(x for x in (doc.get("titre"), doc.get("nom_fichier")) if x) \
        or "Nouveau document déposé"
    return canal(nature, carte=doc, titre=titre, urgent=urgent, cle=cle,
                 lien=doc.get("workdrive_url", ""))


def enregistrer_suite(nom, fonction):
    """Fonction appelée après un envoi différé (ex. mettre à jour la note)."""
    _HOOKS[nom] = fonction


def enregistrer_rappel(fonction):
    """Tâche lancée toutes les 10 minutes par la boucle (relances, escalades)."""
    if fonction not in _CTX["rappels"]:
        _CTX["rappels"].append(fonction)


# ---------------------------------------------------------------------------
# File d'attente : regroupement et vidage
# ---------------------------------------------------------------------------
def vider_file(forcer=False):
    """Envoie les éléments arrivés à échéance, regroupés par destinataire."""
    maintenant = _maintenant()
    if en_silence(maintenant) and not forcer:
        return 0
    with C._verrou:
        e = _etat()
        dus = [x for x in e["attente"] if forcer or x.get("apres", "") <= maintenant.isoformat(timespec="seconds")]
        if not dus:
            return 0
        e["attente"] = [x for x in e["attente"] if x not in dus]
        _sauver(e)
    groupes = {}
    for x in dus:
        groupes.setdefault((x["canal"], str(x["dest"])), []).append(x)
    envoyes = 0
    for (canal_, _), liste in groupes.items():
        if canal_ == "cliq_prive" and len(liste) > 1:
            texte = f"🔔 {len(liste)} notifications de la GED :\n\n" + \
                    "\n\n— — —\n\n".join(x["texte"] for x in liste)
            paquet = [{**liste[0], "texte": texte, "titre": f"{len(liste)} notifications GED",
                       "secours_email": True}]
            resultats = [_envoyer_un(paquet[0])] * len(liste)
        elif canal_ == "cliq_canal" and len(liste) > 3:
            lignes = []
            for x in liste:
                lien = (x.get("carte") or {}).get("workdrive_url", "")
                lignes.append(f"• {x['titre']}" + (f" — {lien}" if lien else ""))
            texte = f"*{len(liste)} événements GED*\n" + "\n".join(lignes)
            ok = _envoyer_un({**liste[0], "carte": None, "texte": texte})
            resultats = [ok] * len(liste)
        elif canal_ == "email" and len(liste) > 1:
            texte = "\n\n— — —\n\n".join(x.get("texte_mail") or x["texte"] for x in liste)
            ok = _envoyer_un({**liste[0], "texte_mail": texte, "texte": texte,
                              "sujet": f"GED — {len(liste)} notifications"})
            resultats = [ok] * len(liste)
        else:
            resultats = [_envoyer_un(x) for x in liste]
        with C._verrou:
            e = _etat()
            for x, (ok, detail) in zip(liste, resultats):
                _journaliser(e, x, ok, detail + (f" (groupé ×{len(liste)})" if len(liste) > 1 else ""))
            _sauver(e)
        for x, (ok, detail) in zip(liste, resultats):
            _apres_envoi(x, ok, detail)
            envoyes += 1
    return envoyes


# ---------------------------------------------------------------------------
# Récapitulatif quotidien
# ---------------------------------------------------------------------------
def construire_recap():
    """Texte du récapitulatif du jour (e-mail)."""
    maintenant = _maintenant()
    jour = maintenant.strftime("%Y-%m-%d")
    e = _etat()
    evts = e["jour"]["evenements"] if e["jour"].get("date") == jour else []
    lignes = [f"Récapitulatif GED du {C.date_longue(jour)}", ""]

    archives = [x for x in evts if x["nature"] == "suivi"]
    lignes.append(f"SUIVI DU JOUR ({len(archives)})")
    for x in archives[:40]:
        lignes.append(f"  {x['le']}  {x['titre']}")
    if len(archives) > 40:
        lignes.append(f"  … et {len(archives) - 40} autre(s)")
    if not archives:
        lignes.append("  Aucun mouvement.")
    lignes.append("")

    alertes = [x for x in evts if x["nature"] == "direction"]
    if alertes:
        lignes.append(f"ALERTES DIRECTION ({len(alertes)})")
        for x in alertes[:20]:
            lignes.append(f"  {x['le']}  {x['titre']}")
        lignes.append("")

    # Courriers en retard
    try:
        retard = []
        for x in C.entrants().values():
            if x.get("statut_reponse") in ("a_preparer", "en_cours") and x.get("echeance"):
                try:
                    if datetime.fromisoformat(x["echeance"]) < maintenant:
                        retard.append(x)
                except ValueError:
                    pass
        lignes.append(f"COURRIERS ENTRANTS SANS RÉPONSE, ÉCHÉANCE DÉPASSÉE ({len(retard)})")
        for x in sorted(retard, key=lambda y: y.get("echeance", ""))[:15]:
            urg = " [URGENT]" if x.get("urgence") == "urgente" else ""
            lignes.append(f"  échéance {C.date_courte(x['echeance'][:10])}{urg} — {x.get('objet', '')[:90]}")
        a_valider = [r for r in C.reponses().values() if r.get("statut") == "a_valider"]
        lignes.append(f"\nPROJETS DE RÉPONSE À VALIDER : {len(a_valider)}")
        for r in a_valider[:10]:
            lignes.append(f"  {r.get('signataire', '')} — {r.get('objet', '')[:90]}")
        lignes.append("")
    except Exception:  # noqa: BLE001
        logging.error("Récap : courriers", exc_info=True)

    # Notes internes
    try:
        import notes as N  # noqa: PLC0415
        en_circuit = [n for n in N.notes().values() if n.get("statut") == "en_circuit"]
        lignes.append(f"NOTES INTERNES EN ATTENTE DE VISA OU SIGNATURE ({len(en_circuit)})")
        for n in en_circuit[:10]:
            et = N.etape_courante(n) or {}
            lignes.append(f"  {N.ROLES.get(et.get('role'), '')} {et.get('code', '')} — {n.get('objet', '')[:80]}")
        diffusees = [n for n in N.notes().values() if n.get("statut") == "diffuse"]
        non_lus = [(n, sum(1 for l in n.get("lectures", []) if not (l.get("lu_le") or l.get("traite_le"))))
                   for n in diffusees]
        non_lus = [(n, k) for n, k in non_lus if k]
        if non_lus:
            lignes.append(f"\nNOTES DIFFUSÉES NON LUES PAR TOUS ({len(non_lus)})")
            for n, k in non_lus[:10]:
                lignes.append(f"  {k} non lu(s) — {n.get('reference', '')} {n.get('objet', '')[:70]}")
        lignes.append("")
    except Exception:  # noqa: BLE001
        pass

    # Remises
    try:
        import remise as R  # noqa: PLC0415
        en_cours = [x for x in R.remises().values() if x.get("statut") == "en_cours"]
        if en_cours:
            lignes.append(f"REMISES PAR COURSIER EN COURS ({len(en_cours)})")
            for x in en_cours[:10]:
                essais = len(x.get("tentatives", []))
                lignes.append(f"  {x.get('objet', '')[:80]}"
                              + (f" — {essais} tentative(s) sans succès" if essais else ""))
            lignes.append("")
    except Exception:  # noqa: BLE001
        pass

    lignes.append("—")
    lignes.append("Huri Money SA — WAKATI Mobile Money | Union des Comores")
    lignes.append("Message automatique de la GED. Les alertes urgentes sont envoyées à part.")
    return "\n".join(lignes)


def envoyer_recap(forcer=False):
    maintenant = _maintenant()
    jour = maintenant.strftime("%Y-%m-%d")
    with C._verrou:
        e = _etat()
        if not forcer and e.get("dernier_recap") == jour:
            return False, "déjà envoyé aujourd'hui"
        e["dernier_recap"] = jour
        _sauver(e)
    dest = emails_direction()
    if not dest:
        return False, "aucun destinataire (Paramètres › Notifications)"
    item = {"nature": "recap", "canal": "email", "dest": dest,
            "titre": f"Récapitulatif GED du {C.date_courte(jour)}",
            "sujet": f"GED — récapitulatif du {C.date_courte(jour)}",
            "texte": construire_recap(), "urgent": True}
    ok, detail = _envoyer_un(item)
    with C._verrou:
        e = _etat()
        _journaliser(e, item, ok, detail)
        _sauver(e)
    return ok, detail


def _heure_du_recap(maintenant):
    r = reglages()
    if not r.get("recap_actif", True) or maintenant.weekday() == 6:
        return False
    return maintenant.hour * 60 + maintenant.minute >= _hm(r.get("recap_heure"), 17 * 60)


# ---------------------------------------------------------------------------
# Boucle de fond
# ---------------------------------------------------------------------------
def boucle():
    time.sleep(5)
    derniers_rappels = 0.0
    while True:
        try:
            vider_file()
            if time.time() - derniers_rappels >= 600:
                derniers_rappels = time.time()
                for f in list(_CTX["rappels"]):
                    try:
                        f()
                    except Exception:  # noqa: BLE001
                        logging.error("Relances automatiques", exc_info=True)
            maintenant = _maintenant()
            if _heure_du_recap(maintenant) and _etat().get("dernier_recap") != maintenant.strftime("%Y-%m-%d"):
                envoyer_recap()
        except Exception:  # noqa: BLE001
            logging.error("Boucle des notifications", exc_info=True)
        time.sleep(30)


# ---------------------------------------------------------------------------
# Écran de réglage et journal (responsable GED)
# ---------------------------------------------------------------------------
@bp_notifications.route("/", methods=["GET", "POST"])
def ecran():
    if not C.est_responsable_ged():
        flash("Écran réservé à la responsable GED.", "error")
        return redirect(url_for("index"))
    if request.method == "POST":
        f = request.form
        action = f.get("action", "enregistrer")
        if action == "enregistrer":
            with C._verrou:
                e = _etat()
                rg = e.setdefault("reglages", {})
                for champ in ("canal_courrier", "canal_direction", "emails_direction",
                              "silence_debut", "silence_fin", "recap_heure", "adresse_ged"):
                    rg[champ] = f.get(champ, "").strip().lstrip("#")
                for champ in ("silence_actif", "silence_dimanche", "recap_actif"):
                    rg[champ] = f.get(champ) == "on"
                for champ, mini, maxi in (("regroupement_s", 0, 900), ("delai_rappel_h", 1, 240)):
                    try:
                        rg[champ] = max(mini, min(maxi, int(f.get(champ) or REGLAGES_DEFAUT[champ])))
                    except ValueError:
                        rg[champ] = REGLAGES_DEFAUT[champ]
                _sauver(e)
            flash("Réglages des notifications enregistrés.", "success")
        elif action in ("tester_courrier", "tester_direction"):
            nature = "suivi" if action == "tester_courrier" else "direction"
            dest = _canal(nature)
            item = {"nature": nature, "canal": "cliq_canal", "dest": dest, "carte": None,
                    "titre": "Test", "texte": f"✅ Test GED — canal {NATURES[nature]}"}
            ok, detail = _envoyer_un(item) if dest else (False, "canal non configuré")
            flash(f"{'Envoyé' if ok else 'Échec'} : {detail}", "success" if ok else "error")
        elif action == "tester_prive":
            email = f.get("email_test", "").strip() or _email_session()
            ok, detail = _envoyer_un({"nature": "info", "canal": "cliq_prive", "dest": email.strip().lower(),
                                      "titre": "Test", "texte": "✅ Test GED — message privé",
                                      "secours_email": False})
            flash(f"{'Envoyé' if ok else 'Échec'} : {detail}", "success" if ok else "error")
        elif action == "recap":
            ok, detail = envoyer_recap(forcer=True)
            flash(f"Récapitulatif {'envoyé' if ok else 'non envoyé'} : {detail}", "success" if ok else "error")
        elif action == "vider":
            nb = vider_file(forcer=True)
            flash(f"{nb} message(s) en attente envoyé(s).", "success")
        return redirect(url_for("notifications.ecran"))
    e = _etat()
    return render_template("notifications.html", page="notifications", r=reglages(),
                           attente=sorted(e["attente"], key=lambda x: x.get("apres", "")),
                           journal=list(reversed(e["journal"]))[:150], natures_notif=NATURES,
                           canal_courrier=_canal("suivi"), canal_direction=_canal("direction"),
                           dest_direction=emails_direction(), silence=en_silence(),
                           apercu_recap=construire_recap() if request.args.get("apercu") else "",
                           **C._contexte_commun())


def init_routeur(app, protection=None):
    """Enregistre l'écran et lance la boucle de fond (une seule fois)."""
    if protection is not None:
        for nom, vue in list(app.view_functions.items()):
            if nom.startswith("notifications."):
                app.view_functions[nom] = protection(vue)
    @app.before_request
    def _memoriser_racine():
        schema = request.headers.get("X-Forwarded-Proto", request.scheme)
        hote = request.headers.get("X-Forwarded-Host", request.host)
        _CTX["racine"] = f"{schema}://{hote}"

    if not _CTX["lances"]:
        _CTX["lances"] = True
        threading.Thread(target=boucle, daemon=True, name="routeur-notifications").start()
    r = reglages()
    print(f"[NOTIF] Routeur actif — #{_canal('suivi') or '?'} / #{_canal('direction') or '?'}, "
          f"silence {r['silence_debut']}–{r['silence_fin']}"
          f"{' + dimanche' if r['silence_dimanche'] else ''}, récap {r['recap_heure']}")


def racine():
    """Adresse publique de la GED : réglage, sinon celle de la dernière requête."""
    return (reglages().get("adresse_ged") or _CTX.get("racine") or "").rstrip("/")


def lien(endpoint, **kw):
    """Lien absolu vers un écran GED (https derrière Caddy)."""
    if not has_request_context():
        return ""
    schema = request.headers.get("X-Forwarded-Proto", request.scheme)
    hote = request.headers.get("X-Forwarded-Host", request.host)
    return f"{schema}://{hote}{url_for(endpoint, **kw)}"


def _email_session():
    if not has_request_context():
        return ""
    from flask import session  # noqa: PLC0415
    return (session.get("email") or "").strip().lower()
