"""La app de macOS: el bundle que se monta y cómo encaja con el actualizador.

Dos cosas distintas se protegen aquí.

La primera es la VERSIÓN, que es la que se estropea sin que nadie lo note. El número
vive en `radar/__init__.py` y de ahí salen tres cosas: lo que compara el actualizador
contra la etiqueta de GitHub, lo que la app enseña en «Acerca de», y lo que hace que la
app se dé cuenta de que se ha quedado atrás tras una actualización. Si el `Info.plist`
se escribiera a mano, ese número se separaría del código en la primera prisa, y el día
que haya que diagnosticar algo la versión que se enseña sería una pista falsa.

La segunda es el TRATO CON EL ACTUALIZADOR: `macos` tiene que estar en la lista de lo
que se sustituye, y no en la de lo imprescindible. Están razonados en
`radar/actualizacion.py`; aquí se fijan para que un cambio de opinión sea explícito.

Lo que toca `sips`, `iconutil`, `swiftc` y `codesign` se salta fuera de macOS: estos
tests tienen que seguir pasando en el Ubuntu de la integración continua.
"""

from __future__ import annotations

import importlib.util
import os
import plistlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radar import __version__, actualizacion

RAIZ = Path(__file__).resolve().parent.parent


def cargar_constructor():
    """Importa `herramientas/construir_app.py`, que no es parte del paquete.

    Vive en `herramientas/` con el regenerador del almacén de certificados: son
    utilidades que se ejecutan a mano, no código que importe la aplicación. Para
    probarlo hay que cargarlo por ruta.
    """
    ruta = RAIZ / "herramientas" / "construir_app.py"
    especificacion = importlib.util.spec_from_file_location("construir_app", ruta)
    modulo = importlib.util.module_from_spec(especificacion)
    especificacion.loader.exec_module(modulo)
    return modulo


ES_MAC = sys.platform == "darwin"
HAY_SWIFT = shutil.which("swiftc") is not None


class TestLaVersionNoSePuedeSeparar(unittest.TestCase):
    """El número vive en un solo sitio y el resto lo lee de ahí."""

    def setUp(self):
        self.constructor = cargar_constructor()

    def test_la_version_sale_de_radar_init(self):
        self.assertEqual(self.constructor.version_del_proyecto(), __version__)

    def test_el_plist_lleva_la_version_del_proyecto(self):
        with tempfile.TemporaryDirectory() as tmp:
            contents = Path(tmp)
            self.constructor.escribir_plist(contents, __version__)
            plist = plistlib.loads((contents / "Info.plist").read_bytes())
        self.assertEqual(plist["CFBundleShortVersionString"], __version__)
        self.assertEqual(plist["CFBundleVersion"], __version__)

    def test_el_plist_trae_lo_que_hace_falta_para_arrancar(self):
        """Cuatro claves que, si faltan, fallan de formas que no se parecen a su causa.

        Sin `NSAppTransportSecurity` la ventana sale en blanco y no hay ningún error:
        App Transport Security bloquea el HTTP de 127.0.0.1 en silencio. Sin
        `CFBundleExecutable` el bundle no arranca. Sin `LSMinimumSystemVersion` el
        sistema se inventa la del SDK —la de la última versión de macOS— y el .app no
        abre en el Mac de un compañero que va una versión por detrás.
        """
        with tempfile.TemporaryDirectory() as tmp:
            contents = Path(tmp)
            self.constructor.escribir_plist(contents, "9.9.9")
            plist = plistlib.loads((contents / "Info.plist").read_bytes())
            # Dentro del `with`: al salir, el temporal ya no existe.
            self.assertEqual((contents / "PkgInfo").read_text(encoding="utf-8"), "APPL????")

        self.assertEqual(plist["CFBundleExecutable"], self.constructor.EJECUTABLE)
        self.assertEqual(plist["CFBundleIconFile"], "AppIcon")
        self.assertEqual(plist["CFBundleIdentifier"], self.constructor.IDENTIFICADOR)
        self.assertEqual(plist["LSMinimumSystemVersion"], self.constructor.MINIMO_MACOS)
        self.assertTrue(
            plist["NSAppTransportSecurity"]["NSAllowsLocalNetworking"],
            "sin esto la ventana sale en blanco y no se dice por qué",
        )

    def test_el_minimo_no_es_la_version_del_mac_que_compila(self):
        """13.0 y no 15, 26 ni 27: el SDK de las Command Line Tools es el de la última
        versión de macOS, y coger ese número dejaría el .app sin abrir en cualquier Mac
        que no esté al día."""
        mayor = int(self.constructor.MINIMO_MACOS.split(".")[0])
        self.assertLessEqual(mayor, 14, "el mínimo se ha ido detrás del SDK")

    def test_el_bundle_montado_cuadra_con_el_codigo(self):
        """Y si hay un .app montado en la carpeta, que no vaya por su cuenta."""
        instalada = self.constructor.version_instalada()
        if instalada is None:
            self.skipTest("no hay ningún .app montado ahora mismo")
        self.assertEqual(instalada, __version__)


class TestTratoConElActualizador(unittest.TestCase):
    def test_macos_se_sustituye_al_actualizar(self):
        """Si no estuviera, un compañero actualizaría el Python y se quedaría con la
        ventana vieja para siempre, sin manera de traer la nueva."""
        self.assertIn("macos", actualizacion.REEMPLAZABLES)

    def test_el_app_montado_no_se_sustituye(self):
        """Un bundle no puede reemplazarse a sí mismo mientras se está ejecutando. Se
        sustituye el fuente y el binario prefabricado, y la app se rehace sola."""
        for entrada in actualizacion.REEMPLAZABLES:
            self.assertNotIn(".app", entrada)

    def test_macos_no_es_imprescindible(self):
        """La ventana nativa es una comodidad, no un requisito: `start.command` hace lo
        mismo sin ella. Exigirla haría que una release publicada sin el binario
        prefabricado se negara a instalarse para todo el mundo."""
        self.assertNotIn("macos", actualizacion.IMPRESCINDIBLES)
        self.assertEqual(actualizacion.IMPRESCINDIBLES, ("radar", "web", "radar.py"))

    def test_los_datos_y_los_perfiles_siguen_intocables(self):
        """Lo de siempre, que no se rompa al añadir cosas a la lista."""
        for prohibido in ("data", "config/suchprofile.json", "config"):
            self.assertNotIn(prohibido, actualizacion.REEMPLAZABLES)


class TestLasPiezasEstan(unittest.TestCase):
    """El repositorio trae lo que hace falta para montar la app."""

    def test_el_fuente_y_el_icono_estan_versionados(self):
        self.assertTrue((RAIZ / "macos" / "Radar.swift").is_file())
        self.assertTrue((RAIZ / "macos" / "icono.svg").is_file())

    def test_el_icono_es_un_svg_que_sips_pueda_leer(self):
        """`sips` no sabe leer un fichero que empiece por un comentario XML: falla con
        «Error 13: an unknown error occurred», que no dice absolutamente nada. El
        comentario del icono va DENTRO del <svg> por esto."""
        texto = (RAIZ / "macos" / "icono.svg").read_text(encoding="utf-8")
        self.assertTrue(texto.lstrip().startswith("<svg"),
                        "el SVG tiene que empezar por <svg>, sin comentario delante")

    def test_hay_binario_prefabricado_para_quien_no_tenga_las_herramientas(self):
        """Es la única manera de que un compañero sin las Command Line Tools pueda
        usar la app: el zipball de la release solo lleva fuentes."""
        binario = RAIZ / "macos" / "prefabricado" / "radar"
        if not binario.exists():
            self.skipTest("todavía no se ha prefabricado (se hace al publicar)")
        self.assertGreater(binario.stat().st_size, 50_000,
                           "un binario de 150 KB no puede pesar tan poco")


class TestElCompiladorSeComprueboEjecutandolo(unittest.TestCase):
    """`which swiftc` no vale, y es la misma trampa que documenta `start.command`.

    macOS trae `/usr/bin/swiftc` en cualquier Mac, esté o no instalado el compilador; si
    no lo está, es un lanzador que abre el instalador de las herramientas de Apple y
    falla. Con solo `which`, el script intentaba compilar en el Mac de un compañero que
    no las tiene, la compilación fallaba y abortaba, **sin llegar nunca a usar el binario
    prefabricado que existe justo para ese caso**.
    """

    def setUp(self):
        self.constructor = cargar_constructor()

    def test_hay_compilador_no_se_fia_de_que_el_fichero_exista(self):
        """Con un `swiftc` que existe y falla, tiene que decir que no hay compilador."""
        with tempfile.TemporaryDirectory() as tmp:
            falso = Path(tmp) / "swiftc"
            falso.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            falso.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": tmp}):
                self.assertTrue(self.constructor.hay("swiftc"),
                                "el fichero está, que es lo que ve `which`")
                self.assertFalse(self.constructor.hay_compilador(),
                                 "pero no arranca, así que no sirve")

    def test_y_reconoce_uno_que_si_funciona(self):
        with tempfile.TemporaryDirectory() as tmp:
            bueno = Path(tmp) / "swiftc"
            bueno.write_text("#!/bin/sh\necho 'Apache Swift version 6.0'\nexit 0\n",
                             encoding="utf-8")
            bueno.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": tmp}):
                self.assertTrue(self.constructor.hay_compilador())

    def test_sin_swiftc_de_ninguna_clase(self):
        with tempfile.TemporaryDirectory() as vacio:
            with mock.patch.dict(os.environ, {"PATH": vacio}):
                self.assertFalse(self.constructor.hay_compilador())


@unittest.skipUnless(ES_MAC, "el bundle solo se monta en macOS")
class TestMontarDeVerdad(unittest.TestCase):
    """Lo que toca las herramientas de Apple. Solo en macOS."""

    def setUp(self):
        self.constructor = cargar_constructor()

    def test_el_icono_se_rasteriza_y_se_empaqueta(self):
        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp) / "AppIcon.icns"
            self.constructor.construir_icono(destino)
            self.assertTrue(destino.is_file())
            # Un .icns con los diez tamaños no baja de unas decenas de KB.
            self.assertGreater(destino.stat().st_size, 20_000)
            # Y el iconset temporal no se queda por ahí.
            self.assertFalse((Path(tmp) / "Radar.iconset").exists())

    @unittest.skipUnless(HAY_SWIFT, "hace falta swiftc")
    def test_el_shell_compila(self):
        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp) / "radar"
            nota = self.constructor.compilar(destino, universal=False)
            self.assertTrue(destino.is_file(), nota)
            self.assertGreater(destino.stat().st_size, 50_000)
            # Y no deja la carpeta de arquitecturas a medio limpiar.
            self.assertFalse((Path(tmp) / "_arq").exists())


if __name__ == "__main__":
    unittest.main()
