web: gunicorn -w 1 --threads 8 --timeout 120 -b 0.0.0.0:$PORT wsgi:app
release: python -m paperagent initdb
