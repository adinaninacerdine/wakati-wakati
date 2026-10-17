"""
zoho_sheet_top.py
=================
Insère la nouvelle ligne EN HAUT du registre Zoho Sheet (juste sous les
en-têtes) au lieu de l'append en bas fait par `worksheet.records.add`.

POURQUOI CE FICHIER
-------------------
`worksheet.records.add` est une API *tabulaire* : Zoho traite la feuille comme
un data store et écrit TOUJOURS après la dernière ligne utilisée. C'est
exactement ce qui t'a envoyé en ligne 225. Aucun paramètre ne permet de
changer ce comportement.

Pour écrire en ligne 2, il faut passer par les *Content APIs* en deux temps :
  1. insérer une ligne vide à l'index 2  -> tout le tableau descend d'un cran
  2. écrire le contenu dans cette ligne 2

ATTENTION — À FAIRE UNE FOIS AVANT UTILISATION
----------------------------------------------
Les noms exacts des méthodes Content API ne sont pas garantis identiques d'un
compte/version à l'autre. Lance le sondage contre TON vrai classeur :

    python3 zoho_sheet_top.py --probe <WORKBOOK_ID> "Registre 2026"

Le script teste les candidats et affiche ceux qui répondent "success".
Reporte ensuite les deux gagnants dans M_INSERT_ROWS / M_SET_ROW ci-dessous.
Le sondage écrit puis supprime une ligne de test : fais-le sur une COPIE du
registre si tu veux être prudent.

USAGE DANS app.py
-----------------
    from zoho_sheet_top import insert_row_top_safe

    insert_row_top_safe(
        zc,                        # instance ZohoConnector
        WORKBOOK_ID,
        "Registre 2026",
        [numero, nom_fichier, service, type_doc, deposant, date, lien, ...],
    )
"""

import json
import sys

import requests

API_BASE = "https://sheet.zoho.com/api/v2"

# --- À AJUSTER APRÈS LE SONDAGE -------------------------------------------
M_INSERT_ROWS = "worksheet.rows.insert"
M_SET_ROW = "worksheet.row.content.set"
M_DELETE_ROWS = "worksheet.rows.delete"
# ---------------------------------------------------------------------------

INSERT_ROWS_CANDIDATES = [
    "worksheet.rows.insert",
    "worksheet.row.insert",
    "worksheet.rows.add",
]

SET_ROW_CANDIDATES = [
    "worksheet.row.content.set",
    "worksheet.content.set",
    "worksheet.row.set",
    "worksheet.data.set",
]

DELETE_ROWS_CANDIDATES = [
    "worksheet.rows.delete",
    "worksheet.row.delete",
]


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _post(connector, workbook_id, method, params, verbose=False):
    """POST générique vers l'API Zoho Sheet. Retourne (http_status, payload)."""
    url = f"{API_BASE}/{workbook_id}"
    data = {"method": method}
    data.update(params)
    headers = {"Authorization": f"Zoho-oauthtoken {connector._token()}"}

    resp = requests.post(url, headers=headers, data=data, timeout=30)
    try:
        payload = resp.json()
    except ValueError:
        payload = {"status": "failure", "raw_response": resp.text}

    if verbose:
        print(f"  [{method}] HTTP {resp.status_code} -> {json.dumps(payload)[:220]}")

    return resp.status_code, payload


def _ok(payload):
    return isinstance(payload, dict) and payload.get("status") == "success"


# ---------------------------------------------------------------------------
# Insertion en haut
# ---------------------------------------------------------------------------
def insert_row_top(connector, workbook_id, worksheet_name, values, target_row=2):
    """Insère `values` en ligne `target_row` (2 par défaut = juste sous l'en-tête).

    Lève RuntimeError si l'une des deux étapes échoue.
    """
    # Étape 1 : ligne vide à l'index cible -> décale tout vers le bas
    _, out = _post(
        connector,
        workbook_id,
        M_INSERT_ROWS,
        {
            "worksheet_name": worksheet_name,
            "row_index": target_row,
            "no_of_rows": 1,
        },
    )
    if not _ok(out):
        raise RuntimeError(f"Insertion de la ligne vide échouée : {out}")

    # Étape 2 : écriture du contenu dans la ligne fraîchement créée
    columns = list(range(1, len(values) + 1))
    cleaned = ["" if v is None else str(v) for v in values]

    _, out = _post(
        connector,
        workbook_id,
        M_SET_ROW,
        {
            "worksheet_name": worksheet_name,
            "row_index": target_row,
            "column_array": json.dumps(columns),
            "data_array": json.dumps(cleaned),
        },
    )
    if not _ok(out):
        # La ligne vide est déjà en place : on la retire pour ne pas laisser un trou
        _post(
            connector,
            workbook_id,
            M_DELETE_ROWS,
            {
                "worksheet_name": worksheet_name,
                "row_index": target_row,
                "no_of_rows": 1,
            },
        )
        raise RuntimeError(f"Écriture du contenu échouée : {out}")

    return {"row": target_row, "worksheet": worksheet_name, "status": "success"}


def insert_row_top_safe(connector, workbook_id, worksheet_name, values, target_row=2):
    """Comme insert_row_top, mais retombe sur l'append classique en cas d'échec.

    Objectif : ne JAMAIS perdre une ligne de registre à cause d'un souci d'API.
    Retourne un dict avec la clé `mode` = 'top' ou 'append'.
    """
    try:
        res = insert_row_top(connector, workbook_id, worksheet_name, values, target_row)
        res["mode"] = "top"
        return res
    except Exception as exc:  # noqa: BLE001
        print(f"[GED] Insertion en haut impossible ({exc}) -> repli sur l'append.")
        res = connector.sheet_add_row(workbook_id, worksheet_name, values)
        if isinstance(res, dict):
            res["mode"] = "append"
        return res


def cleanup_trailing_rows(connector, workbook_id, worksheet_name, first_junk_row, count):
    """Supprime les lignes résiduelles en bas du registre (ton problème de la ligne 225)."""
    _, out = _post(
        connector,
        workbook_id,
        M_DELETE_ROWS,
        {
            "worksheet_name": worksheet_name,
            "row_index": first_junk_row,
            "no_of_rows": count,
        },
    )
    return out


# ---------------------------------------------------------------------------
# Sondage des noms de méthodes
# ---------------------------------------------------------------------------
def probe(connector, workbook_id, worksheet_name):
    """Teste les noms de méthodes candidats et affiche ceux qui fonctionnent."""
    print(f"\n=== SONDAGE sur {workbook_id} / {worksheet_name} ===\n")

    print("1) Insertion d'une ligne vide")
    winner_insert = None
    for method in INSERT_ROWS_CANDIDATES:
        _, out = _post(
            connector,
            workbook_id,
            method,
            {"worksheet_name": worksheet_name, "row_index": 2, "no_of_rows": 1},
            verbose=True,
        )
        if _ok(out):
            winner_insert = method
            print(f"  --> OK : {method}\n")
            break

    if not winner_insert:
        print("  --> Aucun candidat n'a fonctionné. Voir le plan B en bas du fichier.\n")
        return

    print("2) Écriture du contenu dans la ligne 2")
    winner_set = None
    for method in SET_ROW_CANDIDATES:
        _, out = _post(
            connector,
            workbook_id,
            method,
            {
                "worksheet_name": worksheet_name,
                "row_index": 2,
                "column_array": json.dumps([1, 2]),
                "data_array": json.dumps(["TEST-SONDAGE", "a supprimer"]),
            },
            verbose=True,
        )
        if _ok(out):
            winner_set = method
            print(f"  --> OK : {method}\n")
            break

    print("3) Nettoyage de la ligne de test")
    winner_delete = None
    for method in DELETE_ROWS_CANDIDATES:
        _, out = _post(
            connector,
            workbook_id,
            method,
            {"worksheet_name": worksheet_name, "row_index": 2, "no_of_rows": 1},
            verbose=True,
        )
        if _ok(out):
            winner_delete = method
            print(f"  --> OK : {method}\n")
            break

    print("=== À REPORTER EN HAUT DE CE FICHIER ===")
    print(f'M_INSERT_ROWS = "{winner_insert}"')
    print(f'M_SET_ROW     = "{winner_set}"')
    print(f'M_DELETE_ROWS = "{winner_delete}"')
    if not winner_set:
        print("\n(!) Ligne vide insérée mais contenu non écrit : vérifie la ligne 2 "
              "dans le navigateur et supprime-la à la main si besoin.")


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--probe":
        from zoho_connector import ZohoConnector  # noqa: E402

        probe(ZohoConnector(), sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        print('Sondage : python3 zoho_sheet_top.py --probe <WORKBOOK_ID> "Registre 2026"')


# ===========================================================================
# PLAN B — si aucune Content API ne répond (zéro risque, zéro nouvelle méthode)
# ===========================================================================
# 1. Renomme ta feuille actuelle en "DATA" et laisse `worksheet.records.add`
#    continuer à empiler en bas (ça marche déjà, ne touche à rien).
# 2. Crée une feuille "Registre 2026" purement d'affichage :
#       ligne 1 : les en-têtes
#       cellule A2 : =SORT(DATA!A2:K5000; 1; FAUX)
#    Le DG ouvre "Registre 2026" et voit toujours le dernier dépôt en premier.
# 3. Bonus : la feuille DATA peut être masquée, ce qui règle définitivement
#    le problème des lignes résiduelles.
