"""Dónde está el código y dónde se escriben los datos. Son dos cosas distintas.

Durante mucho tiempo fueron la misma: todo colgaba de `Path(__file__).parent.parent`,
la carpeta del proyecto, y ahí dentro vivían tanto `radar/` como `data/ausschreibungen.db`. Para
una copia de trabajo clonada del repositorio eso está bien y sigue siendo lo que pasa.

Lo que no permitía era repartir el programa. Para que un compañero reciba **un solo
fichero** —`Ausschreibungsradar.app` y nada más— el código tiene que ir DENTRO del
bundle, y ahí no se puede escribir: una app puede estar en `/Applications`, que no es
del usuario; puede estar firmada, y escribir dentro rompe la firma; y el actualizador la
sustituye entera, así que cualquier cosa guardada dentro se perdería en la siguiente
versión. La base de datos de alguien no puede vivir en un sitio así.

Así que se parten en dos:

- `CODIGO`  — de dónde se lee: `radar/`, `web/`, los certificados, la plantilla de
              perfiles. Solo lectura, y puede estar dentro del `.app`.
- `DATOS`   — donde se escribe: `data/` (base, caché, registros) y el
              `config/suchprofile.json` de cada uno.

Y la regla para decidir `DATOS` es explícita, no una heurística de permisos:

1. Si `AUSSCHREIBUNGSRADAR_DATEN` está en el entorno, manda. Es lo que usan los tests y quien quiera
   llevarse la base a un disco externo.
2. Si el código está dentro de un `.app`, los datos van a
   `~/Library/Application Support/Ausschreibungsradar`, que es donde macOS espera que
   escriba una aplicación.
3. Si no, la carpeta del proyecto, como siempre. **Una copia de trabajo no cambia de
   comportamiento ni mueve su base de sitio.**

Se comprueba mirando si alguna carpeta por encima del código acaba en `.app`, y no si
la carpeta es escribible: un `.app` en el Escritorio SÍ es escribible, y esa versión de
la regla dejaría la base dentro del bundle, donde la borraría la primera actualización.
"""

from __future__ import annotations

import os
from pathlib import Path

# La carpeta que contiene `radar/`. Dentro de un bundle es `Contents/Resources`.
CODIGO = Path(__file__).resolve().parent.parent

NOMBRE_APP = "Ausschreibungsradar"

# Variable de entorno para mandar los datos a otro sitio. La app la usa para nada, pero
# los tests sí, y evita tener que parchear ocho constantes de módulo.
VAR_ENTORNO = "AUSSCHREIBUNGSRADAR_DATEN"


def empaquetada(codigo: Path | None = None) -> bool:
    """¿El código está dentro de un `.app`?"""
    codigo = codigo or CODIGO
    return any(p.suffix == ".app" for p in (codigo, *codigo.parents))


def soporte_de_aplicaciones(inicio: str | None = None) -> Path:
    """`~/Library/Application Support/Ausschreibungsradar`.

    Se compone a mano en lugar de preguntarle a macOS porque este módulo lo importa
    todo el proyecto y tiene que seguir funcionando en el Linux de la integración
    continua, donde no hay nada de esto.
    """
    return Path(inicio or Path.home()) / "Library" / "Application Support" / NOMBRE_APP


def raiz_datos(entorno: dict | None = None, codigo: Path | None = None) -> Path:
    """Dónde se escribe. Las tres reglas del docstring del módulo, en orden."""
    entorno = entorno if entorno is not None else os.environ
    elegido = (entorno.get(VAR_ENTORNO) or "").strip()
    if elegido:
        return Path(elegido).expanduser()
    if empaquetada(codigo):
        return soporte_de_aplicaciones()
    return codigo or CODIGO


DATOS = raiz_datos()

# Las rutas concretas. Cada módulo las importa de aquí en lugar de recomponerlas, que
# es lo que hacía que hubiera nueve sitios donde cambiarlas.
DIR_DATOS = DATOS / "data"
DIR_CONFIG = DATOS / "config"

BD = DIR_DATOS / "ausschreibungen.db"
CACHE = DIR_DATOS / "cache"
PERFILES = DIR_CONFIG / "suchprofile.json"

# Solo lectura, desde donde esté el código.
WEB = CODIGO / "web"
CERTIFICADOS = CODIGO / "config" / "certs" / "ca-bundle.pem"
PERFILES_PLANTILLA = CODIGO / "config" / "suchprofile.beispiel.json"
ENTRADA_CLI = CODIGO / "radar.py"


def preparar() -> None:
    """Crea las carpetas de escritura. Barato e idempotente.

    Hace falta porque con los datos fuera del proyecto ya no existe el `config/` que
    venía en el repositorio: la primera vez que alguien abre la app empaquetada, ese
    directorio no está y `suchprofile.json` no se podría escribir.
    """
    DIR_DATOS.mkdir(parents=True, exist_ok=True)
    DIR_CONFIG.mkdir(parents=True, exist_ok=True)
