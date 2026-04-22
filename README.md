# Household Resource Planner (HRP)

Kapazitätsplanung für Haushaltsaufgaben – faire Verteilung, transparente Zeitbelastung.

## Quickstart (lokal)

```bash
pip install -r requirements.txt
python main.py
```

App läuft auf **http://localhost:8080**

**Standard-Login:** `admin` / `admin`

## Docker Deployment

```bash
docker-compose up -d --build
```

Die SQLite-Datenbank wird in einem Docker Volume (`hrp_data`) persistiert.

## Struktur

| Datei | Beschreibung |
|---|---|
| `main.py` | App-Einstieg, NiceGUI-UI, Routen |
| `models.py` | SQLAlchemy-Datenmodell (User, Task, TaskInstance) |
| `schemas.py` | Pydantic-Schemas für Validierung |
| `auth.py` | Passwort-Hashing (bcrypt), JWT-Token |
| `Dockerfile` | Container-Image |
| `docker-compose.yml` | Deployment-Konfiguration |

## Features

- **Planungs-Matrix**: Wochen-/Monatsansicht, Aufgaben × Tage
- **Mobile Ansicht**: Listen-Layout mit großen Buttons
- **Zeit-Splitting**: Aufgabendauer wird fair auf zugewiesene Nutzer verteilt
- **Kapazitäts-Check**: Überbuchte Tage werden rot markiert
- **Dark Mode**: Standardmäßig aktiviert
- **Rollen**: Admin (Vollzugriff) und User (eigene Aufgaben abhaken)
