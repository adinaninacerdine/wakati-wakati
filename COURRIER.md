# Module Courrier de réponse

Après chaque courrier entrant, la GED propose un projet de réponse adapté au
destinataire, issu d'une banque de modèles. Le projet suit le circuit de visa
existant, puis s'archive en courrier sortant (CSOR) relié au courrier d'origine.

## Arbitrages de la Direction (octobre 2026)

| Point | Décision | Où c'est réglé |
|---|---|---|
| Rédaction et validation des modèles | Responsable GED | Banque de courriers › Modèles |
| Premiers modèles | Envoyés par le DG ; 8 modèles de démarrage en attendant | Banque de courriers › Modèles |
| Délai de réponse | 48 h, 24 h ou moins si urgent | Banque › Catégories, Paramètres |
| IA | Autorisée ; fournisseur et catégories paramétrables | Paramètres, Banque › Catégories |
| Zoho Sign | Plus tard : le circuit de visa assure validation et signature manuscrite | — |

## Parcours

1. **Dépôt** d'un courrier reçu (écran Courriers, « Reçu — visa du DG ») avec
   « Réponse attendue » : normale (48 h), urgente (24 h) ou aucune. Le texte est
   extrait du PDF (OCR Tesseract si c'est un scan) et l'expéditeur est reconnu
   dans l'annuaire.
2. **Courrier de réponse › Préparer la réponse** : destinataire pré-rempli,
   nature, modèle, consignes (les observations du DG sont reprises).
3. **Édition** : texte fusionné, zones `[à compléter : …]` visibles, aperçu PDF
   à en-tête WAKATI avec filigrane PROJET, assistance IA pour le paragraphe
   central.
4. **Soumettre à validation** : numéro CSOR réservé, PDF définitif, dépôt dans
   l'écran Visa du signataire (DG, DAF, DT…).
5. **Retour** → correction dans le module puis resoumission ;
   **Bon pour signature** → impression, signature, dépôt du scan ;
   **Archivage** → numéro pré-rempli, entrant d'origine passé « Répondu ».

Statuts : Brouillon → À valider → En signature → Signé → Archivé.

## Garde-fous IA

- L'IA ne rédige que le paragraphe central ; en-tête, formules et signature
  viennent du modèle.
- Rien ne part sans relecture : la soumission est bloquée tant qu'il reste une
  zone `[à compléter]`.
- Chiffres et dates absents des sources sont signalés « à vérifier ».
- Courriels, téléphones et numéros de compte sont masqués avant envoi.
- Désactivée par défaut pour Régulateur et Gouvernance, et pour toute fiche
  marquée confidentielle (sauf case cochée par la DG dans les Paramètres).

## Mise en service

```bash
pip install -r requirements.txt          # ajoute fpdf2
sudo apt install tesseract-ocr tesseract-ocr-fra poppler-utils fonts-crosextra-carlito
```

Clé Groq : écran **Paramètres**, ou `IA_API_KEY = "gsk_..."` dans
`notifier_config.py`. Modèle par défaut : `openai/gpt-oss-120b`.

`auth_config.py` (production) :

```python
RESPONSABLES_GED = ["<adresse de la responsable GED>"]
```

Les adresses des signataires se saisissent dans **Paramètres › Signataires**
(et doivent figurer dans `UTILISATEURS_AUTORISES`).

### Colonnes du registre (facultatif)

Ajouter, à l'identique, dans la feuille DATA du registre Zoho Sheet **et** en
fin de ligne 1 de `registre_2026.csv` :

    En réponse à | Statut de la réponse | Échéance

puis passer `COLONNES_REPONSE_ACTIVES = True` dans `scripts/app_ged/app.py`.

## Données

Dossier `courrier/` (hors Git) : annuaire, modèles, catégories, projets,
suivi des entrants, paramètres, clé IA. Sauvegarde depuis **Banque › Sauvegarde**.
En conteneur, `GED_DONNEES=/app/donnees` le place dans le volume persistant.
