# Veeam Notification Monitor

Cette application Flask offre un tableau de bord pour vérifier automatiquement les e-mails de notification Veeam envoyés à une boîte dédiée. Elle vérifie chaque jour à 9h (Europe/Paris) si les objets attendus ont été reçus et attribue un statut : OK, Non reçu, Failed ou Warning.

## Démarrage rapide

### Prérequis
- Docker

### Construction et lancement
```bash
docker build -t veeam-notify .
docker run -p 5000:5000 veeam-notify
```

### Avec Docker Compose
Un fichier `docker-compose.yml` est fourni pour lancer l'application avec un volume persistant pour la base SQLite et un fuseau horaire configuré.

```bash
docker compose up --build
```

Variables utiles (avec valeurs par défaut si non fournies) :
- `SECRET_KEY` : clé secrète Flask (défaut : `change-me`).
- `DATABASE_URL` : URL de la base de données (défaut : `sqlite:////data/app.db`).
- `TZ` : fuseau horaire (défaut : `Europe/Paris`).

L'interface est disponible sur http://localhost:5000.

## Utilisation
1. Rendez-vous dans "Paramètres" et renseignez les informations IMAP (et SMTP optionnel).
2. Ajoutez des clients et définissez pour chacun l'objet attendu de l'e-mail Veeam.
3. Le job planifié vérifie chaque jour à 9h si les messages attendus ont été reçus et déduit le statut (OK, Failed, Warning ou Non reçu). Un bouton permet de lancer une vérification manuelle immédiate.

## Développement local
Installez les dépendances puis lancez l'application :
```bash
pip install -r requirements.txt
flask run --host=0.0.0.0 --port=5000
```
La base SQLite est créée automatiquement dans le répertoire du projet.
