#!/bin/bash

set -e  # Stop on first error

echo "→ Python version check:"
python3 --version

echo "→ Upgrading pip (with override)..."
python3 -m pip install --upgrade pip setuptools wheel --break-system-packages

echo "→ Installing requirements..."
python3 -m pip install -r requirements.txt --no-cache-dir --break-system-packages

# Quick verification that Django is importable
python3 -c "import django; print('Django OK:', django.__version__)" || {
  echo "ERROR: Django not installed/imported after pip!"
  exit 1
}

echo "→ Makemigrations & migrate (optional/safe)..."
python3 manage.py makemigrations --noinput || true
python3 manage.py migrate --noinput || true

echo "→ Collectstatic..."
python3 manage.py collectstatic --noinput --clear

echo "→ Build done"