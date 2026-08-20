#!/usr/bin/env python3
"""
maj_dashboard.py — le tableau de bord lit le circuit reel
==========================================================
A lancer UNE FOIS depuis la racine du projet :

    ./venv/bin/python maj_dashboard.py

PROBLEME CORRIGE
----------------
Les indicateurs lisaient encore en_attente.json, le fichier de Zoho Sign,
vide et gele depuis l'expiration de la licence. La carte « En attente de
signature » affichait donc zero en permanence, alors que des documents
attendent reellement chez le DG dans visas.json.

CE QUE FAIT LE SCRIPT
---------------------
  1. app.py  : calculer_indicateurs() lit visas.json
  2. app.py  : nouveaux compteurs — a viser, approuves en attente de
               signature, suites ouvertes, retours, delai moyen de visa
  3. tableau_de_bord.html : six cartes au lieu de quatre, et une section
               « Ce qui demande une action » en tete

Sauvegarde horodatee avant toute ecriture. Idempotent.
"""

import shutil
import time
from pathlib import Path

RACINE = Path(__file__).resolve().parent
APP = RACINE / "scripts" / "app_ged" / "app.py"
TDB = RACINE / "scripts" / "app_ged" / "templates" / "tableau_de_bord.html"

HORO = time.strftime("%Y%m%d-%H%M%S")


def sauver(chemin):
    copie = chemin.with_suffix(chemin.suffix + f".avant-dash-{HORO}")
    shutil.copy2(chemin, copie)
    print(f"  Sauvegarde : {copie.name}")


# ===========================================================================
# 1. app.py — indicateurs du circuit reel
# ===========================================================================
def corriger_app():
    if not APP.exists():
        print("  app.py introuvable."); return 0

    s = APP.read_text(encoding="utf-8")
    avant = s

    if "indicateurs_visa" in s:
        print("  Deja applique."); return 0

    fonction = '''def indicateurs_visa():
    """Compteurs du circuit de visa, lus dans visas.json.

    Les anciens indicateurs interrogeaient en_attente.json, fichier de
    Zoho Sign gele depuis l'expiration de la licence : ils affichaient
    zero en permanence alors que des documents attendaient reellement.
    """
    vide = {"a_viser": 0, "a_viser_retard": 0, "approuves": 0,
            "suites": 0, "suites_retard": 0, "retours": 0,
            "delai_moyen": None, "vises_mois": 0}

    fichier = ROOT_DIR / "visas.json"
    if not fichier.exists():
        return vide

    try:
        donnees = json.loads(fichier.read_text(encoding="utf-8"))
    except Exception:
        return vide

    maintenant = datetime.now()
    mois = maintenant.strftime("%Y-%m")
    res = dict(vide)
    delais = []

    for d in donnees.values():
        statut = d.get("statut", "")

        if statut == "attente":
            res["a_viser"] += 1
            try:
                jours = (maintenant - datetime.fromisoformat(d.get("depose_le", ""))).days
                if jours >= 3:
                    res["a_viser_retard"] += 1
            except (ValueError, TypeError):
                pass

        elif statut == "approuve":
            res["approuves"] += 1

        elif statut == "retourne":
            res["retours"] += 1

        # Delai entre depot et visa — la mesure du gain de la digitalisation.
        if d.get("vise_le") and d.get("depose_le"):
            try:
                ecart = (datetime.fromisoformat(d["vise_le"])
                         - datetime.fromisoformat(d["depose_le"]))
                delais.append(ecart.total_seconds() / 3600)
            except (ValueError, TypeError):
                pass
            if d["vise_le"].startswith(mois):
                res["vises_mois"] += 1

        # Suites a donner encore ouvertes
        if d.get("action_statut") == "ouverte":
            res["suites"] += 1
            try:
                jours = (maintenant - datetime.fromisoformat(d.get("vise_le", ""))).days
                if jours >= 3:
                    res["suites_retard"] += 1
            except (ValueError, TypeError):
                pass

    if delais:
        moyenne = sum(delais) / len(delais)
        res["delai_moyen"] = (f"{moyenne:.0f} h" if moyenne < 48
                              else f"{moyenne / 24:.1f} j")

    return res


'''

    s = s.replace("def calculer_indicateurs():", fonction + "def calculer_indicateurs():", 1)

    # Fusion dans le dictionnaire retourne
    ancien_retour = """    return {
        'total': total,"""
    nouveau_retour = """    visa = indicateurs_visa()

    return {
        'visa': visa,
        'total': total,"""
    if ancien_retour in s:
        s = s.replace(ancien_retour, nouveau_retour, 1)

    if s != avant:
        sauver(APP)
        APP.write_text(s, encoding="utf-8")
        print("  indicateurs_visa() ajoutee")
        return 1
    print("  ATTENTION : calculer_indicateurs() introuvable")
    return 0


# ===========================================================================
# 2. tableau_de_bord.html — cartes du circuit
# ===========================================================================
def corriger_tdb():
    if not TDB.exists():
        print("  tableau_de_bord.html introuvable."); return 0

    s = TDB.read_text(encoding="utf-8")
    avant = s

    if "kpi.visa" in s:
        print("  Deja applique."); return 0

    # --- La carte « en attente de signature » lisait le mauvais fichier ---
    ancienne_carte = '''      <div class="carte {% if kpi.en_retard > 0 %}alerte{% endif %}">
        <div class="valeur">{{ kpi.en_attente }}</div>
        <div class="etiq">En attente de signature</div>
        <div class="note">
          {% if kpi.en_retard > 0 %}
            {{ kpi.en_retard }} depuis 3 jours ou plus
          {% else %}
            Aucun retard signalé
          {% endif %}
        </div>
      </div>'''

    nouvelle_carte = '''      <div class="carte {% if kpi.visa.a_viser_retard > 0 %}alerte{% endif %}">
        <div class="valeur">{{ kpi.visa.a_viser }}</div>
        <div class="etiq">En attente de visa</div>
        <div class="note">
          {% if kpi.visa.a_viser_retard > 0 %}
            {{ kpi.visa.a_viser_retard }} depuis 3 jours ou plus
          {% else %}
            Aucun retard signalé
          {% endif %}
        </div>
      </div>'''

    if ancienne_carte in s:
        s = s.replace(ancienne_carte, nouvelle_carte, 1)
        print("  Carte « en attente de visa » corrigee")

    # --- Deux cartes supplementaires --------------------------------------
    ancienne_fin = '''      <div class="carte">
        <div class="valeur">{{ kpi.a_archiver }}</div>
        <div class="etiq">Signés, à archiver</div>
        <div class="note">
          {% if kpi.a_archiver > 0 %}Action attendue{% else %}Rien en attente{% endif %}
        </div>
      </div>

    </div>'''

    nouvelle_fin = '''      <div class="carte">
        <div class="valeur">{{ kpi.a_archiver }}</div>
        <div class="etiq">Signés, à archiver</div>
        <div class="note">
          {% if kpi.a_archiver > 0 %}Action attendue{% else %}Rien en attente{% endif %}
        </div>
      </div>

      <div class="carte {% if kpi.visa.suites_retard > 0 %}alerte{% endif %}">
        <div class="valeur">{{ kpi.visa.suites }}</div>
        <div class="etiq">Suites à donner</div>
        <div class="note">
          {% if kpi.visa.suites_retard > 0 %}
            {{ kpi.visa.suites_retard }} ouvertes depuis 3 jours ou plus
          {% else %}
            Instructions de la Direction en cours
          {% endif %}
        </div>
      </div>

      <div class="carte">
        <div class="valeur">{{ kpi.visa.delai_moyen or '—' }}</div>
        <div class="etiq">Délai moyen de visa</div>
        <div class="note">
          Entre la transmission et la décision
        </div>
      </div>

    </div>

    {# Signaux exigeant une decision : regroupes en tete, hors des
       repartitions qui relevent du suivi d'activite. #}
    {% if kpi.visa.approuves or kpi.visa.retours %}
    <div class="bloc" style="margin-bottom:22px;">
      <h2>Ce qui demande une action</h2>
      <div class="corps">
        {% if kpi.visa.approuves %}
        <div class="barre-ligne">
          <div class="barre-tete">
            <span class="nom">Approuvés &mdash; en attente de signature manuscrite</span>
            <span class="nb">{{ kpi.visa.approuves }}</span>
          </div>
        </div>
        {% endif %}
        {% if kpi.visa.retours %}
        <div class="barre-ligne">
          <div class="barre-tete">
            <span class="nom" style="color:var(--rouge);">Retournés au service &mdash; à corriger</span>
            <span class="nb" style="color:var(--rouge);">{{ kpi.visa.retours }}</span>
          </div>
        </div>
        {% endif %}
      </div>
    </div>
    {% endif %}'''

    if ancienne_fin in s:
        s = s.replace(ancienne_fin, nouvelle_fin, 1)
        print("  Cartes « suites » et « delai moyen » ajoutees")

    # Quatre cartes par ligne deviennent trois : six cartes s'alignent mieux
    s = s.replace(".cartes { display: grid; grid-template-columns: repeat(4, 1fr);",
                  ".cartes { display: grid; grid-template-columns: repeat(3, 1fr);")

    if s != avant:
        sauver(TDB)
        TDB.write_text(s, encoding="utf-8")
        return 1
    return 0


def main():
    print("=== TABLEAU DE BORD SUR LE CIRCUIT REEL ===\\n")
    print("app.py")
    a = corriger_app()
    print("\\ntableau_de_bord.html")
    b = corriger_tdb()

    print("\\n" + "=" * 45)
    if a + b == 0:
        print("Rien a faire.")
    else:
        print("TERMINE. Relance l'application.\\n")
        print("Six indicateurs :")
        print("  documents archives      volume du mois")
        print("  en attente de visa      signes a archiver")
        print("  suites a donner         delai moyen de visa")
        print("\\nLe delai moyen mesure le gain de la digitalisation :")
        print("c'est le chiffre que la Direction regardera en premier.")


if __name__ == "__main__":
    main()
