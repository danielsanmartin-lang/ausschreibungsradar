"""Editar los términos desde la aplicación, y el cerrojo de las búsquedas."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from radar import busqueda, db, matching
from radar.model import Licitacion

RAIZ = Path(__file__).resolve().parent.parent


def perfil(**kw):
    base = {
        "name": "Prueba",
        "aktiv": True,
        "starke_begriffe": ["phishing"],
        "schwache_begriffe": ["concienci"],
        "erforderlicher_kontext": ["ciberseguridad"],
        "ausschluss": ["seguridad vial"],
        "cpv_praefixe": ["80533100"],
        "mindestbetrag": 10000,
    }
    base.update(kw)
    return base


class TestPerfilesDeEjemplo(unittest.TestCase):
    """El fichero de verdad no se versiona: solo viaja el ejemplo.

    Los términos con los que uno busca —las raíces, las erratas de los pliegos, las
    lenguas cooficiales— son el trabajo de la persona, y no tienen por qué acabar en un
    repositorio público ni ser sobrescritos por una actualización.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.suyo = Path(self.dir.name) / "suchprofile.json"

    def test_el_ejemplo_que_se_publica_es_valido(self):
        """Si el ejemplo estuviera roto, una instalación nueva no arrancaría."""
        self.assertTrue(matching.PERFILES_EJEMPLO.exists())
        self.assertGreaterEqual(len(matching.cargar_perfiles(matching.PERFILES_EJEMPLO)), 1)

    def test_el_ejemplo_es_un_perfil_de_verdad_y_no_un_juguete(self):
        """Aquí se cambia a propósito el criterio del radar español.

        Allí el ejemplo es una plantilla de mentira, para que los términos afinados de
        cada uno no acaben en un repositorio. El efecto secundario es que los casos
        reales de `test_matching` NO se ejecutan nunca en integración continua, porque se
        saltan cuando no hay perfil propio: el criterio de aceptación de la herramienta
        está apagado.

        Aquí el ejemplo es el perfil alemán completo, y lo que sigue sin versionarse es
        `config/suchprofile.json`, que es donde cada uno mete sus erratas, sus clientes y
        sus afinados. Lo que se protege no es la lista de palabras —cualquiera teclea
        «Phishing» y «DMARC»— sino la calibración y el histórico.
        """
        datos = json.loads(matching.PERFILES_EJEMPLO.read_text(encoding="utf-8"))
        perfiles = matching.validar_perfiles(datos["profile"])
        activos = matching.solo_activos(perfiles)
        self.assertGreaterEqual(len(activos), 2, "el ejemplo tiene que servir tal cual")
        for p in activos:
            with self.subTest(perfil=p.name):
                self.assertTrue(p.starke_begriffe, "un perfil sin términos no busca nada")
        self.assertIn("phishing", activos[0].starke_begriffe)

    def test_el_fichero_de_cada_uno_no_se_versiona(self):
        """Lo que no viaja es `suchprofile.json`, no el ejemplo."""
        ignorados = (Path(__file__).resolve().parent.parent / ".gitignore").read_text()
        self.assertIn("config/suchprofile.json", ignorados)

    def test_se_crea_del_ejemplo_cuando_no_existe(self):
        """Recién descargado solo está el ejemplo; que la primera vez salte un error en
        la cara de un compañero es una forma tonta de perder a un usuario."""
        ruta = matching.preparar_perfiles()
        self.assertTrue(ruta.exists())

    def test_no_pisa_el_fichero_de_quien_ya_lo_tiene(self):
        self.suyo.write_text(json.dumps({"profile": [perfil(name="Los mios")]}),
                             encoding="utf-8")
        matching.preparar_perfiles(self.suyo)
        datos = json.loads(self.suyo.read_text(encoding="utf-8"))
        self.assertEqual(datos["profile"][0]["name"], "Los mios")


class TestValidacion(unittest.TestCase):
    def test_acepta_un_perfil_correcto(self):
        self.assertEqual(len(matching.validar_perfiles([perfil()])), 1)

    def test_rechaza_un_perfil_activo_sin_terminos(self):
        """Aceptaría o rechazaría todo."""
        with self.assertRaises(ValueError) as ctx:
            matching.validar_perfiles([perfil(starke_begriffe=[], schwache_begriffe=[])])
        self.assertIn("keinen einzigen Begriff", str(ctx.exception))

    def test_rechaza_terminos_ambiguos_sin_contexto(self):
        """Es lo que llenaba la bandeja de campañas de feminización y seguridad vial."""
        with self.assertRaises(ValueError):
            matching.validar_perfiles([perfil(erforderlicher_kontext=[])])

    def test_rechaza_nombres_repetidos(self):
        with self.assertRaises(ValueError):
            matching.validar_perfiles([perfil(), perfil()])

    def test_rechaza_si_no_queda_ninguno_activo(self):
        with self.assertRaises(ValueError):
            matching.validar_perfiles([perfil(aktiv=False)])

    def test_rechaza_campos_inventados(self):
        with self.assertRaises(ValueError):
            matching.validar_perfiles([perfil(loquesea=["x"])])

    def test_rechaza_lista_vacia(self):
        with self.assertRaises(ValueError):
            matching.validar_perfiles([])

    def test_avisa_de_terminos_de_tres_letras_sin_espacio(self):
        """«ens» casa dentro de «defensa», «bienes» y «ensayo». Al editar desde la
        pantalla se recortaba el espacio final de «ens » y metía mil licitaciones de
        más. Se avisa, pero no se bloquea: «spf» o «dkim» son cortos y legítimos."""
        p = matching.validar_perfiles([perfil(erforderlicher_kontext=["ens"])])
        avisos = matching.avisos_perfiles(p)
        self.assertTrue(avisos)
        self.assertIn("innerhalb anderer Wörter", avisos[0])

    def test_no_avisa_del_termino_con_espacio(self):
        p = matching.validar_perfiles([perfil(erforderlicher_kontext=["ens "])])
        self.assertEqual(matching.avisos_perfiles(p), [])

    def test_los_perfiles_de_serie_no_dan_avisos_raros(self):
        """Los que vienen en el ejemplo ya usan el espacio donde hace falta.

        Se comprueba el ejemplo y no `suchprofile.json`: el segundo es el de cada uno, no
        se versiona y aquí puede contener cualquier cosa. El ejemplo es lo que recibe
        quien descarga el programa, así que es lo que tiene que estar bien."""
        p = matching.cargar_perfiles(matching.PERFILES_EJEMPLO)
        for aviso in matching.avisos_perfiles(p):
            self.assertNotIn("«ens»", aviso)

    def test_acepta_el_mismo_termino_con_espacio_final(self):
        p = matching.validar_perfiles([perfil(erforderlicher_kontext=["ens "])])
        self.assertEqual(p[0].erforderlicher_kontext, ["ens "])

    def test_el_espacio_significativo_cambia_lo_que_casa(self):
        from radar.matching import evaluar
        con_espacio = matching.validar_perfiles(
            [perfil(schwache_begriffe=["concienci"], erforderlicher_kontext=["ens "])])[0]
        # "defensa" no debe activar el contexto cuando el término lleva espacio.
        r = evaluar(con_espacio, "concienciacion en materia de defensa nacional",
                    [], 50000, None, "p")
        self.assertFalse(r.casa)
        r2 = evaluar(con_espacio, "concienciacion sobre el ens y sus requisitos",
                     [], 50000, None, "p")
        self.assertTrue(r2.casa)


class TestGuardado(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ruta = Path(self.dir.name) / "suchprofile.json"
        # Se parte del ejemplo, no del `suchprofile.json` de quien ejecute los tests: ese
        # no se versiona, así que en un clon recién descargado no existe todavía y en el
        # de cada uno contiene cualquier cosa. Los tests no pueden depender de eso.
        shutil.copy(matching.PERFILES_EJEMPLO, self.ruta)

    def tearDown(self):
        self.dir.cleanup()

    def test_conserva_la_ayuda_del_fichero(self):
        """Son las lecciones que costaron encontrar; perderlas sería un retroceso."""
        antes = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.assertIn("_hilfe", antes)
        matching.guardar_perfiles([perfil()], self.ruta)
        despues = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.assertEqual(despues["_hilfe"], antes["_hilfe"])

    def test_guarda_copia_del_anterior(self):
        matching.guardar_perfiles([perfil()], self.ruta)
        copia = self.ruta.parent / "suchprofile.vorherige.json"
        self.assertTrue(copia.exists())
        self.assertIn("Security Awareness und Phishing", copia.read_text(encoding="utf-8"))

    def test_lo_guardado_lo_puede_leer_la_cli(self):
        matching.guardar_perfiles([perfil(name="Editado desde la app")], self.ruta)
        leidos = matching.cargar_perfiles(self.ruta)
        self.assertEqual([p.name for p in leidos], ["Editado desde la app"])

    def test_un_perfil_invalido_no_toca_el_fichero(self):
        original = self.ruta.read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            matching.guardar_perfiles([perfil(erforderlicher_kontext=[])], self.ruta)
        self.assertEqual(self.ruta.read_text(encoding="utf-8"), original)
        self.assertFalse((self.ruta.parent / "suchprofile.json.tmp").exists())


class TestPrevisualizacion(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        for i, objeto in enumerate([
            "Servicio de concienciación en ciberseguridad para empleados",
            "Plataforma de phishing simulado",
            "Suministro de mobiliario de oficina",
        ]):
            db.guardar(self.con, Licitacion(
                fuente="p", id_externo=f"a{i}", objeto=objeto, organo="Órgano",
                valor_estimado=50000, cpv="80533100",
            ))
        matching.reevaluar(self.con, matching.validar_perfiles([perfil()]))

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def test_dice_cuantas_habria_sin_tocar_nada(self):
        antes = self.con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        p = matching.validar_perfiles([perfil(mindestbetrag=1000000)])
        vista = matching.previsualizar(self.con, p)
        self.assertEqual(vista["antes"], antes)
        self.assertEqual(vista["despues"], 0)
        self.assertEqual(vista["salen"], antes)
        # No ha escrito nada.
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM matches").fetchone()[0], antes
        )

    def test_no_cuenta_los_perfiles_desactivados(self):
        """«Ver qué cambiaría» tiene que decir lo que dejará el botón de guardar.

        La pantalla manda los perfiles tal cual están en la tabla, activos e
        inactivos, así que si aquí se contara uno desactivado la previsualización
        prometería coincidencias que `reevaluar` retira acto seguido."""
        p = matching.validar_perfiles([
            perfil(),
            perfil(name="Amplio", starke_begriffe=["mobiliario"], aktiv=False),
        ])
        vista = matching.previsualizar(self.con, p)
        self.assertEqual(vista["entran"], 0)
        self.assertEqual(vista["despues"], vista["antes"])

    def test_muestra_ejemplos_de_lo_que_entra(self):
        p = matching.validar_perfiles([
            perfil(starke_begriffe=["phishing", "mobiliario"], erforderlicher_kontext=["ciberseguridad"])
        ])
        vista = matching.previsualizar(self.con, p)
        self.assertGreater(vista["entran"], 0)
        self.assertTrue(vista["muestra_entran"])
        self.assertIn("objeto", vista["muestra_entran"][0])


class TestDesactivarDesdeLaAplicacion(unittest.TestCase):
    """Quitar la marca «activo» en la pantalla hace lo mismo que ponerlo a mano.

    Los dos caminos le dan a `reevaluar` listas distintas y esto es a propósito: la
    pantalla guarda con `validar_perfiles`, que devuelve TODOS los perfiles porque esa
    misma lista es la que se escribe en `suchprofile.json`, y la línea de órdenes lee con
    `cargar_perfiles`, que ya se queda solo con los activos. Durante un tiempo eso
    quiso decir que desactivar desde la aplicación no desactivaba: las coincidencias
    del perfil apagado seguían en la bandeja hasta que alguien relanzaba `match`.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ruta = Path(self.dir.name) / "suchprofile.json"
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        self.addCleanup(self.con.close)
        for i, objeto in enumerate([
            "Plataforma de phishing simulado",
            "Suministro de mobiliario de oficina",
        ]):
            db.guardar(self.con, Licitacion(
                fuente="p", id_externo=f"a{i}", objeto=objeto, organo="Órgano",
                valor_estimado=50000,
            ))
        self.con.commit()

    def por_perfil(self) -> dict:
        return {
            f["perfil"]: f["total"] for f in self.con.execute(
                "SELECT perfil, COUNT(*) AS total FROM matches GROUP BY perfil")
        }

    def perfiles_json(self, activo: bool) -> list[dict]:
        return [perfil(),
                perfil(name="Amplio", starke_begriffe=["mobiliario"], aktiv=activo)]

    def test_guardar_con_el_perfil_desactivado_retira_sus_coincidencias(self):
        matching.reevaluar(self.con, matching.validar_perfiles(self.perfiles_json(True)))
        self.assertEqual(self.por_perfil(), {"Prueba": 1, "Amplio": 1})

        stats = matching.reevaluar(
            self.con, matching.validar_perfiles(self.perfiles_json(False))
        )
        self.assertEqual(stats["por_perfil"], {"Prueba": 1})
        self.assertEqual(self.por_perfil(), {"Prueba": 1})

    def test_la_aplicacion_y_la_linea_de_ordenes_dejan_la_misma_bandeja(self):
        """Lo mismo que hace el botón «Guardar» tiene que hacer `radar.py match`."""
        matching.guardar_perfiles(self.perfiles_json(False), self.ruta)

        matching.reevaluar(self.con, matching.validar_perfiles(self.perfiles_json(False)))
        por_la_aplicacion = self.por_perfil()

        matching.reevaluar(self.con, matching.cargar_perfiles(self.ruta))
        self.assertEqual(self.por_perfil(), por_la_aplicacion)
        self.assertNotIn("Amplio", por_la_aplicacion)

    def test_el_perfil_desactivado_sigue_en_el_fichero_para_poder_volver(self):
        """Desactivar no es borrar: los términos se quedan escritos para reactivarlos."""
        matching.guardar_perfiles(self.perfiles_json(False), self.ruta)
        guardados = json.loads(self.ruta.read_text(encoding="utf-8"))["profile"]
        self.assertEqual([(p["name"], p["aktiv"]) for p in guardados],
                         [("Prueba", True), ("Amplio", False)])


class TestCerrojoBusqueda(unittest.TestCase):
    """Dos ingestas a la vez se pelean por el bloqueo de escritura de SQLite, y la
    tarea programada de cada mañana puede coincidir con el botón."""

    def setUp(self):
        self.original = busqueda.CERROJO
        self.dir = tempfile.TemporaryDirectory()
        busqueda.CERROJO = Path(self.dir.name) / "busqueda.lock"

    def tearDown(self):
        busqueda.CERROJO = self.original
        self.dir.cleanup()

    def test_solo_uno_toma_el_cerrojo(self):
        self.assertTrue(busqueda.adquirir("uno"))
        self.assertFalse(busqueda.adquirir("dos"))
        busqueda.liberar()
        self.assertTrue(busqueda.adquirir("tres"))
        busqueda.liberar()

    def test_sin_cerrojo_no_hay_nada_en_marcha(self):
        self.assertIsNone(busqueda.en_marcha())

    def test_el_cerrojo_dice_quien_lo_tiene(self):
        busqueda.adquirir("cli")
        activa = busqueda.en_marcha()
        self.assertEqual(activa["origen"], "cli")
        self.assertIn("iniciada", activa)
        busqueda.liberar()

    def test_un_cerrojo_huerfano_se_limpia_solo(self):
        """Si el proceso murió a lo bruto, la aplicación no puede quedarse
        bloqueada para siempre."""
        busqueda.CERROJO.write_text(json.dumps({"pid": 999999, "iniciada": "2020-01-01T00:00:00+00:00"}))
        self.assertIsNone(busqueda.en_marcha())
        self.assertFalse(busqueda.CERROJO.exists())
        self.assertTrue(busqueda.adquirir("nuevo"))
        busqueda.liberar()

    def test_un_cerrojo_ilegible_no_bloquea(self):
        busqueda.CERROJO.write_text("esto no es json")
        self.assertIsNone(busqueda.en_marcha())
        self.assertTrue(busqueda.adquirir("nuevo"))
        busqueda.liberar()


if __name__ == "__main__":
    unittest.main()
