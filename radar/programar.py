"""Instala (o quita) una tarea de macOS que descarga las novedades cada mañana.

Es lo único que este proyecto escribe fuera de su propia carpeta: un fichero
`.plist` en `~/Library/LaunchAgents/`. Por eso el comando dice exactamente qué
fichero crea antes de crearlo, y `--desinstalar` lo deja todo como estaba.

No usa `cron` porque en macOS `launchd` es lo que funciona de verdad: si el
portátil está dormido a la hora prevista, la tarea se ejecuta al despertar en vez
de perderse.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from . import rutas

RAIZ = rutas.CODIGO
ETIQUETA = "app.zepo.ausschreibungsradar.ingest"
DIR_AGENTES = Path.home() / "Library" / "LaunchAgents"
PLIST = DIR_AGENTES / f"{ETIQUETA}.plist"
LOG = rutas.DIR_DATOS / "ingest.log"


def _definicion(hora: int, minuto: int) -> dict:
    return {
        "Label": ETIQUETA,
        # -u: sin él, stdout hacia un fichero va con buffer de bloque y las
        # líneas de avance del indicador se quedan retenidas hasta el final,
        # que es justo cuando ya no sirven para saber si la tarea seguía viva.
        "ProgramArguments": [sys.executable, "-u", str(RAIZ / "radar.py"), "ingest"],
        "WorkingDirectory": str(RAIZ),
        "StartCalendarInterval": {"Hour": hora, "Minute": minuto},
        # Si el equipo estaba dormido a la hora prevista, que se ejecute al despertar.
        "RunAtLoad": False,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "ProcessType": "Background",
    }


def _launchctl(*args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["launchctl", *args], capture_output=True, text=True, timeout=30
        )
        return r.returncode, (r.stderr or r.stdout).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def estado() -> dict:
    instalada = PLIST.exists()
    codigo, _ = _launchctl("list", ETIQUETA)
    return {"instalada": instalada, "cargada": codigo == 0, "plist": str(PLIST),
            "log": str(LOG)}


def instalar(hora: int = 8, minuto: int = 30) -> str:
    if not (0 <= hora <= 23 and 0 <= minuto <= 59):
        raise ValueError("Hora fuera de rango")
    DIR_AGENTES.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)

    with PLIST.open("wb") as fh:
        plistlib.dump(_definicion(hora, minuto), fh)

    uid = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{uid}/{ETIQUETA}")  # por si ya estaba cargada
    codigo, salida = _launchctl("bootstrap", uid, str(PLIST))
    if codigo != 0:
        # `bootstrap` falla en macOS antiguos; `load` sigue funcionando ahí.
        codigo, salida = _launchctl("load", str(PLIST))
    if codigo != 0:
        raise RuntimeError(
            f"El fichero se ha creado en {PLIST} pero launchctl no lo ha cargado: "
            f"{salida}. Reinicia la sesión o cárgalo a mano con:\n"
            f"  launchctl bootstrap {uid} {PLIST}"
        )
    return str(PLIST)


def desinstalar() -> bool:
    uid = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{uid}/{ETIQUETA}")
    _launchctl("unload", str(PLIST))
    if PLIST.exists():
        PLIST.unlink()
        return True
    return False
