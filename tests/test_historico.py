"""Ampliar la ventana de histórico desde la aplicación.

Es la única acción del programa que se pone a descargar gigabytes, así que lo que estas
pruebas vigilan no es tanto que funcione como que **no pase sola**: que la ventana de
fábrica siga siendo la corta, que el endpoint no acepte cualquier número, y que el
control siga estando en la pantalla con su aviso de lo que cuesta.
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from radar import busqueda, consultas, db, pipeline, server  # noqa: E402


class TestVentana(unittest.TestCase):
    """La preferencia: por defecto corta, acotada, y de una pieza con las etapas."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        self.addCleanup(self.con.close)

    def test_de_fabrica_son_dos_anios(self):
        self.assertEqual(pipeline.meses_historico(self.con), 24)
        self.assertEqual(pipeline.MESES_HISTORICO, 24)

    def test_se_guarda_y_manda_sobre_la_de_fabrica(self):
        pipeline.fijar_meses_historico(self.con, 36)
        self.assertEqual(pipeline.meses_historico(self.con), 36)

    def test_no_se_puede_pedir_más_de_lo_que_hay(self):
        self.assertEqual(pipeline.fijar_meses_historico(self.con, 600), 36)

    def test_no_se_puede_encoger_por_debajo_del_mínimo(self):
        # Encoger no tendría efecto —lo descargado ya está en la base— y dejaría la
        # pantalla diciendo una tapa que no es.
        self.assertEqual(pipeline.fijar_meses_historico(self.con, 1), 24)

    def test_una_preferencia_corrupta_no_tumba_nada(self):
        db.escribir_preferencia(self.con, pipeline.CLAVE_MESES, "muchos")
        self.assertEqual(pipeline.meses_historico(self.con), 24)

    def test_la_ventana_se_traduce_en_años_a_descargar(self):
        from datetime import date

        hoy = date(2026, 9, 17)
        self.assertEqual(pipeline.anios_primera_carga(hoy, 24), [2024, 2025, 2026])
        self.assertEqual(pipeline.anios_primera_carga(hoy, 36), [2023, 2024, 2025, 2026])

    def test_hay_exactamente_una_etapa_que_cubre_el_histórico(self):
        # El endpoint relanza «la» etapa del archivo. Si mañana hubiera dos, o ninguna,
        # ampliar dejaría de hacer lo que dice el botón.
        completas = [e for e in pipeline.ETAPAS_PRIMERA_CARGA if e.get("ventana_completa")]
        self.assertEqual(len(completas), 1)
        self.assertTrue(completas[0].get("coste_por_anio"),
                        "la etapa del archivo tiene que decir lo que cuesta cada año")


class TestResumen(unittest.TestCase):
    """Lo que la pantalla necesita para pintar el bloque sale de `/api/resumen`."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        self.addCleanup(self.con.close)

    def test_dice_la_ventana_el_tope_y_el_precio(self):
        h = consultas.resumen(self.con)["historico"]
        self.assertEqual(h["meses"], 24)
        self.assertEqual(h["maximo"], 36)
        self.assertTrue(h["ampliable"])
        self.assertTrue(h["coste_ampliar"])

    def test_al_tope_deja_de_ser_ampliable(self):
        pipeline.fijar_meses_historico(self.con, 36)
        h = consultas.resumen(self.con)["historico"]
        self.assertEqual(h["meses"], 36)
        self.assertFalse(h["ampliable"])


class TestEndpoint(unittest.TestCase):
    """El POST que amplía. La descarga se simula: aquí se mira quién la pide y con qué."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.bd = Path(self.dir.name) / "t.db"
        db.conectar(self.bd).close()

        self.lanzadas = []
        parche = mock.patch.object(
            busqueda, "lanzar",
            side_effect=lambda **kw: self.lanzadas.append(kw) or {"pid": 1},
        )
        parche.start()
        self.addCleanup(parche.stop)

        self.srv = server.arrancar(puerto=0, ruta_bd=self.bd)
        self.addCleanup(self.srv.server_close)
        hilo = threading.Thread(target=self.srv.serve_forever, daemon=True)
        hilo.start()
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def _post(self, cuerpo):
        pet = urllib.request.Request(
            self.base + "/api/historico",
            data=json.dumps(cuerpo).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(pet, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_ampliar_guarda_la_ventana_y_pide_solo_la_etapa_del_archivo(self):
        codigo, datos = self._post({"meses": 36})
        self.assertEqual(codigo, 200)
        self.assertEqual(datos["meses"], 36)

        etapa = next(i for i, e in enumerate(pipeline.ETAPAS_PRIMERA_CARGA, 1)
                     if e.get("ventana_completa"))
        self.assertEqual(self.lanzadas, [
            {"primera_carga": True, "etapas": [etapa], "meses": 36},
        ])

        con = db.conectar(self.bd)
        self.addCleanup(con.close)
        self.assertEqual(pipeline.meses_historico(con), 36)
        # Desmarcada: si la descarga se corta a medias, el arranque siguiente la retoma.
        self.assertIn(etapa, pipeline.etapas_pendientes(con))

    def test_un_número_fuera_de_rango_no_descarga_nada(self):
        codigo, datos = self._post({"meses": 120})
        self.assertEqual(codigo, 400)
        self.assertIn("36", datos["error"])
        self.assertEqual(self.lanzadas, [])

    def test_encoger_tampoco(self):
        self.assertEqual(self._post({"meses": 6})[0], 400)
        self.assertEqual(self.lanzadas, [])

    def test_sin_meses_no_descarga_nada(self):
        self.assertEqual(self._post({})[0], 400)
        self.assertEqual(self.lanzadas, [])

    def test_si_ya_hay_una_en_marcha_lo_dice_y_no_arranca_otra(self):
        with mock.patch.object(busqueda, "lanzar",
                               side_effect=RuntimeError("Es läuft bereits eine Suche.")):
            codigo, datos = self._post({"meses": 36})
        self.assertEqual(codigo, 409)
        self.assertIn("bereits", datos["error"])

    def test_nada_de_esto_pasa_solo(self):
        # Sin POST, la ventana se queda como estaba y no se lanza ninguna descarga.
        con = db.conectar(self.bd)
        self.addCleanup(con.close)
        self.assertEqual(pipeline.meses_historico(con), 24)
        self.assertEqual(self.lanzadas, [])


class TestPantalla(unittest.TestCase):
    """Que el control siga en la pantalla, con su precio y su confirmación.

    `web/` no lo cubre ninguna otra suite, y es justo donde se pierden las cosas al
    traducir o al reordenar el HTML.
    """

    HTML = (RAIZ / "web" / "index.html").read_text(encoding="utf-8")
    JS = (RAIZ / "web" / "app.js").read_text(encoding="utf-8")

    def test_el_bloque_existe_y_arranca_oculto(self):
        self.assertIn('id="historico"', self.HTML)
        self.assertIn('id="historico-ampliar"', self.HTML)
        self.assertRegex(self.HTML, r'<div id="historico"[^>]*\bhidden\b')

    def test_el_js_lo_pinta_y_llama_al_endpoint(self):
        self.assertIn("pintarHistorico(d.historico)", self.JS)
        self.assertIn("'/api/historico'", self.JS)

    def test_avisa_de_lo_que_cuesta_antes_de_descargar(self):
        self.assertIn("confirm(", self.JS)
        self.assertIn("coste_ampliar", self.JS)

    def test_no_queda_castellano_en_los_botones(self):
        # El 'Buscando…' que se coló en la traducción vivía justo en este camino.
        for palabra in ("Buscando", "Cargando", "Descargando"):
            self.assertNotIn(f"'{palabra}", self.JS)


if __name__ == "__main__":
    unittest.main()
