"""Lanzar una búsqueda desde la interfaz sin bloquearla.

Una ingesta completa tarda alrededor de un minuto (o mucho más con histórico), así
que no puede correr dentro de la petición HTTP: se lanza `radar.py ingest` como
proceso aparte y la interfaz va preguntando cómo va.

Se reutiliza la CLI en lugar de llamar al pipeline directamente. Así hay un único
camino de ejecución —el mismo que usa la tarea programada de cada mañana— y no dos
implementaciones que puedan divergir.

El bloqueo importa: la tarea de las 8:30 y el botón pueden coincidir, y dos ingestas
a la vez sobre el mismo SQLite se pelean por el bloqueo de escritura.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import progreso

from . import rutas

RAIZ = rutas.CODIGO
CERROJO = rutas.DIR_DATOS / "busqueda.lock"
SALIDA = rutas.DIR_DATOS / "busqueda.log"


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _proceso_vivo(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def en_marcha() -> dict | None:
    """Devuelve los datos de la búsqueda en curso, o None si no hay ninguna.

    Si el cerrojo apunta a un proceso que ya no existe (un cierre a lo bruto, un
    reinicio), se limpia solo en lugar de dejar la aplicación bloqueada para
    siempre.
    """
    if not CERROJO.exists():
        return None
    try:
        datos = json.loads(CERROJO.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        CERROJO.unlink(missing_ok=True)
        return None

    pid = datos.get("pid")
    if not isinstance(pid, int) or not _proceso_vivo(pid):
        CERROJO.unlink(missing_ok=True)
        return None
    return datos


def adquirir(origen: str = "cli") -> bool:
    """Toma el cerrojo de forma atómica. False si ya lo tiene otro.

    Lo llama `radar.py ingest`, no solo el botón: la tarea programada de cada mañana
    ejecuta la CLI directamente y también tiene que respetarlo. La creación
    exclusiva del fichero es lo que evita que dos arranques simultáneos crean los
    dos que el cerrojo es suyo.
    """
    en_marcha()  # limpia un cerrojo huérfano si lo hubiera
    CERROJO.parent.mkdir(parents=True, exist_ok=True)
    datos = json.dumps({"pid": os.getpid(), "iniciada": _ahora(), "origen": origen})
    try:
        fd = os.open(CERROJO, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(datos)
    return True


def liberar() -> None:
    """Suelta el cerrojo si es de este proceso."""
    activa = None
    if CERROJO.exists():
        try:
            activa = json.loads(CERROJO.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            activa = None
    if activa is None or activa.get("pid") == os.getpid():
        CERROJO.unlink(missing_ok=True)


def detalle_progreso() -> dict | None:
    """La instantánea que publica la ingesta, o None si todavía no hay ninguna.

    Es lo único que ve la aplicación de una carga que corre en segundo plano: los
    contadores viven en el proceso de la ingesta, no en el del servidor.
    """
    try:
        return json.loads(progreso.ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def lanzar(*, reiniciar_cursor: bool = False, dias: int | None = None,
           fuentes: list | None = None, backfill: list | None = None,
           primera_carga: bool = False, etapas: list | None = None,
           meses: int | None = None) -> dict:
    """Arranca la ingesta en segundo plano. Lanza RuntimeError si ya hay una.

    El cerrojo definitivo lo toma el proceso hijo; esta comprobación solo sirve
    para dar un mensaje claro en lugar de arrancar un proceso que se va a rendir.
    """
    activa = en_marcha()
    if activa:
        raise RuntimeError(
            f"Es läuft bereits eine Suche seit {activa.get('iniciada', '?')}."
        )

    # -u desactiva el buffer del hijo: sin él, las líneas del registro que lee la
    # aplicación llegarían a ráfagas en lugar de a medida que pasan las cosas.
    orden = [sys.executable, "-u", str(rutas.ENTRADA_CLI), "ingest"]
    if primera_carga:
        orden.append("--primera-carga")
        if etapas:
            orden += ["--etapas"] + [str(int(e)) for e in etapas]
    for fuente in fuentes or []:
        orden += ["--fuente", str(fuente)]
    if backfill:
        orden += ["--backfill", ",".join(str(int(a)) for a in backfill)]
    if reiniciar_cursor:
        orden.append("--reiniciar-cursor")
    if dias:
        orden += ["--dias", str(int(dias))]
    if meses:
        orden += ["--meses", str(int(meses))]

    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    # El descriptor se cierra en cuanto arranca el hijo: él ya tiene su propia copia.
    # Dejarlo abierto filtraba un descriptor por búsqueda en el proceso del servidor,
    # que se queda encendido durante días.
    with SALIDA.open("w", encoding="utf-8") as registro:
        proceso = subprocess.Popen(
            orden,
            cwd=str(RAIZ),
            stdout=registro,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # que no muera al parar el servidor
        )
    return {
        "pid": proceso.pid,
        "iniciada": _ahora(),
        "reiniciar_cursor": reiniciar_cursor,
        "primera_carga": primera_carga,
    }


def cancelar() -> bool:
    activa = en_marcha()
    if not activa:
        return False
    try:
        os.kill(activa["pid"], signal.SIGTERM)
    except OSError:
        pass
    CERROJO.unlink(missing_ok=True)
    return True


def ultimas_lineas(n: int = 12) -> list[str]:
    """Las últimas líneas del registro, para ir mostrando el progreso.

    Se filtran las consultas a TED, que ocupan varios miles de caracteres y no
    dicen nada a quien está mirando la pantalla. Se emiten con dos encabezados
    distintos —`TED:` en la ingesta incremental y `TED histórico AAAA:` en el
    backfill—, y colar una de ellas en la cabecera de la aplicación llena la línea de
    paréntesis y códigos CPV.
    """
    if not SALIDA.exists():
        return []
    try:
        lineas = SALIDA.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    utiles = [
        l.strip() for l in lineas
        if l.strip() and "(buyer-country" not in l
    ]
    return utiles[-n:]
