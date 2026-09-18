"""Capa de red. Sin acceso a internet: se falsea la conexión y se lee el bundle de CAs.

Dos cosas que hay que proteger aquí.

La primera es el bundle: es el fallo más probable de todo el proyecto, porque un bundle
recortado deja de responder con un error de TLS que no se parece en nada a "faltan
certificados".

La segunda son las descargas grandes, que se cortan. Lo que se prueba abajo no es que la
descarga funcione —eso es fácil— sino que un corte no tire lo ya bajado y, sobre todo,
que reanudar no pueda coser dos ficheros distintos: un ZIP corrupto no se descubre hasta
mucho después.

Y la tercera, propia de este radar: que las cabeceras propias llegan. El
Bekanntmachungsservice elige el formato por `Accept`, así que una cabecera que se pierda
por el camino no da un fichero raro, da un 406.
"""

# Como en los módulos de `radar/`: sin esto, las anotaciones tipo `dict | None` se
# evalúan al definir la función y este fichero no se puede ni importar en Python 3.9,
# que es el mínimo que declara el proyecto y el que prueba el CI.
from __future__ import annotations

import io
import json
import ssl
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from radar import net


class TestBundleCA(unittest.TestCase):
    def test_el_bundle_existe(self):
        self.assertTrue(
            net.BUNDLE_CA.exists(),
            f"falta {net.BUNDLE_CA}: regenéralo con herramientas/regenerar_ca_bundle.py",
        )

    def test_contiene_las_raices_del_servicio_aleman(self):
        """oeffentlichevergabe.de se firma con Let's Encrypt (ISRG Root X1/X2)."""
        huellas = net._huellas_del_bundle(net.BUNDLE_CA)
        faltan = net.HUELLAS_ESPERADAS - huellas
        self.assertEqual(
            faltan, set(),
            "sin las raíces de ISRG el Bekanntmachungsservice falla el handshake TLS",
        )

    def test_el_bundle_trae_tambien_las_ca_comerciales(self):
        """Se empaqueta el almacén completo: los Python de python.org vienen sin raíces
        cargadas y, si solo lleváramos las de ISRG, TED fallaría."""
        huellas = net._huellas_del_bundle(net.BUNDLE_CA)
        self.assertGreater(len(huellas), 80, "el bundle parece recortado")

    def test_el_contexto_verifica_de_verdad(self):
        ctx = net.contexto_ssl()
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreater(ctx.cert_store_stats()["x509_ca"], 80)


URL = "https://oeffentlichevergabe.de/api/notice-exports?pubMonth=2026-08"
CUERPO = bytes(range(256)) * 8  # 2.048 bytes reconocibles byte a byte


class RespuestaFalsa:
    """Lo mínimo de `urlopen` que consume `descargar_a_fichero`.

    Devuelve el cuerpo a trocitos y, con `cortar_en`, deja de darlo a mitad como hace
    PLACSP: es la única forma de comprobar que lo ya escrito en disco sobrevive.
    """

    TROZO = 64

    def __init__(self, cuerpo: bytes, *, status: int = 200, cabeceras: dict | None = None,
                 cortar_en: int | None = None):
        self.cuerpo = cuerpo
        self.status = status
        self.cortar_en = cortar_en
        self.headers = {"Content-Length": str(len(cuerpo))}
        self.headers.update(cabeceras or {})
        self._pos = 0

    def read(self, n: int = -1) -> bytes:
        if self.cortar_en is not None and self._pos >= self.cortar_en:
            raise TimeoutError("se cortó la conexión")
        tope = self._pos + min(self.TROZO, n if n and n > 0 else self.TROZO)
        if self.cortar_en is not None:
            tope = min(tope, self.cortar_en)
        trozo = self.cuerpo[self._pos:min(tope, len(self.cuerpo))]
        self._pos += len(trozo)
        return trozo

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Servidor:
    """Guion de respuestas para `net._abrir`.

    Se pincha `_abrir` y no `urllib.request.urlopen` porque es la costura que ya aísla
    el contexto TLS: así estas pruebas no necesitan ni red ni bundle de CAs.
    """

    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)
        self.peticiones: list[urllib.request.Request] = []

    def __call__(self, req, timeout):
        self.peticiones.append(req)
        if not self.respuestas:
            raise AssertionError("se ha llamado al servidor más veces de las previstas")
        siguiente = self.respuestas.pop(0)
        if isinstance(siguiente, Exception):
            raise siguiente
        return siguiente

    @property
    def llamadas(self) -> int:
        return len(self.peticiones)

    def rango(self, i: int) -> str | None:
        return self.peticiones[i].get_header("Range")

    def if_range(self, i: int) -> str | None:
        # `Request.add_header` capitaliza la clave: «If-Range» se guarda «If-range».
        return self.peticiones[i].get_header("If-range")


class TestDescargarAFicheroReanudable(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.destino = Path(self.dir.name) / "licitaciones_2026.zip"
        self.parcial = self.destino.with_suffix(".zip.parcial")
        self.meta = self.parcial.with_name(self.parcial.name + ".meta")
        # Sin esto cada reintento dormiría 2, 4 y 8 segundos de verdad.
        espera = mock.patch.object(net, "_esperar")
        self.espera = espera.start()
        self.addCleanup(espera.stop)

    def _servir(self, *respuestas) -> Servidor:
        servidor = Servidor(*respuestas)
        parche = mock.patch.object(net, "_abrir", servidor)
        parche.start()
        self.addCleanup(parche.stop)
        return servidor

    def _dejar_a_medias(self, n: int, *, validador='"abc"', url: str = URL) -> None:
        """Deja en disco un `.parcial` de `n` bytes como lo dejaría un corte."""
        self.parcial.write_bytes(CUERPO[:n])
        if validador is not None:
            self.meta.write_text(json.dumps({"url": url, "validador": validador}),
                                 encoding="utf-8")

    def _error_http(self, codigo: int, razon: str) -> urllib.error.HTTPError:
        """HTTPError como el que suelta urllib, y cerrado al terminar: hereda de
        `addinfourl` y sin cerrarlo el recolector avisa de un recurso sin liberar.

        El cuerpo vacío no es decoración. Con `fp=None`, `HTTPError` no llega a inicializar
        su parte de `addinfourl`, así que en Python 3.9 `close()` muere con
        `KeyError: 'file'` —lo cazó el CI, porque en 3.14 no pasa—. Con un flujo de verdad,
        cerrarlo funciona en las dos versiones. `descargar_a_fichero` no lee el cuerpo de
        los 4xx, así que vacío es suficiente.
        """
        exc = urllib.error.HTTPError(URL, codigo, razon, {}, io.BytesIO(b""))
        self.addCleanup(exc.close)
        return exc

    def test_una_descarga_limpia_deja_el_fichero_y_ningun_resto(self):
        servidor = self._servir(RespuestaFalsa(CUERPO))
        net.descargar_a_fichero(URL, self.destino)
        self.assertEqual(self.destino.read_bytes(), CUERPO)
        self.assertFalse(self.parcial.exists(), "el .parcial se mueve, no se copia")
        self.assertFalse(self.meta.exists(), "el validador solo sirve mientras falta algo")
        self.assertIsNone(servidor.rango(0), "sin nada bajado no se pide ningún rango")

    def test_un_corte_al_final_se_reanuda_en_lugar_de_empezar_de_cero(self):
        """El caso que motiva todo esto: 2 GB al 95% y la conexión se corta."""
        servidor = self._servir(
            RespuestaFalsa(CUERPO, cabeceras={"ETag": '"abc"'}, cortar_en=1024),
            RespuestaFalsa(
                CUERPO[1024:], status=206,
                cabeceras={"ETag": '"abc"',
                           "Content-Range": f"bytes 1024-{len(CUERPO) - 1}/{len(CUERPO)}"},
            ),
        )
        net.descargar_a_fichero(URL, self.destino)
        self.assertEqual(self.destino.read_bytes(), CUERPO)
        self.assertEqual(servidor.llamadas, 2)
        self.assertEqual(servidor.rango(1), "bytes=1024-")
        self.assertEqual(servidor.if_range(1), '"abc"',
                         "sin If-Range se podrían coser dos ficheros distintos")

    def test_si_el_servidor_manda_el_fichero_entero_no_se_cose_detras_de_lo_ya_bajado(self):
        """Es el caso REAL de PLACSP, no uno de laboratorio: probado contra la
        plataforma, ignora el `Range` y contesta 200 con el fichero entero. Pegar eso
        detrás de lo ya bajado daría un ZIP corrupto que no se descubre hasta cuarenta
        minutos después."""
        self._servir(
            RespuestaFalsa(CUERPO, cabeceras={"ETag": '"vieja"'}, cortar_en=1024),
            RespuestaFalsa(CUERPO, cabeceras={"ETag": '"nueva"'}),  # 200, no 206
        )
        net.descargar_a_fichero(URL, self.destino)
        self.assertEqual(self.destino.read_bytes(), CUERPO)
        self.assertEqual(len(self.destino.read_bytes()), len(CUERPO),
                         "1.024 bytes viejos + el fichero nuevo = ZIP corrupto")

    def test_un_etag_debil_no_sirve_de_validador_y_se_usa_el_last_modified(self):
        """Un ETag débil admite que el contenido «cambie poco», que es justo lo que no
        se puede tolerar al pegar dos mitades."""
        servidor = self._servir(
            RespuestaFalsa(
                CUERPO, cortar_en=512,
                cabeceras={"ETag": 'W/"floja"',
                           "Last-Modified": "Mon, 17 Aug 2026 06:30:00 GMT"},
            ),
            RespuestaFalsa(CUERPO[512:], status=206,
                           cabeceras={"Content-Range":
                                      f"bytes 512-{len(CUERPO) - 1}/{len(CUERPO)}"}),
        )
        net.descargar_a_fichero(URL, self.destino)
        self.assertEqual(servidor.if_range(1), "Mon, 17 Aug 2026 06:30:00 GMT")

    def test_un_rango_ya_invalido_borra_el_parcial_y_empieza_de_cero(self):
        """Un 416 no es una avería del servidor: lo que sobraba era nuestro offset, así
        que no puede gastar el único intento que quedaba."""
        self._dejar_a_medias(700)
        servidor = self._servir(
            self._error_http(416, "Range Not Satisfiable"),
            RespuestaFalsa(CUERPO),
        )
        net.descargar_a_fichero(URL, self.destino, intentos=1)
        self.assertEqual(self.destino.read_bytes(), CUERPO)
        self.assertEqual(servidor.llamadas, 2)
        self.assertIsNone(servidor.rango(1), "el segundo intento va sin Range")
        self.espera.assert_not_called()

    def test_un_ano_sin_zip_publicado_sigue_llegando_como_404_y_no_se_reintenta(self):
        """`pipeline.ingerir` distingue ese 404 por el código: es información, no un
        fallo, y reintentarlo cuatro veces solo haría esperar."""
        servidor = self._servir(self._error_http(404, "Not Found"))
        with self.assertRaises(net.ErrorRed) as cm:
            net.descargar_a_fichero(URL, self.destino)
        self.assertEqual(cm.exception.codigo, 404)
        self.assertEqual(servidor.llamadas, 1)
        self.assertFalse(self.parcial.exists())
        self.assertFalse(self.meta.exists())

    def test_un_error_del_servidor_se_reintenta_sin_tirar_lo_ya_bajado(self):
        servidor = self._servir(
            RespuestaFalsa(CUERPO, cabeceras={"ETag": '"abc"'}, cortar_en=512),
            self._error_http(503, "Service Unavailable"),
            RespuestaFalsa(CUERPO[512:], status=206,
                           cabeceras={"ETag": '"abc"',
                                      "Content-Range":
                                      f"bytes 512-{len(CUERPO) - 1}/{len(CUERPO)}"}),
        )
        net.descargar_a_fichero(URL, self.destino)
        self.assertEqual(self.destino.read_bytes(), CUERPO)
        self.assertEqual(servidor.rango(1), "bytes=512-",
                         "el 503 no puede haber borrado lo que ya estaba en disco")
        self.assertEqual(servidor.rango(2), "bytes=512-")

    def test_el_parcial_sobrevive_a_agotar_los_intentos_para_la_ejecucion_siguiente(self):
        """Cuatro cortes seguidos no son una descarga perdida: cada intento suma."""
        cabeceras = {"ETag": '"abc"'}
        def trozo(desde, hasta):
            return RespuestaFalsa(
                CUERPO[desde:], status=206, cortar_en=hasta - desde,
                cabeceras={**cabeceras,
                           "Content-Range": f"bytes {desde}-{len(CUERPO) - 1}/{len(CUERPO)}"},
            )
        self._servir(
            RespuestaFalsa(CUERPO, cabeceras=cabeceras, cortar_en=400),
            trozo(400, 500), trozo(500, 600), trozo(600, 700),
        )
        with self.assertRaises(net.ErrorRed):
            net.descargar_a_fichero(URL, self.destino)
        self.assertFalse(self.destino.exists())
        self.assertEqual(self.parcial.stat().st_size, 700, "los cuatro intentos suman")
        self.assertTrue(self.meta.exists(), "sin el validador no se podrá reanudar")

    def test_un_parcial_de_otra_ejecucion_se_reanuda_sin_volver_a_pedir_lo_que_ya_hay(self):
        """La reanudación que más importa es esta: la del día siguiente."""
        self._dejar_a_medias(700)
        servidor = self._servir(
            RespuestaFalsa(CUERPO[700:], status=206,
                           cabeceras={"Content-Range":
                                      f"bytes 700-{len(CUERPO) - 1}/{len(CUERPO)}"}),
        )
        net.descargar_a_fichero(URL, self.destino)
        self.assertEqual(self.destino.read_bytes(), CUERPO)
        self.assertEqual(servidor.rango(0), "bytes=700-")

    def test_un_parcial_sin_saber_de_que_version_es_no_se_reanuda_a_ciegas(self):
        self._dejar_a_medias(700, validador=None)
        servidor = self._servir(RespuestaFalsa(CUERPO))
        net.descargar_a_fichero(URL, self.destino)
        self.assertIsNone(servidor.rango(0))
        self.assertEqual(self.destino.read_bytes(), CUERPO)

    def test_un_validador_que_no_es_de_este_fichero_no_sirve_para_reanudar(self):
        casos = {
            "de otra url": lambda: self._dejar_a_medias(700, url=URL + "?otra"),
            "corrupto": lambda: (self.parcial.write_bytes(CUERPO[:700]),
                                 self.meta.write_text("{ a medio escri",
                                                      encoding="utf-8")),
        }
        for etiqueta, preparar in casos.items():
            with self.subTest(caso=etiqueta):
                self.destino.unlink(missing_ok=True)
                preparar()
                servidor = self._servir(RespuestaFalsa(CUERPO))
                net.descargar_a_fichero(URL, self.destino)
                self.assertIsNone(servidor.rango(0))
                self.assertEqual(self.destino.read_bytes(), CUERPO)

    def test_el_total_de_una_reanudacion_incluye_lo_que_ya_estaba(self):
        """El Content-Length de un 206 es solo lo que queda: anunciarlo como total
        dejaba la barra diciendo «1,2 GB de 100 MB»."""
        self._dejar_a_medias(700)
        self._servir(
            RespuestaFalsa(CUERPO[700:], status=206,
                           cabeceras={"Content-Range":
                                      f"bytes 700-{len(CUERPO) - 1}/{len(CUERPO)}"}),
        )
        with mock.patch.object(net.progreso, "bytes_totales") as totales:
            net.descargar_a_fichero(URL, self.destino)
        totales.assert_called_with(len(CUERPO))

    def test_la_fase_de_una_reanudacion_sigue_empezando_por_descargando(self):
        """Contrato literal con `progreso._resumen` y con web/app.js, que filtran las
        fases de descarga por ese prefijo."""
        self._dejar_a_medias(700)
        self._servir(
            RespuestaFalsa(CUERPO[700:], status=206,
                           cabeceras={"Content-Range":
                                      f"bytes 700-{len(CUERPO) - 1}/{len(CUERPO)}"}),
        )
        with mock.patch.object(net.progreso, "fase") as fase:
            net.descargar_a_fichero(URL, self.destino)
        fases = [c.args[0] for c in fase.call_args_list]
        reanudando = [f for f in fases if "reanudando" in f]
        self.assertTrue(reanudando, f"ninguna fase dice que se reanuda: {fases}")
        for f in reanudando:
            self.assertTrue(f.startswith("descargando"), f)


class TestCabecerasPropias(unittest.TestCase):
    """El Bekanntmachungsservice elige el formato por `Accept`, no por parámetro de la
    URL. Si la cabecera no llega, no baja un fichero distinto: contesta 406 y la ingesta
    se queda sin ese tramo."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.destino = Path(self.dir.name) / "tramo.zip"

    def _capturar(self, **kwargs):
        vistas = {}

        def abrir(req, timeout):
            vistas.update(req.headers)
            return RespuestaFalsa(CUERPO)

        with mock.patch.object(net, "_abrir", abrir):
            net.descargar_a_fichero(URL, self.destino, **kwargs)
        return vistas

    def test_el_accept_llega_al_servidor(self):
        vistas = self._capturar(
            cabeceras={"Accept": "application/vnd.bekanntmachungsservice.ocds.zip+zip"})
        self.assertEqual(vistas.get("Accept"),
                         "application/vnd.bekanntmachungsservice.ocds.zip+zip")

    def test_el_user_agent_se_conserva(self):
        """Se mezcla sobre el UA, no lo sustituye: los portales alemanes lo miran."""
        vistas = self._capturar(cabeceras={"Accept": "lo-que-sea"})
        self.assertEqual(vistas.get("User-agent"), net.USER_AGENT)

    def test_sin_cabeceras_sigue_funcionando(self):
        vistas = self._capturar()
        self.assertEqual(vistas.get("User-agent"), net.USER_AGENT)
        self.assertEqual(self.destino.read_bytes(), CUERPO)


class TestCuerpoDelError(unittest.TestCase):
    def test_un_406_dice_que_media_types_acepta(self):
        """Sin esto, una errata en el `Accept` da un «HTTP 406 Not Acceptable» a secas y
        hay que ir a adivinar el media type. Con esto, el propio error trae la lista."""
        import io
        import urllib.error

        detalle = ("Not Acceptable: Acceptable representations: "
                   "[application/vnd.bekanntmachungsservice.ocds.zip+zip]")

        def abrir(req, timeout):
            raise urllib.error.HTTPError(
                URL, 406, "Not Acceptable", {}, io.BytesIO(detalle.encode()))

        with mock.patch.object(net, "_abrir", abrir):
            with self.assertRaises(net.ErrorRed) as caja:
                net.descargar_a_fichero(URL, self.destino_tmp())
        self.assertEqual(caja.exception.codigo, 406)
        self.assertIn("ocds.zip+zip", str(caja.exception))

    def destino_tmp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Path(d.name) / "x.zip"


if __name__ == "__main__":
    unittest.main()
