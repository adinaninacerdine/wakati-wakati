import os
import csv
import webbrowser
from flask import Flask, render_template, request, redirect, url_for, flash
import sys
from pathlib import Path

# Définition du dossier racine du projet (wakati-zoho)
ROOT_DIR = Path(__file__).resolve().parent.parent.parent

# Importation de votre connecteur depuis le dossier racine
sys.path.insert(0, str(ROOT_DIR))

from zoho_connector import ZohoConnector
z = ZohoConnector()

app = Flask(__name__)
app.secret_key = "wakati_ged_rh_secret"

# Configuration
DOSSIERS_ZOHO = {
    "entrant": "avajid1a38194ab8f48baa462286eef5a1315",
    "sortant": "5wh2ha7a8ad89bbac457a90d68d9d4d175f97"
}
# Le registre sera sauvegardé à la racine du projet, pas dans un dossier système
FICHIER_REGISTRE = ROOT_DIR / "registre_2026.csv"

# Paramètres Admin (Services, Types, Iles)
SERVICES = {
    "FIN": "Finances", "RH": "Ressources Humaines", "DGA": "Direction / Admin",
    "JUR": "Juridique", "COM": "Commercial", "EXP": "Exploitation"
}
TYPES = {
    "FAC": "Facture", "CNT": "Contrat", "NOT": "Note de service", "VIR": "Virement",
    "DEM": "Demande", "ODM": "Ordre de mission", "ETA": "État / Relevé", "ACC": "Accord",
    "ATT": "Attestation", "BOR": "Bordereau", "COR": "Courrier", "PV": "Procès-verbal", "DOC": "Document divers"
}
ILES = {"NGA": "Ngazidja", "ANJ": "Anjouan", "MOH": "Mohéli"}

def nettoyer_texte(texte):
    texte = texte.upper().replace(" ", "_").replace("é", "e").replace("è", "e").replace("à", "a")
    for char in ['/', '\\', '?', '%', '*', ':', '|', '"', '<', '>', '.', ',', ';', "'"]:
        texte = texte.replace(char, "")
    return texte

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        fichier = request.files['pdf']
        sens = request.form['sens']
        service = request.form['service']
        type_doc = request.form['type_doc']
        date_doc = request.form['date_doc'] # AAAA-MM-JJ
        num_ordre = request.form['num_ordre'].zfill(3) # Force 001
        objet = request.form['objet']
        ile = request.form.get('ile', '')

        if not fichier:
            flash("Veuillez sélectionner un fichier PDF.", "error")
            return redirect(url_for('index'))

        # 1. Génération du nom HM26
        objet_nettoye = nettoyer_texte(objet)
        date_sans_tirets = date_doc.replace("-", "")
        nouveau_nom = f"HM26-{service}-{type_doc}_{date_sans_tirets}_{num_ordre}_{objet_nettoye}.pdf"
        
        # 2. Sauvegarde temporaire et Upload Zoho
        chemin_temp = nouveau_nom
        fichier.save(chemin_temp)
        
        try:
            dossier_id = DOSSIERS_ZOHO[sens]
            resp = z.workdrive_upload(chemin_temp, dossier_id)
            file_data = resp.get('data', {})
            attrs = file_data.get('attributes', {})
            permalink = attrs.get('permalink', 'Lien non disponible')
            
            # 3. Ajout au registre CSV
            with open(FICHIER_REGISTRE, mode='a', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow([nouveau_nom, SERVICES[service], TYPES[type_doc], date_doc, ILES.get(ile, ''), objet, 'Courriers', 'Zoho WorkDrive', 'Classé', permalink])
            
            flash(f"✅ Succès ! Le fichier a été classé sous le nom : {nouveau_nom}", "success")
            
        except Exception as e:
            flash(f"❌ Erreur lors de l'upload Zoho : {str(e)[:100]}", "error")
        finally:
            if os.path.exists(chemin_temp):
                os.remove(chemin_temp)
                
        return redirect(url_for('index'))

    # Lecture du registre pour l'afficher (du plus récent au plus ancien)
    registre = []
    if os.path.exists(FICHIER_REGISTRE):
        with open(FICHIER_REGISTRE, mode='r', encoding='utf-8-sig') as f:
            reader = csv.reader(f, delimiter=';')
            registre = list(reversed(list(reader))) # Inversé pour voir le dernier en haut

    return render_template('index.html', registre=registre, services=SERVICES, types=TYPES, iles=ILES)

if __name__ == '__main__':
    # On crée le fichier s'il n'existe pas avec ses en-têtes
    if not os.path.exists(FICHIER_REGISTRE):
        with open(FICHIER_REGISTRE, mode='w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(['Nom du fichier', 'Service', 'Type', 'Date', 'Île', 'Objet', 'Branche GED', 'Emplacement physique', 'Statut', 'Lien Zoho'])
    
    # Ouverture automatique du navigateur par défaut
    webbrowser.open("http://127.0.0.1:5000")
    
    # Lancement de l'app (debug=False pour éviter que le navigateur ne s'ouvre deux fois)
    app.run(debug=False, port=5000)