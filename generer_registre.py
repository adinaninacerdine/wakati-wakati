import pandas as pd
from pathlib import Path

# Configuration des chemins
ROOT_DIR = Path(__file__).resolve().parent
FICHIER_CSV = ROOT_DIR / "registre_2026.csv"
FICHIER_EXCEL = ROOT_DIR / "Registre_2026.xlsx"

def exporter_vers_excel():
    if not FICHIER_CSV.exists():
        print("❌ Le fichier registre_2026.csv n'existe pas encore.")
        return

    print("📊 Lecture du registre local en cours...")
    try:
        # Lecture du fichier CSV généré par app.py
        df = pd.read_csv(FICHIER_CSV, sep=';', encoding='utf-8-sig')
        
        # Ajout de la colonne N° au début
        df.insert(0, 'N°', range(1, len(df) + 1))
        
        # Réorganisation exacte des colonnes selon votre demande
        colonnes_ordre = [
            'N°', 'Nom du fichier', 'Service', 'Type', 'Date', 
            'Île', 'Objet', 'Branche GED', 'Emplacement physique', 
            'Statut', 'Lien Zoho'
        ]
        
        # On s'assure que toutes les colonnes existent pour éviter les erreurs
        for col in colonnes_ordre:
            if col not in df.columns:
                df[col] = ''
                
        df = df[colonnes_ordre]
        
        # Exportation vers un fichier Excelpython generer_registre.py
        df.to_excel(FICHIER_EXCEL, index=False, engine='openpyxl')
        
        print(f"\n✅ Succès ! Le fichier Excel a été généré :")
        print(f"   📂 {FICHIER_EXCEL}")
        
    except Exception as e:
        print(f"❌ Erreur lors de la génération du fichier Excel : {e}")

if __name__ == '__main__':
    exporter_vers_excel()