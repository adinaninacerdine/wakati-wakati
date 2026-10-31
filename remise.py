"""
remise.py — Remise des courriers par coursier, avec preuve signée
================================================================

La responsable GED crée un BORDEREAU DE REMISE pour un courrier signé.
Le bordereau porte un lien secret (et son QR code) : le coursier l'ouvre
sur son téléphone, sans compte Zoho, et au moment de la remise :

  - le destinataire saisit son nom et sa fonction et signe au doigt ;
  - le coursier photographie le cachet ou la décharge ;
  - la position GPS est relevée ;
  - ou bien le coursier déclare un échec (absent, refus, bureau fermé…).

La GED produit alors un ACCUSÉ DE REMISE en PDF (papier à en-tête Huri
Money), l'archive dans WorkDrive à côté du courrier, l'inscrit dans
l'historique du projet et dans la frise du tableau de bord, et prévient
#ged-notifications dans Cliq.

Deux blueprints :
  bp_remises        /remises/...  écrans GED (connexion requise)
  bp_remise_public  /r/<jeton>    page mobile du coursier (jeton secret)

Données : courrier/remises.json + courrier/remises/<id>/ (signature,
photos, accusé PDF).

Évolution prévue (deuxième temps) : le même mécanisme dans l'autre sens,
pour remettre au déposant d'un courrier entrant un récépissé signé.
"""

import base64
import binascii
import hmac
import io
import logging
import re
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   send_file, url_for)

import courrier as C

bp_remises = Blueprint("remises", __name__, url_prefix="/remises")
bp_remise_public = Blueprint("remise_public", __name__, url_prefix="/r")

F_REMISES = C.F_REMISES
DOSSIER = C.DONNEES / "remises"

VALIDITE_JOURS = 30            # au-delà, le lien du coursier ne s'ouvre plus
TAILLE_MAX = 25 * 1024 * 1024  # corps de requête accepté (photos comprises)
PHOTO_MAX_PX = 1600            # les photos sont réduites à cette taille

STATUTS = {
    "en_cours": "À remettre",
    "remis": "Remis",
    "annule": "Annulé",
}

MOTIFS_ECHEC = {
    "absent": "Destinataire absent",
    "refus": "Refus de réception",
    "ferme": "Bureau fermé",
    "adresse": "Adresse introuvable",
    "autre": "Autre motif",
}

# Rempli par init_remise()
_CTX = {"archiver_annexe": None}


# ---------------------------------------------------------------------------
# Données
# ---------------------------------------------------------------------------
def remises():
    return C._lire(F_REMISES, {})


def _sauver(toutes):
    C._ecrire(F_REMISES, toutes)


def remise(rid):
    return remises().get(rid)


def par_jeton(jeton):
    """Bordereau correspondant au jeton, comparé à temps constant."""
    if not jeton or len(jeton) < 20:
        return None
    for r in remises().values():
        if hmac.compare_digest(str(r.get("jeton", "")), str(jeton)):
            return r
    return None


def remises_du_projet(ref):
    liste = [r for r in remises().values() if r.get("reponse_ref") == ref]
    return sorted(liste, key=lambda r: r.get("cree_le", ""), reverse=True)


def expire(r):
    try:
        return datetime.fromisoformat(r.get("expire_le", "")) < datetime.now()
    except ValueError:
        return False


def _dossier(rid):
    d = DOSSIER / rid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "")


def _url_publique(r):
    """Lien que le coursier ouvre. Derrière Caddy, la requête arrive en http :
    le schéma vient de l'en-tête X-Forwarded-Proto."""
    schema = request.headers.get("X-Forwarded-Proto", request.scheme)
    hote = request.headers.get("X-Forwarded-Host", request.host)
    return f"{schema}://{hote}{url_for('remise_public.page', jeton=r['jeton'])}"


def qr_svg(texte):
    """QR code en SVG (paquet segno). Vide si segno n'est pas installé."""
    try:
        import segno  # noqa: PLC0415
    except ImportError:
        return ""
    try:
        return segno.make(texte, error="m").svg_inline(
            scale=6, border=2, dark="#03224C", light="#FFFFFF")
    except Exception:  # noqa: BLE001
        logging.error("QR code impossible", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# Création d'un bordereau
# ---------------------------------------------------------------------------
def _bloc_destinataire(rep):
    fiche = C.correspondant(rep.get("correspondant_id")) or {}
    if fiche.get("civilite"):
        lignes = [fiche["civilite"]]
    else:
        compo = C.composer_civilite(fiche)
        lignes = [x for x in [compo["attention"], *compo["lignes"]] if x]
    org = fiche.get("organisation") or rep.get("correspondant_libre", "")
    if org and org not in lignes:
        lignes.append(org)
    adresse = [x.strip() for x in (fiche.get("adresse") or "").splitlines() if x.strip()]
    return lignes, adresse, org


def creer_depuis_projet(ref, coursier="", note=""):
    rep = C.reponses().get(ref)
    if not rep:
        return None, "Projet introuvable."
    if rep.get("statut") not in ("signe", "archive"):
        return None, ("Le bordereau se crée une fois l'original signé "
                      "(statut Signé ou Archivé).")
    lignes, adresse, org = _bloc_destinataire(rep)
    return _creer({
        "reponse_ref": ref,
        "objet": rep.get("objet", ""),
        "reference": C.reference_imprimee(rep),
        "destinataire": lignes,
        "organisation": org,
        "adresse": adresse,
        "nom_archive": rep.get("nom_archive", ""),
        "lien_archive": rep.get("lien_archive", ""),
        "service": rep.get("service", ""),
        "type_doc": C.type_document(rep),
    }, coursier, note), ""


def creer_libre(objet, reference, destinataire, adresse, coursier="", note="",
                nom_archive="", lien_archive=""):
    """Bordereau pour un document qui n'a pas été rédigé dans la GED
    (contrat, pli, document archivé directement)."""
    if not objet.strip() or not destinataire.strip():
        return None, "Indiquez au moins l'objet et le destinataire."
    lignes = [x.strip() for x in destinataire.splitlines() if x.strip()]
    return _creer({
        "reponse_ref": "",
        "objet": objet.strip(),
        "reference": reference.strip(),
        "destinataire": lignes,
        "organisation": lignes[-1] if lignes else "",
        "adresse": [x.strip() for x in adresse.splitlines() if x.strip()],
        "nom_archive": nom_archive.strip(),
        "lien_archive": lien_archive.strip(),
        "service": "", "type_doc": "",
    }, coursier, note), ""


def _creer(champs, coursier, note):
    rid = C._id_court("REM")
    r = {
        "id": rid,
        "jeton": secrets.token_urlsafe(24),
        **champs,
        "coursier": coursier.strip(),
        "note": note.strip(),
        "statut": "en_cours",
        "cree_le": C._maintenant(),
        "cree_par": C.utilisateur_courant(),
        "expire_le": (datetime.now() + timedelta(days=VALIDITE_JOURS)).isoformat(timespec="seconds"),
        "tentatives": [],
        "remise": None,
        "accuse": "",
        "lien_accuse": "",
    }
    with C._verrou:
        toutes = remises()
        toutes[rid] = r
        _sauver(toutes)
        if r["reponse_ref"]:
            _historiser_projet(r["reponse_ref"], "Bordereau de remise créé",
                               f"{rid}" + (f" — coursier : {r['coursier']}" if r["coursier"] else ""),
                               C.utilisateur_courant())
    return r


def _historiser_projet(ref, action, detail, par, champs=None):
    """Inscrit l'événement dans l'historique du projet de courrier."""
    with C._verrou:
        reps = C.reponses()
        rep = reps.get(ref)
        if not rep:
            return
        rep.setdefault("historique", []).append({
            "le": C._maintenant(), "par": par, "action": action, "detail": detail})
        rep["maj_le"] = C._maintenant()
        if champs:
            rep.update(champs)
        C._ecrire(C.F_REPONSES, reps)


# ---------------------------------------------------------------------------
# Pièces reçues du téléphone
# ---------------------------------------------------------------------------
def _signature_png(data_url):
    """« data:image/png;base64,… » -> PNG sur fond blanc (le canvas est
    transparent). None si la signature est vide ou invalide."""
    m = re.match(r"^data:image/png;base64,([A-Za-z0-9+/=\s]+)$", data_url or "")
    if not m:
        return None
    try:
        brut = base64.b64decode(m.group(1), validate=False)
    except (binascii.Error, ValueError):
        return None
    if len(brut) > 3 * 1024 * 1024 or not brut.startswith(b"\x89PNG"):
        return None
    try:
        from PIL import Image  # noqa: PLC0415
        with Image.open(io.BytesIO(brut)) as im:
            im = im.convert("RGBA")
            # Signature vide : aucun pixel tracé
            if im.getbbox() is None or im.split()[-1].getbbox() is None:
                return None
            fond = Image.new("RGB", im.size, (255, 255, 255))
            fond.paste(im, mask=im.split()[-1])
            sortie = io.BytesIO()
            fond.save(sortie, "PNG", optimize=True)
            return sortie.getvalue()
    except Exception:  # noqa: BLE001
        logging.error("Signature illisible", exc_info=True)
        return None


def _photo_jpeg(fichier):
    """Photo envoyée -> JPEG redressé et réduit. None si ce n'est pas une image."""
    if not fichier or not fichier.filename:
        return None
    try:
        from PIL import Image, ImageOps  # noqa: PLC0415
        with Image.open(fichier.stream) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((PHOTO_MAX_PX, PHOTO_MAX_PX))
            sortie = io.BytesIO()
            im.save(sortie, "JPEG", quality=82, optimize=True)
            return sortie.getvalue()
    except Exception:  # noqa: BLE001
        logging.error("Photo illisible", exc_info=True)
        return None


def _gps(f):
    try:
        lat, lon = float(f.get("lat", "")), float(f.get("lon", ""))
    except ValueError:
        return {"statut": f.get("gps_statut") or "indisponible"}
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return {"statut": "invalide"}
    try:
        precision = round(float(f.get("precision", "")))
    except ValueError:
        precision = None
    return {"statut": "ok", "lat": round(lat, 6), "lon": round(lon, 6),
            "precision": precision}


def gps_texte(g):
    if not g or g.get("statut") != "ok":
        return {"refuse": "position refusée sur le téléphone",
                "indisponible": "position indisponible"}.get((g or {}).get("statut"),
                                                              "position non relevée")
    p = f" (± {g['precision']} m)" if g.get("precision") is not None else ""
    return f"{g['lat']:.6f}, {g['lon']:.6f}{p}"


def gps_lien(g):
    if g and g.get("statut") == "ok":
        return f"https://www.openstreetmap.org/?mlat={g['lat']}&mlon={g['lon']}#map=18/{g['lat']}/{g['lon']}"
    return ""


# ---------------------------------------------------------------------------
# Accusé de remise (PDF)
# ---------------------------------------------------------------------------
def _date_heure_longue(iso):
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    return f"{C.date_longue(d.date().isoformat())} à {d.strftime('%H h %M')}"


def generer_accuse(r):
    """Accusé de remise sur papier à en-tête Huri Money. Retourne le chemin."""
    pdf, t = C._papier("sans", False)
    pdf.add_page()
    f = pdf.police
    rm = r["remise"]
    dossier = _dossier(r["id"])

    pdf.set_font(f, "B", 15)
    pdf.set_text_color(3, 34, 76)
    pdf.cell(0, 9, t("ACCUSÉ DE REMISE"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(f, "", 9)
    pdf.set_text_color(*C.GRIS)
    pdf.cell(0, 5, t(f"Bordereau {r['id']} — preuve horodatée produite par la GED Huri Money"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_text_color(*C.TEXTE)

    lignes = [
        ("Courrier", r.get("objet", "")),
        ("Référence", r.get("reference") or "—"),
        ("Document archivé", r.get("nom_archive") or "—"),
        ("Destinataire", "\n".join(r.get("destinataire") or []) or "—"),
        ("Adresse", "\n".join(r.get("adresse") or []) or "—"),
        ("Remis à", f"{rm['nom']}" + (f" — {rm['fonction']}" if rm.get("fonction") else "")),
        ("Date et heure", _date_heure_longue(rm["le"])),
        ("Position", gps_texte(rm.get("gps"))),
        ("Coursier", r.get("coursier") or rm.get("coursier") or "—"),
    ]
    larg_lib = 42
    for lib, val in lignes:
        y0 = pdf.get_y()
        pdf.set_font(f, "B", 10)
        pdf.set_fill_color(242, 245, 249)
        pdf.multi_cell(larg_lib, 6.2, t(lib), border=0, fill=True,
                       new_x="RIGHT", new_y="TOP")
        pdf.set_font(f, "", 10)
        pdf.multi_cell(0, 6.2, t(val), border=0, new_x="LMARGIN", new_y="NEXT")
        pdf.set_y(max(pdf.get_y(), y0 + 6.2) + 1.2)
        pdf.set_draw_color(*C.FILET)
        pdf.line(C.MARGE_G, pdf.get_y() - 0.6, 210 - C.MARGE_D, pdf.get_y() - 0.6)

    pdf.ln(4)
    sig = dossier / "signature.png"
    pdf.set_font(f, "B", 10)
    pdf.cell(0, 6, t("Signature du destinataire"), new_x="LMARGIN", new_y="NEXT")
    if sig.exists():
        ratio = C._ratio_image(str(sig))
        w = 70
        h = min(w * ratio, 35)
        w = h / ratio if ratio else w
        pdf.set_draw_color(*C.FILET)
        pdf.rect(C.MARGE_G, pdf.get_y() + 1, w + 4, h + 4)
        pdf.image(str(sig), x=C.MARGE_G + 2, y=pdf.get_y() + 3, w=w, h=h)
        pdf.set_y(pdf.get_y() + h + 8)

    photos = [p for p in rm.get("photos", []) if (dossier / p).exists()]
    if photos:
        pdf.set_font(f, "B", 10)
        pdf.cell(0, 6, t("Photo du cachet / de la décharge"), new_x="LMARGIN", new_y="NEXT")
        chemin = str(dossier / photos[0])
        ratio = C._ratio_image(chemin)
        reste = 297 - C.BAS_CORPS - pdf.get_y() - 4
        w = 80
        h = w * ratio
        if h > reste:
            if reste < 40:
                pdf.add_page()
                reste = 297 - C.BAS_CORPS - pdf.get_y() - 4
            h = min(h, reste)
            w = h / ratio if ratio else w
        pdf.image(chemin, x=C.MARGE_G, y=pdf.get_y() + 1, w=w, h=h)
        pdf.set_y(pdf.get_y() + h + 4)

    if r.get("tentatives"):
        pdf.ln(2)
        pdf.set_font(f, "B", 10)
        pdf.cell(0, 6, t("Tentatives précédentes"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(f, "", 9)
        for tv in r["tentatives"]:
            pdf.multi_cell(0, 5, t(f"• {_date_heure_longue(tv['le'])} — "
                                   f"{MOTIFS_ECHEC.get(tv.get('motif'), tv.get('motif', ''))}"
                                   + (f" : {tv['commentaire']}" if tv.get("commentaire") else "")),
                           new_x="LMARGIN", new_y="NEXT")

    chemin = dossier / "accuse.pdf"
    pdf.output(str(chemin))
    return chemin


def _nom_accuse(r):
    base = (r.get("nom_archive") or "").removesuffix(".pdf")
    if base:
        return f"{base}_AR.pdf"
    return f"AR_{r['id']}_{C._slug(r.get('objet', ''))}.pdf".upper().replace("-", "_")


def archiver_accuse(rid):
    """Dépose l'accusé dans WorkDrive, à côté du courrier. Tourne en tâche
    de fond : le coursier n'attend pas Zoho."""
    fn = _CTX.get("archiver_annexe")
    r = remise(rid)
    if not fn or not r or not r.get("accuse"):
        return
    source = _dossier(rid) / "accuse.pdf"
    if not source.exists():
        return
    copie = _dossier(rid) / _nom_accuse(r)
    copie.write_bytes(source.read_bytes())
    try:
        rm = r["remise"]
        lien = fn(copie, r.get("service") or "", r.get("type_doc") or "",
                  r.get("nom_archive") or "",
                  {"Remise": f"Remis le {C.date_courte(rm['le'])} à {rm['nom']}"})
    except Exception:  # noqa: BLE001
        logging.error("Archivage de l'accusé %s impossible", rid, exc_info=True)
        lien = ""
    finally:
        copie.unlink(missing_ok=True)
    if lien and str(lien).startswith("http"):
        with C._verrou:
            toutes = remises()
            if rid in toutes:
                toutes[rid]["lien_accuse"] = lien
                toutes[rid]["nom_accuse"] = _nom_accuse(r)
                _sauver(toutes)
        if r.get("reponse_ref"):
            _historiser_projet(r["reponse_ref"], "Accusé de remise archivé",
                               _nom_accuse(r), "GED", {"lien_accuse": lien})


# ---------------------------------------------------------------------------
# Enregistrement d'une remise ou d'un échec (page du coursier)
# ---------------------------------------------------------------------------
def enregistrer_remise(r, f, fichiers):
    nom = (f.get("nom") or "").strip()
    fonction = (f.get("fonction") or "").strip()
    if len(nom) < 2:
        return "Le nom de la personne qui reçoit le courrier est obligatoire."
    if not fonction:
        return "La fonction de la personne qui reçoit le courrier est obligatoire."
    png = _signature_png(f.get("signature", ""))
    if not png:
        return "La signature est obligatoire : faites signer dans le cadre."
    photo = _photo_jpeg(fichiers.get("photo"))
    if fichiers.get("photo") and fichiers["photo"].filename and photo is None:
        return "La photo n'a pas pu être lue. Reprenez-la ou envoyez sans photo."

    dossier = _dossier(r["id"])
    (dossier / "signature.png").write_bytes(png)
    photos = []
    if photo:
        (dossier / "photo_remise.jpg").write_bytes(photo)
        photos.append("photo_remise.jpg")

    rm = {
        "le": C._maintenant(),
        "nom": nom[:120],
        "fonction": fonction[:120],
        "coursier": (f.get("coursier") or "").strip()[:80],
        "photos": photos,
        "gps": _gps(f),
        "ip": _ip(),
        "appareil": (request.headers.get("User-Agent") or "")[:200],
    }
    with C._verrou:
        toutes = remises()
        courant = toutes.get(r["id"])
        if not courant or courant.get("statut") != "en_cours":
            return "Ce bordereau n'est plus ouvert."
        courant["statut"] = "remis"
        courant["remise"] = rm
        if rm["coursier"] and not courant.get("coursier"):
            courant["coursier"] = rm["coursier"]
        _sauver(toutes)
        r = courant

    try:
        generer_accuse(r)
        with C._verrou:
            toutes = remises()
            toutes[r["id"]]["accuse"] = "accuse.pdf"
            _sauver(toutes)
            r = toutes[r["id"]]
    except Exception:  # noqa: BLE001
        logging.error("Accusé de remise %s non généré", r["id"], exc_info=True)

    detail = (f"à {rm['nom']} ({rm['fonction']}), {_date_heure_longue(rm['le'])}"
              f" — signature{' + photo' if photos else ''} — {gps_texte(rm['gps'])}")
    if r.get("reponse_ref"):
        _historiser_projet(r["reponse_ref"], "Remis au destinataire", detail,
                           f"Coursier{(' ' + r['coursier']) if r.get('coursier') else ''}",
                           {"remis_le": rm["le"], "remise_id": r["id"]})
    C._cliq("📬 Courrier remis au destinataire", {
        "Statut": "Remis", "Objet": r.get("objet", "")}, "",
        tableau=[("Remis à", f"{rm['nom']} — {rm['fonction']}"),
                 ("Destinataire", r.get("organisation") or "-"),
                 ("Le", _date_heure_longue(rm["le"])),
                 ("Preuve", f"signature{' + photo' if photos else ''}, {gps_texte(rm['gps'])}")])
    threading.Thread(target=archiver_accuse, args=(r["id"],), daemon=True).start()
    return ""


def enregistrer_echec(r, f, fichiers):
    motif = f.get("motif", "")
    if motif not in MOTIFS_ECHEC:
        return "Choisissez le motif de l'échec."
    commentaire = (f.get("commentaire") or "").strip()[:300]
    if motif == "autre" and not commentaire:
        return "Précisez le motif en quelques mots."
    photo = _photo_jpeg(fichiers.get("photo"))
    with C._verrou:
        toutes = remises()
        courant = toutes.get(r["id"])
        if not courant or courant.get("statut") != "en_cours":
            return "Ce bordereau n'est plus ouvert."
        n = len(courant.get("tentatives", [])) + 1
        nom_photo = ""
        if photo:
            nom_photo = f"photo_echec_{n}.jpg"
            (_dossier(r["id"]) / nom_photo).write_bytes(photo)
        tv = {"le": C._maintenant(), "motif": motif, "commentaire": commentaire,
              "photo": nom_photo, "gps": _gps(f), "ip": _ip(),
              "coursier": (f.get("coursier") or "").strip()[:80]}
        courant.setdefault("tentatives", []).append(tv)
        _sauver(toutes)
        r = courant
    libelle = MOTIFS_ECHEC[motif] + (f" : {commentaire}" if commentaire else "")
    if r.get("reponse_ref"):
        _historiser_projet(r["reponse_ref"], "Remise non effectuée",
                           f"{libelle} — {_date_heure_longue(tv['le'])} — {gps_texte(tv['gps'])}",
                           f"Coursier{(' ' + tv['coursier']) if tv['coursier'] else ''}")
    C._cliq("⚠ Remise non effectuée", {"Statut": "Échec remise", "Objet": r.get("objet", "")}, "",
            tableau=[("Motif", libelle), ("Destinataire", r.get("organisation") or "-"),
                     ("Le", _date_heure_longue(tv["le"])),
                     ("Tentative", f"n° {len(r['tentatives'])}")])
    return ""


# ---------------------------------------------------------------------------
# Écrans GED (connexion requise)
# ---------------------------------------------------------------------------
def _affichee(r):
    rm = r.get("remise") or {}
    return {
        **r,
        "statut_lib": STATUTS.get(r.get("statut"), r.get("statut")),
        "expire": expire(r) and r.get("statut") == "en_cours",
        "cree_affiche": C._date_heure(r.get("cree_le")),
        "remis_affiche": _date_heure_longue(rm.get("le")) if rm else "",
        "gps_texte": gps_texte(rm.get("gps")) if rm else "",
        "gps_lien": gps_lien(rm.get("gps")) if rm else "",
        "nb_echecs": len(r.get("tentatives", [])),
        "tentatives_aff": [{**tv, "motif_lib": MOTIFS_ECHEC.get(tv.get("motif"), ""),
                            "le_aff": _date_heure_longue(tv.get("le")),
                            "gps_texte": gps_texte(tv.get("gps")),
                            "gps_lien": gps_lien(tv.get("gps"))}
                           for tv in r.get("tentatives", [])],
    }


def _sans_bordereau():
    """Courriers signés ou archivés depuis 60 jours qui n'ont pas encore de
    bordereau : à remettre."""
    deja = {r.get("reponse_ref") for r in remises().values()
            if r.get("statut") != "annule"}
    limite = (datetime.now() - timedelta(days=60)).isoformat()
    liste = [C._reponse_affichee(rep) for rep in C.reponses().values()
             if rep.get("statut") in ("signe", "archive")
             and rep.get("ref") not in deja
             and rep.get("maj_le", "") >= limite
             and C.mise_en_page(rep) != "decision"]
    return sorted(liste, key=lambda x: x.get("maj_le", ""), reverse=True)


@bp_remises.route("/")
def liste():
    toutes = sorted(remises().values(), key=lambda r: r.get("cree_le", ""), reverse=True)
    en_cours = [_affichee(r) for r in toutes if r.get("statut") == "en_cours"]
    remis = [_affichee(r) for r in toutes if r.get("statut") == "remis"][:30]
    return render_template("remises.html", page="remises", en_cours=en_cours,
                           remis=remis, a_remettre=_sans_bordereau(),
                           motifs=MOTIFS_ECHEC, **C._contexte_commun())


@bp_remises.route("/creer", methods=["POST"])
def creer():
    f = request.form
    if f.get("ref"):
        r, err = creer_depuis_projet(f["ref"], f.get("coursier", ""), f.get("note", ""))
    else:
        r, err = creer_libre(f.get("objet", ""), f.get("reference", ""),
                             f.get("destinataire", ""), f.get("adresse", ""),
                             f.get("coursier", ""), f.get("note", ""),
                             f.get("nom_archive", ""), f.get("lien_archive", ""))
    if not r:
        flash(err, "error")
        return redirect(request.referrer or url_for("remises.liste"))
    flash("Bordereau créé. Faites scanner le QR code au coursier, ou envoyez-lui le lien.",
          "success")
    return redirect(url_for("remises.detail", rid=r["id"]))


@bp_remises.route("/<rid>")
def detail(rid):
    r = remise(rid)
    if not r:
        abort(404)
    lien = _url_publique(r)
    return render_template("remise_detail.html", page="remises", r=_affichee(r),
                           lien=lien, qr=qr_svg(lien) if r.get("statut") == "en_cours" else "",
                           **C._contexte_commun())


@bp_remises.route("/<rid>/annuler", methods=["POST"])
def annuler(rid):
    with C._verrou:
        toutes = remises()
        r = toutes.get(rid)
        if not r or r.get("statut") != "en_cours":
            abort(404)
        r["statut"] = "annule"
        r["annule_le"] = C._maintenant()
        r["annule_par"] = C.utilisateur_courant()
        _sauver(toutes)
    if r.get("reponse_ref"):
        _historiser_projet(r["reponse_ref"], "Bordereau de remise annulé", rid,
                           C.utilisateur_courant())
    flash("Bordereau annulé : le lien du coursier ne s'ouvre plus.", "success")
    return redirect(url_for("remises.liste"))


@bp_remises.route("/<rid>/prolonger", methods=["POST"])
def prolonger(rid):
    with C._verrou:
        toutes = remises()
        r = toutes.get(rid)
        if not r or r.get("statut") != "en_cours":
            abort(404)
        r["expire_le"] = (datetime.now() + timedelta(days=VALIDITE_JOURS)).isoformat(timespec="seconds")
        _sauver(toutes)
    flash(f"Lien prolongé de {VALIDITE_JOURS} jours.", "success")
    return redirect(url_for("remises.detail", rid=rid))


@bp_remises.route("/<rid>/archiver", methods=["POST"])
def rearchiver(rid):
    r = remise(rid)
    if not r or r.get("statut") != "remis":
        abort(404)
    if not r.get("accuse"):
        generer_accuse(r)
        with C._verrou:
            toutes = remises()
            toutes[rid]["accuse"] = "accuse.pdf"
            _sauver(toutes)
    threading.Thread(target=archiver_accuse, args=(rid,), daemon=True).start()
    flash("Archivage de l'accusé relancé. Le lien WorkDrive apparaît dans quelques secondes.",
          "success")
    return redirect(url_for("remises.detail", rid=rid))


_PIECES = re.compile(r"^(signature\.png|accuse\.pdf|photo_[a-z0-9_]+\.jpg)$")


@bp_remises.route("/<rid>/piece/<nom>")
def piece(rid, nom):
    if not remise(rid) or not _PIECES.match(nom):
        abort(404)
    chemin = DOSSIER / rid / nom
    if not chemin.exists():
        abort(404)
    return send_file(chemin, download_name=(_nom_accuse(remise(rid)) if nom == "accuse.pdf" else nom))


# ---------------------------------------------------------------------------
# Page du coursier (sans compte, jeton secret)
# ---------------------------------------------------------------------------
def _ouvert(jeton):
    r = par_jeton(jeton)
    if not r:
        return None, "inconnu"
    if r.get("statut") == "annule":
        return r, "annule"
    if r.get("statut") == "remis":
        return r, "remis"
    if expire(r):
        return r, "expire"
    return r, ""


@bp_remise_public.after_request
def _pas_de_cache(resp):
    # Page contenant un lien secret : ni cache ni référent transmis.
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@bp_remise_public.route("/<jeton>")
def page(jeton):
    r, etat = _ouvert(jeton)
    if etat == "inconnu":
        return render_template("remise_mobile.html", etat="inconnu", r=None), 404
    return render_template("remise_mobile.html", etat=etat, r=_affichee(r),
                           motifs=MOTIFS_ECHEC, jeton=jeton,
                           message=request.args.get("m", ""))


@bp_remise_public.route("/<jeton>/remettre", methods=["POST"])
def remettre(jeton):
    if (request.content_length or 0) > TAILLE_MAX:
        return redirect(url_for("remise_public.page", jeton=jeton,
                                m="La photo est trop lourde. Reprenez-la."))
    r, etat = _ouvert(jeton)
    if etat:
        return redirect(url_for("remise_public.page", jeton=jeton))
    err = enregistrer_remise(r, request.form, request.files)
    if err:
        return render_template("remise_mobile.html", etat="", r=_affichee(r),
                               motifs=MOTIFS_ECHEC, jeton=jeton, erreur=err,
                               saisie=request.form), 400
    return redirect(url_for("remise_public.page", jeton=jeton))


@bp_remise_public.route("/<jeton>/echec", methods=["POST"])
def echec(jeton):
    if (request.content_length or 0) > TAILLE_MAX:
        return redirect(url_for("remise_public.page", jeton=jeton,
                                m="La photo est trop lourde. Reprenez-la."))
    r, etat = _ouvert(jeton)
    if etat:
        return redirect(url_for("remise_public.page", jeton=jeton))
    err = enregistrer_echec(r, request.form, request.files)
    if err:
        return render_template("remise_mobile.html", etat="", r=_affichee(r),
                               motifs=MOTIFS_ECHEC, jeton=jeton, erreur=err,
                               onglet="echec", saisie=request.form), 400
    return redirect(url_for("remise_public.page", jeton=jeton,
                            m="Échec enregistré. Le bordereau reste ouvert pour une nouvelle tentative."))


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------
def init_remise(app, protection=None, archiver_annexe=None):
    """À appeler APRÈS app.register_blueprint(bp_remises) et (bp_remise_public).
    Seuls les écrans /remises sont protégés : la page /r/<jeton> est ouverte
    par le coursier sans compte, le jeton tient lieu d'accès."""
    _CTX["archiver_annexe"] = archiver_annexe
    if protection is not None:
        for nom, vue in list(app.view_functions.items()):
            if nom.startswith("remises."):
                app.view_functions[nom] = protection(vue)
    DOSSIER.mkdir(parents=True, exist_ok=True)
    n = sum(1 for r in remises().values() if r.get("statut") == "en_cours")
    print(f"[REMISE] Module actif — {n} bordereau(x) à remettre, "
          f"QR {'disponible' if qr_svg('test') else 'indisponible (installer segno)'}")
