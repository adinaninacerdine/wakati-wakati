import json
import sys
import os
from pathlib import Path

# Importation de votre connecteur
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zoho_connector import ZohoConnector
from zoho_config import WORKDRIVE_ROOT_ID

z = ZohoConnector()
ROOT_DIR = Path(__file__).resolve().parent
MAP_FILE = ROOT_DIR / "folders_map.json"

# Vos dictionnaires de config
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

def main():
    folder_map = {"services": {}}
    
    print(f"Début de l'arborescence depuis la racine (ID: {WORKDRIVE_ROOT_ID})\n")
    
    # Pour chaque Service
    for code_ser, nom_ser in SERVICES.items():
        print(f"Traitement du service: {nom_ser}...")
        
        # Utilisation de la méthode native de votre connecteur
        ser_id = z.workdrive_find_or_create_folder(nom_ser, WORKDRIVE_ROOT_ID)
        
        if not ser_id:
            print(f"  -> ERREUR lors de la création de {nom_ser}")
            continue
            
        folder_map["services"][code_ser] = {
            "id": ser_id,
            "types": {}
        }
        
        # Pour chaque Type dans ce service
        for code_typ, nom_typ in TYPES.items():
            print(f"  -> Sous-dossier: {nom_typ}...")
            typ_id = z.workdrive_find_or_create_folder(nom_typ, ser_id)
            if typ_id:
                folder_map["services"][code_ser]["types"][code_typ] = typ_id
                
    # Sauvegarde du mappage
    with open(MAP_FILE, 'w', encoding='utf-8') as f:
        json.dump(folder_map, f, ensure_ascii=False, indent=4)
        
    print(f"\n✅ Arborescence terminée et sauvegardée dans {MAP_FILE}")

if __name__ == '__main__':
    main()