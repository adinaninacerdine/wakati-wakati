#!/usr/bin/env python3
"""
corriger_retours.py — le retour redevient une etape du circuit
===============================================================
A lancer UNE FOIS depuis la racine du projet :

    ./venv/bin/python corriger_retours.py

PROBLEME CORRIGE
----------------
Un document retourne par le DG n'avait aucune issue : il restait en
statut « retourne » sans qu'aucun ecran ne permette de le remettre en
circulation. Le bouton « Correction prise en compte » effacait la ligne,
et le courrier disparaissait sans avoir ete ni vise ni archive.

CE QUE FAIT LE SCRIPT
---------------------
  1. visa.py    : route /visa/<ref>/resoumettre — remet le document en
                  attente de visa apres correction
  2. visa.py    : observation obligatoire sur un retour (sans motif, le
                  service ne sait pas quoi corriger)
  3. index.html : les retours sortent de « Suites a donner » et prennent
                  leur propre section, avec le bouton « Resoumettre »

Sauvegarde horodatee avant toute ecriture. Idempotent.
"""

import re
import shutil
import sys
import time
from pathlib import Path

RACINE = Path(__file__).resolve().parent
VISA = RACINE / "visa.py"
INDEX = RACINE / "scripts" / "app_ged" / "templates" / "index.html"
APP = RACINE / "scripts" / "app_ged" / "app.py"

HORO = time.strftime("%Y%m%d-%H%M%S")


def sauver(chemin):
    copie = chemin.with_suffix(chemin.suffix + f".avant-retours-{HORO}")
    shutil.copy2(chemin, copie)
    print(f"  Sauvegarde : {copie.name}")


# ===========================================================================
# 1. visa.py
# ===========================================================================
def corriger_visa():
    if not VISA.exists():
        print("  visa.py introuvable."); return 0

    s = VISA.read_text(encoding="utf-8")
    avant = s
    n = 0

    # --- Observation obligatoire sur un retour --------------------------
    ancien = '''    elif decision == "retourner":
        d["statut"] = "retourne"'''
    nouveau = '''    elif decision == "retourner":
        # Sans motif, le service ne sait pas quoi corriger : on refuse.
        if not observations:
            flash("Merci d'indiquer le motif du retour.", "error")
            return redirect(url_for("visa.ecran_visa"))
        d["statut"] = "retourne"'''
    if ancien in s and "Merci d'indiquer le motif" not in s:
        s = s.replace(ancien, nouveau, 1); n += 1
        print("  Observation obligatoire sur retour")

    # --- Route de resoumission -------------------------------------------
    if "def resoumettre" not in s:
        route = '''

@bp_visa.route("/visa/<reference>/resoumettre", methods=["POST"])
def resoumettre(reference):
    """Remet un document corrige en attente de visa.

    Le retour n'est pas une fin de parcours : le document repart chez le
    DG apres correction. Sans cette route, un courrier retourne restait
    bloque sans qu'aucun ecran ne permette de le relancer.
    """
    donnees = _charger()
    d = donnees.get(reference)
    if not d:
        flash("Document introuvable.", "error")
        return redirect(url_for("index"))

    if d.get("statut") != "retourne":
        flash("Seul un document retourné peut être resoumis.", "error")
        return redirect(url_for("index"))

    d["statut"] = "attente"
    d["action_statut"] = ""
    d["resoumis_le"] = datetime.now().isoformat(timespec="seconds")
    d["nb_retours"] = int(d.get("nb_retours", 0)) + 1
    # On conserve le motif du retour precedent dans l'historique.
    d.setdefault("historique_retours", []).append({
        "le": d.get("vise_le", ""),
        "motif": d.get("observations", ""),
    })
    d["observations"] = ""
    _sauver(donnees)

    flash(f"Courrier « {d.get('objet', '')} » resoumis au visa.", "success")
    return redirect(url_for("index"))
'''
        s = s.rstrip() + route
        n += 1
        print("  Route /visa/<ref>/resoumettre ajoutee")

    # datetime doit etre importe
    if "from datetime import datetime" not in s:
        s = s.replace("import json", "import json\nfrom datetime import datetime", 1)
        print("  Import datetime ajoute")

    if s != avant:
        sauver(VISA)
        VISA.write_text(s, encoding="utf-8")
    return n


# ===========================================================================
# 2. index.html — section propre aux retours
# ===========================================================================
def corriger_index():
    if not INDEX.exists():
        print("  index.html introuvable."); return 0

    s = INDEX.read_text(encoding="utf-8")
    avant = s
    n = 0

    # --- Les retours sortent de « Suites a donner » -----------------------
    ancien_filtre = "          {% for s in suites %}"
    nouveau_filtre = "          {% for s in suites if s.statut != 'retourne' %}"
    if ancien_filtre in s and "if s.statut != 'retourne'" not in s:
        s = s.replace(ancien_filtre, nouveau_filtre, 1); n += 1
        print("  Retours retires des suites")

    # --- Section dediee, avant les suites ---------------------------------
    if 'id="section-retours"' not in s:
        section = '''
    <section class="bloc" id="section-retours">
      <h2>Retournés par la Direction &mdash; à corriger
        {% if retournes %}<span class="compte">{{ retournes|length }}</span>{% endif %}
      </h2>
      <div class="corps">
        {% if retournes %}
          {% for r in retournes %}
          <div class="dossier" style="align-items:flex-start;">
            <div style="flex:1;">
              <div class="objet">{{ r.objet }}</div>
              <div class="meta">
                {{ r.service_lib }} &middot; {{ r.type_lib }}
                {% if r.correspondant %}&middot; {{ r.correspondant }}{% endif %}
                {% if r.nb_retours %}&middot; {{ r.nb_retours }}e retour{% endif %}
              </div>
              {% if r.observations %}
              <div style="margin-top:8px;padding:10px 14px;background:var(--rouge-fond);
                          border-left:3px solid var(--rouge);border-radius:3px;
                          font-size:13.5px;white-space:pre-wrap;">{{ r.observations }}</div>
              {% endif %}
              <div class="meta" style="margin-top:6px;">
                Retourné par {{ r.vise_par }}{% if r.jours %} &mdash; il y a {{ r.jours }} jour(s){% endif %}
              </div>
            </div>
            <form method="POST"
                  action="{{ url_for('visa.resoumettre', reference=r.reference) }}">
              <button type="submit" class="mineur"
                      title="Le document corrigé repart au visa de la Direction.">
                Resoumettre au visa
              </button>
            </form>
          </div>
          {% endfor %}
        {% else %}
          <p class="vide">Aucun courrier retourné.</p>
        {% endif %}
      </div>
    </section>
'''
        # Insertion juste avant la section des suites
        marqueur = '\n    <section class="bloc" id="section-suites">'
        if marqueur in s:
            s = s.replace(marqueur, section + marqueur, 1); n += 1
            print("  Section « Retournes » ajoutee")
        else:
            print("  ATTENTION : section-suites introuvable, section non inseree")

    if s != avant:
        sauver(INDEX)
        INDEX.write_text(s, encoding="utf-8")
    return n


def main():
    print("=== CORRECTION DU TRAITEMENT DES RETOURS ===\n")

    print("1) visa.py")
    a = corriger_visa()
    print(f"   {a} modification(s)\n")

    print("2) index.html")
    b = corriger_index()
    print(f"   {b} modification(s)\n")

    print("=" * 45)
    if a + b == 0:
        print("Rien a faire — corrections deja appliquees.")
    else:
        print("TERMINE. Relance l'application :")
        print("  pkill -f 'scripts/app_ged/app.py'; sleep 1")
        print("  ./venv/bin/python scripts/app_ged/app.py")
        print("\nLe circuit devient :")
        print("  vise      -> archivage (+ suite si observation)")
        print("  retourne  -> section « a corriger » -> resoumission au visa")


if __name__ == "__main__":
    main()
