"""El autodiagnóstico.

Lo que hay que proteger aquí es, sobre todo, que el diagnóstico **no toque nada**. Es un
comando que se lanza precisamente cuando algo va raro, a veces con una carga de dos horas
corriendo por detrás, y la tentación de «ya que estamos, lo arreglo» es exactamente lo
que no puede hacer: `db.conectar()` crearía la base si no está y podría disparar un
VACUUM de varios minutos sobre 3 GB, y `matching.preparar_perfiles()` crearía el fichero
de términos que ha ido a comprobar si existe.

Lo segundo es la invariante que hace útil el comando: nada que no salga «ok» puede
quedarse sin remedio.
"""

import json
import os
import plistlib
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock

from radar import busqueda, db, diagnostico, matching, net, programar
from radar.model import Licitacion


def lic(**kw) -> Licitacion:
    base = dict(
        fuente="placsp:licitaciones",
        id_externo="licitaciones:1",
        objeto="Servicio de simulación de phishing",
        organo="Ayuntamiento de Prueba",
        valor_estimado=120000,
    )
    base.update(kw)
    return Licitacion(**base)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.raiz = Path(self.dir.name)
        self.bd = self.raiz / "ausschreibungen.db"

    def _base_con_datos(self) -> None:
        con = db.conectar(self.bd)
        db.guardar(con, lic())
        con.commit()
        con.close()


class TestNoTocaNada(Base):
    def test_una_base_que_no_existe_no_se_crea(self):
        """Con `db.conectar` este test falla: dejaría un ausschreibungen.db vacío y sus -wal/-shm."""
        c = diagnostico.base_de_datos(self.bd)
        self.assertEqual(c.estado, "aviso")
        self.assertFalse(self.bd.exists())
        self.assertEqual(list(self.raiz.iterdir()), [])

    def test_el_diagnostico_no_dispara_ninguna_migracion(self):
        """Migrar puede recalcular las claves de grupo de 673.755 filas y lanzar un
        VACUUM de minutos. Un diagnóstico no puede permitirse eso."""
        self._base_con_datos()
        con_migrar = mock.patch.object(
            db, "migrar", side_effect=AssertionError("el diagnóstico no migra"))
        con_claves = mock.patch.object(
            db, "recomputar_claves_grupo",
            side_effect=AssertionError("el diagnóstico no recalcula nada"))
        with con_migrar, con_claves:
            comprobaciones = diagnostico.diagnosticar(bd=self.bd, perfiles=self.raiz / "p.json")
        self.assertTrue(comprobaciones)

    def test_dice_que_faltan_migraciones_sin_ejecutarlas(self):
        self._base_con_datos()
        con = db.conectar(self.bd)  # este arranque escribe las versiones al día
        db.escribir_preferencia(con, "version_clave_grupo", "2")
        con.commit()
        con.close()

        c = diagnostico.migraciones_pendientes(self.bd)
        self.assertEqual(c.estado, "aviso")
        self.assertIn("version_clave_grupo", c.mensaje)
        self.assertTrue(c.remedio)

        con = diagnostico._abrir_solo_lectura(self.bd)
        self.addCleanup(con.close)
        self.assertEqual(
            con.execute("SELECT valor FROM preferencias WHERE clave = 'version_clave_grupo'")
            .fetchone()[0], "2", "el diagnóstico no ha migrado nada")

    def test_una_base_al_dia_no_tiene_nada_pendiente(self):
        self._base_con_datos()
        # Las migraciones de datos solo se aplican con la tabla ya poblada, así que hace
        # falta un arranque más: es exactamente lo que pasa en una instalación nueva
        # entre la primera ingesta y la siguiente vez que se abre la aplicación.
        db.conectar(self.bd).close()
        self.assertEqual(diagnostico.migraciones_pendientes(self.bd).estado, "ok")

    def test_entre_la_primera_ingesta_y_el_siguiente_arranque_lo_dice(self):
        """Es un estado real y transitorio, y avisar de que el próximo arranque va a
        tardar unos minutos recalculando es justo lo que evita pensar que se ha colgado."""
        self._base_con_datos()
        c = diagnostico.migraciones_pendientes(self.bd)
        self.assertEqual(c.estado, "aviso")
        self.assertIn("stehen Schemaänderungen aus", c.mensaje)
        self.assertIn("automatisch", c.remedio)

    def test_un_fichero_que_no_es_una_base_da_un_error_legible(self):
        self.bd.write_bytes(b"esto no es sqlite")
        c = diagnostico.base_de_datos(self.bd)
        self.assertEqual(c.estado, "error")
        self.assertTrue(c.remedio)
        self.assertNotIn("Traceback", c.mensaje)

    def test_sin_perfiles_json_se_avisa_pero_no_se_crea_el_fichero(self):
        """`matching.leer_fichero_perfiles` lo crearía copiando el ejemplo."""
        ruta = self.raiz / "suchprofile.json"
        c = diagnostico.terminos_de_busqueda(ruta)
        self.assertEqual(c.estado, "aviso")
        self.assertFalse(ruta.exists())

    def test_el_diagnostico_no_escribe_nada_en_la_carpeta_de_datos(self):
        """Los `-wal`/`-shm` se quedan fuera de la foto: cualquier lector de una base en
        WAL los crea si no están, y eso no es escribir en la base."""
        def foto():
            return {f.name: f.stat().st_mtime_ns for f in self.raiz.iterdir()
                    if not f.name.endswith(("-wal", "-shm"))}

        self._base_con_datos()
        antes = foto()
        diagnostico.diagnosticar(bd=self.bd, perfiles=self.raiz / "p.json")
        self.assertEqual(foto(), antes)


class TestCertificados(unittest.TestCase):
    def test_el_bundle_real_del_repo_pasa_el_diagnostico(self):
        self.assertEqual(diagnostico.bundle_de_certificados().estado, "ok")

    def test_un_bundle_que_falta_es_un_error_con_remedio(self):
        """Sin las raíces de la FNMT, PLACSP falla el handshake con un mensaje que no se
        parece en nada a «faltan certificados»."""
        dir_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(dir_tmp.cleanup)
        parches = [
            mock.patch.object(net, "BUNDLE_CA", Path(dir_tmp.name) / "no-esta.pem"),
            # `contexto_ssl` memoiza en este global: sin resetearlo, el test mediría el
            # contexto que haya dejado cargado cualquier otra prueba.
            mock.patch.object(net, "_contexto", None),
        ]
        for p in parches:
            p.start()
            self.addCleanup(p.stop)
        c = diagnostico.bundle_de_certificados()
        self.assertEqual(c.estado, "error")
        self.assertIn("regenerar_ca_bundle", c.remedio)


class TestEspacioEnDisco(unittest.TestCase):
    def _fingir(self, libres: int):
        uso = type("Uso", (), {"total": 500_000_000_000, "used": 0, "free": libres})()
        return mock.patch.object(diagnostico.shutil, "disk_usage", return_value=uso)

    def test_sin_espacio_para_escribir_es_un_error(self):
        with self._fingir(900_000_000):
            self.assertEqual(diagnostico.espacio_en_disco().estado, "error")

    def test_sin_espacio_para_una_carga_inicial_es_solo_un_aviso(self):
        with self._fingir(8_000_000_000):
            c = diagnostico.espacio_en_disco()
        self.assertEqual(c.estado, "aviso")
        self.assertIn("Erstbefüllung", c.mensaje)

    def test_con_sitio_de_sobra_no_dice_nada(self):
        with self._fingir(300_000_000_000):
            self.assertEqual(diagnostico.espacio_en_disco().estado, "ok")


class TestIngesta(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        parche = mock.patch.object(busqueda, "CERROJO",
                                   Path(self.dir.name) / "busqueda.lock")
        parche.start()
        self.addCleanup(parche.stop)

    def test_sin_nada_en_marcha_no_hay_nada_que_decir(self):
        c = diagnostico.ingesta_en_marcha()
        self.assertEqual(c.estado, "ok")
        self.assertFalse(c.datos["en_marcha"])

    def test_un_cerrojo_huerfano_se_reporta_y_se_limpia(self):
        """El cerrojo de un proceso muerto bloquea el botón «Buscar ahora» y hay que
        poder saber que era eso."""
        busqueda.CERROJO.write_text(json.dumps(
            {"pid": 999999, "iniciada": "2020-01-01T00:00:00+00:00"}), encoding="utf-8")
        c = diagnostico.ingesta_en_marcha()
        self.assertEqual(c.estado, "aviso")
        self.assertIn("Sperre", c.mensaje)
        self.assertFalse(busqueda.CERROJO.exists())

    def test_una_ingesta_en_marcha_no_se_confunde_con_un_cerrojo_roto(self):
        """Es el mismo error que ya arregló `consultas.salud` con `en_curso`: una carga
        de dos horas en segundo plano no es una avería."""
        with mock.patch.object(busqueda, "en_marcha",
                               return_value={"pid": 1, "iniciada": "2026-08-17T09:00:00"}):
            c = diagnostico.ingesta_en_marcha()
        self.assertEqual(c.estado, "ok")
        self.assertIn("läuft ein Abruf", c.mensaje)


class TestCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cache = Path(self.dir.name)

    def _zip(self, nombre: str) -> Path:
        ruta = self.cache / nombre
        with zipfile.ZipFile(ruta, "w") as zf:
            zf.writestr("lote_1.atom", "<feed/>")
        return ruta

    def test_una_cache_normal_no_es_un_problema(self):
        """Tener 5 GB de históricos es lo esperado, no una avería."""
        self._zip("licitaciones_2024.zip")
        c = diagnostico.cache_de_historicos(self.cache)
        self.assertEqual(c.estado, "ok")
        self.assertEqual(c.datos["ficheros"], 1)

    def test_avisa_de_los_zip_que_no_se_pueden_leer(self):
        self._zip("licitaciones_2024.zip")
        (self.cache / "licitaciones_2025.zip").write_text("no soy un zip")
        c = diagnostico.cache_de_historicos(self.cache)
        self.assertEqual(c.estado, "aviso")
        self.assertEqual(c.datos["ilegibles"], ["licitaciones_2025.zip"])
        self.assertTrue(c.remedio)

    def test_avisa_de_una_descarga_a_medias(self):
        self._zip("licitaciones_2024.zip")
        (self.cache / "licitaciones_2026.zip.parcial").write_bytes(b"x" * 100)
        c = diagnostico.cache_de_historicos(self.cache)
        self.assertEqual(c.estado, "aviso")
        self.assertEqual(c.datos["parciales"], ["licitaciones_2026.zip.parcial"])
        self.assertIn("setzen dort fort", c.remedio)

    def test_el_hueco_de_icloud_se_reconoce_por_los_bloques(self):
        """Un fichero que declara 1,8 GB y no tiene ni un bloque detrás está vaciado."""
        class Hueco:
            st_size, st_blocks = 1_800_000_000, 0

        class Entero:
            st_size, st_blocks = 1_800_000_000, 3_515_625

        class SinSoporte:  # Windows no trae st_blocks
            st_size = 1_800_000_000

        self.assertTrue(diagnostico._sin_bloques(Hueco()))
        self.assertFalse(diagnostico._sin_bloques(Entero()))
        self.assertFalse(diagnostico._sin_bloques(SinSoporte()),
                         "sin el dato, lo correcto es no tocar nada")

    def test_un_zip_vaciado_por_icloud_no_se_abre(self):
        """Abrirlo dispara la descarga de 1,8 GB que iCloud se llevó, y este comando
        promete no hacer nada.

        Se parchea `_sin_bloques` y no `os.stat`: el código llama a `Path.stat()`, que en
        Python 3.9 no pasa por el `os` del módulo, así que parchear ahí daba un test que
        pasaba en 3.14 y fallaba en 3.9.
        """
        self._zip("licitaciones_2024.zip")
        with mock.patch.object(diagnostico, "_sin_bloques", return_value=True), \
             mock.patch.object(diagnostico.zipfile, "is_zipfile",
                               side_effect=AssertionError("no se puede abrir")):
            c = diagnostico.cache_de_historicos(self.cache)
        self.assertEqual(c.estado, "aviso")
        self.assertEqual(c.datos["vaciados"], ["licitaciones_2024.zip"])

    def test_un_tramo_antiguo_de_la_cache_nunca_caduca(self):
        """El radar español vigilaba la edad del ZIP porque PLACSP reescribía el del año
        en curso cada día, y reaprovecharlo en noviembre hacía parecer que el año estaba
        completo cuando le faltaban semanas.

        Aquí esa clase de fallo no puede ocurrir: solo se cachean tramos ya CERRADOS —un
        mes que terminó, un día que terminó— y esos no vuelven a cambiar nunca. Un
        fichero de hace diez días es tan válido como el de esta mañana."""
        ruta = self._zip("ov_ocds_2025-03.zip")
        viejo = os.stat(ruta).st_mtime - 10 * 24 * 3600
        os.utime(ruta, (viejo, viejo))
        c = diagnostico.cache_de_historicos(self.cache)
        self.assertEqual(c.estado, "ok")
        self.assertNotIn("refrescará", c.mensaje)


class TestTareaDiaria(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.plist = Path(self.dir.name) / "app.zepo.ausschreibungsradar.ingest.plist"
        parche = mock.patch.object(programar, "PLIST", self.plist)
        parche.start()
        self.addCleanup(parche.stop)

    def _instalar(self, *, python=None, guion=None, hora=8, minuto=30) -> None:
        with self.plist.open("wb") as fh:
            plistlib.dump({
                "Label": programar.ETIQUETA,
                "ProgramArguments": [python or os.sys.executable, "-u",
                                     guion or str(diagnostico.RAIZ / "radar.py"),
                                     "ingest"],
                "StartCalendarInterval": {"Hour": hora, "Minute": minuto},
            }, fh)

    def _launchctl(self, codigo: int):
        return mock.patch.object(programar, "_launchctl", return_value=(codigo, ""))

    def test_no_tenerla_instalada_es_una_eleccion_legitima(self):
        c = diagnostico.tarea_diaria()
        self.assertEqual(c.estado, "aviso")
        self.assertIn("programar", c.remedio)

    def test_la_tarea_dice_a_que_hora_se_ejecuta(self):
        self._instalar(hora=8, minuto=30)
        with self._launchctl(0):
            c = diagnostico.tarea_diaria()
        self.assertEqual(c.estado, "ok")
        self.assertIn("08:30", c.mensaje)

    def test_la_tarea_instalada_pero_no_cargada_es_un_error(self):
        self._instalar()
        with self._launchctl(1):
            c = diagnostico.tarea_diaria()
        self.assertEqual(c.estado, "error")
        self.assertIn("launchd", c.mensaje)

    def test_una_tarea_que_apunta_a_un_python_que_ya_no_existe_es_un_error(self):
        """Fallo silencioso de verdad: launchd la ejecuta cada mañana y no hace nada.
        Pasa al actualizar Python, que se lleva el intérprete que se grabó al instalar."""
        self._instalar(python="/opt/python-de-hace-dos-anos/bin/python3")
        with self._launchctl(0):
            c = diagnostico.tarea_diaria()
        self.assertEqual(c.estado, "error")
        self.assertIn("Python", c.mensaje)

    def test_una_tarea_que_apunta_a_otra_copia_del_proyecto_es_un_error(self):
        self._instalar(guion=str(Path(self.dir.name) / "otra-copia" / "radar.py"))
        with self._launchctl(0):
            c = diagnostico.tarea_diaria()
        self.assertEqual(c.estado, "error")
        self.assertIn("andere Kopie", c.mensaje)

    def test_el_registro_que_crece_sin_limite_se_avisa(self):
        registro = Path(self.dir.name) / "ingest.log"
        registro.write_bytes(b"x" * 200)
        with mock.patch.object(programar, "LOG", registro), \
             mock.patch.object(diagnostico, "MB_MAXIMO_LOG", 0):
            c = diagnostico.registro_de_la_tarea()
        self.assertEqual(c.estado, "aviso")
        self.assertIn("rotiert", c.mensaje)


class TestTerminosDeBusqueda(Base):
    def _escribir(self, perfiles) -> Path:
        ruta = self.raiz / "suchprofile.json"
        ruta.write_text(json.dumps({"profile": perfiles}), encoding="utf-8")
        return ruta

    def test_el_ejemplo_que_viaja_en_el_repositorio_es_valido(self):
        c = diagnostico.terminos_de_busqueda(matching.PERFILES_EJEMPLO)
        self.assertEqual(c.estado, "ok", c.mensaje)

    def test_unos_perfiles_invalidos_se_reportan_con_el_motivo_exacto(self):
        ruta = self._escribir([{
            "name": "Roto", "terminos_debiles": ["concienci"], "contexto_requerido": [],
        }])
        c = diagnostico.terminos_de_busqueda(ruta)
        self.assertEqual(c.estado, "error")
        self.assertIn("contexto", c.mensaje)

    def test_un_json_a_medio_escribir_no_tumba_el_diagnostico(self):
        ruta = self.raiz / "suchprofile.json"
        ruta.write_text("{ a medio escri", encoding="utf-8")
        c = diagnostico.terminos_de_busqueda(ruta)
        self.assertEqual(c.estado, "error")
        self.assertIn("suchprofile.vorherige.json", c.remedio)

    def test_los_avisos_de_terminos_cortos_llegan_al_diagnostico(self):
        ruta = self._escribir([{
            "name": "Corto", "starke_begriffe": ["bsi"],
        }])
        c = diagnostico.terminos_de_busqueda(ruta)
        self.assertEqual(c.estado, "aviso")
        self.assertIn("innerhalb anderer Wörter", c.mensaje)


class TestIntegridad(Base):
    def test_sin_la_bandera_no_se_lee_la_base_entera(self):
        """Son casi 50 s medidos sobre 3,3 GB: no puede ser el comportamiento normal."""
        self._base_con_datos()
        c = diagnostico.integridad_de_la_base(self.bd)
        self.assertEqual(c.estado, "omitida")
        self.assertIn("--integridad", c.remedio)

    def test_con_la_bandera_comprueba_de_verdad(self):
        self._base_con_datos()
        c = diagnostico.integridad_de_la_base(self.bd, ejecutar=True)
        self.assertEqual(c.estado, "ok", c.mensaje)
        self.assertEqual(c.datos["quick_check"], "ok")

    def test_un_indice_de_texto_desincronizado_es_un_error(self):
        """No da ningún error por su cuenta: la caja de búsqueda simplemente deja de
        encontrar cosas, y eso no se nota."""
        self._base_con_datos()
        con = sqlite3.connect(self.bd)
        con.execute("DELETE FROM licitaciones_fts")
        con.commit()
        con.close()
        c = diagnostico.integridad_de_la_base(self.bd, ejecutar=True)
        self.assertEqual(c.estado, "error")
        self.assertIn("die Suche findet nicht alles", c.mensaje)


class TestVersionPublicada(unittest.TestCase):
    def test_sin_con_red_no_se_pregunta_a_github(self):
        """Detrás de un portal cautivo son más de treinta segundos de espera."""
        from radar import actualizacion

        with mock.patch.object(actualizacion, "comprobar",
                               side_effect=AssertionError("no debe salir a internet")):
            c = diagnostico.version_publicada()
        self.assertEqual(c.estado, "omitida")
        self.assertIn("--con-red", c.remedio)

    def test_quedarse_sin_red_no_es_un_error_del_programa(self):
        from radar import actualizacion

        respuesta = {"version_actual": "1.0.1", "version_nueva": None, "hay_nueva": False,
                     "notas": "", "url_zip": None, "error": "No se ha podido preguntar"}
        with mock.patch.object(actualizacion, "comprobar", return_value=respuesta):
            c = diagnostico.version_publicada(ejecutar=True)
        self.assertEqual(c.estado, "aviso", "el código de salida no puede depender del wifi")

    def test_una_version_nueva_se_anuncia_con_el_comando_para_traerla(self):
        from radar import actualizacion

        respuesta = {"version_actual": "1.0.1", "version_nueva": "1.2.0", "hay_nueva": True,
                     "notas": "", "url_zip": "https://x", "error": None}
        with mock.patch.object(actualizacion, "comprobar", return_value=respuesta):
            c = diagnostico.version_publicada(ejecutar=True)
        self.assertEqual(c.estado, "aviso")
        self.assertIn("radar.py actualizar", c.remedio)


class TestConjunto(Base):
    def test_toda_comprobacion_que_no_esta_en_ok_trae_remedio(self):
        """Es la invariante que hace útil el comando: decir «algo va mal» y dejar al
        usuario buscando en el README no vale para nada.

        Se exige a los avisos y a los errores, que son los que señalan algo. Una
        comprobación `omitida` porque no hay base que mirar no tiene nada que remediar
        por su cuenta: eso ya lo dice «Base de datos».
        """
        comprobaciones = diagnostico.diagnosticar(
            bd=self.bd, perfiles=self.raiz / "no-esta.json")
        problemas = [c for c in comprobaciones if c.estado in ("aviso", "error")]
        self.assertTrue(problemas, "esta instalación de mentira tiene que dar avisos")
        for c in problemas:
            with self.subTest(comprobacion=c.nombre):
                self.assertTrue(c.remedio, f"«{c.nombre}» no dice qué hacer")

    def test_todos_los_estados_son_de_los_previstos(self):
        for c in diagnostico.diagnosticar(bd=self.bd, perfiles=self.raiz / "p.json"):
            with self.subTest(comprobacion=c.nombre):
                self.assertIn(c.estado, diagnostico.ESTADOS)

    def test_el_codigo_de_salida_solo_falla_con_errores(self):
        aviso = diagnostico.Comprobacion("X", "aviso", "algo")
        error = diagnostico.Comprobacion("Y", "error", "algo")
        omitida = diagnostico.Comprobacion("Z", "omitida", "algo")
        self.assertFalse(diagnostico.hay_errores([aviso, omitida]))
        self.assertTrue(diagnostico.hay_errores([aviso, error]))

    def test_las_comprobaciones_se_serializan_a_json(self):
        """Es lo que consume `doctor --json`."""
        self._base_con_datos()
        salida = diagnostico.a_json(
            diagnostico.diagnosticar(bd=self.bd, perfiles=self.raiz / "p.json"))
        json.dumps(salida)  # no debe lanzar
        self.assertIn("comprobaciones", salida)
        self.assertIsInstance(salida["ok"], bool)


if __name__ == "__main__":
    unittest.main()
