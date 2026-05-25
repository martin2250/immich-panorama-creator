
nix-shell -p hugin perlPackages.ImageExifTool imagemagick python3 uv
uv --no-managed-python run main.py
