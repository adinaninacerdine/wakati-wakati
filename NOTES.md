# Courriers internes — notes.py

Mise en œuvre du § 5 du manuel « Gestion des courriers digitalisée »
(docs/procedures/3.4.1_ARFINA_MZEMBABA_Gestion_de_courrier_digitalise.docx).

## Types et circuit

| Type | Visa | Signature | Destinataires |
|---|---|---|---|
| Note de service (NS) | DAF | DG si « importante », sinon DAF | lecture confirmée |
| Circulaire (CIR) | DAF | DG | lecture confirmée |
| Mise à disposition (MAD) | DAF | DG | lecture confirmée |
| Demande interne (DEM) | — | direction émettrice | traitement sous 24 h (paramétrable) |

- Quand la note est saisie pour une direction par quelqu'un d'autre (responsable GED),
  une étape « Validation » de cette direction s'ajoute en tête.
- Pas de visa DAF quand le DAF est lui-même l'émetteur.
- Signature électronique interne : approbation du signataire connecté (SSO Zoho),
  horodatée et portée sur le PDF (cartouche « Signé électroniquement »).
- À la signature : numéro (suite HM26 du couple service/type), PDF définitif,
  archivage WorkDrive + registre (sens « Interne ») + index de recherche,
  diffusion par Cliq et e-mail d'un lien personnel `/n/<jeton>`.

## Classement par direction (modifiable à chaque note)

| Direction | Service GED |
|---|---|
| DG — Direction générale | DGA — Direction / Admin |
| DAF | FIN — Finances |
| DT — outils informatiques, application WAKATI | DSI — Systèmes d'information |
| DCOM — Direction commerciale, SC — Service client | COM — Commercial |
| DOP — Direction des Opérations (transversale : formations à l'application, ouverture de comptes, appui à l'animation) ; intérim assuré par le DG | OPS — Opérations |

Intérim : dans Paramètres > Signataires, la colonne « Intérim par » (ex. DG pour DOP)
donne au titulaire de l'intérim le droit de rédiger, viser et signer pour la direction.

## Aides à la rédaction

- **Modèles de notes** (menu Courrier interne > Modèles de notes) : 14 modèles fournis
  (horaires, réunion, congés, intérim, règlement intérieur, confidentialité des données
  clients, procédure, fermeture des bureaux, lancement d'offre, demande d'achat,
  intervention informatique, demande de document, mise à disposition d'agent ou de
  matériel). La responsable GED les modifie, en crée et les désactive ; toute note peut
  devenir un modèle (« Enregistrer comme modèle »). Un modèle supprimé n'est pas recréé.
- **Zones `[à compléter : …]`** : la soumission est refusée tant qu'il en reste dans
  l'objet ou le texte (même règle que les courriers).
- **Assistance IA** (fournisseur, clé et modèle des Paramètres du courrier) :
  « Rédiger le texte » à partir des points clés (complète le modèle s'il y en a un),
  « Corriger et reformuler » le texte existant, « Annuler la proposition ».
  Courriels, téléphones et numéros de compte sont remplacés par des repères avant l'envoi
  puis restaurés : ils ne quittent pas la GED. Chaque usage est tracé dans l'historique.
  Une note cochée **confidentielle** n'a pas d'IA.
- **Réutilisation** : la recherche de la liste des notes porte sur l'objet, le texte, la
  référence et la direction, y compris les notes diffusées par les autres directions
  (sauf confidentielles). « Dupliquer en nouveau brouillon » repart d'une note existante.

## Mise en service

1. `auth_config.py` (VPS) : ajouter les adresses des directeurs à `UTILISATEURS_AUTORISES`.
2. GED > Paramètres > Signataires : renseigner l'e-mail de chaque direction
   (c'est lui qui donne le droit de rédiger, viser et signer pour elle).
3. GED > Annuaire du personnel : importer la liste « Nom ; Fonction ; Service ; E-mail ».
4. Dossiers WorkDrive (types Circulaire et Mise à disposition, services DSI et Opérations) :
   `python setup_folders.py` (aperçu) puis `python setup_folders.py --confirm`, puis
   `docker compose restart ged` (folders_map.json est monté en lecture seule).

Données : `courrier/notes/notes.json`, `courrier/notes/personnel.json`, `courrier/notes/modeles.json`,
`courrier/notes/<id>/note.pdf`.
