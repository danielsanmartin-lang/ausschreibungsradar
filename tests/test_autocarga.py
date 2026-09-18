"""Que la aplicación se ponga al día sola al abrirse."""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from radar import busqueda, db, pipeline  # noqa: E402


def _entrada():
    """Carga `radar.py`, el punto de entrada.

    Hay que hacerlo por ruta: `import radar` trae el PAQUETE `radar/`, que tiene el mismo
    nombre y lo tapa. Sin esto, ninguna prueba puede tocar la CLI, que es justo donde
    estaba el fallo de `paginas_primera_vez` que nadie vio.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("cli_radar", RAIZ / "radar.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


cli = _entrada()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.bd = Path(self.dir.name) / "t.db"
        self.con = db.conectar(self.bd)
        self.addCleanup(self.con.close)

    @property
    def TODAS(self):
        return list(range(1, len(pipeline.ETAPAS_PRIMERA_CARGA) + 1))

    def _marcar_todas(self):
        for n in self.TODAS:
            pipeline.marcar_etapa(self.con, n)

    def _args(self, sin_autocarga=False):
        return SimpleNamespace(bd=self.bd, sin_autocarga=sin_autocarga)

    def _ingesta(self, *, hace_horas, ok=True):
        cuando = datetime.now(timezone.utc) - timedelta(hours=hace_horas)
        log_id = db.abrir_ingest(self.con, "oeffentlichevergabe")
        db.cerrar_ingest(self.con, log_id, vistos=10, nuevos=10, actualizados=0,
                         error=None if ok else "caída")
        self.con.execute("UPDATE ingest_log SET terminado_en = ? WHERE id = ?",
                         (cuando.isoformat(), log_id))
        self.con.commit()


class TestQueLeFalta(Base):
    def test_una_base_recien_creada_necesita_todas_las_etapas(self):
        t = pipeline.trabajo_pendiente(self.con)
        self.assertEqual(t["tipo"], "primera_carga")
        self.assertEqual(t["etapas"], self.TODAS)

    def test_una_etapa_hecha_deja_de_pedirse(self):
        pipeline.marcar_etapa(self.con, 1)
        self.assertEqual(pipeline.trabajo_pendiente(self.con)["etapas"], self.TODAS[1:])

    def test_con_todas_hechas_y_una_ingesta_reciente_no_falta_nada(self):
        self._marcar_todas()
        self._ingesta(hace_horas=2)
        self.assertIsNone(pipeline.trabajo_pendiente(self.con))

    def test_con_todas_hechas_pero_la_ingesta_vieja_toca_incremental(self):
        """20 horas y no 24: quien abre la aplicación cada mañana a la misma hora la
        abriría a veces cinco minutos antes, y con 24 se saltaría el día entero."""
        self._marcar_todas()
        self._ingesta(hace_horas=21)
        self.assertEqual(pipeline.trabajo_pendiente(self.con)["tipo"], "incremental")

    def test_una_ingesta_que_falló_no_cuenta_como_estar_al_día(self):
        self._marcar_todas()
        self._ingesta(hace_horas=1, ok=False)
        self.assertEqual(pipeline.trabajo_pendiente(self.con)["tipo"], "incremental")

    def test_las_etapas_mandan_sobre_la_ingesta_del_dia(self):
        """No tiene sentido pedir las novedades de ayer mientras faltan dos años de
        histórico; y además es la última etapa la que deja el cursor puesto."""
        pipeline.marcar_etapa(self.con, 1)
        self._ingesta(hace_horas=100)
        self.assertEqual(pipeline.trabajo_pendiente(self.con)["tipo"], "primera_carga")


class TestAlAbrir(Base):
    def test_lanza_las_etapas_que_faltan(self):
        with mock.patch.object(busqueda, "lanzar") as lanzar:
            cli._ponerse_al_dia(self._args())
        lanzar.assert_called_once_with(primera_carga=True, etapas=self.TODAS)

    def test_lanza_la_incremental_cuando_solo_falta_eso(self):
        self._marcar_todas()
        self._ingesta(hace_horas=30)
        with mock.patch.object(busqueda, "lanzar") as lanzar:
            cli._ponerse_al_dia(self._args())
        lanzar.assert_called_once_with()

    def test_no_lanza_nada_si_está_al_día(self):
        self._marcar_todas()
        self._ingesta(hace_horas=1)
        with mock.patch.object(busqueda, "lanzar") as lanzar:
            cli._ponerse_al_dia(self._args())
        lanzar.assert_not_called()

    def test_la_bandera_lo_apaga(self):
        with mock.patch.object(busqueda, "lanzar") as lanzar:
            cli._ponerse_al_dia(self._args(sin_autocarga=True))
        lanzar.assert_not_called()

    def test_si_ya_hay_una_en_marcha_no_se_pisa(self):
        """`lanzar` se niega con RuntimeError; abrir la bandeja no puede fallar por eso."""
        with mock.patch.object(busqueda, "lanzar",
                               side_effect=RuntimeError("ya hay una")) as lanzar:
            cli._ponerse_al_dia(self._args())   # no debe propagar
        lanzar.assert_called_once()


if __name__ == "__main__":
    unittest.main()
