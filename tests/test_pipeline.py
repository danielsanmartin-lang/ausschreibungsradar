"""Orquestación de la ingesta.

Lo que se protege aquí es que un fallo parcial no se lleve por delante lo que sí se
puede traer. La carga inicial pide varios años de golpe y no todas las fuentes publican
todos los años: con un solo `try` para todos, un 404 en uno dejaba sin histórico a los
que sí existían.
"""

import tempfile
import unittest
import zipfile
from unittest import mock
from pathlib import Path

from radar import db, net, pipeline
from radar.matching import Perfil
from radar.model import Licitacion
from radar.sources.ted import FuenteTED


class FuenteFalsa:
    """Fuente de mentira con un guion por año: licitaciones o excepción."""

    def __init__(self, nombre="falsa", guion=None):
        self.nombre = nombre
        self.guion = guion or {}
        self.anios_pedidos = []

    def historico(self, anio):
        self.anios_pedidos.append(anio)
        guion = self.guion.get(anio, 0)
        if isinstance(guion, Exception):
            raise guion
        for i in range(guion):
            yield Licitacion(fuente=self.nombre, id_externo=f"{anio}-{i}",
                             objeto=f"Concienciación {anio}-{i}", organo="Órgano")

    def incremental(self, cursor):
        return iter(())

    def cursor_nuevo(self):
        return None


class TestBackfillPorAnios(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        self.addCleanup(self.con.close)

    def _contar(self):
        return self.con.execute("SELECT COUNT(*) FROM licitaciones").fetchone()[0]

    def test_un_anio_sin_zip_publicado_no_es_un_error(self):
        """Un 404 en el ZIP anual es información, no una avería: si contara como
        error, una carga inicial que fue bien acabaría diciendo «alguna fuente ha
        fallado» y el compañero pensaría que hay algo roto."""
        fuente = FuenteFalsa(guion={
            2024: 3,
            2025: net.ErrorRed("no existe", codigo=404),
            2026: 2,
        })
        resumen = pipeline.ingerir(self.con, [fuente], anios=[2024, 2025, 2026])

        self.assertIsNone(resumen["falsa"]["error"])
        self.assertEqual(resumen["falsa"]["vistos"], 5)
        self.assertEqual(self._contar(), 5)
        self.assertEqual(fuente.anios_pedidos, [2024, 2025, 2026])

    def test_un_fallo_de_verdad_no_se_lleva_los_demas_anios(self):
        """Antes, cualquier excepción salía del bucle de años y los que quedaban se
        perdían. Se sigue con ellos, pero la fuente queda marcada."""
        fuente = FuenteFalsa(guion={
            2024: 3,
            2025: net.ErrorRed("timeout tras 4 intentos"),
            2026: 2,
        })
        resumen = pipeline.ingerir(self.con, [fuente], anios=[2024, 2025, 2026])

        self.assertIn("timeout", resumen["falsa"]["error"])
        self.assertEqual(self._contar(), 5)
        self.assertEqual(fuente.anios_pedidos, [2024, 2025, 2026])

    def test_un_zip_ilegible_en_la_cache_no_se_lleva_los_demas_anios(self):
        """Pasó de verdad: iCloud vació `licitaciones_2024.zip` dejando el tamaño en el
        directorio, `zipfile` respondió BadZipFile y —al capturarse solo ErrorRed— 2025 y
        2026 se quedaron sin intentar. La fuente entera acabó con 0 fichas."""
        fuente = FuenteFalsa(guion={
            2024: zipfile.BadZipFile("File is not a zip file"),
            2025: 3,
            2026: 2,
        })
        resumen = pipeline.ingerir(self.con, [fuente], anios=[2024, 2025, 2026])

        self.assertIn("BadZipFile", resumen["falsa"]["error"])
        self.assertEqual(self._contar(), 5, "los años buenos tienen que entrar igual")
        self.assertEqual(fuente.anios_pedidos, [2024, 2025, 2026])

    def test_lo_traido_antes_del_fallo_se_conserva(self):
        """El generador puede reventar a mitad de un año; lo ya guardado se queda."""
        class MitadYFallo(FuenteFalsa):
            def historico(self, anio):
                self.anios_pedidos.append(anio)
                yield Licitacion(fuente="falsa", id_externo=f"{anio}-ok",
                                 objeto="Concienciación superviviente", organo="Órgano")
                raise net.ErrorRed("se cortó la descarga")

        resumen = pipeline.ingerir(self.con, [MitadYFallo()], anios=[2024])
        self.assertIsNotNone(resumen["falsa"]["error"])
        self.assertEqual(self._contar(), 1)

    def test_una_fuente_rota_no_impide_la_siguiente(self):
        """Comportamiento de siempre: perder TED una mañana no debe impedir ver lo
        que ha publicado el servicio alemán."""
        rota = FuenteFalsa("rota", guion={2024: net.ErrorRed("caída")})
        buena = FuenteFalsa("buena", guion={2024: 4})
        resumen = pipeline.ingerir(self.con, [rota, buena], anios=[2024])

        self.assertIsNotNone(resumen["rota"]["error"])
        self.assertIsNone(resumen["buena"]["error"])
        self.assertEqual(resumen["buena"]["vistos"], 4)


class TestConstruirFuentes(unittest.TestCase):
    PERFILES = [Perfil(name="p", starke_begriffe=["phishing"])]

    def test_ted_se_pide_para_alemania(self):
        """El conector es el mismo que el del radar español; lo único que cambia es el
        país. Si esto se queda en ESP, la herramienta alemana trae licitaciones
        españolas y nadie se da cuenta hasta ver la bandeja."""
        (f,) = pipeline.construir_fuentes(["ted"], self.PERFILES)
        self.assertIsInstance(f, FuenteTED)
        self.assertEqual(f.pais, "DEU")

    def test_las_opciones_de_la_etapa_llegan_al_constructor(self):
        (f,) = pipeline.construir_fuentes(["ted"], self.PERFILES,
                                          opciones={"dias_ventana": 7})
        self.assertEqual(f.dias_ventana, 7)

    def test_ted_no_entra_en_el_conjunto_por_defecto(self):
        """Desde el 25/10/2023 todo lo que supera el umbral europeo pasa ya por el
        Bekanntmachungsservice (VgV §10a Abs. 5), así que TED no aporta registros nuevos
        y sí obligaría a reconciliar dos fuentes que se solapan casi del todo."""
        self.assertNotIn("ted", pipeline.FUENTES_POR_DEFECTO)
        self.assertIn("oeffentlichevergabe", pipeline.FUENTES_POR_DEFECTO)

    def test_las_opciones_de_cada_etapa_llegan_al_conector(self):
        """Lo que distingue una etapa de otra son justo esas opciones: sin llegar, las
        tres etapas harían exactamente lo mismo y la primera carga bajaría dos años con
        eForms incluido."""
        (f,) = pipeline.construir_fuentes(
            ["oeffentlichevergabe"], self.PERFILES,
            opciones={"meses_atras": 2, "con_eforms": True},
        )
        self.assertTrue(f.con_eforms)
        self.assertEqual(f.meses_atras, 2)

    def test_todas_las_etapas_piden_eforms(self):
        """Cuesta 2,3 GB más y aun así compensa, por dos motivos medidos.

        Uno: el precio de adjudicación solo está ahí. De 314 adjudicaciones de un día, el
        OCDS publica importe en 50 y el eForms en 231, o sea 15 % contra 73 %. Sin esto la
        pestaña de Auftragnehmer sale casi entera a 0 €.

        Dos: una pasada sin eForms sobre meses que ya se trajeron con eForms deja el plazo
        a NULL. Mientras hubo una etapa intermedia sin ellos, la primera carga terminaba
        con medio millón de anuncios y ni una sola fecha de cierre."""
        for etapa in pipeline.ETAPAS_PRIMERA_CARGA:
            with self.subTest(etapa=etapa["etiqueta"]):
                self.assertTrue(etapa["opciones"].get("con_eforms"),
                                "una etapa sin eForms borra lo que trajo la anterior")

    def test_la_ultima_etapa_tiene_que_traer_los_plazos(self):
        """El orden no es decorativo y esto se descubrió con la base llena.

        La etapa 2 reingiere los mismos meses que la 1 desde el export mensual, que no
        trae la Angebotsfrist, y al guardar la deja a NULL. Si la última etapa no vuelve a
        pasar con plazos, la primera carga termina con 575.000 anuncios y CERO fechas de
        cierre —medido—, o sea sin «cierran ≤7 días», sin orden por urgencia y sin la
        mitad del sentido de la bandeja."""
        self.assertTrue(pipeline.ETAPAS_PRIMERA_CARGA[-1]["opciones"]["con_eforms"])

    def test_una_fuente_desconocida_se_dice_con_las_opciones(self):
        with self.assertRaises(ValueError) as caja:
            pipeline.construir_fuentes(["placsp"], self.PERFILES)
        self.assertIn("ted", str(caja.exception))


class TestFusionCondicional(unittest.TestCase):
    """La fusión TED↔fuente nacional cuesta un GROUP BY por importe y fecha de cierre
    sobre toda la tabla, sin índice que lo apoye. Con TED apagado —que es el caso
    normal— no puede fusionar nada, así que ni se intenta."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        self.addCleanup(self.con.close)

    def test_sin_ted_no_se_fusiona(self):
        with mock.patch.object(db, "fusionar_grupos_ted") as fusionar:
            pipeline.ingerir(self.con, [FuenteFalsa("alemania", guion={2026: 2})],
                             anios=[2026])
        fusionar.assert_not_called()

    def test_con_ted_si_se_fusiona(self):
        with mock.patch.object(db, "fusionar_grupos_ted",
                               return_value={"anuncios_fusionados": 0, "ambiguos": 0}) as fusionar:
            pipeline.ingerir(self.con, [FuenteFalsa("ted", guion={2026: 1})], anios=[2026])
        fusionar.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class TestLaPrimeraCargaUsaLasOpcionesDeCadaEtapa(unittest.TestCase):
    """Regresión de un fallo que ninguna prueba cubría: `radar.py` llamaba a
    `construir_fuentes` con un parámetro que ya no existía, así que la primera carga se
    caía con un AttributeError en la primera etapa.

    No se comprueba la excepción sino lo que de verdad importa: que las `opciones`
    declaradas en cada etapa llegan al conector. Sin ellas las tres etapas harían lo
    mismo —dos años con eForms incluido— y la primera sería tan cara como la segunda.
    """

    def test_cada_etapa_construye_su_fuente_con_sus_opciones(self):
        perfiles = [Perfil(name="p", starke_begriffe=["phishing"])]
        for etapa in pipeline.ETAPAS_PRIMERA_CARGA:
            with self.subTest(etapa=etapa["etiqueta"]):
                fuentes = pipeline.construir_fuentes(
                    etapa["fuentes"], perfiles, dias_ventana=30,
                    opciones=etapa.get("opciones"),
                )
                self.assertTrue(fuentes)
                for f in fuentes:
                    self.assertEqual(f.con_eforms,
                                     etapa["opciones"].get("con_eforms", False))
                    self.assertEqual(f.meses_atras, etapa["opciones"].get("meses_atras"))

    def test_radar_py_no_usa_parametros_que_ya_no_existen(self):
        """La llamada vive en `radar.py`, que ninguna prueba importaba."""
        fuente = (Path(__file__).resolve().parent.parent / "radar.py").read_text()
        self.assertNotIn("paginas_primera_vez", fuente)
        self.assertIn("opciones=pipeline.opciones_de_etapa(etapa, meses)", fuente)


class FuenteConPlazos:
    """Fuente de mentira que solo publica la fecha de cierre si se le piden los plazos.

    Es exactamente lo que hace el conector real: la Angebotsfrist no está en el OCDS, así
    que una pasada sin eForms no la conoce y la guarda a NULL.
    """

    nombre = "falsa"

    def __init__(self, con_eforms=False, meses_atras=None):
        self.con_eforms = con_eforms
        self.meses_atras = meses_atras
        self._cursor_nuevo = "2026-09-16" if meses_atras else None

    def _ficha(self):
        return Licitacion(
            fuente=self.nombre, id_externo="uno", objeto="Awareness-Plattform",
            organo="Vergabestelle", id_procedimiento="ocds-x",
            fecha_limite_presentacion="2026-10-01T12:00:00" if self.con_eforms else None,
        )

    def historico(self, anio):
        yield self._ficha()

    def incremental(self, cursor):
        yield self._ficha()

    def cursor_nuevo(self):
        return self._cursor_nuevo


class TestLaPrimeraCargaNoSePisaAsiMisma(unittest.TestCase):
    """Regresión del fallo que dejó la base entera sin fechas de cierre."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        self.addCleanup(self.con.close)

    def _plazo(self):
        return self.con.execute(
            "SELECT fecha_limite_presentacion FROM licitaciones").fetchone()[0]

    def test_una_pasada_sin_plazos_borra_la_fecha_que_trajo_la_anterior(self):
        """El fallo, aislado: es por esto que el orden de las etapas importa."""
        pipeline.ingerir(self.con, [FuenteConPlazos(con_eforms=True)], anios=[2026])
        self.assertIsNotNone(self._plazo())
        pipeline.ingerir(self.con, [FuenteConPlazos(con_eforms=False)], anios=[2026])
        self.assertIsNone(self._plazo(), "una pasada sin plazos los deja a NULL")

    def test_las_tres_etapas_en_orden_dejan_la_fecha_puesta(self):
        """Y esta es la garantía de verdad: la primera carga completa, tal y como la
        ejecuta `radar.py`, tiene que terminar con las fechas de cierre en su sitio."""
        for etapa in pipeline.ETAPAS_PRIMERA_CARGA:
            fuente = FuenteConPlazos(
                con_eforms=etapa["opciones"].get("con_eforms", False),
                meses_atras=etapa["opciones"].get("meses_atras"),
            )
            pipeline.ingerir(self.con, [fuente],
                             anios=[2026] if etapa["historico"] else None)
        self.assertIsNotNone(self._plazo(),
                             "la primera carga ha terminado sin una sola fecha de cierre")

    def test_el_historico_deja_el_cursor_puesto(self):
        """Sin esto, tras una primera carga entera la ingesta de cada mañana volvería a
        empezar desde la ventana corta en vez de desde el último día traído."""
        pipeline.ingerir(self.con, [FuenteConPlazos(meses_atras=24)], anios=[2026])
        fila = db.leer_cursor(self.con, "falsa")
        self.assertEqual(fila["cursor"], "2026-09-16")

    def test_un_historico_que_falla_no_deja_cursor(self):
        """Dar el cursor por puesto tras un año a medias deja un hueco que no trae nadie."""
        rota = FuenteConPlazos(meses_atras=24)
        rota.historico = lambda anio: (_ for _ in ()).throw(net.ErrorRed("caída"))
        pipeline.ingerir(self.con, [rota], anios=[2026])
        self.assertIsNone(db.leer_cursor(self.con, "falsa"))
