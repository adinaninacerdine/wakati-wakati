import os
import csv
import json
import webbrowser
import threading
import time
import sys
import re
import logging
import traceback
from pathlib import Path

import requests
from flask import Flask, render_template, request, redirect, url_for, flash

# ==========================================
# CONFIGURATION DES CHEMINS ET LOGS
# ==========================================
CURRENT_DIR = Path(__file__).resolve().parent
ROOT_DIR = CURRENT_DIR.parent.parent

# On ajoute TOUT les chemins possibles pour être sûr que Python trouve les fichiers
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(CURRENT_DIR))
sys.path.insert(0, os.getcwd())

# On force la lecture du fichier de config AVANT d'importer le connecteur
try:
    import zoho_config
    # On l'injecte dans le système pour que le connecteur le trouve
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
# IMPORTATION DU CONNECTEUR ZOHO
# ==========================================
from zoho_connector import ZohoConnector
z = ZohoConnector()

# ==========================================
# IMPORTATION DE LA COUCHE NOTIFICATION
# ==========================================
# notifier.py vit à la racine du projet, déjà ajoutée à sys.path ci-dessus.
# S'il est absent ou mal configuré, l'app continue de fonctionner sans notifier.
try:
    from notifier import dispatch, statuts_pour_registre
    NOTIFIER_DISPONIBLE = True
except Exception as _notif_err:  # noqa: BLE001
    print(f"[GED] notifier.py indisponible ({_notif_err}) — notifications désactivées.")
    NOTIFIER_DISPONIBLE = False

# ==========================================
# CONFIGURATION FLASK & ARBORESCENCE
# ==========================================
template_dir = os.path.join(CURRENT_DIR, 'templates')
app = Flask(__name__, template_folder=template_dir)
app.secret_key = "wakati_ged_rh_secret"

MAP_FILE = ROOT_DIR / "folders_map.json"
FICHIER_REGISTRE = ROOT_DIR / "registre_2026.csv"

ZOHO_SHEET_WORKBOOK_ID = "avaji4d47ec1cbffc4f5184dc8b3127b83ae7"

# /!\ IMPORTANT — À VÉRIFIER AVANT DE LANCER
# Si tu as appliqué le découpage feuille technique + feuille de vue (formule
# INDEX pour afficher la dernière ligne en haut), mets ici le nom de la feuille
# TECHNIQUE, pas celui de la vue. Sinon l'app écrase les formules.
#   - découpage NON fait  -> "Registre 2026"
#   - découpage fait      -> "DATA"
ZOHO_SHEET_WORKSHEET_ID = "Registre 2026"

# Passe à True UNIQUEMENT après avoir ajouté ces trois en-têtes en ligne 1 de la
# feuille technique : "Statut Cliq", "Statut Sign", "Sign Request ID".
COLONNES_NOTIF_ACTIVES = False

# Affiche la réponse brute de WorkDrive dans le terminal à chaque upload.
DEBUG_UPLOAD = True

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
    "ATT": "Attestation", "BOR": "Bordereau", "CENT": "Courrier Entrant", "CSOR": "Courrier Sortant",
    "PV": "Procès-verbal", "DOC": "Document divers"
}
ILES = {"NGA": "Ngazidja", "ANJ": "Anjouan", "MOH": "Mohéli"}

COLONNES_REGISTRE = [
    'Nom du fichier', 'Service', 'Type', 'Date', 'Île', 'Objet',
    'Branche GED', 'Emplacement physique', 'Statut', 'Lien Zoho'
]
COLONNES_NOTIF = ['Statut Cliq', 'Statut Sign', 'Sign Request ID']


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


def upload_avec_diagnostic(chemin, dossier_id):
    """Appelle workdrive_upload en rendant visible le message d'erreur de Zoho.

    raise_for_status() dans le connecteur leve une HTTPError sans afficher le
    corps de la reponse : on se retrouve avec « 500 Server Error » sans savoir
    pourquoi. Ici on recupere et on affiche ce corps avant de relancer.
    """
    try:
        return z.workdrive_upload(str(chemin), dossier_id)
    except requests.HTTPError as err:
        corps = ""
        statut = "?"
        if err.response is not None:
            statut = err.response.status_code
            corps = err.response.text[:1200]

        print("\n--- ERREUR WORKDRIVE ---")
        print(f"Statut       : {statut}")
        print(f"Dossier cible: {dossier_id}")
        print(f"Fichier      : {Path(chemin).name}")
        print(f"Taille       : {Path(chemin).stat().st_size} octets")
        print("Reponse Zoho :")
        print(corps or "(corps vide)")
        print("------------------------\n")

        logging.error("Upload WorkDrive %s : %s", statut, corps)
        raise


@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        fichier = request.files['pdf']
        service = request.form['service']
        type_doc = request.form['type_doc']
        date_doc = request.form['date_doc']
        num_ordre = request.form['num_ordre'].zfill(3)
        objet = request.form['objet']
        ile = request.form.get('ile', '')
        # Case a cocher optionnelle dans index.html (name="signature").
        besoin_signature = request.form.get('signature') in ('on', 'true', '1')

        if not fichier or fichier.filename == '':
            flash("Veuillez sélectionner un fichier PDF.", "error")
            return redirect(url_for('index'))

        objet_nettoye = nettoyer_texte(objet)
        date_sans_tirets = date_doc.replace("-", "")
        nouveau_nom = f"HM26-{service}-{type_doc}_{date_sans_tirets}_{num_ordre}_{objet_nettoye}.pdf"

        chemin_temp = TEMP_DIR / nouveau_nom
        fichier.save(str(chemin_temp))

        # On lit le contenu MAINTENANT : le fichier temporaire est supprime dans
        # le finally, et Zoho Sign en a besoin apres l'upload.
        try:
            contenu_pdf = chemin_temp.read_bytes()
        except Exception:
            contenu_pdf = None

        try:
            # RETABLI : le rafraichissement force du token avant chaque depot.
            # Il avait ete retire par optimisation, et l'upload s'est mis a
            # renvoyer des 500. Le cout d'un appel OAuth est negligeable face a
            # un depot perdu — on garde la version qui fonctionne.
            z._refresh_access_token()

            try:
                dossier_id = get_zoho_folder_id(service, type_doc)
            except Exception as map_err:
                logging.error(f"Mapping JSON manquant, utilisation défaut. Erreur: {map_err}")
                dossier_id = DOSSIERS_ZOHO_DEFAUT["sortant"] if type_doc == 'CSOR' else DOSSIERS_ZOHO_DEFAUT["entrant"]

            resp = upload_avec_diagnostic(chemin_temp, dossier_id)

            if DEBUG_UPLOAD:
                print("\n--- REPONSE BRUTE WORKDRIVE ---")
                print(json.dumps(resp, indent=2)[:1500])
                print("-------------------------------\n")

            permalink, file_id = extraire_lien_fichier(resp)

            if DEBUG_UPLOAD:
                print(f"[GED] resource_id : {file_id or '(absent)'}")
                print(f"[GED] lien retenu : {permalink}\n")

            # ==========================================
            # NOTIFICATIONS (Cliq / Email / Sign)
            # ==========================================
            lien_registre = f"https://sheet.zoho.com/sheet/open/{ZOHO_SHEET_WORKBOOK_ID}"
            statut_cliq, statut_sign, sign_id = "Non envoyé", "Non requis", ""

            if NOTIFIER_DISPONIBLE:
                doc_notif = {
                    "numero": f"{num_ordre} — {nouveau_nom.split('_')[0]}",
                    "nom_fichier": nouveau_nom,
                    "service": SERVICES[service],
                    "type_doc": TYPES[type_doc],
                    "deposant": ILES.get(ile, '') or "GED",
                    "date": date_doc,
                    "workdrive_url": permalink,
                    "sheet_url": lien_registre,
                }
                try:
                    resultats = dispatch(
                        doc_notif,
                        connector=z,
                        pdf_bytes=contenu_pdf,
                        signature=besoin_signature,
                    )
                    statut_cliq, statut_sign, sign_id = statuts_pour_registre(resultats)
                except Exception:
                    logging.error(
                        "Échec notifications pour %s\n%s", nouveau_nom, traceback.format_exc()
                    )

            # ==========================================
            # REGISTRE CSV
            # ==========================================
            row_data_csv = [
                nouveau_nom, SERVICES[service], TYPES[type_doc], date_doc,
                ILES.get(ile, ''), objet, 'Courriers', 'Zoho WorkDrive', 'Classé', permalink
            ]
            if COLONNES_NOTIF_ACTIVES:
                row_data_csv += [statut_cliq, statut_sign, sign_id]

            with open(FICHIER_REGISTRE, mode='a', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(row_data_csv)

            # ==========================================
            # REGISTRE ZOHO SHEET
            # ==========================================
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
                "Lien Zoho": permalink
            }
            if COLONNES_NOTIF_ACTIVES:
                row_data_dict["Statut Cliq"] = statut_cliq
                row_data_dict["Statut Sign"] = statut_sign
                row_data_dict["Sign Request ID"] = sign_id

            try:
                z.sheet_add_row(ZOHO_SHEET_WORKBOOK_ID, ZOHO_SHEET_WORKSHEET_ID, row_data_dict)
            except Exception:
                logging.error("Échec envoi Zoho Sheet pour %s\n%s", nouveau_nom, traceback.format_exc())

            message = f"✅ Succès ! Classé dans {SERVICES[service]} > {TYPES[type_doc]} : {nouveau_nom}"
            if besoin_signature:
                message += f" — Signature : {statut_sign}"
            flash(message, "success")

        except Exception as e:
            logging.error("Échec upload Zoho pour %s\n%s", nouveau_nom, traceback.format_exc())
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
            reader = csv.reader(f, delimiter=';')
            lignes = list(reader)
            if len(lignes) > 1:
                registre = list(reversed(lignes[1:]))[:10]

    return render_template('index.html', registre=registre, services=SERVICES, types=TYPES, iles=ILES)


def extraire_lien_fichier(resp):
    """Construit le lien vers LE FICHIER uploade, pas vers son dossier parent.

    L'endpoint /upload de WorkDrive renvoie dans attributes.Permalink le lien du
    DOSSIER PARENT, pas celui du fichier cree. C'etait la cause du bug : l'ancien
    code preferait Permalink, donc le lien pointait toujours sur le dossier.

    Ordre correct :
      1. resource_id  -> https://workdrive.zoho.com/file/{resource_id}
      2. Permalink    -> uniquement si aucun identifiant de fichier n'est trouve
    """
    data = resp.get('data', {}) if isinstance(resp, dict) else {}
    if isinstance(data, list) and len(data) > 0:
        data = data[0]
    if not isinstance(data, dict):
        data = {}

    attrs = data.get('attributes', {}) or {}

    file_id = (
        attrs.get('resource_id')
        or attrs.get('ResourceId')
        or data.get('id')
        or attrs.get('id')
        or ''
    )

    if file_id:
        return f"https://workdrive.zoho.com/file/{file_id}", file_id

    permalink = attrs.get('Permalink') or attrs.get('permalink') or ''
    if permalink:
        logging.error(
            "Aucun resource_id dans la reponse d'upload, repli sur Permalink : %s",
            json.dumps(resp)[:600]
        )
        return permalink, ''

    return "Lien non disponible", ''


def ouvrir_navigateur():
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:5000/")


if __name__ == '__main__':
    if not os.path.exists(FICHIER_REGISTRE):
        entetes = list(COLONNES_REGISTRE)
        if COLONNES_NOTIF_ACTIVES:
            entetes += COLONNES_NOTIF
        with open(FICHIER_REGISTRE, mode='w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(entetes)

    print(f"[GED] Feuille cible   : {ZOHO_SHEET_WORKSHEET_ID}")
    print(f"[GED] Notifications   : {'actives' if NOTIFIER_DISPONIBLE else 'INDISPONIBLES'}")
    print(f"[GED] Colonnes notif  : {'oui' if COLONNES_NOTIF_ACTIVES else 'non'}")

    threading.Thread(target=ouvrir_navigateur, daemon=True).start()
    app.run(debug=False, port=5000)
