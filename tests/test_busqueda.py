"""Lanzar la ingesta desde la interfaz, sin bloquearla.

Se reutiliza la CLI a propósito, así que lo que hay que proteger es la traducción de
opciones a argumentos: un argumento mal construido no falla aquí, falla dentro de un
proceso en segundo plano cuya salida nadie está mirando.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radar import busqueda, progreso


class ProcesoFalso:
    pid = 4242


class TestArgumentos(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        raiz = Path(self.dir.name)
        # Ni cerrojo ni registro dentro del repositorio, y `en_marcha()` no debe ver
        # una búsqueda de verdad que estuviera corriendo en esta máquina.
        for atributo, valor in (("CERROJO", raiz / "b.lock"), ("SALIDA", raiz / "b.log")):
            parche = mock.patch.object(busqueda, atributo, valor)
            parche.start()
            self.addCleanup(parche.stop)

    def _orden(self, **kw):
        with mock.patch.object(subprocess, "Popen", return_value=ProcesoFalso()) as popen:
            busqueda.lanzar(**kw)
        return popen.call_args.args[0]

    def test_sin_opciones_es_una_ingesta_normal(self):
        orden = self._orden()
        self.assertEqual(orden[-1], "ingest")
        self.assertIn("-u", orden)  # sin esto el registro llega a ráfagas

    def test_primera_carga_con_etapas(self):
        orden = self._orden(primera_carga=True, etapas=[2, 3, 4])
        self.assertEqual(orden[orden.index("ingest") + 1:],
                         ["--primera-carga", "--etapas", "2", "3", "4"])

    def test_primera_carga_sin_etapas_no_pasa_la_opcion(self):
        """Sin --etapas la CLI hace todas, que es lo que se quiere por defecto."""
        orden = self._orden(primera_carga=True)
        self.assertIn("--primera-carga", orden)
        self.assertNotIn("--etapas", orden)

    def test_las_etapas_solo_se_pasan_con_primera_carga(self):
        self.assertNotIn("--etapas", self._orden(etapas=[2]))

    def test_fuentes_y_backfill(self):
        orden = self._orden(fuentes=["ted", "catalunya"], backfill=[2024, 2025])
        self.assertEqual(orden.count("--fuente"), 2)
        self.assertIn("ted", orden)
        self.assertIn("--backfill", orden)
        self.assertIn("2024,2025", orden)

    def test_reiniciar_cursor_y_dias(self):
        orden = self._orden(reiniciar_cursor=True, dias=365)
        self.assertIn("--reiniciar-cursor", orden)
        self.assertEqual(orden[orden.index("--dias") + 1], "365")

    def test_los_numeros_se_normalizan(self):
        """Llegan de un JSON de la aplicación: si viene '3' como texto, tiene que
        acabar siendo un argumento válido y no reventar dentro del hijo."""
        orden = self._orden(primera_carga=True, etapas=["3"], dias="90",
                            backfill=["2024"])
        self.assertIn("3", orden)
        self.assertIn("90", orden)
        self.assertIn("2024", orden)

    def test_no_se_lanzan_dos_a_la_vez(self):
        """Dos escritores sobre el mismo SQLite se pelean por el bloqueo."""
        with mock.patch.object(busqueda, "en_marcha",
                               return_value={"iniciada": "ayer", "pid": 1}):
            with self.assertRaises(RuntimeError):
                busqueda.lanzar()


class TestUltimasLineas(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.salida = Path(self.dir.name) / "b.log"
        parche = mock.patch.object(busqueda, "SALIDA", self.salida)
        parche.start()
        self.addCleanup(parche.stop)

    def test_las_consultas_a_ted_no_llegan_a_la_cabecera(self):
        """Ocupan miles de caracteres de paréntesis y códigos CPV. Se emiten con dos
        encabezados —`TED:` en la incremental y `TED histórico AAAA:` en el backfill— y
        el segundo se colaba en la línea de estado de la aplicación."""
        self.salida.write_text("\n".join([
            "TED: (buyer-country=ESP) AND (publication-date>=20260803) AND ((classi…",
            "TED histórico 2025: (buyer-country=ESP) AND (publication-date>=2025010…",
            "ted                            7776 vistas · 7776 nuevas",
        ]), encoding="utf-8")

        lineas = busqueda.ultimas_lineas()
        self.assertEqual(len(lineas), 1)
        self.assertIn("7776 vistas", lineas[0])

    def test_sin_registro_no_revienta(self):
        self.assertEqual(busqueda.ultimas_lineas(), [])


class TestDetalleProgreso(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.estado = Path(self.dir.name) / "progreso.json"
        parche = mock.patch.object(progreso, "ESTADO", self.estado)
        parche.start()
        self.addCleanup(parche.stop)

    def test_sin_fichero_devuelve_none(self):
        """Al arrancar la ingesta pasa un segundo hasta la primera instantánea; la
        aplicación tiene que aguantar ese hueco sin romperse."""
        self.assertIsNone(busqueda.detalle_progreso())

    def test_un_json_roto_no_revienta(self):
        self.estado.write_text("{esto no es json", encoding="utf-8")
        self.assertIsNone(busqueda.detalle_progreso())

    def test_devuelve_la_instantanea(self):
        self.estado.write_text(json.dumps({"etapa": 2, "etapas": 4}), encoding="utf-8")
        self.assertEqual(busqueda.detalle_progreso()["etapa"], 2)


if __name__ == "__main__":
    unittest.main()

