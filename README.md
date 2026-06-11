# Meridian Platform

A multi-tenant SaaS platform for project and workflow management.

## Stack

- Python 3.11 / FastAPI
- PostgreSQL + SQLAlchemy
- Redis (caching, sessions)
- Celery (background tasks)
- Node.js (auxiliary services)

## Structure

```
meridian/
  api/          FastAPI application
  workers/      Celery background workers
  models/       SQLAlchemy models
  services/     Business logic layer
  utils/        Shared utilities
scripts/        Dev/ops scripts
tests/          Test suite
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn meridian.api.main:app --reload
```
