"""WSGI entry point for cloud hosting:  gunicorn wsgi:app --workers 1 --threads 4

Use exactly ONE worker process: each worker would start its own MQTT subscriber."""
from app import create_app

app = create_app()
