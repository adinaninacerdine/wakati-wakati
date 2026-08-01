"""
releve.py — Releve des courriers signes dans Zoho Sign
======================================================
Tourne en tache planifiee (cron sous Linux, Planificateur de taches sous
Windows). Pour chaque courrier en attente de signature :

  - statut « completed » -> telecharge le PDF signe dans a_classer/
                            et notifie la responsable GED dans Cliq
  - statut « declined »  -> notifie le redacteur, retire l'entree
  - statut « expired »   -> idem
  - sinon                -> laisse en attente

Il n'ecrit RIEN dans WorkDrive ni dans le registre. Le classement et
l'attribution du numero d'ordre restent la main de la responsable GED,
depuis l'ecran habituel de l'application.

USAGE
-----
    python releve.py               # une passe
    python releve.py --verbeux     # avec le detail de chaque appel
    python releve.py --probe ID    # sonde les endpoints Sign sur une demande

PLANIFICATION (Linux, toutes les 15 min)
----------------------------------------
    crontab -e
    */15 * * * * cd /chemin/wakati-wakati && ./venv/bin/python releve.py >> releve.log 2>&1

PLANIFICATION (Windows)
-----------------------
    Planificateur de taches -> Creer une tache de base -> toutes les 15 min
    Programme : C:\\chemin\\venv\\Scripts\\python.exe
    Arguments : releve.py
    Dossier   : C:\\chemin\\wakati-wakati

AVANT LA PREMIERE UTILISATION
-----------------------------
Les noms exacts des endpoints Sign de consultation et de telechargement
varient. Lance une fois :

    python releve.py --probe <UN_REQUEST_ID_REEL>

Le script teste les candidats et affiche ceux qui repondent. Reporte les
gagnants dans EP_STATUT et EP_PDF ci-dessous.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FICHIER_ATTENTE = ROOT / "en_attente.json"
DOSSIER_CLASSER = ROOT / "a_classer"

# --- ENDPOINTS (a ajuster apres --probe) -----------------------------------
EP_STATUT = "/requests/{rid}"
EP_PDF = "/requests/{rid}/pdf"

STATUT_CANDIDATS = ["/requests/{rid}"]
PDF_CANDIDATS = [
    "/requests/{rid}/pdf",
    "/requests/{rid}/documents/pdf",
    "/requests/{rid}/download",
]
# ---------------------------------------------------------------------------

STATUTS_SIGNE = {"completed", "signed"}
STATUTS_ECHEC = {"declined", "expired", "recalled"}


def _base():
    """SIGN_API avec ou sans /api/v1 — on normalise."""
    from zoho_config import SIGN_API
    b = str(SIGN_API).rstrip("/")
    return b if b.endswith("/api/v1") else b + "/api/v1"


def _entetes():
    from zoho_connector import ZohoConnector
    return {"Authorization": f"Zoho-oauthtoken {ZohoConnector()._token()}"}


def charger_attente():
    if not FICHIER_ATTENTE.exists():
        return {}
    try:
        return json.loads(FICHIER_ATTENTE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[RELEVE] {FICHIER_ATTENTE.name} illisible : {exc}")
        return {}


def sauver_attente(donnees):
    FICHIER_ATTENTE.write_text(
        json.dumps(donnees, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def lire_statut(rid, entetes, verbeux=False):
    """Retourne le statut textuel de la demande, ou None si illisible."""
    url = _base() + EP_STATUT.format(rid=rid)
    try:
        r = requests.get(url, headers=entetes, timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"  [{rid}] appel impossible : {exc}")
        return None

    if verbeux:
        print(f"  [{rid}] HTTP {r.status_code} {r.text[:160]}")

    if r.status_code != 200:
        return None

    try:
        req = r.json().get("requests", {})
    except ValueError:
        return None

    statut = req.get("request_status") or req.get("status")
    return str(statut).lower() if statut else None


def telecharger_pdf(rid, nom_cible, entetes):
    """Telecharge le PDF signe dans a_classer/. Retourne le chemin ou None."""
    url = _base() + EP_PDF.format(rid=rid)
    try:
        r = requests.get(url, headers=entetes, timeout=90)
    except Exception as exc:  # noqa: BLE001
        print(f"  [{rid}] telechargement impossible : {exc}")
        return None

    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        apercu = r.text[:200] if r.status_code != 200 else "(contenu non PDF)"
        print(f"  [{rid}] telechargement refuse : HTTP {r.status_code} {apercu}")
        return None

    DOSSIER_CLASSER.mkdir(exist_ok=True)
    cible = DOSSIER_CLASSER / nom_cible
    n = 1
    while cible.exists():
        cible = DOSSIER_CLASSER / f"{Path(nom_cible).stem}-{n}.pdf"
        n += 1

    cible.write_bytes(r.content)
    return cible


def notifier(doc, canal_texte):
    """Envoie une notification Cliq. Silencieux si notifier.py indisponible."""
    try:
        from notifier import notify_cliq
    except Exception as exc:  # noqa: BLE001
        print(f"  notification impossible ({exc})")
        return
    doc = dict(doc)
    doc["numero"] = canal_texte
    ok, detail = notify_cliq(doc)
    print(f"  notification : {'OK' if ok else 'KO'} {detail}")


def nom_provisoire(meta, rid):
    """Nom de fichier lisible dans a_classer/, SANS numero d'ordre.

    Le numero est attribue par la responsable GED au moment du classement.
    """
    service = meta.get("service", "XXX")
    type_doc = meta.get("type_doc", "XXX")
    date = str(meta.get("date_doc", "")).replace("-", "")
    objet = meta.get("objet_nettoye") or meta.get("objet", "SANS_OBJET")
    return f"ASIGNER_HM26-{service}-{type_doc}_{date}_{objet}_{rid[-6:]}.pdf"


def une_passe(verbeux=False):
    attente = charger_attente()
    if not attente:
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] Rien en attente.")
        return

    entetes = _entetes()
    restants = {}
    signes = echecs = 0

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] {len(attente)} demande(s) en attente")

    for rid, meta in attente.items():
        statut = lire_statut(rid, entetes, verbeux)

        if statut is None:
            print(f"  [{rid}] statut illisible — conserve")
            restants[rid] = meta
            continue

        if statut in STATUTS_SIGNE:
            nom = nom_provisoire(meta, rid)
            chemin = telecharger_pdf(rid, nom, entetes)
            if chemin is None:
                print(f"  [{rid}] signe mais telechargement echoue — conserve")
                restants[rid] = meta
                continue

            print(f"  [{rid}] SIGNE -> {chemin.name}")
            notifier(
                {
                    "nom_fichier": chemin.name,
                    "service": meta.get("service", "-"),
                    "type_doc": meta.get("type_doc", "-"),
                    "deposant": meta.get("deposant", "-"),
                    "date": meta.get("date_doc", "-"),
                },
                "Courrier signe — a classer",
            )
            signes += 1
            continue

        if statut in STATUTS_ECHEC:
            print(f"  [{rid}] {statut.upper()} — retire de l'attente")
            notifier(
                {
                    "nom_fichier": meta.get("objet", "-"),
                    "service": meta.get("service", "-"),
                    "type_doc": meta.get("type_doc", "-"),
                    "deposant": meta.get("deposant", "-"),
                    "date": meta.get("date_doc", "-"),
                },
                f"Signature {statut}",
            )
            echecs += 1
            continue

        if verbeux:
            print(f"  [{rid}] {statut} — toujours en attente")
        restants[rid] = meta

    sauver_attente(restants)
    print(f"  Bilan : {signes} signe(s), {echecs} echec(s), {len(restants)} en attente")


def sonder(rid):
    """Teste les endpoints candidats sur une demande reelle."""
    entetes = _entetes()
    base = _base()
    print(f"\n=== SONDAGE sur {rid} ===")
    print(f"Base : {base}\n")

    print("1) Consultation du statut")
    for ep in STATUT_CANDIDATS:
        url = base + ep.format(rid=rid)
        r = requests.get(url, headers=entetes, timeout=30)
        print(f"  {ep:34} HTTP {r.status_code}")
        if r.status_code == 200:
            try:
                req = r.json().get("requests", {})
                print(f"     request_status = {req.get('request_status')}")
                print(f"     cles disponibles : {list(req)[:12]}")
            except ValueError:
                print(f"     {r.text[:200]}")

    print("\n2) Telechargement du PDF signe")
    for ep in PDF_CANDIDATS:
        url = base + ep.format(rid=rid)
        r = requests.get(url, headers=entetes, timeout=60)
        pdf = r.content.startswith(b"%PDF") if r.content else False
        marque = "PDF OK" if pdf else r.text[:120]
        print(f"  {ep:34} HTTP {r.status_code} {marque}")

    print("\nReporte les gagnants dans EP_STATUT et EP_PDF en haut du fichier.")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        i = sys.argv.index("--probe")
        if len(sys.argv) <= i + 1:
            print("Usage : python releve.py --probe <REQUEST_ID>")
            sys.exit(1)
        sonder(sys.argv[i + 1])
    else:
        une_passe(verbeux="--verbeux" in sys.argv)
