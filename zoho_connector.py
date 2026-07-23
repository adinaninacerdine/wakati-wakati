"""
Configuration Zoho — SECRETS. NE JAMAIS COMMITER SUR GIT.
Région datacenter : .com (US)
"""
import requests
import time
import json
import os
import sys
from pathlib import Path

# --- RECHERCHE INTELLIGENTE DU DOSSIER RACINE ---
CURRENT_DIR = Path(__file__).resolve().parent

ROOT_DIR = None
for parent in [CURRENT_DIR] + list(CURRENT_DIR.parents):
    if (parent / 'zoho_config.py').exists():
        ROOT_DIR = parent
        break

if not ROOT_DIR:
    raise RuntimeError("Impossible de trouver le fichier zoho_config.py.")

sys.path.insert(0, str(ROOT_DIR))

try:
    from zoho_config import (CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN,
                             ACCOUNTS_URL, WORKDRIVE_API, SIGN_API,
                             WORKDRIVE_TEAM_ID)
except ImportError as e:
    print("ERREUR : zoho_config.py introuvable ou incomplet.")
    print(f"  Détail : {e}")
    raise SystemExit(1)

TOKEN_CACHE = os.path.join(str(ROOT_DIR), '.zoho_token_cache.json')


class ZohoConnector:
    def __init__(self):
        self.access_token = None
        self.token_expiry = 0
        if os.path.exists(TOKEN_CACHE):
            try:
                with open(TOKEN_CACHE) as f:
                    c = json.load(f)
                    self.access_token = c.get('access_token')
                    self.token_expiry = c.get('expiry', 0)
            except Exception:
                pass

    def _refresh_access_token(self):
        resp = requests.post(f'{ACCOUNTS_URL}/oauth/v2/token', params={
            'refresh_token': REFRESH_TOKEN,
            'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET,
            'grant_type': 'refresh_token',
        })
        resp.raise_for_status()
        data = resp.json()
        if 'access_token' not in data:
            raise RuntimeError(f"Echec refresh token : {data}")
        self.access_token = data['access_token']
        self.token_expiry = time.time() + data.get('expires_in', 3600)
        with open(TOKEN_CACHE, 'w') as f:
            json.dump({'access_token': self.access_token,
                       'expiry': self.token_expiry}, f)
        return self.access_token

    def _token(self):
        if not self.access_token or self.token_expiry < time.time() + 60:
            self._refresh_access_token()
        return self.access_token

    def _headers(self, extra=None):
        h = {'Authorization': f'Zoho-oauthtoken {self._token()}'}
        if extra:
            h.update(extra)
        return h

    def workdrive_list_teams(self):
        resp = requests.get(f'{WORKDRIVE_API}/teams', headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def workdrive_list_folders(self, parent_id):
        resp = requests.get(f'{WORKDRIVE_API}/files/{parent_id}/files',
                            headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def workdrive_create_folder(self, name, parent_id):
        payload = {'data': {'attributes': {'name': name, 'parent_id': parent_id},
                            'type': 'files'}}
        resp = requests.post(f'{WORKDRIVE_API}/files',
                             headers=self._headers({'Content-Type': 'application/json'}),
                             json=payload)
        resp.raise_for_status()
        return resp.json()['data']['id']

    def workdrive_find_or_create_folder(self, name, parent_id):
        try:
            listing = self.workdrive_list_folders(parent_id)
            for item in listing.get('data', []):
                attrs = item.get('attributes', {})
                if attrs.get('name') == name and attrs.get('is_folder', attrs.get('type') == 'folder'):
                    return item['id']
        except Exception:
            pass
        return self.workdrive_create_folder(name, parent_id)

    def workdrive_upload(self, filepath, folder_id):
        """Upload un fichier. Retourne {id, name, permalink}."""
        with open(filepath, 'rb') as f:
            files = {'content': (os.path.basename(filepath), f)}
            data = {'parent_id': folder_id}
            resp = requests.post(f'{WORKDRIVE_API}/upload',
                                 headers=self._headers(),
                                 files=files, data=data)
        
        resp.raise_for_status()
        return resp.json()

    # --------------------------------------------------------
    # ZOHO SHEET
    # --------------------------------------------------------
    def sheet_add_row(self, workbook_id, worksheet_name, row_data):
        url = f"https://sheet.zoho.com/api/v2/{workbook_id}?method=worksheet.records.add"
        
        headers = self._headers({'Content-Type': 'application/x-www-form-urlencoded'})
        
        payload = {
            'worksheet_name': worksheet_name,
            'json_data': json.dumps([row_data])
        }
        
        resp = requests.post(url, headers=headers, data=payload)
        
        if resp.status_code != 200:
            print("\n--- ERREUR ZOHO SHEET ---")
            print(f"Statut: {resp.status_code}")
            try:
                print(json.dumps(resp.json(), indent=2))
            except:
                print(resp.text)
            print("-------------------------\n")
            resp.raise_for_status()
        
        # --- ON AJOUTE ÇA POUR VOIR LE SUCCÈS ---
        print("\n--- SUCCÈS ZOHO SHEET ---")
        print(json.dumps(resp.json(), indent=2))
        print("-------------------------\n")
            
        return resp.json()


def get_refresh_token(grant_code):
    resp = requests.post(f'{ACCOUNTS_URL}/oauth/v2/token', params={
        'code': grant_code,
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'grant_type': 'authorization_code',
    })
    data = resp.json()
    print(json.dumps(data, indent=2))
    if 'refresh_token' in data:
        print(f"\n>>> REFRESH_TOKEN à copier dans zoho_config.py :")
        print(f"    {data['refresh_token']}")
    else:
        print("\n!!! Pas de refresh_token. Le grant code a peut-être expiré (régénère-le).")
    return data


if __name__ == '__main__':
    print("Test connexion Zoho WorkDrive (.com)...")
    z = ZohoConnector()
    try:
        teams = z.workdrive_list_teams()
        print(f"OK — Teams WorkDrive :\n{json.dumps(teams, indent=2)[:800]}")
    except Exception as e:
        print(f"ERREUR : {e}")