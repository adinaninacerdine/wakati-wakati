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
from flask import Flask, render_template, request, redirect, url_for, flash

# ==========================================
# CONFIGURATION DES CHEMINS ET LOGS
# ==========================================
CURRENT_DIR = Path(__file__).resolve().parent
ROOT_DIR = CURRENT_DIR.parent.parent

sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, os.getcwd())

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
# CONFIGURATION FLASK & ARBORESCENCE
# ==========================================
template_dir = os.path.join(CURRENT_DIR, 'templates')
app = Flask(__name__, template_folder=template_dir)
app.secret_key = "wakati_ged_rh_secret"

MAP_FILE = ROOT_DIR / "folders_map.json"
FICHIER_REGISTRE = ROOT_DIR / "registre_2026.csv"

ZOHO_SHEET_WORKBOOK_ID = "avaji4d47ec1cbffc4f5184dc8b3127b83ae7"
ZOHO_SHEET_WORKSHEET_ID = "3" 

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

        if not fichier or fichier.filename == '':
            flash("Veuillez sélectionner un fichier PDF.", "error")
            return redirect(url_for('index'))

        objet_nettoye = nettoyer_texte(objet)
        date_sans_tirets = date_doc.replace("-", "")
        nouveau_nom = f"HM26-{service}-{type_doc}_{date_sans_tirets}_{num_ordre}_{objet_nettoye}.pdf"
        
        chemin_temp = TEMP_DIR / nouveau_nom
        fichier.save(str(chemin_temp))
        
        try:
            z._refresh_access_token()
            
            try:
                dossier_id = get_zoho_folder_id(service, type_doc)
            except Exception as map_err:
                logging.error(f"Mapping JSON manquant, utilisation défaut. Erreur: {map_err}")
                dossier_id = DOSSIERS_ZOHO_DEFAUT["sortant"] if type_doc == 'CSOR' else DOSSIERS_ZOHO_DEFAUT["entrant"]
            
            resp = z.workdrive_upload(str(chemin_temp), dossier_id)
            
            data = resp.get('data', {})
            if isinstance(data, list) and len(data) > 0:
                data = data[0]
                
            attrs = data.get('attributes', {})
            
            # --- CORRECTION DU LIEN ICI ---
            # Zoho renvoie 'Permalink' (avec une majuscule) ou 'permalink'
            permalink = attrs.get('Permalink', attrs.get('permalink', ''))
            file_id = attrs.get('resource_id', attrs.get('id', ''))
            
            if not permalink and file_id:
                permalink = f"https://workdrive.zoho.com/file/{file_id}"
            elif not permalink:
                permalink = "Lien non disponible"
            
            row_data_csv = [
                nouveau_nom, SERVICES[service], TYPES[type_doc], date_doc, 
                ILES.get(ile, ''), objet, 'Courriers', 'Zoho WorkDrive', 'Classé', permalink
            ]

            with open(FICHIER_REGISTRE, mode='a', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(row_data_csv)
            
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
            try:
                z.sheet_add_row(ZOHO_SHEET_WORKBOOK_ID, ZOHO_SHEET_WORKSHEET_ID, row_data_dict)
            except Exception as sheet_err:
                logging.error("Échec envoi Zoho Sheet pour %s\n%s", nouveau_nom, traceback.format_exc())
            
            flash(f"✅ Succès ! Classé dans {SERVICES[service]} > {TYPES[type_doc]} : {nouveau_nom}", "success")
            
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

def ouvrir_navigateur():
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:5000/")

if __name__ == '__main__':
    if not os.path.exists(FICHIER_REGISTRE):
        with open(FICHIER_REGISTRE, mode='w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(['Nom du fichier', 'Service', 'Type', 'Date', 'Île', 'Objet', 'Branche GED', 'Emplacement physique', 'Statut', 'Lien Zoho'])
    
    threading.Thread(target=ouvrir_navigateur, daemon=True).start()
    app.run(debug=False, port=5000)