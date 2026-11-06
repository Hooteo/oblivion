#!/bin/sh
# Avvio come root solo per sistemare i permessi della cartella dati,
# poi l'app gira come utente non privilegiato (PUID/PGID, default 1000).
set -e
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
DATA_DIR="${DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    if [ "$(stat -c %u:%g "$DATA_DIR")" != "$PUID:$PGID" ]; then
        echo "entrypoint: imposto il proprietario di $DATA_DIR a $PUID:$PGID"
        chown -R "$PUID:$PGID" "$DATA_DIR"
    fi
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

# container avviato gia' con "user:": nessuna correzione possibile, solo un controllo
if [ ! -w "$DATA_DIR" ]; then
    echo "entrypoint: ERRORE, $DATA_DIR non è scrivibile dall'utente $(id -u). Esegui sul server: chown -R $(id -u):$(id -g) <cartella del volume>" >&2
    exit 1
fi
exec "$@"
