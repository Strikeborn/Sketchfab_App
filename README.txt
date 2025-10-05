Sketchfab Collections — v6 (Flet)

1) Activate venv and install deps:
   python -m pip install -r requirements.txt

2) Put your SKETCHFAB_TOKEN in .env (next to requirements.txt):
   SKETCHFAB_TOKEN=xxxxxxxx

3) Run:
   python src/main.py

4) Package (optional):
   flet pack src/main.py --name "Sketchfab Collections"