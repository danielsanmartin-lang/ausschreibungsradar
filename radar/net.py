"""Capa de red: descargas con TLS verificado, reintentos y caché condicional.

Por qué existe este módulo en lugar de llamar a urlopen directamente:

Hay dos problemas de TLS que rompen la ingesta si se usa urlopen a pelo:

1. Sitios de la administración española (PLACSP, euskadi.eus) sirven cadenas
   firmadas por raíces ausentes del almacén de macOS/OpenSSL:
   `AC RAIZ FNMT-RCM SERVIDORES SEGUROS` (FNMT-RCM) e `Izenpe.com`. Sin ellas el
   handshake falla con "self signed certificate in certificate chain".
2. Los Python instalados desde python.org suelen venir con CERO raíces cargadas
   (`cert_store_stats() == {'x509': 0}`), así que depender del almacén del sistema
   hace que la herramienta funcione en un Mac y falle en el de al lado.

La solución NO es desactivar la verificación en ningún caso. Se usa el bundle
autocontenido `config/certs/ca-bundle.pem` (almacén de Mozilla, que ya incluye las
raíces españolas) y, de forma aditiva, lo que el sistema tenga cargado. Resultado:
comportamiento idéntico en cualquier equipo y verificación siempre activa.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import progreso

log = logging.getLogger(__name__)

from . import rutas

RAIZ = rutas.CODIGO
BUNDLE_CA = rutas.CERTIFICADOS

# Huellas SHA-256 de las raíces que el bundle DEBE contener. No es una comprobación de
# integridad del bundle completo: es un canario para detectar que alguien lo ha
# regenerado desde una fuente que se dejó fuera estas CAs.
#
# El radar español anclaba aquí las raíces de la FNMT y de Izenpe porque PLACSP y
# euskadi.eus se firman con ellas y faltan del almacén de macOS. El Bekanntmachungsservice
# se firma con Let's Encrypt (ISRG Root X1/X2), que sí está en todos los almacenes serios,
# así que el riesgo es menor; se mantiene el canario porque cuesta cero y porque un bundle
# recortado seguiría siendo un fallo silencioso.
HUELLAS_ESPERADAS = {
    "96bcec06264976f37460779acf28c5a7cfe8a3c0aae11a8ffcee05c0bddf08c6",  # ISRG Root X1
    "69729b8e15a86efc177a57afb7171dfc64add28c2fca8cf1507e34453ccb1470",  # ISRG Root X2
}

USER_AGENT = (
    "ausschreibungsradar/1.0 (internes Werkzeug zur Beobachtung oeffentlicher "
    "Ausschreibungen; +https://github.com/danielsanmartin-lang/ausschreibungsradar)"
)

_contexto: ssl.SSLContext | None = None


class ErrorRed(Exception):
    """Fallo de red no recuperable tras agotar los reintentos.

    `codigo` lleva el estado HTTP cuando lo hubo, y sirve para distinguir «esto no
    existe» de «esto se ha roto»: PLACSP no publica el ZIP anual de todos los años en
    todos los datasets, y un 404 ahí es información, no una avería.
    """

    def __init__(self, mensaje: str, *, codigo: int | None = None):
        super().__init__(mensaje)
        self.codigo = codigo


def _huellas_del_bundle(ruta: Path) -> set[str]:
    texto = ruta.read_text(encoding="utf-8")
    huellas = set()
    marca_ini = "-----BEGIN CERTIFICATE-----"
    marca_fin = "-----END CERTIFICATE-----"
    pos = 0
    while True:
        ini = texto.find(marca_ini, pos)
        if ini == -1:
            break
        fin = texto.find(marca_fin, ini)
        if fin == -1:
            break
        fin += len(marca_fin)
        pem = texto[ini:fin] + "\n"
        huellas.add(hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest())
        pos = fin
    return huellas


def contexto_ssl() -> ssl.SSLContext:
    """Contexto TLS con el bundle del repo MÁS lo que tenga cargado el sistema."""
    global _contexto
    if _contexto is not None:
        return _contexto

    if not BUNDLE_CA.exists():
        raise ErrorRed(
            f"Falta el bundle de CAs en {BUNDLE_CA}. Sin él fallará el "
            "handshake TLS. Regenéralo con: python3 herramientas/regenerar_ca_bundle.py"
        )

    huellas = _huellas_del_bundle(BUNDLE_CA)
    if not HUELLAS_ESPERADAS.issubset(huellas):
        raise ErrorRed(
            "El bundle de CAs no incluye las raíces esperadas "
            f"(faltan {len(HUELLAS_ESPERADAS - huellas)} de {len(HUELLAS_ESPERADAS)}): "
            "parece recortado. Regenera config/certs/ca-bundle.pem antes de seguir."
        )

    ctx = ssl.create_default_context(cafile=str(BUNDLE_CA))
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    # Aditivo: si el intérprete sí tiene raíces del sistema, las sumamos para no
    # quedarnos atrás cuando el bundle empaquetado envejezca.
    try:
        ctx.load_default_certs()
    except Exception:  # noqa: BLE001 - almacén del sistema ausente o ilegible
        log.debug("El sistema no aporta raíces adicionales; se usa solo el bundle del repo")

    _contexto = ctx
    return ctx


def _abrir(req: urllib.request.Request, timeout: int):
    return urllib.request.urlopen(req, timeout=timeout, context=contexto_ssl())


def _longitud(resp) -> int | None:
    try:
        return int(resp.headers.get("Content-Length") or 0) or None
    except (TypeError, ValueError):
        return None


def _validador(resp) -> str | None:
    """Lo que se puede meter en `If-Range` para exigir que el fichero no haya cambiado.

    Solo vale un ETag FUERTE: uno débil (`W/"..."`) declara que el contenido puede
    haber cambiado «poco», y eso es justo lo intolerable cuando se van a coser dos
    mitades de un ZIP de 2 GB. Si no hay ETag fuerte sirve el `Last-Modified`, que lo
    manda cualquier servidor de ficheros estáticos.
    """
    etag = (resp.headers.get("ETag") or "").strip()
    if etag and not etag.upper().startswith("W/"):
        return etag
    return (resp.headers.get("Last-Modified") or "").strip() or None


def _tamano_total(resp, desde: int) -> int | None:
    """Tamaño COMPLETO del fichero en una respuesta parcial.

    El `Content-Length` de un 206 es solo lo que queda, así que pasarlo como total
    dejaría la barra diciendo «1,2 GB de 100 MB». El total real viene en
    `Content-Range: bytes 1200-1399/1400`; si el servidor no lo manda, se suma el
    offset a lo que queda, que da lo mismo.
    """
    rango = resp.headers.get("Content-Range") or ""
    if "/" in rango:
        total = rango.rsplit("/", 1)[-1].strip()
        if total.isdigit():
            return int(total)
    quedan = _longitud(resp)
    return desde + quedan if quedan else None


def _reanudacion(parcial: Path, meta: Path, url: str) -> tuple[int, str | None]:
    """Bytes ya en disco y validador con el que pedir el resto. (0, None) = de cero.

    Devuelve (0, None) siempre que no haya CERTEZA de que el `.parcial` pertenece al
    mismo fichero que hay ahora en el servidor. El ZIP del año en curso lo reescribe
    PLACSP cada día, y reanudar a ciegas cose dos ficheros distintos: el destrozo no
    se ve hasta cuarenta minutos más tarde, cuando `zipfile` dice «File is not a zip
    file» y el año entero queda marcado como fallido.

    El tamaño se lee del `stat`, nunca del contador de bytes en memoria: si una
    excepción corta un `write` a medias, en disco queda exactamente el prefijo que se
    volcó, y el fichero es la única fuente de verdad.
    """
    try:
        bytes_en_disco = parcial.stat().st_size
    except OSError:
        return 0, None
    if not bytes_en_disco:
        return 0, None
    try:
        datos = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Sin saber de qué versión es lo que hay, se baja entero. Un `.meta` a medias
        # no se puede parsear y cae aquí, que es el lado seguro de equivocarse.
        return 0, None
    validador = datos.get("validador")
    if not isinstance(validador, str) or not validador or datos.get("url") != url:
        return 0, None
    return bytes_en_disco, validador


def _guardar_validador(meta: Path, url: str, validador: str) -> None:
    """Deja constancia de QUÉ versión del fichero es el `.parcial` de al lado.

    Un sidecar JSON de cien bytes junto a un fichero de 2 GB, porque el dato tiene
    que sobrevivir al proceso: la reanudación que de verdad importa es la de la
    ejecución siguiente, no la del reintento de dentro de dos segundos. Mismo recurso
    que `data/busqueda.lock`.

    No hace falta escribirlo de forma atómica: un `.meta` roto no se parsea y
    `_reanudacion` lo trata como «no se puede reanudar», que es exactamente lo que
    hay que hacer si no se sabe.
    """
    try:
        meta.write_text(
            json.dumps({"url": url, "validador": validador}), encoding="utf-8"
        )
    except OSError:
        # Quedarse sin poder anotar el validador cuesta la reanudación, no la
        # descarga: se seguirá bajando entero y ya está.
        log.debug("No se ha podido anotar el validador en %s", meta)


def _leer_contando(resp) -> bytes:
    """Lee la respuesta a trozos, informando de lo que va llegando.

    `resp.read()` de una sentada daría el mismo resultado, pero deja el contador
    a cero hasta que termina: justo lo que hace que una descarga lenta parezca
    un cuelgue. Los trozos de 64 kB no cambian el rendimiento de forma medible
    en feeds de decenas de MB y sí permiten pintar el avance.
    """
    progreso.bytes_totales(_longitud(resp))
    trozos = []
    while trozo := resp.read(64 << 10):
        trozos.append(trozo)
        progreso.sumar_bytes(len(trozo))
    return b"".join(trozos)


def _esperar(segundos: int, etiqueta: str) -> None:
    """Duerme mostrando la cuenta atrás, para que la espera no parezca un cuelgue."""
    for restante in range(segundos, 0, -1):
        progreso.fase(f"{progreso.FASE_FALLO}, neuer Versuch in {restante}s")
        time.sleep(1)


def descargar(
    url: str,
    *,
    datos: bytes | None = None,
    cabeceras: dict[str, str] | None = None,
    timeout: int = 120,
    intentos: int = 4,
    etag: str | None = None,
    modificado_desde: str | None = None,
) -> tuple[bytes, dict[str, str], int]:
    """Descarga una URL. Devuelve (cuerpo, cabeceras_respuesta, codigo).

    Con `etag` o `modificado_desde` puede devolver código 304 y cuerpo vacío:
    el llamante debe interpretarlo como "sin cambios" y no reprocesar nada.
    """
    cab = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
    }
    if cabeceras:
        cab.update(cabeceras)
    if etag:
        cab["If-None-Match"] = etag
    if modificado_desde:
        cab["If-Modified-Since"] = modificado_desde

    ultimo_error: Exception | None = None
    for intento in range(1, intentos + 1):
        req = urllib.request.Request(url, data=datos, headers=cab)
        progreso.reiniciar_bytes()
        progreso.fase(progreso.FASE_CONECTANDO if intento == 1
                      else f"{progreso.FASE_RECONECTANDO} ({intento}/{intentos})")
        try:
            with _abrir(req, timeout) as resp:
                progreso.fase(progreso.FASE_DESCARGANDO)
                cuerpo = _leer_contando(resp)
                if resp.headers.get("Content-Encoding") == "gzip":
                    cuerpo = gzip.decompress(cuerpo)
                return cuerpo, dict(resp.headers), resp.status
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return b"", dict(exc.headers or {}), 304
            # 4xx (salvo 429) no se arregla reintentando.
            if 400 <= exc.code < 500 and exc.code != 429:
                # El cuerpo del 400 suele decir exactamente qué campo sobra o falta.
                # Sin él, depurar una consulta a TED es adivinar.
                try:
                    detalle = exc.read().decode("utf-8", "replace")[:800]
                except Exception:  # noqa: BLE001
                    detalle = ""
                raise ErrorRed(
                    f"{url} -> HTTP {exc.code} {exc.reason}"
                    + (f"\n  respuesta: {detalle}" if detalle else ""),
                    codigo=exc.code,
                ) from exc
            ultimo_error = exc
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            ultimo_error = exc

        if intento < intentos:
            espera = min(2 ** intento, 30)
            log.warning(
                "Fallo al descargar %s (intento %d/%d): %s. Reintento en %ds",
                url, intento, intentos, ultimo_error, espera,
            )
            _esperar(espera, f"{progreso.FASE_FALLO} {intento}/{intentos}")

    raise ErrorRed(f"{url} falló tras {intentos} intentos: {ultimo_error}")


def descargar_json(url: str, **kwargs) -> object:
    cuerpo, _, codigo = descargar(url, **kwargs)
    if codigo == 304:
        return None
    return json.loads(cuerpo.decode("utf-8"))


def post_json(url: str, payload: dict, *, timeout: int = 120, intentos: int = 4) -> object:
    cuerpo, _, _ = descargar(
        url,
        datos=json.dumps(payload).encode("utf-8"),
        cabeceras={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=timeout,
        intentos=intentos,
    )
    return json.loads(cuerpo.decode("utf-8"))


def descargar_a_fichero(url: str, destino: Path, *, timeout: int = 600,
                        intentos: int = 4,
                        cabeceras: dict[str, str] | None = None) -> Path:
    """Descarga en streaming a disco, reintentando y REANUDANDO lo que ya haya bajado.

    Los exports mensuales pesan decenas de MB y las descargas grandes se cortan. Con un
    solo intento y el `.parcial` abierto en modo "wb", un corte al 95% tiraba la descarga
    entera y volvía a empezar por el byte cero; y como el `.parcial` se borraba al fallar,
    la ejecución siguiente tampoco tenía de dónde tirar. Ahora se conserva entre intentos
    Y entre ejecuciones, y se pide el resto con `Range`.

    `cabeceras` se mezcla SOBRE el User-Agent, no lo sustituye. Hace falta porque el
    Bekanntmachungsservice elige el formato por `Accept` —no por parámetro de la URL— y
    sin la cabecera correcta contesta 406 en vez de darte el fichero.

    El peligro de reanudar está en el ZIP del año en curso, que el servidor reescribe
    cada día: pegar lo nuevo detrás de lo viejo da un ZIP corrupto. Por eso el `Range`
    va siempre acompañado de un `If-Range` con el validador que anotó el intento
    anterior; si el fichero ya no es el mismo, el servidor responde 200 con el fichero
    entero y aquí se trunca y se empieza de cero.

    Conviene saber que no todas las plataformas admiten reanudar: algunas responden 200
    al `Range`, sin `Accept-Ranges` y sin `Content-Range`, o sea que mandan el fichero
    entero otra vez y la rama del 206 no llega a ejecutarse. Se pide igualmente porque no
    cuesta nada, porque el día que la plataforma lo permita empieza a funcionar solo, y
    porque el `If-Range` es lo que garantiza que nunca se cosan dos mitades de ficheros
    distintos.

    No se manda `Accept-Encoding: gzip` a propósito, al contrario que en `descargar()`:
    un ZIP ya viene comprimido y el gzip por encima solo estorbaría al contar bytes.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    parcial = destino.with_suffix(destino.suffix + ".parcial")
    meta = parcial.with_name(parcial.name + ".meta")

    def _tirar_lo_bajado() -> None:
        parcial.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)

    ultimo_error: Exception | None = None
    # No es un `for` sobre `range(intentos)` porque un 416 no gasta intento: ver más
    # abajo. Con un `for`, un 416 en el último intento dejaría la descarga muerta sin
    # haber probado nunca a bajarla desde cero.
    presupuesto = intentos
    intento = 0
    while intento < presupuesto:
        intento += 1
        desde, validador = _reanudacion(parcial, meta, url)
        # El User-Agent va primero para que quien pase `cabeceras` pueda sobreescribirlo
        # a conciencia, pero Range e If-Range van DESPUÉS: son de la reanudación y
        # pisarlos desde fuera cosería dos mitades de ficheros distintos.
        peticion = {"User-Agent": USER_AGENT, **(cabeceras or {})}
        if desde:
            peticion["Range"] = f"bytes={desde}-"
            peticion["If-Range"] = validador
        req = urllib.request.Request(url, headers=peticion)

        progreso.reiniciar_bytes(heredados=desde)
        progreso.fase(progreso.FASE_CONECTANDO if intento == 1
                      else f"{progreso.FASE_RECONECTANDO} ({intento}/{presupuesto})")
        try:
            with _abrir(req, timeout) as resp:
                if desde and resp.status == 206:
                    modo, total = "ab", _tamano_total(resp, desde)
                    # El prefijo «descargando» es obligatorio: la línea de la terminal
                    # y la barra de la aplicación filtran las fases por ahí.
                    progreso.fase(progreso.FASE_REANUDANDO)
                    log.info("Se reanuda %s desde %s bytes", destino.name, f"{desde:,}")
                else:
                    if desde:
                        log.info(
                            "%s llega completo (HTTP %s): o el servidor ignora el Range "
                            "o el fichero ha cambiado. Se empieza de cero.",
                            destino.name, resp.status,
                        )
                    desde, modo, total = 0, "wb", _longitud(resp)
                    progreso.reiniciar_bytes()
                    progreso.fase(progreso.FASE_DESCARGANDO)
                nuevo = _validador(resp)
                if nuevo:
                    _guardar_validador(meta, url, nuevo)
                else:
                    # Sin validador no se podrá reanudar, y decirlo en el `.meta` viejo
                    # sería mentir sobre lo que hay en el `.parcial`.
                    meta.unlink(missing_ok=True)
                progreso.bytes_totales(total)
                with parcial.open(modo) as fh:
                    while trozo := resp.read(1 << 20):
                        fh.write(trozo)
                        progreso.sumar_bytes(len(trozo))
            parcial.replace(destino)  # atómico: mismo directorio
            meta.unlink(missing_ok=True)
            return destino
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and desde:
                # El rango ya no vale: el fichero encogió, o el `.parcial` era ya del
                # tamaño completo. No es un fallo del servidor —lo que sobraba era
                # nuestro offset—, así que no gasta intento ni espera.
                log.info("%s: el servidor rechaza el rango; se baja desde cero",
                         destino.name)
                _tirar_lo_bajado()
                if presupuesto == intentos:
                    presupuesto += 1
                ultimo_error = exc
                continue
            if 400 <= exc.code < 500 and exc.code != 429:
                # No todas las fuentes publican todos los tramos, y `pipeline.ingerir`
                # distingue ese 404 por el código: es información, no una avería, y
                # reintentarlo solo haría esperar.
                #
                # El cuerpo se lee igual que en `descargar()`, y aquí importa más: el 406
                # del Bekanntmachungsservice trae la lista de media types aceptables, que
                # es literalmente la respuesta a una errata en el `Accept`.
                try:
                    detalle = exc.read().decode("utf-8", "replace")[:800]
                except Exception:  # noqa: BLE001
                    detalle = ""
                _tirar_lo_bajado()
                raise ErrorRed(
                    f"{url} -> HTTP {exc.code} {exc.reason}"
                    + (f"\n  respuesta: {detalle}" if detalle else ""),
                    codigo=exc.code,
                ) from exc
            ultimo_error = exc
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            # A propósito NO se borra el `.parcial`: borrarlo es lo que convertía un
            # corte al 95% de 2 GB en una descarga entera desde el principio.
            ultimo_error = exc

        if intento < presupuesto:
            espera = min(2 ** intento, 30)  # el mismo backoff que descargar()
            log.warning(
                "Fallo al descargar %s (intento %d/%d): %s. Reintento en %ds",
                url, intento, presupuesto, ultimo_error, espera,
            )
            _esperar(espera, f"{progreso.FASE_FALLO} {intento}/{presupuesto}")

    raise ErrorRed(
        f"{url} falló tras {intentos} intentos: {ultimo_error}. Lo descargado se "
        f"conserva en {parcial.name} y la próxima vez se reanudará desde ahí."
    )
