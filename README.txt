Sketchfab Collections — Desktop (Flet)

SETUP
-----
1) Activate venv and install deps:
   python -m pip install -r requirements.txt

2) Create .env next to requirements.txt:
   SKETCHFAB_TOKEN=your_api_token_here

   Optional — folders to scan for downloaded model files (semicolon-separated):
   SKETCHFAB_DOWNLOAD_DIRS=F:\MySketchfabDownloads;C:\Users\you\Downloads

   If omitted, scans these folders when they exist:
   ~/sketchfab, ~/blender, D:\sketchfab, D:\blender, F:\sketchfab, F:\blender
   (also Sketchfab / Blender capitalized variants)

3) Run from project root:
   python src/main.py

   Or double-click:
   launch.bat

LAUNCHER
--------
launch.bat — double-click to start (uses .venv automatically, pauses on error)

Package (optional)
------------------
.venv\Scripts\flet pack src/main.py --name "Sketchfab Collections"

WHAT YOU'LL SEE
---------------
- Liked Models tab: thumbnail preview, downloadable (API), downloaded locally, file path
- Collect: refresh likes + collections from Sketchfab API
- Scan Downloads: re-scan local folders and update Downloaded / Download Path columns
- Match / Auto-Assign: sort models into collections using terms/collections_terms.yaml
- Push Assigned: send assignments to Sketchfab (dry-run on by default)

LOCAL DOWNLOAD DETECTION
------------------------
Matches Sketchfab model UIDs (32-char hex) in filenames or folder names for:
.glb .gltf .fbx .obj .blend .zip .usdz .dae .stl

CLI (optional)
--------------
python src/pipeline.py collect
python src/pipeline.py match
python src/pipeline.py auto-assign
python src/pipeline.py push --dry-run
python src/pipeline.py report
