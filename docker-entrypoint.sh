#!/bin/sh
set -e

echo "Veritabani migration kontrol ediliyor (alembic upgrade head)..."
python -m alembic upgrade head

exec "$@"
