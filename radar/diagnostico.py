"""Autodiagnóstico: qué está roto y qué hay que hacer para arreglarlo.

Existe porque media sección «Si algo va mal» del README consistía en decirle a alguien
por dónde mirar: si el bundle de certificados sigue vigente, si la tarea de cada mañana
está cargada de verdad, si la caché tiene un ZIP a medias, si la base viene de una
versión anterior. Todo eso se puede preguntar en un segundo y nadie debería tener que
saber dónde.

Dos reglas que dan forma a este módulo:

1. **No toca nada.** La base se abre en SOLO LECTURA. `db.conectar()` no sirve aquí: crea
   la base si no existe y ejecuta `migrar()`, que puede recalcular claves de grupo y
   lanzar un VACUUM de varios minutos sobre una base de 3 GB. Un comando que promete
   diagnosticar no puede arreglar cosas por su cuenta ni tardar minutos.
2. **Toda comprobación que no sale «ok» trae remedio.** Un diagnóstico que dice «algo va
   mal» y deja al usuario buscando en el README no vale para nada.

La lógica vive aquí y no en `radar.py` por el mismo motivo que `consultas.py` está
separado de `server.py`: para poder probarla sin montar el andamio. El CLI, además, no se
puede importar desde los tests, porque `import radar` choca con el paquete `radar/`.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import sqlite3
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import busqueda, consultas, db, matching, net, programar, rutas

RAIZ = Path(__file__).resolve().parent.parent

# De menos a más grave. `omitida` no es un adorno: sin ella, una comprobación que no se
# ha ejecutado —la integridad y la versión publicada son opt-in— habría que contarla
# como «ok», que es mentir, o como «aviso», que es dar una alarma falsa.
ESTADOS = ("ok", "omitida", "aviso", "error")

VERSION_MINIMA_PYTHON = (3, 9)

# Umbrales. Como constantes de módulo para poder redirigirlos en los tests en lugar de
# fabricar ficheros de 60 MB o llenar el disco.
GB_MINIMO_LIBRE = 1.0        # por debajo de esto SQLite no puede ni escribir el WAL
GB_PARA_CARGA_INICIAL = 10.0  # medido aquí: 5,3 GB de ZIP en la caché + 3,3 GB de base
MB_MAXIMO_LOG = 50           # launchd abre ingest.log en append y nadie lo rota
CA_MINIMAS = 80              # el bundle completo trae más de 140; con menos está recortado


@dataclass
class Comprobacion:
    nombre: str
    estado: str
    mensaje: str
    remedio: str = ""
    datos: dict = field(default_factory=dict)


def _miles(n: int) -> str:
    """Los miles con punto. Va número a número, y no con un `replace` sobre la frase
    entera, porque eso le cambiaba también la coma decimal a «3,4 GB»."""
    return f"{n:,}".replace(",", ".")


def _mb(n: float) -> str:
    return f"{_miles(round(n / 1e6))} MB"


def _gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB".replace(".", ",")


def _sin_bloques(info) -> bool:
    """¿El fichero declara un tamaño que no está en el disco?

    iCloud vacía los ficheros grandes de las carpetas sincronizadas y deja el hueco: el
    directorio sigue diciendo 1,8 GB y detrás no hay ni un bloque. Abrirlo dispara la
    descarga, y este comando promete no hacer nada.

    Es una función con nombre y no dos líneas dentro del bucle para que se pueda probar sin
    falsear `os.stat`: el código llama a `Path.stat()`, que en Python 3.9 no pasa por el
    `os` de este módulo, así que un test que parchee ahí funciona en 3.14 y falla en 3.9.
    Lo cazó el CI.

    `st_blocks` no existe en todas las plataformas —Windows no lo trae— y ahí la respuesta
    correcta es «no lo sé, no toques nada».
    """
    bloques = getattr(info, "st_blocks", None)
    return bool(info.st_size) and bloques == 0


def _abrir_solo_lectura(bd: Path) -> sqlite3.Connection:
    """Abre la base sin poder escribirla ni crearla.

    `mode=ro` es lo que garantiza las dos cosas: sobre una instalación nueva no deja un
    `ausschreibungen.db` vacío detrás, y sobre una en uso no puede estropear nada aunque haya una
    ingesta escribiendo al mismo tiempo. El precedente está en start.command.

    Un lector de una base en WAL sí crea los `-wal`/`-shm` si no están: es la memoria
    compartida que necesita cualquier lector y no se puede evitar. No se usa
    `immutable=1` para librarse de ellos porque eso declara que el fichero no cambia, y
    mientras corre una ingesta es falso.
    """
    con = sqlite3.connect(f"file:{bd}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# --- comprobaciones --------------------------------------------------------


def version_de_python(version: tuple | None = None) -> Comprobacion:
    """Hoy esto solo se comprueba en start.command, no desde Python."""
    v = tuple(version or sys.version_info[:3])
    texto = ".".join(str(p) for p in v)
    if v < VERSION_MINIMA_PYTHON:
        return Comprobacion(
            "Python", "error",
            f"du hast {texto}, nötig ist "
            f"{'.'.join(str(p) for p in VERSION_MINIMA_PYTHON)} o superior",
            "Actualízalo desde https://www.python.org/downloads/macos",
            {"version": texto},
        )
    return Comprobacion("Python", "ok", texto, datos={"version": texto,
                                                      "ejecutable": sys.executable})


def bundle_de_certificados() -> Comprobacion:
    """El fallo más probable de todo el proyecto y el que peor se explica solo.

    Son las cuatro comprobaciones que ya hace `tests/test_net.py`: que el fichero está,
    que trae las raíces de la FNMT y de Izenpe, que no está recortado y que el contexto
    verifica de verdad. Sin las raíces esperadas, el servicio falla el handshake con un mensaje que
    no se parece en nada a «faltan certificados».
    """
    try:
        contexto = net.contexto_ssl()
    except net.ErrorRed as exc:
        # `contexto_ssl` ya redacta el problema y el remedio.
        return Comprobacion("Zertifikate", "error", str(exc).replace("\n", " "),
                            "python3 herramientas/regenerar_ca_bundle.py")
    raices = len(net._huellas_del_bundle(net.BUNDLE_CA))
    cargadas = contexto.cert_store_stats().get("x509_ca", 0)
    if raices < CA_MINIMAS:
        return Comprobacion(
            "Zertifikate", "aviso",
            f"das Bundle hat nur {raices} Wurzeln: es wirkt beschnitten, und TED oder "
            "andere Quellen können fehlschlagen, auch wenn der Bekanntmachungsservice geht",
            "python3 herramientas/regenerar_ca_bundle.py",
            {"raices": raices},
        )
    return Comprobacion(
        "Zertifikate", "ok",
        f"{raices} Wurzeln im Bundle, inklusive der erwarteten ({cargadas} geladen)",
        datos={"raices": raices, "cargadas": cargadas},
    )


def espacio_en_disco(ruta: Path | None = None) -> Comprobacion:
    """Una carga inicial completa pide unos 9 GB entre la caché y la base."""
    uso = shutil.disk_usage(ruta or RAIZ)
    libres_gb = uso.free / 1e9
    datos = {"libres": uso.free, "total": uso.total}
    if libres_gb < GB_MINIMO_LIBRE:
        return Comprobacion(
            "Speicherplatz", "error",
            f"nur {_gb(uso.free)} frei: damit kann SQLite nicht einmal schreiben",
            "Schaff Platz; der Archiv-Cache kann ohne Datenverlust gelöscht werden "
            "mit: python3 radar.py estado --limpiar-cache",
            datos,
        )
    if libres_gb < GB_PARA_CARGA_INICIAL:
        return Comprobacion(
            "Speicherplatz", "aviso",
            f"{_gb(uso.free)} frei, eine vollständige Erstbefüllung braucht rund "
            f"{GB_PARA_CARGA_INICIAL:.0f} GB für Cache und Datenbank",
            "Wenn das Archiv schon da ist, passiert nichts; zum Aufbauen schaff "
            "vorher Platz",
            datos,
        )
    return Comprobacion("Speicherplatz", "ok", f"{_gb(uso.free)} frei", datos=datos)


def base_de_datos(bd: Path | str | None = None) -> Comprobacion:
    """Existencia, tamaño y recuentos. Sin `MAX(visto_ultima_vez)`: 14 s medidos."""
    bd = Path(bd) if bd else db.BD_POR_DEFECTO
    if not bd.exists():
        return Comprobacion(
            "Datenbank", "aviso", f"{bd.name} gibt es noch nicht",
            "Wird beim ersten Start automatisch angelegt: Doppelklick auf start.command",
            {"ruta": str(bd)},
        )
    try:
        con = _abrir_solo_lectura(bd)
    except sqlite3.Error as exc:
        return Comprobacion("Datenbank", "error", f"nicht zu öffnen: {exc}",
                            "Wenn es sich nicht erholt, lösche den Ordner data/ und starte "
                            "start.command erneut (der Bearbeitungsstand geht verloren)", {"ruta": str(bd)})
    try:
        with con:
            pagina = con.execute("PRAGMA page_size").fetchone()[0]
            paginas = con.execute("PRAGMA page_count").fetchone()[0]
            diario = con.execute("PRAGMA journal_mode").fetchone()[0]
            licitaciones = con.execute("SELECT COUNT(*) FROM licitaciones").fetchone()[0]
            coincidencias = con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
            revisiones = con.execute("SELECT COUNT(*) FROM revisiones").fetchone()[0]
            versiones = con.execute(
                "SELECT COUNT(*) FROM licitaciones_versiones").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        # Un fichero que no es SQLite, o que lo era: el mensaje de la excepción es
        # legible ("file is not a database") y no hace falta adornarlo.
        return Comprobacion(
            "Datenbank", "error", f"{bd.name} ist nicht lesbar: {exc}",
            "Lösche data/ausschreibungen.db und starte start.command erneut; das Archiv "
            "wird aus dem Cache neu aufgebaut, ohne erneut zu laden", {"ruta": str(bd)},
        )
    finally:
        con.close()

    tamano = pagina * paginas
    wal = bd.with_name(bd.name + "-wal")
    datos = {
        "ruta": str(bd), "bytes": tamano, "licitaciones": licitaciones,
        "coincidencias": coincidencias, "revisiones": revisiones,
        "versiones": versiones, "journal_mode": diario,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
    }
    mensaje = (f"{_gb(tamano)} · {_miles(licitaciones)} Bekanntmachungen · "
               f"{_miles(coincidencias)} Treffer · {_miles(revisiones)} bearbeitet")
    if not coincidencias:
        return Comprobacion(
            "Datenbank", "aviso", mensaje + " — der Posteingang ist leer",
            "python3 radar.py ingest --primera-carga", datos,
        )
    return Comprobacion("Datenbank", "ok", mensaje, datos=datos)


def integridad_de_la_base(bd: Path | str | None = None, *,
                          ejecutar: bool = False) -> Comprobacion:
    """Verificación de integridad, solo si se pide.

    Medido sobre la base real de 3,3 GB: `quick_check` 49,6 s y `integrity_check` 17,0 s
    con la caché del sistema ya caliente. Las dos leen el fichero entero, así que la
    diferencia la manda el disco, y ninguna cabe en un comando que promete ser
    instantáneo. Se usa `quick_check` porque lo que añade `integrity_check` —la
    verificación cruzada entre índices y tablas— no cambia el remedio: si algo sale mal,
    la respuesta es restaurar o rehacer `data/`.

    Se comprueba además que el índice de texto no se haya desincronizado, porque eso no
    da ningún error: simplemente la caja de búsqueda deja de encontrar cosas.
    """
    bd = Path(bd) if bd else db.BD_POR_DEFECTO
    if not ejecutar:
        return Comprobacion("Integrität", "omitida",
                            "nicht geprüft (liest die ganze Datenbank, fast eine Minute)",
                            "python3 radar.py doctor --integridad")
    if not bd.exists():
        return Comprobacion("Integrität", "omitida", "keine Datenbank zu prüfen")
    try:
        con = _abrir_solo_lectura(bd)
    except sqlite3.Error as exc:
        return Comprobacion("Integrität", "error", f"nicht zu öffnen: {exc}")
    try:
        with con:
            veredicto = con.execute("PRAGMA quick_check(1)").fetchone()[0]
            fichas = con.execute("SELECT COUNT(*) FROM licitaciones").fetchone()[0]
            indexadas = con.execute("SELECT COUNT(*) FROM licitaciones_fts").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        return Comprobacion("Integrität", "error", f"die Datenbank ist beschädigt: {exc}",
                            "Lösche data/ausschreibungen.db und baue mit start.command neu auf")
    finally:
        con.close()

    datos = {"quick_check": veredicto, "licitaciones": fichas, "indexadas": indexadas}
    if veredicto != "ok":
        return Comprobacion("Integrität", "error", f"quick_check dice: {veredicto}",
                            "Lösche data/ausschreibungen.db und baue mit start.command neu auf", datos)
    if fichas != indexadas:
        return Comprobacion(
            "Integrität", "error",
            f"der Textindex hat {_miles(indexadas)} Datensätze und die Tabelle "
            f"{_miles(fichas)}: die Suche findet nicht alles",
            "python3 radar.py ingest --reiniciar-cursor", datos,
        )
    return Comprobacion(
        "Integrität", "ok",
        f"unbeschädigt und der Textindex stimmt ({_miles(fichas)} Datensätze)", datos=datos,
    )


def migraciones_pendientes(bd: Path | str | None = None) -> Comprobacion:
    """¿Le falta a la base algo que la versión instalada ya espera?

    No se ejecuta nada: se dice. Las migraciones se aplican solas al arrancar, y saber
    de antemano que la próxima vez habrá un rato de espera es justo lo que evita pensar
    que se ha colgado.
    """
    bd = Path(bd) if bd else db.BD_POR_DEFECTO
    if not bd.exists():
        return Comprobacion("Migrationen", "omitida", "noch keine Datenbank")
    try:
        con = _abrir_solo_lectura(bd)
    except sqlite3.Error as exc:
        return Comprobacion("Migrationen", "error", f"nicht zu öffnen: {exc}")
    esperadas = {
        "version_clave_grupo": db.VERSION_CLAVE_GRUPO,
        "version_texto_reglas": db.VERSION_TEXTO_REGLAS,
        "version_snapshot": db.VERSION_SNAPSHOT,
    }
    pendientes = []
    try:
        with con:
            vacia = not con.execute("SELECT COUNT(*) FROM licitaciones").fetchone()[0]
            for clave, esperada in esperadas.items():
                fila = con.execute(
                    "SELECT valor FROM preferencias WHERE clave = ?", (clave,)
                ).fetchone()
                if not vacia and (fila["valor"] if fila else None) != esperada:
                    pendientes.append(clave)
            for tabla, columnas in db.COLUMNAS_NUEVAS.items():
                existentes = {
                    f["name"] for f in con.execute(f"PRAGMA table_info({tabla})")
                }
                pendientes += [f"{tabla}.{c}" for c, _ in columnas
                               if c not in existentes]
    except sqlite3.DatabaseError as exc:
        return Comprobacion("Migrationen", "error", f"nicht lesbar: {exc}")
    finally:
        con.close()

    if pendientes:
        return Comprobacion(
            "Migrationen", "aviso",
            "es stehen Schemaänderungen aus: " + ", ".join(pendientes),
            "Sie werden beim nächsten Start automatisch angewandt; die Gruppenschlüssel "
            "neu zu berechnen dauert ein paar Minuten und verliert nichts",
            {"pendientes": pendientes},
        )
    return Comprobacion("Migrationen", "ok", "die Datenbank ist aktuell")


def terminos_de_busqueda(ruta: Path | str | None = None) -> Comprobacion:
    """Valida `config/suchprofile.json` sin escribirlo.

    Se lee con `json.loads` a pelo y NO con `matching.leer_fichero_perfiles`, que llama a
    `preparar_perfiles` y crea el fichero copiando el ejemplo. Un diagnóstico no puede
    dejar creado lo que ha ido a comprobar que existe.
    """
    ruta = Path(ruta) if ruta else matching.PERFILES_POR_DEFECTO
    if not ruta.exists():
        return Comprobacion(
            "Suchbegriffe", "aviso", f"{ruta.name} gibt es noch nicht",
            "Wird beim Start aus config/suchprofile.beispiel.json angelegt",
            {"ruta": str(ruta)},
        )
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Comprobacion(
            "Suchbegriffe", "error", f"{ruta.name} ist nicht lesbar: {exc}",
            f"Hol die vorherige Kopie zurück: cp config/suchprofile.vorherige.json {ruta}",
            {"ruta": str(ruta)},
        )
    if isinstance(datos, dict):
        datos = datos.get("profile", [])
    try:
        perfiles = matching.validar_perfiles(datos)
    except (ValueError, TypeError) as exc:
        return Comprobacion(
            "Suchbegriffe", "error", str(exc),
            "Korrigiere es im Reiter „Suchbegriffe“ oder hole "
            "config/suchprofile.vorherige.json", {"ruta": str(ruta)},
        )
    activos = matching.solo_activos(perfiles)
    terminos = sum(len(p.starke_begriffe) + len(p.schwache_begriffe) for p in activos)
    avisos = matching.avisos_perfiles(perfiles)
    resumen = (f"{len(activos)} aktive Profile von {len(perfiles)} · "
               f"{terminos} Begriffe")
    if avisos:
        return Comprobacion("Suchbegriffe", "aviso",
                            resumen + " — " + avisos[0],
                            "Klick „Vorschau“ vor dem Speichern, um es zu messen",
                            {"avisos": avisos})
    return Comprobacion("Suchbegriffe", "ok", resumen,
                        datos={"activos": len(activos), "terminos": terminos})


def salud_de_las_fuentes(bd: Path | str | None = None) -> Comprobacion:
    """La última ingesta de cada fuente, con el mismo criterio que la bandeja."""
    bd = Path(bd) if bd else db.BD_POR_DEFECTO
    if not bd.exists():
        return Comprobacion("Quellen", "omitida", "noch keine Datenbank")
    try:
        con = _abrir_solo_lectura(bd)
    except sqlite3.Error as exc:
        return Comprobacion("Quellen", "error", f"nicht zu öffnen: {exc}")
    try:
        with con:
            fuentes = consultas.salud(con, busqueda.en_marcha() is not None)
    except sqlite3.DatabaseError as exc:
        return Comprobacion("Quellen", "error", f"nicht lesbar: {exc}")
    finally:
        con.close()

    if not fuentes:
        return Comprobacion("Quellen", "aviso", "es wurde noch kein Abruf ausgeführt",
                            "python3 radar.py ingest --primera-carga")
    roto = [f["fuente"] for f in fuentes if f["aviso"] == "der letzte Abruf ist fehlgeschlagen"]
    vacio = [f["fuente"] for f in fuentes if f["aviso"] and f["fuente"] not in roto]
    datos = {"fuentes": [{k: f[k] for k in ("fuente", "ok", "vistos", "nuevos",
                                            "iniciado_en", "aviso")} for f in fuentes]}
    if roto:
        return Comprobacion(
            "Quellen", "error", "der letzte Abruf schlug fehl bei: " + ", ".join(roto),
            "Details unter: python3 radar.py estado", datos,
        )
    if vacio:
        return Comprobacion(
            "Quellen", "aviso", "keine Datensätze beim letzten Abruf: " + ", ".join(vacio),
            "Ein Tag ohne Veröffentlichungen kann normal sein; wenn es sich wiederholt, sieh in "
            "python3 radar.py estado", datos,
        )
    return Comprobacion("Quellen", "ok",
                        f"{len(fuentes)} Quellen, der letzte Abruf lief überall gut",
                        datos=datos)


def ingesta_en_marcha() -> Comprobacion:
    """Si hay una descarga corriendo, y si había un cerrojo de un proceso muerto.

    La foto del cerrojo se toma ANTES de preguntar, porque `busqueda.en_marcha()` limpia
    los huérfanos por su cuenta: sin ella, «no había cerrojo» y «había uno de un proceso
    que ya no existe y lo acabo de limpiar» serían indistinguibles.
    """
    habia = busqueda.CERROJO.exists()
    activa = busqueda.en_marcha()
    if activa:
        return Comprobacion(
            "Abruf", "ok",
            f"seit {activa.get('iniciada', '?')} läuft ein Abruf "
            f"(pid {activa.get('pid', '?')})",
            datos={"en_marcha": True, **{k: activa.get(k) for k in ("pid", "iniciada")}},
        )
    if habia:
        return Comprobacion(
            "Abruf", "aviso",
            "es gab eine Sperre von einem Abruf, den es nicht mehr gibt; sie wurde entfernt",
            "Nichts zu tun: passiert, wenn das Terminal mittendrin geschlossen wird. Wenn der Abruf "
            "unvollständig blieb, starte ihn erneut und er setzt dort fort, wo er war",
            {"cerrojo_huerfano": True},
        )
    return Comprobacion("Abruf", "ok", "kein Abruf läuft",
                        datos={"en_marcha": False})


def cache_de_historicos(dir_cache: Path | None = None) -> Comprobacion:
    """Los ZIP del histórico: cuánto ocupan y si alguno no sirve.

    Antes de abrir un fichero se compara lo que ocupa de verdad con lo que dice ocupar:
    iCloud vacía los ficheros grandes de las carpetas sincronizadas y deja el hueco, y
    abrir uno de esos dispara la descarga entera otra vez. Desde un comando que promete
    no hacer nada, eso es lo último que puede pasar.

    No se vigila la caducidad: el servicio alemán solo se cachea por tramos ya CERRADOS
    —un mes que terminó, un día que terminó—, y esos no vuelven a cambiar nunca.
    """
    dir_cache = Path(dir_cache) if dir_cache else rutas.CACHE
    zips = sorted(dir_cache.glob("*.zip"))
    parciales = sorted(dir_cache.glob("*.zip.parcial"))
    if not zips and not parciales:
        return Comprobacion("Archiv-Cache", "ok", "leer",
                            datos={"ficheros": 0, "bytes": 0})

    total = 0
    ilegibles, sin_bytes = [], []
    for f in zips:
        info = f.stat()
        total += info.st_size
        if _sin_bloques(info):
            sin_bytes.append(f.name)
            continue
        try:
            if not zipfile.is_zipfile(f):
                ilegibles.append(f.name)
                continue
        except OSError:
            sin_bytes.append(f.name)
            continue
    total += sum(f.stat().st_size for f in parciales)

    datos = {"ficheros": len(zips), "bytes": total, "ilegibles": ilegibles,
             "vaciados": sin_bytes, "parciales": [f.name for f in parciales]}
    resumen = f"{len(zips)} Dateien, {_mb(total)}"
    if sin_bytes or ilegibles:
        return Comprobacion(
            "Archiv-Cache", "aviso",
            resumen + " · nicht lesbar: " + ", ".join(ilegibles + sin_bytes),
            "Sie werden beim nächsten --backfill erneut geladen. Wenn iCloud sie geleert hat, "
            "nimm das Projekt aus dem synchronisierten Ordner", datos,
        )
    if parciales:
        return Comprobacion(
            "Archiv-Cache", "aviso",
            resumen + f" · {len(parciales)} unvollständige(r) Abruf(e)",
            "Sie setzen dort fort, wo sie abbrachen; zum Verwerfen: "
            "python3 radar.py estado --limpiar-cache", datos,
        )
    return Comprobacion("Archiv-Cache", "ok", resumen, datos=datos)


def tarea_diaria() -> Comprobacion:
    """La tarea de launchd: instalada, cargada, y apuntando a algo que existe.

    Las dos últimas son fallos silenciosos de verdad: launchd la ejecuta cada mañana y no
    hace nada, sin avisar a nadie. Pasa al actualizar Python —el `sys.executable` que se
    grabó al instalarla desaparece— y al mover la carpeta del proyecto.
    """
    estado = programar.estado()
    datos = dict(estado)
    if not estado["instalada"]:
        return Comprobacion(
            "Tägliche Aufgabe", "aviso", "kein automatischer Abruf eingerichtet",
            "Wenn du sie willst: python3 radar.py programar --hora 8 --minuto 30", datos,
        )
    try:
        with open(programar.PLIST, "rb") as fh:
            plist = plistlib.load(fh)
    except (OSError, ValueError) as exc:
        return Comprobacion("Tägliche Aufgabe", "error", f"die plist ist nicht lesbar: {exc}",
                            "python3 radar.py programar", datos)

    cuando = plist.get("StartCalendarInterval") or {}
    argumentos = plist.get("ProgramArguments") or []
    datos["hora"] = f"{cuando.get('Hour', '?'):02}:{cuando.get('Minute', 0):02}"
    datos["argumentos"] = argumentos
    resumen = f"installiert um {datos['hora']}"

    if argumentos and not Path(argumentos[0]).exists():
        return Comprobacion(
            "Tägliche Aufgabe", "error",
            f"{resumen}, zeigt aber auf ein Python, das es nicht mehr gibt ({argumentos[0]}): "
            "läuft jeden Morgen und tut nichts",
            "python3 radar.py programar", datos,
        )
    guion = next((a for a in argumentos if a.endswith("radar.py")), None)
    if guion and Path(guion).resolve().parent != RAIZ:
        return Comprobacion(
            "Tägliche Aufgabe", "error",
            f"{resumen}, zeigt aber auf eine andere Kopie des Projekts ({guion})",
            "python3 radar.py programar", datos,
        )
    if not estado["cargada"]:
        return Comprobacion(
            "Tägliche Aufgabe", "error", f"{resumen}, aber launchd hat sie nicht geladen",
            "python3 radar.py programar", datos,
        )
    return Comprobacion("Tägliche Aufgabe", "ok", resumen + " y cargada en launchd",
                        datos=datos)


def registro_de_la_tarea() -> Comprobacion:
    """`data/ingest.log` lo abre launchd en modo append y nadie lo rota."""
    log = programar.LOG
    if not log.exists():
        return Comprobacion("Protokoll der Aufgabe", "ok", "noch kein Protokoll",
                            datos={"bytes": 0})
    tam = log.stat().st_size
    datos = {"ruta": str(log), "bytes": tam}
    if tam > MB_MAXIMO_LOG * 1e6:
        return Comprobacion(
            "Protokoll der Aufgabe", "aviso",
            f"{log.name} belegt {_mb(tam)} und niemand rotiert es",
            f"Lösch es, es geht nichts verloren: rm {log}", datos,
        )
    return Comprobacion("Protokoll der Aufgabe", "ok", f"{log.name}, {_mb(tam)}",
                        datos=datos)


def version_publicada(*, ejecutar: bool = False, timeout: int = 8) -> Comprobacion:
    """¿Hay una versión más nueva publicada? Solo si se pide: sale a internet.

    `comprobar()` reintenta dos veces con 15 s de timeout, así que detrás de un portal
    cautivo son más de treinta segundos de espera en un comando que promete ser
    instantáneo.
    """
    from . import __version__

    if not ejecutar:
        return Comprobacion("Version", "omitida", f"{__version__} installiert (ungeprüft, "
                            "ob es eine neuere gibt)", "python3 radar.py doctor --con-red",
                            {"instalada": __version__})
    from . import actualizacion

    info = actualizacion.comprobar(timeout=timeout)
    datos = {"instalada": info["version_actual"], "publicada": info["version_nueva"]}
    if info["error"]:
        # Quedarse sin red no es una avería del programa, y el código de salida no puede
        # depender del wifi.
        return Comprobacion("Version", "aviso", info["error"], datos=datos)
    if info["hay_nueva"]:
        return Comprobacion(
            "Version", "aviso",
            f"es gibt eine neue Version ({info['version_nueva']}); installiert ist "
            f"{info['version_actual']}",
            "python3 radar.py actualizar", datos,
        )
    return Comprobacion("Version", "ok", f"aktuell ({info['version_actual']})", datos=datos)


# --- orquestación ----------------------------------------------------------


def diagnosticar(*, bd: Path | str | None = None, perfiles: Path | str | None = None,
                 con_red: bool = False, integridad: bool = False) -> list[Comprobacion]:
    """Todas las comprobaciones, en el orden en que conviene leerlas.

    Primero lo que impide arrancar (Python, certificados, disco), luego lo que hay
    dentro (base, Begriffe), y al final lo que se puede consultar en la aplicación
    (fuentes, caché, tarea).
    """
    return [
        version_de_python(),
        bundle_de_certificados(),
        espacio_en_disco(),
        base_de_datos(bd),
        integridad_de_la_base(bd, ejecutar=integridad),
        migraciones_pendientes(bd),
        terminos_de_busqueda(perfiles),
        salud_de_las_fuentes(bd),
        ingesta_en_marcha(),
        cache_de_historicos(),
        tarea_diaria(),
        registro_de_la_tarea(),
        version_publicada(ejecutar=con_red),
    ]


def hay_errores(comprobaciones: list[Comprobacion]) -> bool:
    """Solo los errores cuentan para el código de salida.

    Un aviso puede ser una elección legítima —no tener la tarea diaria instalada, o
    quedarse en 8 GB libres—, y si esos devolvieran 1 el código de salida dejaría de
    significar nada.
    """
    return any(c.estado == "error" for c in comprobaciones)


def a_json(comprobaciones: list[Comprobacion]) -> dict:
    return {
        "ok": not hay_errores(comprobaciones),
        "comprobaciones": [
            {"nombre": c.nombre, "estado": c.estado, "mensaje": c.mensaje,
             "remedio": c.remedio, "datos": c.datos}
            for c in comprobaciones
        ],
    }
