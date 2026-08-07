import os
import csv
import json
import webbrowser
import threading
import time
import sys
import re
import shutil
import logging
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import requests
from flask import (Flask, render_template, request, redirect, url_for, flash,
                   jsonify)

# ==========================================
# CONFIGURATION DES CHEMINS ET LOGS
# ==========================================
CURRENT_DIR = Path(__file__).resolve().parent
ROOT_DIR = CURRENT_DIR.parent.parent

sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(CURRENT_DIR))
sys.path.insert(0, os.getcwd())

try:
    import zoho_config
    sys.modules['zoho_config'] = zoho_config
except ImportError:
    print("ERREUR CRITIQUE : zoho_config.py est introuvable.")
    print(f"Chemin recherché : {ROOT_DIR}")
    sys.exit(1)

TEMP_DIR = CURRENT_DIR / "_tmp"
TEMP_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=str(ROOT_DIR / "ged_rh_errors.log"),
    level=logging.ERROR,
    encoding='utf-8'
)

# ==========================================
# CONNECTEUR ZOHO
# ==========================================
from zoho_connector import ZohoConnector
z = ZohoConnector()

# ==========================================
# COUCHE NOTIFICATION
# ==========================================
try:
    from notifier import dispatch, statuts_pour_registre, send_for_signature
    NOTIFIER_DISPONIBLE = True
except Exception as _notif_err:  # noqa: BLE001
    print(f"[GED] notifier.py indisponible ({_notif_err}) — notifications désactivées.")
    NOTIFIER_DISPONIBLE = False

# ==========================================
# FLASK & ARBORESCENCE
# ==========================================
template_dir = os.path.join(CURRENT_DIR, 'templates')
app = Flask(__name__, template_folder=template_dir)
# ==========================================
# AUTHENTIFICATION
# ==========================================
AUTH_ACTIVE = os.getenv("GED_AUTH", "0") == "1"

if AUTH_ACTIVE:
    from auth import init_auth, connexion_requise
    init_auth(app)
else:
    app.secret_key = "wakati_ged_rh_secret"

    def connexion_requise(vue):
        return vue

MAP_FILE = ROOT_DIR / "folders_map.json"
FICHIER_REGISTRE = ROOT_DIR / "registre_2026.csv"

# Circuit de signature
FICHIER_ATTENTE = ROOT_DIR / "en_attente.json"
DOSSIER_CLASSER = ROOT_DIR / "a_classer"

ZOHO_SHEET_WORKBOOK_ID = "avaji4d47ec1cbffc4f5184dc8b3127b83ae7"

# /!\ Si tu as fait le decoupage feuille technique + feuille de vue,
# mets ici le nom de la feuille TECHNIQUE ("DATA"), pas celui de la vue.
ZOHO_SHEET_WORKSHEET_ID = "DATA"

# Passe a True apres avoir ajoute les 3 en-tetes dans la feuille.
COLONNES_NOTIF_ACTIVES = False

DEBUG_UPLOAD = False

# Releve automatique des courriers signes : intervalle en minutes.
# La responsable GED n'a rien a lancer — l'application s'en charge.
INTERVALLE_RELEVE_MIN = 15

DOSSIERS_ZOHO_DEFAUT = {
    "entrant": "avajid1a38194ab8f48baa462286eef5a1315",
    "sortant": "5wh2ha7a8ad89bbac457a90d68d9d4d175f97"
}

SERVICES = {
    "FIN": "Finances", "RH": "Ressources Humaines", "DGA": "Direction / Admin",
    "JUR": "Juridique", "COM": "Commercial", "EXP": "Exploitation"
}
TYPES = {
    "FAC": "Facture", "CNT": "Contrat", "NOT": "Note de service", "VIR": "Virement",
    "DEM": "Demande", "ODM": "Ordre de mission", "ETA": "État / Relevé", "ACC": "Accord",
    "ATT": "Attestation", "BOR": "Bordereau", "CENT": "Courrier Entrant",
    "CSOR": "Courrier Sortant", "PV": "Procès-verbal", "DOC": "Document divers"
}
ILES = {"NGA": "Ngazidja", "ANJ": "Anjouan", "MOH": "Mohéli"}

COLONNES_REGISTRE = [
    'Nom du fichier', 'Service', 'Type', 'Date', 'Île', 'Objet',
    'Branche GED', 'Emplacement physique', 'Statut', 'Lien Zoho',
    'Sens', 'Correspondant'
]

# Le sens determine si le correspondant est l'expediteur ou le destinataire.
SENS = {"entrant": "Entrant", "sortant": "Sortant"}
LIBELLE_CORRESPONDANT = {"entrant": "Expéditeur", "sortant": "Destinataire"}
COLONNES_NOTIF = ['Statut Cliq', 'Statut Sign', 'Sign Request ID']


# ==========================================
# OUTILS
# ==========================================
def get_zoho_folder_id(service_code, type_code):
    if not MAP_FILE.exists():
        raise FileNotFoundError("Le fichier folders_map.json est introuvable.")
    with open(MAP_FILE, 'r', encoding='utf-8') as f:
        folder_map = json.load(f)
    try:
        return folder_map["services"][service_code]["types"][type_code]
    except KeyError:
        raise ValueError(f"Dossier cible introuvable pour {service_code}/{type_code}.")


def nettoyer_texte(texte):
    if not texte:
        return "SANS_OBJET"
    texte = texte.upper()
    replacements = {'É': 'E', 'È': 'E', 'Ê': 'E', 'Ë': 'E', 'À': 'A', 'Â': 'A',
                    'Ä': 'A', 'Ô': 'O', 'Ö': 'O', 'Î': 'I', 'Ï': 'I', 'Ç': 'C',
                    'Ù': 'U', 'Ú': 'U', 'Û': 'U', 'Ü': 'U', '’': "'", "'": "'"}
    for char, repl in replacements.items():
        texte = texte.replace(char, repl)
    texte = texte.replace(" ", "_")
    texte = re.sub(r"[^A-Z0-9_\-]", "", texte)
    return texte[:60]


def est_un_pdf(chemin):
    """Un PDF commence toujours par %PDF. Attrape les scans mal convertis et
    les fichiers renommes par erreur — un fichier de logs a deja fini classe
    comme courrier entrant faute de ce controle."""
    try:
        with open(chemin, 'rb') as f:
            return f.read(4) == b'%PDF'
    except Exception:
        return False


def dernier_numero(service, type_doc):
    """Dernier numero d'ordre utilise pour ce couple service/type, cette annee.

    La responsable GED garde la main sur la numerotation : on se contente de
    lui rappeler ou elle en est, pour eviter doublons et trous.
    """
    if not FICHIER_REGISTRE.exists():
        return None
    motif = re.compile(
        rf"^HM26-{re.escape(service)}-{re.escape(type_doc)}_\d{{8}}_(\d{{3}})_"
    )
    dernier = None
    try:
        with open(FICHIER_REGISTRE, 'r', encoding='utf-8-sig') as f:
            for ligne in csv.reader(f, delimiter=';'):
                if not ligne:
                    continue
                m = motif.match(ligne[0])
                if m:
                    n = int(m.group(1))
                    if dernier is None or n > dernier:
                        dernier = n
    except Exception:
        return None
    return dernier


MOTIF_A_CLASSER = re.compile(
    r"^ASIGNER_HM26-([A-Z]+)-([A-Z]+)_(\d{8})_(.+)_([A-Za-z0-9]{6})\.pdf$"
)


def correspondants_memorises():
    """Correspondant saisi a la soumission, indexe par reference Sign.

    Le nom de fichier depose par releve.py se termine par les 6 derniers
    caracteres de la reference : c'est la cle de rapprochement.
    """
    index = {}
    for rid, meta in charger_attente().items():
        index[rid[-6:]] = {
            "correspondant": meta.get("correspondant", ""),
            "sens": meta.get("sens", "sortant"),
        }
    return index


def lister_a_classer():
    """Courriers signes en attente de classement, avec metadonnees relues
    depuis le nom de fichier depose par releve.py."""
    if not DOSSIER_CLASSER.exists():
        return []

    resultat = []
    memoire = correspondants_memorises()
    for chemin in sorted(DOSSIER_CLASSER.glob("*.pdf")):
        m = MOTIF_A_CLASSER.match(chemin.name)
        if m:
            service, type_doc, date_brute, objet, _ = m.groups()
            date_doc = f"{date_brute[:4]}-{date_brute[4:6]}-{date_brute[6:]}"
        else:
            service = type_doc = ""
            date_doc = datetime.now().strftime("%Y-%m-%d")
            objet = chemin.stem

        suffixe = m.group(5) if m else ""
        memo = memoire.get(suffixe, {})

        resultat.append({
            "fichier": chemin.name,
            "service": service,
            "type_doc": type_doc,
            "date_doc": date_doc,
            "objet": objet.replace("_", " "),
            "service_lib": SERVICES.get(service, service or "Service inconnu"),
            "type_lib": TYPES.get(type_doc, type_doc or "Type inconnu"),
            "correspondant": memo.get("correspondant", ""),
            "sens": memo.get("sens", "sortant"),
        })
    return resultat


def lister_en_attente():
    """Courriers partis chez le signataire, avec leur anciennete.

    Permet a la responsable GED de voir d'un coup d'oeil ce qui traine chez
    le DG et de relancer, sans ouvrir Zoho Sign.
    """
    resultat = []
    for rid, m in charger_attente().items():
        try:
            soumis = datetime.fromisoformat(m.get("soumis_le", ""))
        except (ValueError, TypeError):
            soumis = datetime.now()
        jours = (datetime.now() - soumis).days
        service = m.get("service", "")
        type_doc = m.get("type_doc", "")
        resultat.append({
            "reference": rid,
            "objet": m.get("objet", "(sans objet)"),
            "service_lib": SERVICES.get(service, service or "—"),
            "type_lib": TYPES.get(type_doc, type_doc or "—"),
            "soumis_affiche": soumis.strftime("%d/%m/%Y"),
            "jours": jours,
            "correspondant": m.get("correspondant", ""),
        })
    # Les plus anciens en premier : ce sont eux qui demandent une relance.
    resultat.sort(key=lambda d: d["jours"], reverse=True)
    return resultat


def charger_attente():
    if not FICHIER_ATTENTE.exists():
        return {}
    try:
        return json.loads(FICHIER_ATTENTE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def ajouter_attente(request_id, meta):
    donnees = charger_attente()
    donnees[request_id] = meta
    FICHIER_ATTENTE.write_text(
        json.dumps(donnees, indent=2, ensure_ascii=False), encoding='utf-8'
    )


def extraire_lien_fichier(resp):
    """Lien vers LE FICHIER uploade. On construit depuis resource_id ; Permalink
    ne sert qu'en dernier recours."""
    data = resp.get('data', {}) if isinstance(resp, dict) else {}
    if isinstance(data, list) and len(data) > 0:
        data = data[0]
    if not isinstance(data, dict):
        data = {}

    attrs = data.get('attributes', {}) or {}
    file_id = (attrs.get('resource_id') or attrs.get('ResourceId')
               or data.get('id') or attrs.get('id') or '')

    if file_id:
        return f"https://workdrive.zoho.com/file/{file_id}", file_id

    permalink = attrs.get('Permalink') or attrs.get('permalink') or ''
    if permalink:
        logging.error("Pas de resource_id, repli sur Permalink : %s",
                      json.dumps(resp)[:600])
        return permalink, ''

    return "Lien non disponible", ''


def upload_avec_diagnostic(chemin, dossier_id):
    """workdrive_upload en rendant visible le message d'erreur de Zoho."""
    try:
        return z.workdrive_upload(str(chemin), dossier_id)
    except requests.HTTPError as err:
        statut, corps = "?", ""
        if err.response is not None:
            statut = err.response.status_code
            corps = err.response.text[:1200]
        print("\n--- ERREUR WORKDRIVE ---")
        print(f"Statut       : {statut}")
        print(f"Dossier cible: {dossier_id}")
        print(f"Fichier      : {Path(chemin).name}")
        print("Reponse Zoho :")
        print(corps or "(corps vide)")
        print("------------------------\n")
        logging.error("Upload WorkDrive %s : %s", statut, corps)
        raise


# ==========================================
# ROUTE 1 — CLASSEMENT (responsable GED)
# ==========================================
@app.route('/', methods=['GET', 'POST'])
@connexion_requise
def index():
    if request.method == 'POST':
        service = request.form['service']
        type_doc = request.form['type_doc']
        date_doc = request.form['date_doc']
        num_ordre = request.form['num_ordre'].zfill(3)
        objet = request.form['objet']
        ile = request.form.get('ile', '')
        sens = request.form.get('sens', 'entrant')
        correspondant = (request.form.get('correspondant') or '').strip()
        besoin_signature = request.form.get('signature') in ('on', 'true', '1')

        # Deux origines possibles : un courrier deja signe qui attend dans
        # a_classer/, ou un depot manuel classique.
        fichier_local = (request.form.get('fichier_local') or '').strip()
        source_signee = None

        if fichier_local:
            # Garde-fou : on refuse tout chemin, seul un nom de fichier presente
            # dans le dossier est accepte.
            if fichier_local != Path(fichier_local).name:
                flash("Nom de fichier invalide.", "error")
                return redirect(url_for('index'))
            source_signee = DOSSIER_CLASSER / fichier_local
            if not source_signee.exists():
                flash("Ce courrier signé n'est plus disponible. "
                      "Il a peut-être déjà été classé.", "error")
                return redirect(url_for('index'))
        else:
            fichier = request.files.get('pdf')
            if not fichier or fichier.filename == '':
                flash("Veuillez sélectionner un fichier PDF.", "error")
                return redirect(url_for('index'))

        objet_nettoye = nettoyer_texte(objet)
        date_sans_tirets = date_doc.replace("-", "")
        nouveau_nom = (f"HM26-{service}-{type_doc}_{date_sans_tirets}_"
                       f"{num_ordre}_{objet_nettoye}.pdf")

        chemin_temp = TEMP_DIR / nouveau_nom
        if source_signee is not None:
            shutil.copy2(source_signee, chemin_temp)
        else:
            fichier.save(str(chemin_temp))

        if not est_un_pdf(chemin_temp):
            chemin_temp.unlink(missing_ok=True)
            flash("Ce fichier n'est pas un PDF valide. Vérifiez le document "
                  "avant de le déposer.", "error")
            return redirect(url_for('index'))

        try:
            contenu_pdf = chemin_temp.read_bytes()
        except Exception:
            contenu_pdf = None

        try:
            z._refresh_access_token()

            try:
                dossier_id = get_zoho_folder_id(service, type_doc)
            except Exception as map_err:
                logging.error(f"Mapping JSON manquant, défaut. Erreur: {map_err}")
                dossier_id = (DOSSIERS_ZOHO_DEFAUT["sortant"] if type_doc == 'CSOR'
                              else DOSSIERS_ZOHO_DEFAUT["entrant"])

            resp = upload_avec_diagnostic(chemin_temp, dossier_id)

            if DEBUG_UPLOAD:
                print("\n--- REPONSE BRUTE WORKDRIVE ---")
                print(json.dumps(resp, indent=2)[:1500])
                print("-------------------------------\n")

            permalink, file_id = extraire_lien_fichier(resp)

            lien_registre = f"https://sheet.zoho.com/sheet/open/{ZOHO_SHEET_WORKBOOK_ID}"
            statut_cliq, statut_sign, sign_id = "Non envoyé", "Non requis", ""

            if NOTIFIER_DISPONIBLE:
                doc_notif = {
                    "numero": num_ordre,
                    "nom_fichier": nouveau_nom,
                    "service": SERVICES[service],
                    "type_doc": TYPES[type_doc],
                    "deposant": correspondant or ILES.get(ile, '') or "GED",
                    "date": date_doc,
                    "workdrive_url": permalink,
                    "sheet_url": lien_registre,
                }
                try:
                    resultats = dispatch(doc_notif, connector=z,
                                         pdf_bytes=contenu_pdf,
                                         signature=besoin_signature)
                    statut_cliq, statut_sign, sign_id = statuts_pour_registre(resultats)
                except Exception:
                    logging.error("Échec notifications pour %s\n%s",
                                  nouveau_nom, traceback.format_exc())

            row_data_csv = [
                nouveau_nom, SERVICES[service], TYPES[type_doc], date_doc,
                ILES.get(ile, ''), objet, 'Courriers', 'Zoho WorkDrive',
                'Classé', permalink, SENS.get(sens, ''), correspondant
            ]
            if COLONNES_NOTIF_ACTIVES:
                row_data_csv += [statut_cliq, statut_sign, sign_id]

            with open(FICHIER_REGISTRE, mode='a', encoding='utf-8-sig', newline='') as f:
                csv.writer(f, delimiter=';').writerow(row_data_csv)

            row_data_dict = {
                "Nom du fichier": nouveau_nom,
                "Service": SERVICES[service],
                "Type": TYPES[type_doc],
                "Date": date_doc,
                "Île": ILES.get(ile, ''),
                "Objet": objet,
                "Branche GED": "Courriers",
                "Emplacement physique": "Zoho WorkDrive",
                "Statut": "Classé",
                "Lien Zoho": permalink,
                "Sens": SENS.get(sens, ''),
                "Correspondant": correspondant
            }
            if COLONNES_NOTIF_ACTIVES:
                row_data_dict["Statut Cliq"] = statut_cliq
                row_data_dict["Statut Sign"] = statut_sign
                row_data_dict["Sign Request ID"] = sign_id

            try:
                z.sheet_add_row(ZOHO_SHEET_WORKBOOK_ID, ZOHO_SHEET_WORKSHEET_ID,
                                row_data_dict)
            except Exception:
                logging.error("Échec Zoho Sheet pour %s\n%s",
                              nouveau_nom, traceback.format_exc())

            # Le courrier signe est classe : il sort de la file d'attente.
            if source_signee is not None:
                try:
                    source_signee.unlink()
                except Exception:
                    logging.error("Suppression impossible : %s", source_signee)

            flash(f"✅ Classé dans {SERVICES[service]} > {TYPES[type_doc]} : "
                  f"{nouveau_nom}", "success")

        except Exception as e:
            logging.error("Échec upload Zoho pour %s\n%s",
                          nouveau_nom, traceback.format_exc())
            flash(f"❌ Erreur lors de l'upload Zoho : {repr(e)}", "error")

        finally:
            try:
                if chemin_temp.exists():
                    chemin_temp.unlink()
            except Exception:
                pass

        return redirect(url_for('index'))

    registre = []
    if os.path.exists(FICHIER_REGISTRE):
        with open(FICHIER_REGISTRE, mode='r', encoding='utf-8-sig') as f:
            lignes = list(csv.reader(f, delimiter=';'))
            if len(lignes) > 1:
                registre = list(reversed(lignes[1:]))[:10]

    # Courriers signes en attente de classement
    a_classer = lister_a_classer()

    return render_template('index.html', registre=registre, services=SERVICES,
                           types=TYPES, iles=ILES, a_classer=a_classer,
                           en_attente=lister_en_attente())


# ==========================================
# ROUTE 2 — SOUMISSION EN SIGNATURE (rédacteurs)
# ==========================================
@app.route('/soumettre', methods=['GET', 'POST'])
@connexion_requise
def soumettre():
    if request.method == 'POST':
        if not NOTIFIER_DISPONIBLE:
            flash("Le module de signature n'est pas disponible.", "error")
            return redirect(url_for('index'))

        fichier = request.files['pdf']
        service = request.form['service']
        type_doc = request.form['type_doc']
        date_doc = request.form['date_doc']
        objet = request.form['objet']
        ile = request.form.get('ile', '')
        sens = request.form.get('sens', 'sortant')
        correspondant = (request.form.get('correspondant') or '').strip()
        deposant = request.form.get('deposant', '').strip() or "Non précisé"

        if not fichier or fichier.filename == '':
            flash("Veuillez sélectionner un fichier PDF.", "error")
            return redirect(url_for('index'))

        objet_nettoye = nettoyer_texte(objet)
        chemin_temp = TEMP_DIR / f"projet_{int(time.time())}.pdf"
        fichier.save(str(chemin_temp))

        if not est_un_pdf(chemin_temp):
            chemin_temp.unlink(missing_ok=True)
            flash("Ce fichier n'est pas un PDF valide.", "error")
            return redirect(url_for('index'))

        try:
            contenu_pdf = chemin_temp.read_bytes()

            # Nom lisible pour le DG dans Zoho Sign — SANS numero d'ordre,
            # qui n'existe pas encore a ce stade.
            nom_projet = (f"PROJET_HM26-{service}-{type_doc}_"
                          f"{date_doc.replace('-', '')}_{objet_nettoye}.pdf")

            doc = {"numero": "Projet", "nom_fichier": nom_projet}
            ok, detail = send_for_signature(z, doc, pdf_bytes=contenu_pdf)

            if not ok:
                logging.error("Échec envoi signature pour %s : %s", nom_projet, detail)
                flash(f"❌ Envoi en signature impossible : {detail}", "error")
                return redirect(url_for('index'))

            ajouter_attente(detail, {
                "service": service,
                "type_doc": type_doc,
                "date_doc": date_doc,
                "objet": objet,
                "objet_nettoye": objet_nettoye,
                "ile": ile,
                "sens": sens,
                "correspondant": correspondant,
                "deposant": deposant,
                "soumis_le": datetime.now().isoformat(timespec='seconds'),
            })

            flash(f"✅ Envoyé au DG pour signature. Vous serez notifié dès la "
                  f"signature. Référence : {detail}", "success")

        except Exception as e:
            logging.error("Échec soumission %s\n%s", objet, traceback.format_exc())
            flash(f"❌ Erreur : {repr(e)}", "error")

        finally:
            try:
                if chemin_temp.exists():
                    chemin_temp.unlink()
            except Exception:
                pass

        return redirect(url_for('index'))

    return render_template('soumettre.html', services=SERVICES, types=TYPES,
                           iles=ILES)


# ==========================================
# API — suggestion du numéro d'ordre
# ==========================================
@app.route('/api/dernier-numero')
@connexion_requise
def api_dernier_numero():
    service = request.args.get('service', '')
    type_doc = request.args.get('type_doc', '')
    if not service or not type_doc:
        return jsonify({"dernier": None, "suggestion": None})
    dernier = dernier_numero(service, type_doc)
    suggestion = f"{(dernier or 0) + 1:03d}"
    return jsonify({"dernier": dernier, "suggestion": suggestion})


def passe_de_releve():
    """Une passe de relevé. Ne leve jamais : une panne de Sign ne doit pas
    empecher l'application de fonctionner."""
    try:
        import releve
        releve.une_passe()
        return True, None
    except Exception as exc:  # noqa: BLE001
        logging.error("Relève automatique : %s", traceback.format_exc())
        return False, str(exc)


def boucle_releve():
    """Tourne en arriere-plan pendant toute la duree de vie de l'application."""
    # Premiere passe au demarrage : l'ecran est a jour des l'ouverture.
    time.sleep(3)
    while True:
        ok, err = passe_de_releve()
        if not ok:
            print(f"[GED] Relève en échec : {err}")
        time.sleep(INTERVALLE_RELEVE_MIN * 60)


@app.route('/actualiser')
@connexion_requise
def actualiser():
    """Bouton « Vérifier maintenant » — pour ne pas attendre le cycle suivant."""
    ok, err = passe_de_releve()
    if ok:
        flash("Vérification effectuée auprès de Zoho Sign.", "success")
    else:
        flash(f"Vérification impossible : {err}", "error")
    return redirect(url_for('index'))


def calculer_indicateurs():
    """Indicateurs de pilotage, calcules depuis le registre local.

    Source : registre_2026.csv, alimente a chaque archivage. Les documents
    eux-memes restent dans WorkDrive et le registre de reference dans Zoho
    Sheet : ce fichier n'est qu'un journal local servant l'affichage.
    """
    lignes = []
    if os.path.exists(FICHIER_REGISTRE):
        try:
            with open(FICHIER_REGISTRE, 'r', encoding='utf-8-sig') as f:
                toutes = list(csv.reader(f, delimiter=';'))
                lignes = toutes[1:] if len(toutes) > 1 else []
        except Exception:
            logging.error("Lecture du registre : %s", traceback.format_exc())

    aujourdhui = datetime.now()
    mois_courant = aujourdhui.strftime('%Y-%m')
    # Mois precedent, sans dependance a dateutil
    premier = aujourdhui.replace(day=1)
    mois_precedent = (premier - timedelta(days=1)).strftime('%Y-%m')

    total = 0
    ce_mois = 0
    mois_avant = 0
    par_service = {}
    par_type = {}
    par_sens = {'Entrant': 0, 'Sortant': 0}
    correspondants = {}

    for ligne in lignes:
        if not ligne or not ligne[0]:
            continue
        total += 1

        date_doc = ligne[3] if len(ligne) > 3 else ''
        if date_doc.startswith(mois_courant):
            ce_mois += 1
        elif date_doc.startswith(mois_precedent):
            mois_avant += 1

        service = ligne[1] if len(ligne) > 1 else ''
        if service:
            par_service[service] = par_service.get(service, 0) + 1

        type_doc = ligne[2] if len(ligne) > 2 else ''
        if type_doc:
            par_type[type_doc] = par_type.get(type_doc, 0) + 1

        sens = ligne[10] if len(ligne) > 10 else ''
        if sens in par_sens:
            par_sens[sens] += 1

        corr = ligne[11].strip() if len(ligne) > 11 else ''
        if corr:
            correspondants[corr] = correspondants.get(corr, 0) + 1

    def classer(dico, limite=6):
        items = sorted(dico.items(), key=lambda x: x[1], reverse=True)[:limite]
        maxi = items[0][1] if items else 1
        return [{'nom': n, 'nombre': v, 'part': round(v * 100 / maxi)}
                for n, v in items]

    # Evolution par rapport au mois precedent
    if mois_avant > 0:
        evolution = round((ce_mois - mois_avant) * 100 / mois_avant)
    else:
        evolution = None

    attente = lister_en_attente()
    en_retard = [d for d in attente if d['jours'] >= 3]

    return {
        'total': total,
        'ce_mois': ce_mois,
        'mois_avant': mois_avant,
        'evolution': evolution,
        'en_attente': len(attente),
        'en_retard': len(en_retard),
        'a_archiver': len(lister_a_classer()),
        'par_service': classer(par_service),
        'par_type': classer(par_type),
        'par_sens': par_sens,
        'correspondants': classer(correspondants, 5),
        'mois_libelle': aujourdhui.strftime('%m/%Y'),
    }


@app.route('/tableau-de-bord')
@connexion_requise
def tableau_de_bord():
    return render_template('tableau_de_bord.html',
                           kpi=calculer_indicateurs())


def ouvrir_navigateur():
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:5000/")


if __name__ == '__main__':
    if not os.path.exists(FICHIER_REGISTRE):
        entetes = list(COLONNES_REGISTRE)
        if COLONNES_NOTIF_ACTIVES:
            entetes += COLONNES_NOTIF
        with open(FICHIER_REGISTRE, mode='w', encoding='utf-8-sig', newline='') as f:
            csv.writer(f, delimiter=';').writerow(entetes)

    DOSSIER_CLASSER.mkdir(exist_ok=True)

    print(f"[GED] Feuille cible  : {ZOHO_SHEET_WORKSHEET_ID}")
    print(f"[GED] Notifications  : {'actives' if NOTIFIER_DISPONIBLE else 'INDISPONIBLES'}")
    print(f"[GED] Colonnes notif : {'oui' if COLONNES_NOTIF_ACTIVES else 'non'}")
    print(f"[GED] En attente     : {len(charger_attente())} courrier(s)")
    print("[GED] Classement     : http://127.0.0.1:5000/")
    print("[GED] Soumission     : http://127.0.0.1:5000/soumettre")

    print(f"[GED] Relève auto    : toutes les {INTERVALLE_RELEVE_MIN} min")

    # En conteneur, l'application doit ecouter sur toutes les interfaces et
    # ne pas tenter d'ouvrir un navigateur.
    en_conteneur = os.getenv("GED_CONTENEUR", "0") == "1"
    hote = "0.0.0.0" if en_conteneur else "127.0.0.1"

    print(f"[GED] Authentification: {'active' if AUTH_ACTIVE else 'DESACTIVEE (local)'}")

    if not en_conteneur:
        threading.Thread(target=ouvrir_navigateur, daemon=True).start()
    threading.Thread(target=boucle_releve, daemon=True).start()
    app.run(debug=False, host=hote, port=5000)
