#!/usr/bin/env python3
"""
integrer_sortants.py — suivi de signature manuscrite
=====================================================
A lancer UNE FOIS depuis la racine du projet :

    ./venv/bin/python integrer_sortants.py

CE QUE CA FAIT
--------------
Les courriers SORTANTS suivent desormais le meme circuit interne que les
entrants, avec une etape de plus : le retour du scan signe.

    soumission -> approbation DG -> impression et signature manuscrite
    -> depot du scan -> archivage

L'application ne signe pas : elle trace. Elle sait qui a approuve, quand,
et conserve le lien entre le projet soumis et sa version signee.

Le jour ou la licence Zoho Sign est retablie, seule l'etape de signature
change : le circuit et les ecrans restent identiques.

PORTEE
------
Aucune valeur probante numerique. Le document qui fait foi reste le papier
signe. L'application en garde une image et l'historique du parcours.

Sauvegarde horodatee avant toute ecriture. Idempotent.
"""

import shutil
import sys
import time
from pathlib import Path

RACINE = Path(__file__).resolve().parent
VISA = RACINE / "visa.py"
APP = RACINE / "scripts" / "app_ged" / "app.py"
INDEX = RACINE / "scripts" / "app_ged" / "templates" / "index.html"
VISA_HTML = RACINE / "scripts" / "app_ged" / "templates" / "visa.html"

HORO = time.strftime("%Y%m%d-%H%M%S")


def sauver(chemin):
    copie = chemin.with_suffix(chemin.suffix + f".avant-sortants-{HORO}")
    shutil.copy2(chemin, copie)
    print(f"  Sauvegarde : {copie.name}")


# ===========================================================================
# 1. visa.py — statut « approuve » et depot du scan
# ===========================================================================
def corriger_visa():
    if not VISA.exists():
        print("  visa.py introuvable."); return 0

    s = VISA.read_text(encoding="utf-8")
    avant = s
    n = 0

    # --- Un sortant approuve n'est PAS archive : il attend son scan ------
    ancien = '''    if decision == "viser":
        d["statut"] = "vise"'''
    nouveau = '''    if decision == "viser":
        # Un courrier SORTANT approuve doit encore etre imprime, signe a la
        # main puis rescanne : il n'est pas archivable en l'etat. Un ENTRANT
        # vise, lui, part directement a l'archivage.
        if d.get("sens") == "sortant":
            d["statut"] = "approuve"
            d["approuve_le"] = datetime.now().isoformat(timespec="seconds")
            _sauver(donnees)
            flash("Courrier approuvé. À imprimer, faire signer, puis déposer "
                  "le scan signé.", "success")
            return redirect(url_for("visa.ecran_visa"))

        d["statut"] = "vise"'''
    if ancien in s and '"approuve"' not in s:
        s = s.replace(ancien, nouveau, 1); n += 1
        print("  Sortant approuve -> attente du scan")

    # --- Liste des approuves en attente de scan --------------------------
    if "def lister_approuves" not in s:
        fonction = '''

def lister_approuves(services=None, types=None):
    """Courriers sortants approuves, en attente du scan signe."""
    services, types = services or {}, types or {}
    return [_enrichir(r, d, services, types)
            for r, d in _charger().items() if d.get("statut") == "approuve"]
'''
        s = s.replace("\ndef lister_retournes(", fonction + "\ndef lister_retournes(", 1)
        n += 1
        print("  lister_approuves() ajoutee")

    # --- Route de depot du scan signe ------------------------------------
    if "def deposer_scan" not in s:
        route = '''

@bp_visa.route("/visa/<reference>/scan", methods=["POST"])
def deposer_scan(reference):
    """Recoit le scan du document signe a la main.

    Le fichier archive n'est pas celui qui a ete soumis : c'est le scan
    portant la signature manuscrite. On remplace donc le document avant
    de l'envoyer au circuit d'archivage.
    """
    donnees = _charger()
    d = donnees.get(reference)
    if not d:
        flash("Document introuvable.", "error")
        return redirect(url_for("index"))

    if d.get("statut") != "approuve":
        flash("Ce courrier n'est pas en attente de signature.", "error")
        return redirect(url_for("index"))

    fichier = request.files.get("scan")
    if not fichier or not fichier.filename:
        flash("Merci de joindre le scan du document signé.", "error")
        return redirect(url_for("index"))

    DOSSIER_A_CLASSER.mkdir(exist_ok=True)
    nom = d["fichier"].replace("AVISER_", "ASIGNER_", 1)
    cible = DOSSIER_A_CLASSER / nom
    fichier.save(str(cible))

    # Controle des quatre premiers octets : un scan mal converti ou un
    # fichier renomme passerait sinon dans la GED sans etre detecte.
    try:
        with open(cible, "rb") as f:
            if f.read(4) != b"%PDF":
                cible.unlink(missing_ok=True)
                flash("Le fichier déposé n'est pas un PDF valide.", "error")
                return redirect(url_for("index"))
    except Exception:  # noqa: BLE001
        flash("Lecture du fichier impossible.", "error")
        return redirect(url_for("index"))

    # Le projet non signe n'a plus lieu d'etre conserve.
    ancien = DOSSIER_VISA / d["fichier"] if "DOSSIER_VISA" in globals() else None
    try:
        if ancien is not None and ancien.exists():
            ancien.unlink()
    except Exception:  # noqa: BLE001
        pass

    d["statut"] = "vise"
    d["signe_le"] = datetime.now().isoformat(timespec="seconds")
    d["signature"] = "manuscrite"
    if d.get("observations"):
        d["action_statut"] = "ouverte"
    _sauver(donnees)

    flash(f"Scan signé enregistré. « {d.get('objet', '')} » est prêt à archiver.",
          "success")
    return redirect(url_for("index"))
'''
        s = s.rstrip() + route
        n += 1
        print("  Route /visa/<ref>/scan ajoutee")

    # Imports necessaires
    if "from flask import" in s and "request" not in s.split("from flask import")[1][:200]:
        s = s.replace("from flask import (Blueprint, abort, flash, redirect, render_template,",
                      "from flask import (Blueprint, abort, flash, redirect, render_template, request,", 1)
        print("  Import request ajoute")

    if s != avant:
        sauver(VISA)
        VISA.write_text(s, encoding="utf-8")
    return n


# ===========================================================================
# 2. app.py — aiguiller les sortants vers le visa
# ===========================================================================
def corriger_app():
    if not APP.exists():
        print("  app.py introuvable."); return 0

    s = APP.read_text(encoding="utf-8")
    avant = s
    n = 0

    # Import
    if "lister_approuves" not in s:
        s = s.replace("lister_a_viser, lister_suites, lister_retournes)",
                      "lister_a_viser, lister_suites, lister_retournes,\n"
                      "                      lister_approuves)", 1)
        s = s.replace("    def lister_retournes(*a, **k):",
                      "    def lister_approuves(*a, **k):\n        return []\n\n"
                      "    def lister_retournes(*a, **k):", 1)
        n += 1
        print("  Import lister_approuves")

    # Passage au gabarit
    if "approuves=" not in s:
        s = s.replace("retournes=lister_retournes(SERVICES, TYPES))",
                      "retournes=lister_retournes(SERVICES, TYPES),\n"
                      "                           approuves=lister_approuves(SERVICES, TYPES))", 1)
        n += 1
        print("  Variable approuves passee au gabarit")

    # Aiguillage : tout passe par le visa interne tant que Sign est indisponible
    if "SIGN_DISPONIBLE" not in s:
        s = s.replace("COLONNES_NOTIF_ACTIVES = False",
                      "COLONNES_NOTIF_ACTIVES = False\n\n"
                      "# Zoho Sign indisponible : les sortants suivent le circuit interne\n"
                      "# (approbation DG, signature manuscrite, depot du scan).\n"
                      "# Repasser a True une fois la licence retablie.\n"
                      "SIGN_DISPONIBLE = False", 1)
        n += 1
        print("  Constante SIGN_DISPONIBLE ajoutee")

    if s != avant:
        sauver(APP)
        APP.write_text(s, encoding="utf-8")
    return n


# ===========================================================================
# 3. index.html — section « en attente de signature manuscrite »
# ===========================================================================
def corriger_index():
    if not INDEX.exists():
        print("  index.html introuvable."); return 0

    s = INDEX.read_text(encoding="utf-8")
    avant = s
    n = 0

    if 'id="section-signature"' not in s:
        section = '''
    <section class="bloc" id="section-signature">
      <h2>Approuvés &mdash; en attente de signature manuscrite
        {% if approuves %}<span class="compte">{{ approuves|length }}</span>{% endif %}
      </h2>
      <div class="corps">
        {% if approuves %}
          <p class="vide" style="margin-bottom:14px;">
            Imprimez le courrier, faites-le signer, puis déposez le scan signé.
          </p>
          {% for a in approuves %}
          <div class="dossier" style="align-items:flex-start;">
            <div style="flex:1;">
              <div class="objet">{{ a.objet }}</div>
              <div class="meta">
                {{ a.service_lib }} &middot; {{ a.type_lib }}
                {% if a.correspondant %}&middot; {{ a.correspondant }}{% endif %}
                &middot; <strong class="marque-etat vise">Approuvé</strong>
              </div>
              {% if a.observations %}
              <div style="margin-top:8px;padding:10px 14px;background:var(--navy-fond);
                          border-left:3px solid var(--navy);border-radius:3px;
                          font-size:13.5px;white-space:pre-wrap;">{{ a.observations }}</div>
              {% endif %}
              <div class="meta" style="margin-top:6px;">
                Approuvé par {{ a.vise_par }}{% if a.jours %} &mdash; il y a {{ a.jours }} jour(s){% endif %}
              </div>
            </div>
            <form method="POST" enctype="multipart/form-data"
                  action="{{ url_for('visa.deposer_scan', reference=a.reference) }}"
                  style="min-width:230px;">
              <label style="font-size:12px;">Scan signé (PDF)</label>
              <input type="file" name="scan" accept=".pdf" required
                     style="font-size:12px;margin-bottom:6px;">
              <button type="submit" class="mineur" style="width:100%;">
                Déposer le scan signé
              </button>
            </form>
          </div>
          {% endfor %}
        {% else %}
          <p class="vide">Aucun courrier en attente de signature.</p>
        {% endif %}
      </div>
    </section>
'''
        marqueur = '\n    <section class="bloc" id="section-retours">'
        if marqueur in s:
            s = s.replace(marqueur, section + marqueur, 1); n += 1
            print("  Section « en attente de signature » ajoutee")
        else:
            print("  ATTENTION : section-retours introuvable")

    if s != avant:
        sauver(INDEX)
        INDEX.write_text(s, encoding="utf-8")
    return n


# ===========================================================================
# 4. visa.html — libelle adapte au sens
# ===========================================================================
def corriger_visa_html():
    if not VISA_HTML.exists():
        print("  visa.html introuvable."); return 0

    s = VISA_HTML.read_text(encoding="utf-8")
    avant = s

    ancien = '<button type="submit" name="decision" value="viser">Viser le courrier</button>'
    nouveau = ('<button type="submit" name="decision" value="viser">'
               "{% if d.sens == 'sortant' %}Bon pour signature{% else %}"
               "Viser le courrier{% endif %}</button>")
    if ancien in s:
        s = s.replace(ancien, nouveau, 1)
        print("  Libelle du bouton adapte au sens")

    if s != avant:
        sauver(VISA_HTML)
        VISA_HTML.write_text(s, encoding="utf-8")
        return 1
    return 0



# ===========================================================================
# 5. index.html — « Origine du document » au lieu de « Sens du courrier »
# ===========================================================================
# « Sens » désignait la direction géographique, ce qui est trompeur : une note
# de service ne sort pas de l'entreprise mais elle est ÉMISE et signée par le
# DG. Ce qui compte réellement, c'est qui signe.
ORIGINE_PAR_TYPE = {
    # Reçus : le signataire est ailleurs, le DG vise pour prise de connaissance
    "CENT": "entrant",   # Courrier entrant
    "FAC":  "entrant",   # Facture reçue
    "ETA":  "entrant",   # État / relevé
    # Émis : Huri Money produit le document, le DG le signe
    "CSOR": "sortant",   # Courrier sortant
    "NOT":  "sortant",   # Note de service
    "ATT":  "sortant",   # Attestation
    "ODM":  "sortant",   # Ordre de mission
    "BOR":  "sortant",   # Bordereau
    "DEM":  "sortant",   # Demande
    # Bilatéraux ou variables : pas de présélection
    "CNT":  "",          # Contrat
    "ACC":  "",          # Accord
    "PV":   "",          # Procès-verbal
    "VIR":  "",          # Virement
    "DOC":  "",          # Document divers
}


def corriger_libelle_origine():
    if not INDEX.exists():
        print("  index.html introuvable."); return 0

    s = INDEX.read_text(encoding="utf-8")
    avant = s
    n = 0

    # --- Libellés explicites ------------------------------------------
    remplacements = [
        ('<label for="s_sens">Sens du courrier</label>',
         '<label for="s_sens">Origine du document</label>'),
        ('<label for="champ_sens">Sens du courrier</label>',
         '<label for="champ_sens">Origine du document</label>'),
        ('<option value="sortant">Sortant</option>\n                <option value="entrant">Entrant</option>',
         '<option value="sortant">Émis &mdash; signature du DG</option>\n'
         '                <option value="entrant">Reçu &mdash; visa du DG</option>'),
        ('<option value="entrant">Entrant</option>\n                <option value="sortant">Sortant</option>',
         '<option value="entrant">Reçu &mdash; visa du DG</option>\n'
         '                <option value="sortant">Émis &mdash; signature du DG</option>'),
    ]
    for a, b in remplacements:
        if a in s:
            s = s.replace(a, b, 1); n += 1

    if n:
        print(f"  {n} libelle(s) clarifie(s)")

    # --- Présélection selon le type -----------------------------------
    if "ORIGINE_TYPE" not in s:
        table = ",\n        ".join(f'"{k}": "{v}"' for k, v in ORIGINE_PAR_TYPE.items())
        script = """
<script>
// Chaque type de document a une origine naturelle : une note de service est
// toujours emise, un courrier entrant toujours recu. On preselectionne, sans
// verrouiller — un contrat ou une facture peuvent aller dans les deux sens.
var ORIGINE_TYPE = {
        %s
};

function majOrigineDepuisType(prefixe) {
    var type = document.getElementById(prefixe === 's' ? 's_type' : 'champ_type');
    var sens = document.getElementById(prefixe === 's' ? 's_sens' : 'champ_sens');
    if (!type || !sens) { return; }
    var attendu = ORIGINE_TYPE[type.value];
    if (attendu) {
        sens.value = attendu;
        majLibelle(prefixe);
    }
}

document.addEventListener('DOMContentLoaded', function () {
    var st = document.getElementById('s_type');
    if (st) { st.addEventListener('change', function () { majOrigineDepuisType('s'); }); }
    var at = document.getElementById('champ_type');
    if (at) { at.addEventListener('change', function () { majOrigineDepuisType('a'); }); }
});
</script>
</body>""" % table

        if "</body>" in s:
            s = s.replace("</body>", script, 1); n += 1
            print("  Preselection de l'origine selon le type")

    if s != avant:
        sauver(INDEX)
        INDEX.write_text(s, encoding="utf-8")
    return n


def main():
    print("=== SUIVI DE SIGNATURE MANUSCRITE ===\\n")
    total = 0
    for titre, fn in (("visa.py", corriger_visa), ("app.py", corriger_app),
                      ("index.html", corriger_index), ("visa.html", corriger_visa_html),
                      ("index.html (libelles)", corriger_libelle_origine)):
        print(f"{titre}")
        total += fn()
        print()

    print("=" * 45)
    if total == 0:
        print("Rien a faire — deja applique.")
    else:
        print("TERMINE. Relance l'application.\\n")
        print("Circuit des SORTANTS :")
        print("  soumission -> DG « Bon pour signature » -> impression")
        print("  -> signature manuscrite -> depot du scan -> archivage")
        print("\\nCircuit des ENTRANTS : inchange (visa -> archivage).")


if __name__ == "__main__":
    main()
