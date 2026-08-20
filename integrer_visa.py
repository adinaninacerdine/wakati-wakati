#!/usr/bin/env python3
"""
integrer_visa.py — branche le module de visa dans l'application
================================================================
A lancer UNE FOIS depuis la racine du projet :

    ./venv/bin/python integrer_visa.py

Le script est idempotent : le relancer ne fait rien de plus. Il modifie
app.py et templates/index.html en place, apres sauvegarde horodatee.

CE QU'IL FAIT
-------------
  1. app.py       : importe le module, enregistre le blueprint
  2. app.py       : aiguille /soumettre — entrant vers le visa interne,
                    sortant vers Zoho Sign
  3. app.py       : passe les listes de visa aux gabarits
  4. index.html   : section « Suites a donner » + entree de menu

Il ne touche ni a AUTH_ACTIVE ni a ZOHO_SHEET_WORKSHEET_ID.
"""

import shutil
import sys
import time
from pathlib import Path

RACINE = Path(__file__).resolve().parent
APP = RACINE / "scripts" / "app_ged" / "app.py"
INDEX = RACINE / "scripts" / "app_ged" / "templates" / "index.html"

horodatage = time.strftime("%Y%m%d-%H%M%S")


def sauver(chemin):
    copie = chemin.with_suffix(chemin.suffix + f".avant-visa-{horodatage}")
    shutil.copy2(chemin, copie)
    print(f"  Sauvegarde : {copie.name}")


def patch(chemin, operations, libelle):
    if not chemin.exists():
        print(f"  ERREUR : {chemin} introuvable.")
        return False

    contenu = chemin.read_text(encoding="utf-8")
    origine = contenu
    applique = 0

    for marqueur, ancien, nouveau in operations:
        if marqueur in contenu:
            print(f"  Deja present : {marqueur}")
            continue
        if ancien not in contenu:
            print(f"  NON TROUVE : {ancien[:60].strip()}...")
            continue
        contenu = contenu.replace(ancien, nouveau, 1)
        applique += 1

    if contenu != origine:
        sauver(chemin)
        chemin.write_text(contenu, encoding="utf-8")
        print(f"  {libelle} : {applique} modification(s)")
    else:
        print(f"  {libelle} : rien a faire")
    return True


# ===========================================================================
# app.py
# ===========================================================================
OPS_APP = [
    # --- 1. Import et enregistrement du blueprint -------------------------
    (
        "from visa import",
        """# ==========================================
# FLASK & ARBORESCENCE
# ==========================================""",
        """# ==========================================
# CIRCUIT DE VISA (courriers entrants)
# ==========================================
# Le visa remplace Zoho Sign pour les courriers entrants : le DG atteste
# avoir vu le document sans consommer d'enveloppe. Les sortants, qui
# engagent l'entreprise, continuent de passer par la signature.
try:
    from visa import (bp_visa, init_visa, deposer_pour_visa,
                      lister_a_viser, lister_suites, lister_retournes)
    VISA_DISPONIBLE = True
except Exception as _visa_err:  # noqa: BLE001
    print(f"[GED] visa.py indisponible ({_visa_err}) — circuit de visa desactive.")
    VISA_DISPONIBLE = False

    def lister_a_viser(*a, **k):
        return []

    def lister_suites(*a, **k):
        return []

    def lister_retournes(*a, **k):
        return []


# ==========================================
# FLASK & ARBORESCENCE
# ==========================================""",
    ),
    # --- 2. Enregistrement apres creation de l'app ------------------------
    (
        "init_visa(app",
        """# ==========================================
# AUTHENTIFICATION""",
        """if VISA_DISPONIBLE:
    app.register_blueprint(bp_visa)


# ==========================================
# AUTHENTIFICATION""",
    ),
    # --- 3. Protection du blueprint apres definition du decorateur --------
    (
        "_visa_protege",
        """MAP_FILE = ROOT_DIR / "folders_map.json\"""",
        """if VISA_DISPONIBLE:
    _visa_protege = True
    init_visa(app, connexion_requise)

MAP_FILE = ROOT_DIR / "folders_map.json\"""",
    ),
    # --- 4. Aiguillage de la soumission ------------------------------------
    (
        "deposer_pour_visa(",
        """            doc = {"numero": "Projet", "nom_fichier": nom_projet}
            ok, detail = send_for_signature(z, doc, pdf_bytes=contenu_pdf)""",
        """            # Aiguillage : un courrier ENTRANT part au visa interne, un
            # SORTANT en signature electronique. Le visa ne consomme aucune
            # enveloppe Zoho Sign.
            if sens == "entrant" and VISA_DISPONIBLE:
                try:
                    ref = deposer_pour_visa(chemin_temp, {
                        "service": service,
                        "type_doc": type_doc,
                        "date_doc": date_doc,
                        "objet": objet,
                        "sens": sens,
                        "correspondant": correspondant,
                        "deposant": deposant,
                    })
                    flash(f"✅ Transmis à la Direction Générale pour visa. "
                          f"Référence : {ref}", "success")
                except Exception as exc:  # noqa: BLE001
                    logging.error("Dépôt pour visa : %s", traceback.format_exc())
                    flash(f"❌ Dépôt pour visa impossible : {exc}", "error")
                return redirect(url_for('index'))

            doc = {"numero": "Projet", "nom_fichier": nom_projet}
            ok, detail = send_for_signature(z, doc, pdf_bytes=contenu_pdf)""",
    ),
    # --- 5. Passer les listes au gabarit -----------------------------------
    (
        "suites=lister_suites",
        """    return render_template('index.html', registre=registre, services=SERVICES,
                           types=TYPES, iles=ILES, a_classer=a_classer,
                           en_attente=lister_en_attente())""",
        """    return render_template('index.html', registre=registre, services=SERVICES,
                           types=TYPES, iles=ILES, a_classer=a_classer,
                           en_attente=lister_en_attente(),
                           a_viser=lister_a_viser(SERVICES, TYPES),
                           suites=lister_suites(SERVICES, TYPES),
                           retournes=lister_retournes(SERVICES, TYPES))""",
    ),
]

# ===========================================================================
# index.html
# ===========================================================================
SECTION_SUITES = """
    <!-- ============================================================
         SUITES A DONNER — observations de la Direction Generale
         ============================================================ -->
    <section class="bloc" id="section-suites">
      <h2>Suites à donner
        {% if suites %}<span class="compte">{{ suites|length }}</span>{% endif %}
      </h2>
      <div class="corps">
        {% if suites %}
          {% for s in suites %}
          <div class="dossier" style="align-items:flex-start;">
            <div style="flex:1;">
              <div class="objet">{{ s.objet }}</div>
              <div class="meta">
                {{ s.service_lib }} &middot; {{ s.type_lib }}
                {% if s.correspondant %}&middot; {{ s.correspondant }}{% endif %}
                {% if s.statut == 'retourne' %}
                  &middot; <strong style="color:var(--rouge);">Retourné au service</strong>
                {% endif %}
              </div>
              {% if s.observations %}
              <div style="margin-top:8px;padding:10px 14px;background:var(--navy-fond);
                          border-left:3px solid var(--navy);border-radius:3px;
                          font-size:13.5px;white-space:pre-wrap;">{{ s.observations }}</div>
              {% endif %}
              <div class="meta" style="margin-top:6px;">
                Visé par {{ s.vise_par }}{% if s.jours %} — il y a {{ s.jours }} jour(s){% endif %}
              </div>
            </div>
            <form method="POST"
                  action="{{ url_for('visa.cloturer', reference=s.reference) }}">
              <button type="submit" class="mineur">Marquer traité</button>
            </form>
          </div>
          {% endfor %}
        {% else %}
          <p class="vide">Aucune suite en attente. Les observations de la
          Direction Générale apparaissent ici après visa.</p>
        {% endif %}
      </div>
    </section>
"""

MENU_SUITES = """    <a class="item" href="#section-suites">
      <span class="ico">✎</span> Suites à donner
      {% if suites %}<span class="pastille">{{ suites|length }}</span>{% endif %}
    </a>
"""

OPS_INDEX = [
    (
        "section-suites",
        """    <!-- 5. REGISTRE -->""",
        SECTION_SUITES + """
    <!-- 5. REGISTRE -->""",
    ),
    (
        "Suites à donner\n",
        """    <div class="rubrique">Zoho</div>""",
        MENU_SUITES + """
    <div class="rubrique">Zoho</div>""",
    ),
]


def main():
    print("\n=== INTEGRATION DU CIRCUIT DE VISA ===\n")

    if not (RACINE / "visa.py").exists():
        print("  ERREUR : visa.py absent de la racine du projet.")
        sys.exit(1)

    print("1) app.py")
    patch(APP, OPS_APP, "app.py")

    print("\n2) index.html")
    patch(INDEX, OPS_INDEX, "index.html")

    print("\n" + "=" * 45)
    print("TERMINE.")
    print("\nVerifications :")
    print("  grep -c 'deposer_pour_visa' scripts/app_ged/app.py     -> 2")
    print("  grep -c 'section-suites' scripts/app_ged/templates/index.html -> 2")
    print("\nAjoute UTILISATEURS_DG dans auth_config.py, place visa.html")
    print("dans templates/, puis relance l'application.")


if __name__ == "__main__":
    main()
