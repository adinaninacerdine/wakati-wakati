import requests
import csv
from pathlib import Path
from zoho_connector import ZohoConnector

# Configuration
ROOT_DIR = Path(__file__).resolve().parent
FICHIER_CSV = ROOT_DIR / "registre_2026.csv"

# Vos identifiants Zoho Sheet (depuis votre lien)
WORKBOOK_ID = "avaji4d47ec1cbffc4f5184dc8b3127b83ae7"
WORKSHEET_ID = "3" 

def envoyer_vers_sheet():
    if not FICHIER_CSV.exists():
        print("❌ Le fichier registre_2026.csv n'existe pas encore.")
        return

    z = ZohoConnector()
    
    # Lecture du fichier CSV généré par app.py
    with open(FICHIER_CSV, mode='r', encoding='utf-8-sig') as f:
        reader = csv.reader(f, delimiter=';')
        lignes = list(reader)
        
    if len(lignes) <= 1:
        print("Aucune donnée à envoyer.")
        return
        
    # On prend toutes les lignes sauf l'en-tête
    donnees = lignes[1:]

    # Préparation de l'URL et de la requête pour Zoho Sheet
    url = f"https://www.zohoapis.com/sheet/api/v2/workbooks/{WORKBOOK_ID}/worksheets/{WORKSHEET_ID}/content"
    payload = {
        "is_append": True, # Ajoute à la suite du fichier
        "content": donnees
    }
    
    try:
        print("Envoi des données vers Zoho Sheet en cours...")
        resp = requests.post(url, headers=z._headers({'Content-Type': 'application/json'}), json=payload)
        resp.raise_for_status()
        print("\n✅ Succès ! Toutes les lignes ont été envoyées vers Zoho Sheet.")
    except Exception as e:
        print(f"\n❌ Erreur lors de l'envoi vers Zoho Sheet : {e}")
        print("👉 Cause probable : Votre token n'a pas le scope 'ZohoSheet.dataAPI.ALL'.")

if __name__ == '__main__':
    envoyer_vers_sheet()