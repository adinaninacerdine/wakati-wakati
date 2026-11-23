"""
notes.py — Courriers internes : notes de service, circulaires, demandes
internes et mises à disposition
=======================================================================

Mise en œuvre de la procédure « Courriers internes » du manuel de gestion
des courriers (Admin/Archives, § 5) dans la GED :

  1. Rédaction par une direction (ou par la responsable GED pour elle).
  2. Circuit de validation calculé selon le type :
       - validation de la direction émettrice quand la note est saisie
         pour elle par quelqu'un d'autre ;
       - visa du DAF pour les notes de service, circulaires et mises à
         disposition (sauf si le DAF en est l'émetteur) ;
       - signature : DG pour les circulaires, les mises à disposition et
         les notes de service marquées « importantes » ; DAF pour les
         autres notes de service ; la direction émettrice pour les
         demandes internes.
  3. Signature électronique interne : l'approbation du signataire, connecté
     par son compte Zoho, est horodatée et portée sur le PDF.
  4. Archivage automatique dans WorkDrive (nommage HM26, registre).
  5. Diffusion : chaque destinataire reçoit par Cliq et par e-mail un lien
     personnel ; il confirme « lu et pris connaissance ». Pour une demande
     interne, le service traitant prend en charge puis répond (24 h).

Les destinataires n'ont pas de compte GED : le lien personnel (jeton)
tient lieu d'accès à cette seule note.

Données : courrier/notes/notes.json, courrier/notes/personnel.json,
courrier/notes/<id>/ (PDF).
"""

import functools
import logging
import re
import secrets
import threading
from datetime import datetime, timedelta

from flask import (Blueprint, abort, flash, has_request_context, redirect,
                   render_template, request, send_file, session, url_for)

import courrier as C

bp_notes = Blueprint("notes", __name__, url_prefix="/notes")
bp_note_public = Blueprint("note_public", __name__, url_prefix="/n")

DOSSIER = C.DONNEES / "notes"
F_NOTES = DOSSIER / "notes.json"
F_PERSONNEL = DOSSIER / "personnel.json"

# ---------------------------------------------------------------------------
# Référentiels
# ---------------------------------------------------------------------------
# type_ged : type du nommage HM26 et du dossier WorkDrive.
TYPES_NOTE = {
    "NS": {"nom": "Note de service", "titre": "NOTE DE SERVICE", "type_ged": "NOT",
           "visa_daf": True, "action": "lecture"},
    "CIR": {"nom": "Circulaire", "titre": "CIRCULAIRE", "type_ged": "CIR",
            "visa_daf": True, "action": "lecture"},
    "DEM": {"nom": "Demande interne", "titre": "DEMANDE INTERNE", "type_ged": "DEM",
            "visa_daf": False, "action": "traitement"},
    "MAD": {"nom": "Mise à disposition", "titre": "MISE À DISPOSITION", "type_ged": "MAD",
            "visa_daf": True, "action": "lecture"},
}

STATUTS = {
    "brouillon": "Brouillon",
    "en_circuit": "En validation",
    "retourne": "Retournée",
    "diffuse": "Diffusée",
    "traitee": "Traitée",
    "abandonne": "Abandonnée",
}

ROLES = {"validation": "Validation", "visa": "Visa", "signature": "Signature"}

# Service GED (dossier d'archive) proposé par défaut selon la direction émettrice
# DG : Direction / Admin ; DT : outils informatiques et application WAKATI (DSI) ;
# DOP : direction transversale des opérations (formations, ouverture de comptes,
# appui à l'animation), intérim assuré par le DG.
SERVICE_PAR_DIRECTION = {"DG": "DGA", "DAF": "FIN", "DT": "DSI", "DCOM": "COM", "SC": "COM",
                         "DOP": "OPS"}

DELAI_DEMANDE_H = 24
FORMAT_REFERENCE = "N°{annee}-{numero}/{type}/{emetteur}/HM"

_CTX = {"archiver_note": None, "dernier_numero": None}


# ---------------------------------------------------------------------------
# Données
# ---------------------------------------------------------------------------
def notes():
    return C._lire(F_NOTES, {})


def note(nid):
    return notes().get(nid)


def _sauver(toutes):
    C._ecrire(F_NOTES, toutes)


def personnel(actifs_seulement=False):
    liste = C._lire(F_PERSONNEL, [])
    if actifs_seulement:
        liste = [p for p in liste if p.get("actif", True)]
    return sorted(liste, key=lambda p: (p.get("service", ""), p.get("nom", "").lower()))


def _id_personne():
    return f"P-{secrets.token_hex(5)}"


def _dossier(nid):
    d = DOSSIER / nid
    d.mkdir(parents=True, exist_ok=True)
    return d


def directions():
    """Les directions sont les signataires déclarés dans Paramètres."""
    return C.parametres().get("signataires", [])


def direction(code):
    return C.signataire(code) or {"code": code, "fonction": code, "nom": "", "email": ""}


def _services():
    return C._CTX.get("services", {}) or {}


# ---------------------------------------------------------------------------
# Droits
# ---------------------------------------------------------------------------
def _email():
    if not has_request_context():
        return ""
    return (session.get("email") or "").strip().lower()


def _sans_auth():
    return not _email()


def codes_utilisateur():
    """Directions au nom desquelles l'utilisateur connecté agit."""
    if _sans_auth():
        return {d["code"] for d in directions()}
    codes = set(C.codes_signataire_pour(_email()))
    if _email() in (C._auth_liste("UTILISATEURS_DG") or set()):
        codes.add("DG")
        codes |= {d["code"] for d in directions() if d.get("interim") == "DG"}
    return codes


def peut_rediger_pour(code):
    return C.est_responsable_ged() or code in codes_utilisateur()


def directions_redigeables():
    return [d for d in directions() if peut_rediger_pour(d["code"])]


def peut_modifier(n):
    return (n.get("statut") in ("brouillon", "retourne")
            and (C.est_responsable_ged() or n.get("emetteur") in codes_utilisateur()
                 or n.get("cree_par_email") == _email()))


def etape_courante(n):
    if n.get("statut") != "en_circuit":
        return None
    i = n.get("etape", 0)
    etapes = n.get("etapes", [])
    return etapes[i] if 0 <= i < len(etapes) else None


def peut_decider(n):
    e = etape_courante(n)
    return bool(e) and e["code"] in codes_utilisateur()


def _reserve(vue):
    """Écrans de rédaction : responsable GED ou membre d'une direction."""
    @functools.wraps(vue)
    def enveloppe(*args, **kwargs):
        if not (C.est_responsable_ged() or codes_utilisateur()):
            flash("Écran réservé aux directions et à la responsable GED.", "error")
            return redirect(url_for("index"))
        return vue(*args, **kwargs)
    return enveloppe


# ---------------------------------------------------------------------------
# Circuit
# ---------------------------------------------------------------------------
def signataire_final(n):
    t = n.get("type")
    if t in ("CIR", "MAD"):
        return "DG"
    if t == "NS":
        return "DG" if n.get("importante") else "DAF"
    return n.get("emetteur")


def calculer_etapes(n, soumis_par_emetteur):
    etapes = []
    em = n.get("emetteur")
    if not soumis_par_emetteur:
        etapes.append({"role": "validation", "code": em})
    if TYPES_NOTE[n["type"]]["visa_daf"] and em != "DAF" \
            and not (etapes and etapes[-1]["code"] == "DAF"):
        etapes.append({"role": "visa", "code": "DAF"})
    sig = signataire_final(n)
    if etapes and etapes[-1]["code"] == sig:
        etapes[-1]["role"] = "signature"
    elif not (soumis_par_emetteur and sig == em and not etapes):
        etapes.append({"role": "signature", "code": sig})
    for e in etapes:
        d = direction(e["code"])
        e.update({"fonction": d.get("fonction", e["code"]), "nom": d.get("nom", ""),
                  "statut": "attente", "le": "", "par": "", "observation": ""})
    return etapes


def apercu_circuit(n):
    """Étapes telles qu'elles seront à la soumission (pour l'écran)."""
    soumis = n.get("emetteur") in codes_utilisateur()
    return calculer_etapes(n, soumis)


def _historiser(n, action, detail="", par=None):
    n.setdefault("historique", []).append({
        "le": C._maintenant(), "par": par or C.utilisateur_courant(),
        "action": action, "detail": detail})
    n["maj_le"] = C._maintenant()


def _notifier_valideur(n, e):
    """Message privé Cliq au signataire attendu, avec le lien vers la note."""
    d = direction(e["code"])
    if not d.get("email"):
        return
    try:
        import notifier  # noqa: PLC0415
        lien = _url_ged("notes.edition", nid=n["id"])
        notifier.message_cliq_personne(
            d["email"],
            f"✍ {TYPES_NOTE[n['type']]['nom']} à {ROLES[e['role']].lower()} : "
            f"{n.get('objet', '')}\nÉmetteur : {direction(n['emetteur']).get('fonction')}\n{lien}")
    except Exception:  # noqa: BLE001
        logging.error("Notification du valideur impossible", exc_info=True)


def _racine():
    """https://ged.wakati.km derrière Caddy (la requête arrive en http)."""
    schema = request.headers.get("X-Forwarded-Proto", request.scheme)
    hote = request.headers.get("X-Forwarded-Host", request.host)
    return f"{schema}://{hote}"


def _url_ged(endpoint, **kw):
    return f"{_racine()}{url_for(endpoint, **kw)}"


def soumettre(nid):
    with C._verrou:
        toutes = notes()
        n = toutes.get(nid)
        if not n or n.get("statut") not in ("brouillon", "retourne"):
            return "Cette note n'est plus modifiable."
        erreurs = verifier(n)
        if erreurs:
            return " ".join(erreurs)
        soumis_par_emetteur = n["emetteur"] in codes_utilisateur()
        n["etapes"] = calculer_etapes(n, soumis_par_emetteur)
        n["etape"] = 0
        n["soumis_le"] = C._maintenant()
        n["soumis_par"] = C.utilisateur_courant()
        n["motif_retour"] = ""
        if n["etapes"]:
            n["statut"] = "en_circuit"
            _historiser(n, "Soumise au circuit",
                        " → ".join(f"{ROLES[e['role']]} {e['code']}" for e in n["etapes"]))
            _sauver(toutes)
            suivante = n["etapes"][0]
        else:
            _historiser(n, "Signée par l'émetteur", direction(n["emetteur"]).get("fonction", ""))
            _sauver(toutes)
            suivante = None
    if suivante:
        _notifier_valideur(n, suivante)
        return ""
    return finaliser(nid, C.utilisateur_courant())


def decider(nid, decision, observation=""):
    with C._verrou:
        toutes = notes()
        n = toutes.get(nid)
        e = etape_courante(n or {})
        if not e or e["code"] not in codes_utilisateur():
            return "Cette étape n'est pas la vôtre, ou elle a déjà été traitée."
        observation = (observation or "").strip()
        if decision == "retourner":
            if not observation:
                return "Indiquez ce qu'il faut corriger."
            e.update({"statut": "retour", "le": C._maintenant(),
                      "par": C.utilisateur_courant(), "observation": observation})
            n["statut"] = "retourne"
            n["motif_retour"] = observation
            _historiser(n, f"Retournée ({ROLES[e['role']]} {e['code']})", observation)
            _sauver(toutes)
            createur = n.get("cree_par_email")
            suivante = None
        else:
            e.update({"statut": "fait", "le": C._maintenant(),
                      "par": C.utilisateur_courant(), "observation": observation})
            _historiser(n, f"{ROLES[e['role']]} {e['code']}", observation)
            n["etape"] = n.get("etape", 0) + 1
            suivante = etape_courante(n)
            _sauver(toutes)
            createur = None
    if decision == "retourner":
        if createur:
            try:
                import notifier  # noqa: PLC0415
                notifier.message_cliq_personne(
                    createur, f"↩ Note retournée : {n.get('objet', '')}\n{observation}\n"
                              f"{_url_ged('notes.edition', nid=nid)}")
            except Exception:  # noqa: BLE001
                logging.error("Notification du retour impossible", exc_info=True)
        return ""
    if suivante:
        _notifier_valideur(n, suivante)
        return ""
    return finaliser(nid, C.utilisateur_courant())


# ---------------------------------------------------------------------------
# Signature, numérotation, PDF, archivage, diffusion
# ---------------------------------------------------------------------------
def _reserver_numero(n):
    if n.get("numero"):
        return n["numero"]
    type_ged = TYPES_NOTE[n["type"]]["type_ged"]
    service = n.get("service", "DGA")
    dernier = 0
    fn = _CTX.get("dernier_numero") or C._CTX.get("dernier_numero")
    if fn:
        try:
            dernier = fn(service, type_ged) or 0
        except Exception:  # noqa: BLE001
            dernier = 0
    annee = datetime.now().strftime("%Y")
    for autre in notes().values():
        if (autre.get("numero") and autre.get("service") == service
                and TYPES_NOTE.get(autre.get("type"), {}).get("type_ged") == type_ged
                and str(autre.get("signe_le", "")).startswith(annee)):
            try:
                dernier = max(dernier, int(autre["numero"]))
            except ValueError:
                pass
    n["numero"] = f"{dernier + 1:03d}"
    return n["numero"]


def reference(n):
    if not n.get("numero"):
        return ""
    annee = (n.get("signe_le") or C._maintenant())[:4]
    return FORMAT_REFERENCE.format(annee=annee, numero=n["numero"], type=n["type"],
                                   emetteur=n.get("emetteur", ""))


def finaliser(nid, par):
    """Dernière signature : numéro, PDF définitif, archivage et diffusion."""
    racine = _racine()
    with C._verrou:
        toutes = notes()
        n = toutes[nid]
        _reserver_numero(n)
        n["signe_le"] = C._maintenant()
        n["signe_par"] = par
        n["signataire"] = signataire_final(n)
        n["reference"] = reference(n)
        n["statut"] = "diffuse"
        if n["type"] == "DEM":
            n["echeance"] = (datetime.now() + timedelta(hours=int(n.get("delai_h") or DELAI_DEMANDE_H))
                             ).isoformat(timespec="seconds")
        n["lectures"] = _lectures(n)
        _historiser(n, "Signée", f"{n['reference']} — {direction(n['signataire']).get('fonction')}", par)
        _sauver(toutes)
    try:
        generer_pdf(n, _dossier(nid) / "note.pdf", definitif=True)
    except Exception:  # noqa: BLE001
        logging.error("PDF de la note %s", nid, exc_info=True)
    threading.Thread(target=_apres_signature, args=(nid, racine), daemon=True).start()
    return ""


def _apres_signature(nid, racine):
    diffuser(nid, racine)
    archiver(nid)


def _lectures(n):
    """Liste de diffusion résolue au moment de la signature."""
    dest = n.get("destinataires", {})
    pers = personnel(actifs_seulement=True)
    choisis = []
    if dest.get("tous"):
        choisis = list(pers)
    else:
        services = set(dest.get("services", []))
        ids = set(dest.get("personnes", []))
        choisis = [p for p in pers if p.get("service") in services or p.get("id") in ids]
    traitants = set(n.get("traitants", [])) if n.get("type") == "DEM" else set()
    if traitants:
        choisis += [p for p in pers if p.get("id") in traitants]
    vus, lectures = set(), []
    for p in choisis:
        cle = (p.get("email") or p.get("id")).lower()
        if cle in vus:
            continue
        vus.add(cle)
        lectures.append({
            "id": p.get("id"), "nom": p.get("nom", ""), "fonction": p.get("fonction", ""),
            "service": p.get("service", ""), "email": p.get("email", ""),
            "jeton": secrets.token_urlsafe(24),
            "traitant": p.get("id") in traitants,
            "envoye_le": "", "canaux": "", "lu_le": "", "pris_en_charge_le": "",
            "traite_le": "", "reponse": "",
        })
    return lectures


def _message(n, l, racine, relance=False):
    t = TYPES_NOTE[n["type"]]
    # Construit à la main : la diffusion tourne hors requête (tâche de fond).
    lien = f"{racine}/n/{l['jeton']}"
    action = ("Merci de la prendre en charge et d'y répondre" if l.get("traitant")
              else "Merci de la lire et de confirmer « lu et pris connaissance »")
    tete = "🔁 Rappel — " if relance else "📄 "
    return (f"{tete}{t['nom']} {n.get('reference', '')}\n"
            f"Objet : {n.get('objet', '')}\n"
            f"De : {direction(n['emetteur']).get('fonction', '')}\n"
            f"{action} :\n{lien}")


def diffuser(nid, racine, seulement_non_lus=False):
    """Envoie à chaque destinataire son lien personnel (Cliq + e-mail)."""
    try:
        import notifier  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        logging.error("notifier indisponible : diffusion impossible")
        return 0
    n = note(nid)
    if not n:
        return 0
    envoyes = 0
    resultats = {}
    for l in n.get("lectures", []):
        if seulement_non_lus and (l.get("lu_le") or l.get("traite_le")):
            continue
        if not l.get("email"):
            resultats[l["jeton"]] = ("", "sans adresse")
            continue
        texte = _message(n, l, racine, relance=seulement_non_lus)
        canaux = []
        ok, _ = notifier.message_cliq_personne(l["email"], texte)
        if ok:
            canaux.append("Cliq")
        ok, _ = notifier.envoyer_email(
            [l["email"]], f"{TYPES_NOTE[n['type']]['nom']} — {n.get('objet', '')}", texte)
        if ok:
            canaux.append("e-mail")
        resultats[l["jeton"]] = (C._maintenant(), " + ".join(canaux) or "échec d'envoi")
        envoyes += 1 if canaux else 0
    with C._verrou:
        toutes = notes()
        n = toutes.get(nid)
        for l in n.get("lectures", []):
            if l["jeton"] in resultats:
                le, canaux = resultats[l["jeton"]]
                if le:
                    l["envoye_le"] = le
                l["canaux"] = canaux
        _historiser(n, "Relance des non-lus" if seulement_non_lus else "Diffusée",
                    f"{envoyes} destinataire(s) prévenu(s)", "GED")
        _sauver(toutes)
    return envoyes


def _objet_fichier(texte):
    """Même nettoyage que l'archivage manuel (app.nettoyer_texte)."""
    import unicodedata  # noqa: PLC0415
    t = unicodedata.normalize("NFKD", texte or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).upper().replace(" ", "_")
    t = re.sub(r"[^A-Z0-9_\-]", "", t)
    return re.sub(r"_{2,}", "_", t).strip("_")[:60] or "SANS_OBJET"


def _nom_archive(n):
    objet = _objet_fichier(n.get("objet", ""))
    date = (n.get("signe_le") or C._maintenant())[:10].replace("-", "")
    return (f"HM26-{n.get('service', 'DGA')}-{TYPES_NOTE[n['type']]['type_ged']}_"
            f"{date}_{n['numero']}_{objet}.pdf")


def archiver(nid):
    fn = _CTX.get("archiver_note")
    n = note(nid)
    source = DOSSIER / nid / "note.pdf"
    if not fn or not n or not source.exists():
        return
    nom = _nom_archive(n)
    try:
        lien = fn(source, nom, n.get("service", "DGA"), TYPES_NOTE[n["type"]]["type_ged"],
                  n.get("signe_le", "")[:10], n.get("objet", ""),
                  _libelle_traitants(n) if n.get("type") == "DEM" else _libelle_destinataires(n))
    except Exception:  # noqa: BLE001
        logging.error("Archivage de la note %s impossible", nid, exc_info=True)
        lien = ""
    with C._verrou:
        toutes = notes()
        n = toutes.get(nid)
        if lien and str(lien).startswith("http"):
            n["nom_archive"], n["lien_archive"] = nom, lien
            _historiser(n, "Archivée", nom, "GED")
        else:
            n["archive_echec"] = C._maintenant()
            _historiser(n, "Archivage à relancer", "WorkDrive n'a pas répondu", "GED")
        _sauver(toutes)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _libelle_destinataires(n):
    dest = n.get("destinataires", {})
    if dest.get("tous"):
        return "Tout le personnel"
    services = _services()
    morceaux = [services.get(s, s) for s in dest.get("services", [])]
    noms = {p["id"]: p.get("nom", "") for p in personnel()}
    morceaux += [noms.get(i, i) for i in dest.get("personnes", [])]
    return ", ".join(m for m in morceaux if m) or "—"


def _libelle_traitants(n):
    noms = {p["id"]: f"{p.get('nom', '')}" + (f" ({p['fonction']})" if p.get("fonction") else "")
            for p in personnel()}
    return ", ".join(noms.get(i, i) for i in n.get("traitants", [])) or "—"


def generer_pdf(n, chemin, definitif=False):
    pdf, t = C._papier("serif", not definitif)
    pdf.c_margin = 0
    pdf.add_page()
    f = pdf.police
    L = 4.6
    cfg = TYPES_NOTE[n["type"]]
    em = direction(n.get("emetteur", ""))

    # Émetteur et date
    pdf.set_font(f, "B", 11)
    pdf.multi_cell(110, L, t(em.get("fonction", "")), new_x="LMARGIN", new_y="TOP")
    pdf.set_font(f, "", 11)
    date = (n.get("signe_le") or C._maintenant())[:10]
    pdf.cell(0, L, t(f"Moroni, le {C.date_longue(date)}"), align="R",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(9)

    # Titre et numéro
    pdf.set_font(f, "B", 15)
    pdf.cell(0, 7, t(cfg["titre"]), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(f, "", 11)
    ref = n.get("reference") or "N° …… (attribué à la signature)"
    pdf.cell(0, 6, t(ref), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(7)

    # Destinataires / objet
    def ligne(lib, val, gras=False):
        pdf.set_font(f, "BU", 11)
        pdf.write(L, t(lib))
        pdf.set_font(f, "B" if gras else "", 11)
        pdf.write(L, t(f" : {val}"))
        pdf.ln(L + 1.5)

    if n["type"] == "DEM":
        ligne("À l'attention de", _libelle_traitants(n))
        if n.get("destinataires") and _libelle_destinataires(n) != "—":
            ligne("Copie", _libelle_destinataires(n))
    else:
        ligne("Destinataires", _libelle_destinataires(n))
    ligne("Objet", n.get("objet") or "[à compléter : objet]", gras=True)
    if n["type"] == "DEM":
        delai = int(n.get("delai_h") or DELAI_DEMANDE_H)
        ech = f" (avant le {C.date_courte(n['echeance'][:10])} à {n['echeance'][11:16]})" \
            if n.get("echeance") else ""
        ligne("Réponse attendue", f"sous {delai} h{ech}")
    pdf.ln(4)

    # Corps, justifié
    pdf.set_font(f, "", 11)
    for paragraphe in C._paragraphes(n.get("corps")):
        pdf.multi_cell(0, L, t(paragraphe), align="J", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3.5)

    # Signature
    sig = direction(signataire_final(n))
    if pdf.get_y() > 297 - C.BAS_CORPS - 52:
        pdf.add_page()
    pdf.ln(8)
    x_sig = 210 - C.MARGE_D - 78
    pdf.set_x(x_sig)
    pdf.set_font(f, "", 11)
    pdf.multi_cell(78, L, t(sig.get("fonction", "")), align="C", new_x="LMARGIN", new_y="NEXT")
    if sig.get("interim"):
        pdf.set_x(x_sig)
        pdf.set_font(f, "I", 9.5)
        pdf.multi_cell(78, 4.2, t(f"par intérim : {C._sans_article(direction(sig['interim']).get('fonction', ''))}"),
                       align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    if definitif and n.get("signe_le"):
        y = pdf.get_y()
        with pdf.local_context(draw_color=(3, 34, 76), text_color=(3, 34, 76)):
            pdf.set_line_width(0.4)
            pdf.rect(x_sig + 6, y, 66, 15)
            pdf.set_xy(x_sig + 6, y + 1.6)
            pdf.set_font(f, "B", 8.5)
            pdf.cell(66, 4, t("SIGNÉ ÉLECTRONIQUEMENT"), align="C", new_x="LEFT", new_y="NEXT")
            pdf.set_font(f, "", 8)
            le = n["signe_le"]
            pdf.cell(66, 3.8, t(f"le {C.date_courte(le[:10])} à {le[11:16]} — GED Huri Money"),
                     align="C", new_x="LEFT", new_y="NEXT")
            pdf.cell(66, 3.8, t(f"réf. {n['id']}"), align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.set_y(y + 18)
    else:
        pdf.ln(16)
    pdf.set_x(x_sig)
    pdf.set_font(f, "B", 11)
    pdf.cell(78, L, t(sig.get("nom", "")), align="C", new_x="LMARGIN", new_y="NEXT")

    # Visas
    visas = [e for e in n.get("etapes", []) if e.get("role") in ("visa", "validation")
             and e.get("statut") == "fait"]
    if visas:
        pdf.ln(6)
        pdf.set_font(f, "I", 9)
        pdf.set_text_color(*C.GRIS)
        for e in visas:
            pdf.cell(0, 4.2, t(f"{ROLES[e['role']]} : {e.get('fonction', e['code'])} — "
                               f"le {C.date_courte(e['le'][:10])} à {e['le'][11:16]}"),
                     new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*C.TEXTE)

    # Ampliations
    copies = [c.strip() for c in re.split(r"[;\n]", n.get("copies") or "") if c.strip()]
    if copies:
        pdf.ln(5)
        pdf.set_font(f, "U", 10)
        pdf.cell(0, 5, t("Ampliation :"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(f, "", 10)
        for c in copies:
            pdf.cell(0, 4.8, t(f"- {c}"), new_x="LMARGIN", new_y="NEXT")

    chemin.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(chemin))
    return chemin


# ---------------------------------------------------------------------------
# Saisie
# ---------------------------------------------------------------------------
def verifier(n):
    erreurs = []
    if not (n.get("objet") or "").strip():
        erreurs.append("L'objet est obligatoire.")
    if len((n.get("corps") or "").strip()) < 20:
        erreurs.append("Le texte de la note est trop court.")
    if n.get("type") == "DEM":
        if not n.get("traitants"):
            erreurs.append("Choisissez au moins une personne pour traiter la demande.")
    else:
        d = n.get("destinataires", {})
        if not (d.get("tous") or d.get("services") or d.get("personnes")):
            erreurs.append("Choisissez les destinataires.")
    return erreurs


def _champs_formulaire(f, n):
    type_ = f.get("type", n.get("type", "NS"))
    if type_ not in TYPES_NOTE:
        type_ = "NS"
    em = f.get("emetteur", n.get("emetteur", ""))
    if not peut_rediger_pour(em):
        raise PermissionError("Vous ne pouvez pas rédiger au nom de cette direction.")
    service = f.get("service") or SERVICE_PAR_DIRECTION.get(em, "DGA")
    if service not in _services():
        service = "DGA"
    try:
        delai = max(1, min(240, int(f.get("delai_h") or DELAI_DEMANDE_H)))
    except ValueError:
        delai = DELAI_DEMANDE_H
    n.update({
        "type": type_, "emetteur": em, "service": service,
        "objet": f.get("objet", "").strip()[:200],
        "corps": f.get("corps", "").replace("\r\n", "\n").strip(),
        "importante": f.get("importante") == "on",
        "copies": f.get("copies", "").strip(),
        "destinataires": {
            "tous": f.get("tous") == "on",
            "services": [s for s in f.getlist("services") if s in _services()],
            "personnes": f.getlist("personnes"),
        },
        "traitants": f.getlist("traitants") if type_ == "DEM" else [],
        "delai_h": delai,
    })
    return n


def creer(f):
    nid = C._id_court("NOTE")
    n = {"id": nid, "statut": "brouillon", "cree_le": C._maintenant(),
         "cree_par": C.utilisateur_courant(), "cree_par_email": _email(),
         "historique": []}
    _champs_formulaire(f, n)
    _historiser(n, "Créée", TYPES_NOTE[n["type"]]["nom"])
    with C._verrou:
        toutes = notes()
        toutes[nid] = n
        _sauver(toutes)
    return n


# ---------------------------------------------------------------------------
# Présentation
# ---------------------------------------------------------------------------
def _affichee(n):
    lectures = n.get("lectures", [])
    lus = sum(1 for l in lectures if l.get("lu_le") or l.get("traite_le"))
    traitants = [l for l in lectures if l.get("traitant")]
    try:
        retard = (n.get("type") == "DEM" and n.get("statut") == "diffuse" and n.get("echeance")
                  and datetime.fromisoformat(n["echeance"]) < datetime.now())
    except ValueError:
        retard = False
    e = etape_courante(n)
    return {
        **n,
        "type_nom": TYPES_NOTE.get(n.get("type"), {}).get("nom", n.get("type")),
        "statut_lib": STATUTS.get(n.get("statut"), n.get("statut")),
        "emetteur_lib": direction(n.get("emetteur", "")).get("fonction", ""),
        "destinataires_lib": _libelle_destinataires(n),
        "traitants_lib": _libelle_traitants(n),
        "nb_lectures": len(lectures), "nb_lus": lus,
        "taux_lecture": round(lus * 100 / len(lectures)) if lectures else None,
        "nb_traitants": len(traitants),
        "nb_traites": sum(1 for l in traitants if l.get("traite_le")),
        "retard": bool(retard),
        "etape_lib": (f"{ROLES[e['role']]} — {e.get('fonction', e['code'])}" if e else ""),
        "maj_affiche": C._date_heure(n.get("maj_le")),
        "echeance_affiche": (f"{C.date_courte(n['echeance'][:10])} {n['echeance'][11:16]}"
                             if n.get("echeance") else ""),
    }


def compteurs():
    """Pour la barre latérale : notes attendant l'utilisateur connecté."""
    try:
        codes = codes_utilisateur()
        a_valider = sum(1 for n in notes().values()
                        if (etape_courante(n) or {}).get("code") in codes)
        return {"notes_a_valider": a_valider}
    except Exception:  # noqa: BLE001
        return {"notes_a_valider": 0}


# ---------------------------------------------------------------------------
# Écrans GED
# ---------------------------------------------------------------------------
def _contexte():
    return {"types_note": TYPES_NOTE, "statuts_note": STATUTS, "roles": ROLES,
            "directions": directions(), "personnel": personnel(actifs_seulement=True),
            **C._contexte_commun()}


@bp_notes.route("/")
@_reserve
def liste():
    codes = codes_utilisateur()
    toutes = sorted(notes().values(), key=lambda n: n.get("maj_le", ""), reverse=True)
    visibles = [n for n in toutes if C.est_responsable_ged() or n.get("emetteur") in codes
                or any(e.get("code") in codes for e in n.get("etapes", []))]
    a_valider = [_affichee(n) for n in visibles if (etape_courante(n) or {}).get("code") in codes]
    en_cours = [_affichee(n) for n in visibles
                if n.get("statut") in ("brouillon", "retourne", "en_circuit")
                and n not in a_valider]
    diffusees = [_affichee(n) for n in visibles if n.get("statut") in ("diffuse", "traitee")][:40]
    return render_template("notes.html", page="notes", a_valider=a_valider,
                           en_cours=[x for x in en_cours if x["id"] not in {a["id"] for a in a_valider}],
                           diffusees=diffusees, **_contexte())


@bp_notes.route("/nouvelle", methods=["GET", "POST"])
@_reserve
def nouvelle():
    if request.method == "POST":
        try:
            n = creer(request.form)
        except PermissionError as err:
            flash(str(err), "error")
            return redirect(url_for("notes.nouvelle"))
        flash("Brouillon enregistré. Relisez l'aperçu, puis soumettez.", "success")
        return redirect(url_for("notes.edition", nid=n["id"]))
    codes = codes_utilisateur()
    defaut = next((d["code"] for d in directions_redigeables() if d["code"] in codes),
                  (directions_redigeables() or [{"code": "DG"}])[0]["code"])
    n = {"type": request.args.get("type", "NS"), "emetteur": defaut,
         "service": SERVICE_PAR_DIRECTION.get(defaut, "DGA"), "destinataires": {},
         "traitants": [], "delai_h": DELAI_DEMANDE_H, "statut": "brouillon"}
    return render_template("note_edition.html", page="notes", n=_affichee(n), nouvelle=True,
                           modifiable=True, redigeables=directions_redigeables(),
                           circuit=apercu_circuit(n), **_contexte())


@bp_notes.route("/<nid>", methods=["GET", "POST"])
@_reserve
def edition(nid):
    n = note(nid)
    if not n:
        abort(404)
    if request.method == "POST":
        if not peut_modifier(n):
            flash("Cette note n'est plus modifiable.", "error")
            return redirect(url_for("notes.edition", nid=nid))
        try:
            with C._verrou:
                toutes = notes()
                _champs_formulaire(request.form, toutes[nid])
                _historiser(toutes[nid], "Modifiée")
                _sauver(toutes)
        except PermissionError as err:
            flash(str(err), "error")
            return redirect(url_for("notes.edition", nid=nid))
        if request.form.get("action") == "soumettre":
            err = soumettre(nid)
            if err:
                flash(err, "error")
            else:
                n = note(nid)
                flash("Note signée et diffusée." if n.get("statut") == "diffuse"
                      else "Note soumise au circuit de validation.", "success")
        else:
            flash("Modifications enregistrées.", "success")
        return redirect(url_for("notes.edition", nid=nid))
    return render_template("note_edition.html", page="notes", n=_affichee(n), nouvelle=False,
                           modifiable=peut_modifier(n), decision=peut_decider(n),
                           redigeables=directions_redigeables() or [direction(n["emetteur"])],
                           circuit=(n.get("etapes") if n.get("statut") not in ("brouillon", "retourne")
                                    else apercu_circuit(n)),
                           **_contexte())


@bp_notes.route("/<nid>/decision", methods=["POST"])
@_reserve
def decision(nid):
    choix = request.form.get("decision")
    if choix not in ("approuver", "retourner"):
        abort(400)
    err = decider(nid, choix, request.form.get("observation", ""))
    if err:
        flash(err, "error")
    else:
        n = note(nid)
        flash({"retourner": "Note retournée à son rédacteur.",
               "approuver": ("Note signée : elle est archivée et diffusée."
                             if n.get("statut") == "diffuse"
                             else "Visa enregistré : la note passe à l'étape suivante.")}[choix],
              "success")
    return redirect(url_for("notes.edition", nid=nid))


@bp_notes.route("/<nid>/apercu.pdf")
@_reserve
def apercu(nid):
    n = note(nid)
    if not n:
        abort(404)
    final = DOSSIER / nid / "note.pdf"
    if n.get("statut") in ("diffuse", "traitee") and final.exists():
        return send_file(final, mimetype="application/pdf")
    chemin = _dossier(nid) / "apercu.pdf"
    generer_pdf(n, chemin, definitif=False)
    return send_file(chemin, mimetype="application/pdf", max_age=0)


@bp_notes.route("/<nid>/relancer", methods=["POST"])
@_reserve
def relancer(nid):
    n = note(nid)
    if not n or n.get("statut") not in ("diffuse",):
        abort(404)
    nb = diffuser(nid, _racine(), seulement_non_lus=True)
    flash(f"Rappel envoyé à {nb} destinataire(s) qui n'ont pas encore confirmé.", "success")
    return redirect(url_for("notes.edition", nid=nid))


@bp_notes.route("/<nid>/archiver", methods=["POST"])
@_reserve
def rearchiver(nid):
    n = note(nid)
    if not n or n.get("statut") not in ("diffuse", "traitee") or n.get("lien_archive"):
        abort(404)
    threading.Thread(target=archiver, args=(nid,), daemon=True).start()
    flash("Archivage relancé. Le lien WorkDrive apparaît dans quelques secondes.", "success")
    return redirect(url_for("notes.edition", nid=nid))


@bp_notes.route("/<nid>/abandonner", methods=["POST"])
@_reserve
def abandonner(nid):
    with C._verrou:
        toutes = notes()
        n = toutes.get(nid)
        if not n or not peut_modifier(n):
            abort(404)
        n["statut"] = "abandonne"
        _historiser(n, "Abandonnée")
        _sauver(toutes)
    flash("Note abandonnée.", "success")
    return redirect(url_for("notes.liste"))


# --- Personnel (annuaire interne) ------------------------------------------
@bp_notes.route("/personnel", methods=["GET", "POST"])
@_reserve
def ecran_personnel():
    if request.method == "POST":
        if not C.est_responsable_ged():
            flash("Seule la responsable GED tient l'annuaire du personnel.", "error")
            return redirect(url_for("notes.ecran_personnel"))
        f = request.form
        with C._verrou:
            liste = C._lire(F_PERSONNEL, [])
            if f.get("action") == "importer":
                ajoutes = 0
                connus = {(p.get("email") or "").lower() for p in liste if p.get("email")}
                for ligne in (f.get("lignes") or "").splitlines():
                    morceaux = [x.strip() for x in re.split(r"[;\t]", ligne)]
                    if len(morceaux) < 2 or not morceaux[0] or morceaux[0].lower() == "nom":
                        continue
                    nom, fonction, service, email = (morceaux + ["", "", ""])[:4]
                    service = service.upper() if service.upper() in _services() else "DGA"
                    if email and email.lower() in connus:
                        continue
                    liste.append({"id": _id_personne(), "nom": nom, "fonction": fonction,
                                  "service": service, "email": email.lower(), "actif": True})
                    connus.add(email.lower())
                    ajoutes += 1
                flash(f"{ajoutes} personne(s) ajoutée(s).", "success")
            else:
                pid = f.get("id") or _id_personne()
                p = next((x for x in liste if x.get("id") == pid), None)
                if p is None:
                    p = {"id": pid}
                    liste.append(p)
                p.update({"nom": f.get("nom", "").strip(), "fonction": f.get("fonction", "").strip(),
                          "service": f.get("service", "DGA"),
                          "email": f.get("email", "").strip().lower(),
                          "actif": f.get("actif") == "on"})
                if not p["nom"]:
                    liste.remove(p)
                    flash("Le nom est obligatoire.", "error")
                else:
                    flash(f"{p['nom']} enregistré(e).", "success")
            C._ecrire(F_PERSONNEL, liste)
        return redirect(url_for("notes.ecran_personnel"))
    edite = next((p for p in personnel() if p.get("id") == request.args.get("id")), None)
    return render_template("notes_personnel.html", page="notes_personnel",
                           tout=personnel(), edite=edite, **_contexte())


# ---------------------------------------------------------------------------
# Page du destinataire (sans compte, jeton personnel)
# ---------------------------------------------------------------------------
def _par_jeton(jeton):
    if not jeton or len(jeton) < 20:
        return None, None
    import hmac  # noqa: PLC0415
    for n in notes().values():
        for l in n.get("lectures", []):
            if hmac.compare_digest(str(l.get("jeton", "")), str(jeton)):
                return n, l
    return None, None


def _ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "")


@bp_note_public.after_request
def _pas_de_cache(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@bp_note_public.route("/<jeton>")
def page(jeton):
    n, l = _par_jeton(jeton)
    if not n:
        return render_template("note_lecture.html", n=None, l=None), 404
    return render_template("note_lecture.html", n=_affichee(n), l=l, jeton=jeton,
                           paragraphes=C._paragraphes(n.get("corps")),
                           message=request.args.get("m", ""))


@bp_note_public.route("/<jeton>/note.pdf")
def pdf_public(jeton):
    n, _ = _par_jeton(jeton)
    chemin = DOSSIER / (n or {}).get("id", "_") / "note.pdf"
    if not n or not chemin.exists():
        abort(404)
    return send_file(chemin, mimetype="application/pdf",
                     download_name=f"{n.get('reference', n['id']).replace('/', '-')}.pdf")


@bp_note_public.route("/<jeton>/action", methods=["POST"])
def action(jeton):
    n, l = _par_jeton(jeton)
    if not n:
        abort(404)
    quoi = request.form.get("quoi")
    reponse = (request.form.get("reponse") or "").strip()[:2000]
    if quoi == "traite" and not reponse:
        return redirect(url_for("note_public.page", jeton=jeton,
                                m="Indiquez la réponse ou la suite donnée."))
    message = ""
    with C._verrou:
        toutes = notes()
        n = toutes[n["id"]]
        l = next(x for x in n["lectures"] if x["jeton"] == jeton)
        trace = {"ip": _ip(), "appareil": (request.headers.get("User-Agent") or "")[:200]}
        if quoi == "lu" and not l.get("lu_le"):
            l["lu_le"] = C._maintenant()
            l["trace_lecture"] = trace
            _historiser(n, "Lu et pris connaissance", f"{l['nom']}", l["nom"])
            message = "Merci : votre lecture est enregistrée."
        elif quoi == "prise" and l.get("traitant") and not l.get("pris_en_charge_le"):
            l["pris_en_charge_le"] = C._maintenant()
            l["lu_le"] = l.get("lu_le") or l["pris_en_charge_le"]
            _historiser(n, "Prise en charge", l["nom"], l["nom"])
            message = "Prise en charge enregistrée."
        elif quoi == "traite" and l.get("traitant") and not l.get("traite_le"):
            l["traite_le"] = C._maintenant()
            l["reponse"] = reponse
            l["lu_le"] = l.get("lu_le") or l["traite_le"]
            l["trace_traitement"] = trace
            _historiser(n, "Demande traitée", f"{l['nom']} — {reponse[:200]}", l["nom"])
            traitants = [x for x in n["lectures"] if x.get("traitant")]
            if traitants and all(x.get("traite_le") for x in traitants):
                n["statut"] = "traitee"
                n["traitee_le"] = C._maintenant()
            message = "Réponse enregistrée. Merci."
        _sauver(toutes)
    if quoi == "traite":
        createur = n.get("cree_par_email") or direction(n["emetteur"]).get("email")
        try:
            import notifier  # noqa: PLC0415
            if createur:
                notifier.message_cliq_personne(
                    createur, f"✅ Demande traitée par {l['nom']} : {n.get('objet', '')}\n{reponse[:500]}")
        except Exception:  # noqa: BLE001
            logging.error("Notification de traitement impossible", exc_info=True)
    return redirect(url_for("note_public.page", jeton=jeton, m=message))


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------
def init_notes(app, protection=None, archiver_note=None, dernier_numero=None):
    """À appeler APRÈS l'enregistrement des deux blueprints. Seuls les écrans
    /notes sont protégés : /n/<jeton> est ouvert par le destinataire."""
    _CTX["archiver_note"] = archiver_note
    _CTX["dernier_numero"] = dernier_numero
    if protection is not None:
        for nom, vue in list(app.view_functions.items()):
            if nom.startswith("notes."):
                app.view_functions[nom] = protection(vue)
    DOSSIER.mkdir(parents=True, exist_ok=True)
    print(f"[NOTES] Module actif — {len(notes())} note(s), "
          f"{len(personnel(actifs_seulement=True))} personne(s) dans l'annuaire interne")
