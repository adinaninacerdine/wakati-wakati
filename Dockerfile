# ============================================================
# GED WAKATI — image applicative
# ============================================================
FROM python:3.12-slim

# Fuseau des Comores : les horodatages du registre doivent être locaux.
ENV TZ=Indian/Comoro \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GED_CONTENEUR=1 \
    GED_AUTH=1

WORKDIR /app

# Dépendances système : ghostscript sert à alléger les PDF volumineux
# avant envoi en signature, la liaison depuis les Comores étant limitée.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ghostscript tzdata curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

# Compte non privilégié : l'application n'a aucune raison de tourner en root.
RUN useradd -m -u 1000 ged \
    && mkdir -p /app/donnees/a_classer \
    && chown -R ged:ged /app
USER ged

EXPOSE 5000

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:5000/connexion || exit 1

# Un seul worker : la relève tourne dans un thread interne, plusieurs
# workers la déclencheraient en parallèle et téléchargeraient en double.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", \
     "--workers", "1", "--threads", "4", \
     "--timeout", "300", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "--chdir", "/app/scripts/app_ged", "app:app"]
