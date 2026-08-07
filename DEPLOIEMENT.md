# Déploiement de la GED WAKATI sur le VPS

Guide pas à pas. Chaque étape est vérifiable avant de passer à la suivante.

---

## 1. DNS — à faire en premier

Chez DIRECOM ANADEN, ajouter un enregistrement pour `wakati.km` :

```
Type : A
Nom  : ged
Cible: <adresse IP du VPS>
```

Vérifier la propagation avant de continuer — Let's Encrypt échouera si le
nom ne résout pas encore :

```bash
dig +short ged.wakati.km
```

La commande doit renvoyer l'adresse du VPS. Compter jusqu'à une heure.

---

## 2. Client OAuth pour l'authentification

Sur **api-console.zoho.com** → **Add Client** → **Server-based Application**.

| Champ | Valeur |
|---|---|
| Client Name | GED WAKATI |
| Homepage URL | `https://ged.wakati.km` |
| Authorized Redirect URI | `https://ged.wakati.km/oauth/callback` |

Noter le **Client ID** et le **Client Secret**.

> Ce client est distinct du Self Client utilisé pour les appels API. Ne pas
> les confondre : celui-ci sert uniquement à identifier les utilisateurs.

---

## 3. Préparation du VPS

```bash
ssh <utilisateur>@<ip_vps>
sudo mkdir -p /opt/ged-wakati
sudo chown $USER:$USER /opt/ged-wakati
cd /opt/ged-wakati

git clone https://github.com/adinaninacerdine/wakati-wakati.git .
```

---

## 4. Fichiers de configuration

Ils ne sont pas dans Git : il faut les transférer depuis le poste local.

```bash
# Depuis le poste local
cd ~/Documents/wakati-wakati
scp zoho_config.py notifier_config.py folders_map.json \
    <utilisateur>@<ip_vps>:/opt/ged-wakati/
```

Puis créer `auth_config.py` sur le VPS :

```bash
cd /opt/ged-wakati
python3 -c "import secrets; print(secrets.token_hex(32))"   # noter la valeur

nano auth_config.py
```

```python
SSO_CLIENT_ID     = "1000.XXXXXXXX"
SSO_CLIENT_SECRET = "xxxxxxxxxxxx"
SSO_REDIRECT_URI  = "https://ged.wakati.km/oauth/callback"

SECRET_KEY = "<la valeur générée ci-dessus>"

UTILISATEURS_AUTORISES = [
    "nacer-dine.a@hurimoney.com",
    "<adresse du DG>",
    "<adresse de la responsable GED>",
]

DUREE_SESSION_H = 12
COOKIE_SECURE = True
```

Restreindre les permissions :

```bash
chmod 600 zoho_config.py notifier_config.py auth_config.py
```

---

## 5. Dépendances Python

```bash
cd /opt/ged-wakati
cat > requirements.txt <<'EOF'
Flask>=3.0
requests>=2.31
EOF
```

---

## 6. Démarrage

Vérifier d'abord si un proxy occupe déjà les ports 80 et 443 :

```bash
sudo ss -ltnp | grep -E ':80 |:443 '
```

**Aucun résultat** → démarrer tel quel :

```bash
docker compose up -d --build
docker compose logs -f ged
```

**Un proxy existe déjà** (installation Odoo) → commenter le service `caddy`
dans `docker-compose.yml`, ajouter le bloc `ged.wakati.km` à la configuration
du proxy existant, et rattacher le conteneur à son réseau.

---

## 7. Vérification

```bash
curl -I https://ged.wakati.km/connexion
```

Attendu : `HTTP/2 200`. Ouvrir ensuite l'adresse dans un navigateur : la page
de connexion WAKATI doit s'afficher.

Tester la connexion avec une adresse autorisée, puis avec une adresse absente
de la liste — la seconde doit être refusée.

---

## 8. Lien depuis wakati.km

Ajouter dans le pied de page du site, discrètement :

```html
<a href="https://ged.wakati.km">Espace documentaire</a>
```

L'accès reste protégé : sans compte autorisé, le visiteur ne voit que la page
de connexion.

---

## 9. Exploitation courante

**Mise à jour applicative**

```bash
cd /opt/ged-wakati
git pull
docker compose up -d --build
```

**Ajouter ou retirer un utilisateur**

```bash
nano auth_config.py          # modifier UTILISATEURS_AUTORISES
docker compose restart ged
```

Le retrait prend effet immédiatement : l'accès est contrôlé à chaque requête,
sans attendre l'expiration de la session.

**Sauvegarde**

Les documents et le registre sont dans Zoho. Ne restent à sauvegarder que les
fichiers de configuration et les données de travail :

```bash
cd /opt/ged-wakati
tar czf ~/ged-sauvegarde-$(date +%F).tar.gz \
    zoho_config.py notifier_config.py auth_config.py folders_map.json
docker run --rm -v ged-wakati_ged-donnees:/d -v ~:/s alpine \
    tar czf /s/ged-donnees-$(date +%F).tar.gz -C /d .
```

À planifier en tâche mensuelle et à conserver hors du VPS.

---

## 10. Points de vigilance

**Un seul worker.** La relève tourne dans un thread interne à l'application.
Augmenter le nombre de workers Gunicorn la déclencherait en parallèle et
provoquerait des téléchargements en double.

**Sessions et redémarrage.** Un redémarrage du conteneur déconnecte les
utilisateurs si `SECRET_KEY` n'est pas fixée dans `auth_config.py`.

**Certificat.** Le renouvellement est automatique. En cas d'échec, vérifier
que les ports 80 et 443 sont bien ouverts sur le pare-feu du VPS.

**Cohabitation avec Odoo.** Les deux applications partagent les ressources du
serveur. Surveiller la mémoire disponible après quelques jours d'exploitation.
