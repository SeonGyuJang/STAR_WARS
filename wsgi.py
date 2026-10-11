"""WSGI entry point for production, e.g.:  gunicorn -w 1 --threads 8 -b 0.0.0.0:8000 wsgi:app
Use a single worker process so only one scheduler runs."""
from paperagent import create_app

app = create_app()
