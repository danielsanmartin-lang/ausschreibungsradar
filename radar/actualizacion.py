"""Traer una versión nueva del código sin salir de la aplicación.

Hoy una versión nueva se distribuye mandando un zip por correo, y no hay manera de
saber qué versión tiene cada compañero ni de pedirle que actualice. Esto lo convierte
en un botón.

Es más sencillo aquí que en un proyecto normal por dos razones. Una: el proyecto no
tiene ni una dependencia externa —todo es biblioteca estándar—, así que actualizar es
literalmente sustituir ficheros, sin pip, ni entornos, ni versiones que resolver. Dos:
lo que de verdad rompe una actualización no es traer el código nuevo, es que el código
nuevo se encuentre una base de datos vieja, y de eso ya se encarga `db.migrar()` en cada
arranque.

Lo que NO se toca, nunca:

- `data/`, donde viven la base, el triaje y las notas de cada uno.
- `config/suchprofile.json`, que es lo que cada persona ajusta desde la aplicación. Si
  algún día hay que añadir términos nuevos por defecto, se hará fusionando con un
  marcador de versión, como las migraciones de la base; sobrescribirlo le borraría a un
  compañero el trabajo de meses.

Y una advertencia que conviene tener presente: esto es, por diseño, ejecución de código
descargado de internet. Quien controle el repositorio controla el equipo de quien
actualiza. Por eso solo se acepta la release del repositorio de abajo, por HTTPS y con
el almacén de certificados propio del proyecto, y se comprueba el SHA-256 cuando las
notas de la release lo publican.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from . import net, rutas
from . import __version__

RAIZ = rutas.CODIGO
REPO = "danielsanmartin-lang/ausschreibungsradar"
API_ULTIMA = f"https://api.github.com/repos/{REPO}/releases/latest"

# Lo único que se sustituye. Es una lista blanca y no una negra a propósito: con una
# lista de exclusiones, cualquier carpeta nueva que apareciera en el repositorio pasaría
# a sobrescribir lo que hubiera en su sitio sin que nadie lo hubiera decidido.
REEMPLAZABLES = (
    "radar",
    "web",
    "tests",
    "docs",
    "herramientas",
    "radar.py",
    "start.command",
    "README.md",
    # El almacén de certificados sí se actualiza: caduca, y si se queda atrás el
    # Bekanntmachungsservice deja de validar. `config/suchprofile.json`, en cambio, no se
    # toca jamás.
    "config/certs",
    # El fuente del envoltorio de macOS y su binario prefabricado. Lo que NO se sustituye
    # es «Ausschreibungsradar.app»: un bundle no se puede cambiar por debajo de sí
    # mismo mientras se está ejecutando, y no hace falta, porque se reconstruye desde
    # esto. La propia app detecta al abrirse que su versión ya no cuadra con
    # `radar/__init__.py` y ofrece rehacerse.
    "macos",
)

# Sin estas tres no hay aplicación: si el zip descargado no las trae, no es lo que
# esperábamos y no se sustituye nada.
#
# `macos` NO está aquí a propósito, aunque esté en REEMPLAZABLES: la ventana nativa es
# una comodidad y `start.command` sigue haciendo lo mismo sin ella. Exigirla haría que
# una release publicada sin el binario prefabricado se negara a instalarse para todo el
# mundo, incluido quien nunca ha usado la app.
IMPRESCINDIBLES = ("radar", "web", "radar.py")

log = logging.getLogger(__name__)


def _tupla(version: str) -> tuple[int, ...]:
    """«v1.4.2» -> (1, 4, 2). Lo que no sea número cuenta como 0.

    Comparar tuplas y no cadenas es lo que evita que «1.10» se considere anterior a
    «1.9», que es el fallo clásico de comparar versiones como texto.
    """
    partes = []
    for trozo in version.strip().lstrip("vV").split("."):
        digitos = "".join(c for c in trozo if c.isdigit())
        partes.append(int(digitos) if digitos else 0)
    return tuple(partes) or (0,)


def _sha256(ruta: Path) -> str:
    h = hashlib.sha256()
    with ruta.open("rb") as fh:
        while trozo := fh.read(1 << 20):
            h.update(trozo)
    return h.hexdigest()


def _sha_publicado(notas: str) -> str | None:
    """Busca un SHA-256 en las notas de la release.

    Es opcional a propósito: una release sin él se instala igual, porque HTTPS contra el
    repositorio correcto ya es la defensa principal. Cuando está, se comprueba, y así
    una descarga corrompida a medio camino no llega a sustituir nada.
    """
    for palabra in (notas or "").replace("`", " ").split():
        limpia = palabra.strip().lower()
        if len(limpia) == 64 and all(c in "0123456789abcdef" for c in limpia):
            return limpia
    return None


def comprobar(timeout: int = 15) -> dict:
    """¿Hay publicada una versión más nueva que la instalada?

    Devuelve siempre un diccionario y nunca lanza: esto lo llama la interfaz al abrirse,
    y quedarse sin internet un rato no es motivo para estropear la pantalla.
    """
    respuesta = {
        "version_actual": __version__,
        "version_nueva": None,
        "hay_nueva": False,
        "notas": "",
        "url_zip": None,
        # Para la app empaquetada: de dónde se baja el .app nuevo, y la página de la
        # release como recurso si la release no trae el adjunto.
        "url_app": None,
        "url_release": None,
        "empaquetada": rutas.empaquetada(),
        "error": None,
    }
    try:
        datos = net.descargar_json(API_ULTIMA, timeout=timeout, intentos=2)
    except net.ErrorRed as exc:
        if getattr(exc, "codigo", None) == 404:
            respuesta["error"] = (
                "GitHub meldet, dass es kein veröffentlichtes Release gibt (oder das Repository "
                "ist privat und diese Kopie hat keine Zugangsdaten dafür)."
            )
        else:
            respuesta["error"] = f"GitHub konnte nicht gefragt werden: {exc}"
        return respuesta
    except (ValueError, TypeError) as exc:  # JSON inesperado
        respuesta["error"] = f"GitHub hat etwas Unverständliches geantwortet: {exc}"
        return respuesta

    if not isinstance(datos, dict) or not datos.get("tag_name"):
        respuesta["error"] = "Die Antwort von GitHub enthält keine Version."
        return respuesta

    respuesta["version_nueva"] = datos["tag_name"]
    respuesta["notas"] = datos.get("body") or ""
    respuesta["url_zip"] = datos.get("zipball_url")
    respuesta["url_app"] = _app_publicada(datos)
    respuesta["url_release"] = datos.get("html_url")
    respuesta["hay_nueva"] = _tupla(datos["tag_name"]) > _tupla(__version__)
    return respuesta


def _app_publicada(datos: dict) -> str | None:
    """La URL del `.app` comprimido que viaje como fichero adjunto de la release.

    Es lo que necesita la app empaquetada, que no puede actualizarse sustituyendo
    ficheros: su código va dentro del bundle. Se busca por nombre y no por posición
    porque una release puede llevar varios adjuntos.
    """
    for adjunto in datos.get("assets") or []:
        nombre = (adjunto.get("name") or "").lower()
        if nombre.endswith(".zip") and "radar" in nombre:
            return adjunto.get("browser_download_url")
    return None


def _raiz_del_zip(extraido: Path) -> Path:
    """GitHub empaqueta todo dentro de una carpeta «repo-sha», no en la raíz."""
    hijos = [h for h in extraido.iterdir() if h.is_dir()]
    return hijos[0] if len(hijos) == 1 else extraido


def _version_del_arbol(raiz: Path) -> str | None:
    """Lee `__version__` del código descargado sin importarlo.

    Importar el módulo nuevo dentro del proceso viejo mezclaría dos versiones del
    paquete en memoria; leer la línea es suficiente y no ejecuta nada de lo descargado.
    """
    init = raiz / "radar" / "__init__.py"
    if not init.exists():
        return None
    for linea in init.read_text(encoding="utf-8").splitlines():
        if linea.startswith("__version__"):
            return linea.split("=", 1)[1].strip().strip("\"'")
    return None


def _borrar(ruta: Path) -> None:
    if ruta.is_dir() and not ruta.is_symlink():
        shutil.rmtree(ruta, ignore_errors=True)
    else:
        ruta.unlink(missing_ok=True)


def aplicar(timeout: int = 600) -> dict:
    """Descarga la última release y sustituye el código. Devuelve qué ha pasado.

    El orden importa: se descarga y se verifica TODO en un temporal, y solo cuando está
    comprobado se mueve a su sitio. Descomprimir encima de la carpeta viva dejaría, si
    algo falla a mitad, una instalación mezclada, que es bastante peor que una versión
    vieja. Y lo que se sustituye se guarda como «.anterior» para poder volver atrás.
    """
    from . import busqueda

    if rutas.empaquetada():
        # Sustituir ficheros aquí sería escribir DENTRO del .app, y eso invalida la
        # firma y no se puede hacer si la app está en /Applications, que no es del
        # usuario. Un programa tampoco puede reemplazarse a sí mismo mientras corre.
        # En Mac esto se resuelve como siempre: se baja la versión nueva y se arrastra
        # encima. Aquí solo se dice, con la URL a mano.
        info = comprobar()
        if info.get("error"):
            return {"ok": False, "mensaje": info["error"]}
        if not info["hay_nueva"]:
            return {"ok": True, "sin_cambios": True, "mensaje":
                    f"Du hast bereits die neueste Version ({info['version_actual']})."}
        destino = info.get("url_app") or info.get("url_release")
        return {
            "ok": False,
            "hay_que_descargar": True,
            "url": destino,
            "version_nueva": info["version_nueva"],
            "mensaje": (
                f"Veröffentlicht ist Version {info['version_nueva']}. Diese Kopie trägt das "
                "Programm in der Anwendung, sie wird also aktualisiert, indem man die neue "
                "Version lädt und über die alte zieht, wie bei jedem anderen Programm. "
                ""
                "Deine Daten liegen außerhalb der Anwendung und bleiben unberührt."
            ),
        }

    activa = busqueda.en_marcha()
    if activa:
        return {"ok": False, "mensaje": (
            f"Seit {activa.get('iniciada', '?')} läuft ein Abruf. "
            "Den Code unter einem stundenlangen Ladevorgang auszutauschen heißt, sich "
            "Ärger einzuhandeln: warte, bis er fertig ist."
        )}

    info = comprobar()
    if info["error"]:
        return {"ok": False, "mensaje": info["error"]}
    if not info["hay_nueva"]:
        return {"ok": True, "sin_cambios": True, "mensaje":
                f"Du hast bereits die neueste Version ({info['version_actual']})."}
    if not info["url_zip"]:
        return {"ok": False, "mensaje": "Das Release enthält keine Datei zum Laden."}

    temporal = Path(tempfile.mkdtemp(prefix="radar-actualizacion-"))
    try:
        zip_nuevo = temporal / "nueva.zip"
        log.info("Version %s wird geladen …", info["version_nueva"])
        # Dos intentos y no los cuatro por defecto: `aplicar_en_subproceso` mata este
        # proceso a los 900 s, y con cuatro pasadas de 600 s de timeout el peor caso se
        # come el plazo y muere a mitad. Un zipball de unos pocos MB no necesita más.
        net.descargar_a_fichero(info["url_zip"], zip_nuevo, timeout=timeout, intentos=2)

        esperado = _sha_publicado(info["notas"])
        if esperado:
            real = _sha256(zip_nuevo)
            if real != esperado:
                return {"ok": False, "mensaje": (
                    "Die geladene Datei stimmt nicht mit dem in den Release-Notes "
                    f"veröffentlichten SHA-256 überein. Es wurde nichts geändert.\n"
                    f"  erwartet: {esperado}\n  "
                    f"geladen: {real}"
                )}

        if not zipfile.is_zipfile(zip_nuevo):
            return {"ok": False, "mensaje": "Das Geladene ist kein ZIP. Es wird nichts geändert."}
        destino = temporal / "nuevo"
        with zipfile.ZipFile(zip_nuevo) as zf:
            zf.extractall(destino)
        arbol = _raiz_del_zip(destino)

        faltan = [n for n in IMPRESCINDIBLES if not (arbol / n).exists()]
        if faltan:
            return {"ok": False, "mensaje":
                    f"Im ZIP fehlt {', '.join(faltan)}. Es wird nichts geändert."}

        version_real = _version_del_arbol(arbol)
        if version_real is None or _tupla(version_real) != _tupla(info["version_nueva"]):
            return {"ok": False, "mensaje": (
                f"Das Release-Tag sagt {info['version_nueva']}, der enthaltene Code "
                f"sagt aber {version_real}. Es wird nichts geändert."
            )}

        cambiados, hechos = [], []
        try:
            for rel in REEMPLAZABLES:
                nuevo = arbol / rel
                if not nuevo.exists():
                    continue
                actual = RAIZ / rel
                anterior = actual.with_name(actual.name + ".anterior")
                _borrar(anterior)
                if actual.exists():
                    actual.rename(anterior)
                # Se anota ANTES de mover, no después: si el movimiento falla, este es
                # precisamente el que hay que devolver a su sitio, y anotándolo después
                # se quedaba fuera del deshacer con la carpeta ya renombrada. Resultado:
                # `web/` desaparecía del proyecto.
                hechos.append((actual, anterior))
                # `config/certs` cuelga de una carpeta que podría no existir en una
                # instalación vieja; sin esto el movimiento fallaría por el padre.
                actual.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(nuevo), str(actual))
                cambiados.append(rel)
        except OSError as exc:
            # Deshacer en orden inverso: mejor quedarse en la versión vieja entera que
            # en una mitad de cada.
            for actual, anterior in reversed(hechos):
                _borrar(actual)
                if anterior.exists():
                    anterior.rename(actual)
            return {"ok": False, "mensaje":
                    f"„{rel}“ konnte nicht ersetzt werden: {exc}. Es bleibt, wie es war."}

        log.info("Actualizado a %s: %s", info["version_nueva"], ", ".join(cambiados))
        return {
            "ok": True,
            "version_nueva": info["version_nueva"],
            "cambiados": cambiados,
            "mensaje": (
                f"Auf Version {info['version_nueva']} aktualisiert. Schließe die Anwendung "
                "und öffne sie mit start.command erneut. Deine Datenbank, dein "
                "Bearbeitungsstand und deine Suchbegriffe wurden nicht angerührt."
            ),
        }
    except net.ErrorRed as exc:
        return {"ok": False, "mensaje": f"Laden nicht möglich: {exc}"}
    except (OSError, zipfile.BadZipFile) as exc:
        return {"ok": False, "mensaje": f"Die Aktualisierung ist fehlgeschlagen: {exc}. Es wird nichts geändert."}
    finally:
        shutil.rmtree(temporal, ignore_errors=True)


def aplicar_en_subproceso(timeout: int = 900) -> dict:
    """Lanza `radar.py actualizar` y espera a que termine.

    Va en un proceso aparte a propósito, por el mismo motivo que la ingesta: quien
    sustituye el código no debería ser el proceso que está ejecutando ese código. Y
    reutilizar la CLI deja un único camino de ejecución, en vez de una versión para el
    botón y otra para la terminal.
    """
    try:
        r = subprocess.run(
            [sys.executable, "-u", str(RAIZ / "radar.py"), "actualizar", "--json"],
            cwd=str(RAIZ), capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "mensaje": f"Die Aktualisierung konnte nicht gestartet werden: {exc}"}

    for linea in reversed((r.stdout or "").splitlines()):
        try:
            return json.loads(linea)
        except ValueError:
            continue
    return {"ok": False, "mensaje":
            (r.stderr or r.stdout or "Die Aktualisierung hat nichts gemeldet.").strip()[-2000:]}
