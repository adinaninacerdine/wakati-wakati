# Notifications de la GED — routeur.py

Chaque événement a une nature, et une nature passe par un seul canal.

| Nature | Exemples | Canal | E-mail |
|---|---|---|---|
| Action | viser / signer une note, valider un projet de réponse, traiter une demande, corriger un retour | message privé Cliq | si Cliq échoue ; en plus si urgent ou note importante |
| Information | note à lire, demande traitée | message privé Cliq | si Cliq échoue ; note non lue après 24 h (une fois) |
| Suivi | document archivé, courrier remis, visa rendu, RDV | #ged-courrier | non (récapitulatif) |
| Direction | échéance dépassée, remise échouée, note bloquée, courrier entrant urgent | #ged-direction | si urgent, aux e-mails Direction |
| Escalade | étape non traitée après 2 × le délai, demande interne en retard | privé + e-mail | copie DG (ou responsables GED) |
| Récapitulatif | bilan du jour, lundi – samedi à 17 h | — | e-mails Direction |

Anti-surcharge :
- plage de silence 19 h – 7 h 30 et dimanche, les messages partent au matin et les urgences passent ;
- regroupement : plusieurs messages privés pour une même personne en 90 s partent en un seul message ;
  plus de 3 cartes pour un canal deviennent un résumé ;
- dédoublonnage des relances (une par 24 h) ;
- plus d'e-mail par document archivé : le récapitulatif de 17 h le remplace.

## Mise en service

1. Dans Cliq, créer les canaux **ged-courrier** (équipe courrier, secrétariat) et **ged-direction**
   (DG, DAF, responsable GED). Le compte qui a généré le jeton Cliq (CLIQ_REFRESH_TOKEN) doit être
   membre des deux. Relever le « nom unique » de chaque canal (informations du canal).
2. GED > Notifications : saisir les deux noms uniques, l'adresse de la GED
   (`https://ged.wakati.km`), vérifier les e-mails Direction, enregistrer.
3. Boutons « Tester » : une carte doit arriver dans chaque canal, et un message privé chez vous.
4. Tant que les canaux ne sont pas saisis, tout part dans l'ancien canal (CLIQ_CHANNEL).

Au premier démarrage, les relances d'échéances dépassées se mettent à tourner sur le serveur
(elles ne tournaient qu'en lancement local). Les courriers déjà en retard arrivent en un seul
résumé dans #ged-direction.

Données : `courrier/notifications.json` (réglages, file d'attente, journal des 400 derniers envois).
