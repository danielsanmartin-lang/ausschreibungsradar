"""Deduplicación, historial de versiones y triaje.

La ingesta se ejecuta a diario y el servicio nacional republica una licitación cada vez que se
toca cualquier cosa: si el dedup falla, la bandeja se llena de duplicados y la
herramienta se vuelve inservible en una semana.
"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from radar import db
from radar.model import Licitacion


def lic(**kw):
    base = dict(
        fuente="prueba",
        id_externo="X1",
        objeto="Servicio de concienciación en ciberseguridad",
        organo="Ayuntamiento de Prueba",
        valor_estimado=120000,
        cpv="80533100-3",
        estado="veroeffentlicht",
        fecha_limite_presentacion="2026-12-01T13:00:00",
    )
    base.update(kw)
    return Licitacion(**base)


class TestGuardar(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = db.conectar(Path(self.dir.name) / "t.db")

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def test_insercion_y_dedup(self):
        self.assertEqual(db.guardar(self.con, lic()), "nueva")
        self.assertEqual(db.guardar(self.con, lic()), "igual")
        self.assertEqual(db.guardar(self.con, lic()), "igual")
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM licitaciones").fetchone()[0], 1
        )
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM licitaciones_versiones").fetchone()[0], 1,
            "una republicación idéntica no debe crear versión nueva",
        )

    def test_la_fecha_de_actualizacion_no_cuenta_como_cambio(self):
        """el servicio nacional cambia <updated> en cada republicación aunque el fondo sea igual."""
        db.guardar(self.con, lic(fecha_actualizacion="2026-08-01T10:00:00"))
        estado = db.guardar(self.con, lic(fecha_actualizacion="2026-08-05T18:30:00"))
        self.assertEqual(estado, "igual")

    def test_cambio_real_crea_version_con_transicion(self):
        db.guardar(self.con, lic())
        estado = db.guardar(self.con, lic(estado="zuschlag", adjudicatario="Empresa X",
                                         importe_adjudicacion=98000))
        self.assertEqual(estado, "actualizada")
        versiones = self.con.execute(
            "SELECT estado_anterior, estado, adjudicatario FROM licitaciones_versiones ORDER BY id"
        ).fetchall()
        self.assertEqual(len(versiones), 2)
        self.assertEqual(versiones[-1]["estado_anterior"], "veroeffentlicht")
        self.assertEqual(versiones[-1]["estado"], "zuschlag")
        self.assertEqual(versiones[-1]["adjudicatario"], "Empresa X")

    def test_la_version_se_fecha_con_la_fecha_de_la_fuente(self):
        """El historial cuenta cuándo pasó, no cuándo lo descargamos.

        Es la regresión que hacía inútil el bloque: en la carga inicial las cuatro
        republicaciones de un expediente de 2024 se leen del mismo ZIP en el mismo
        minuto, y las cuatro salían fechadas hoy.
        """
        db.guardar(self.con, lic(fecha_publicacion="2024-04-23"))
        db.guardar(self.con, lic(estado="zuschlag", adjudicatario="Empresa X",
                                 fecha_publicacion="2024-04-23",
                                 fecha_actualizacion="2024-06-10T09:00:00"))
        fechas = [
            f["fecha_cambio"] for f in self.con.execute(
                "SELECT fecha_cambio FROM licitaciones_versiones ORDER BY id"
            )
        ]
        self.assertEqual(fechas[0][:10], "2024-04-23")
        self.assertEqual(fechas[1][:10], "2024-06-10")
        hoy = date.today().isoformat()
        self.assertNotIn(hoy, [f[:10] for f in fechas])

    def test_sin_fechas_en_la_fuente_la_version_queda_sin_fecha_de_cambio(self):
        """NULL es lo que hace que la interfaz diga «visto el …» en vez de inventarse
        una fecha oficial."""
        db.guardar(self.con, lic())
        fila = self.con.execute(
            "SELECT detectado_en, fecha_cambio FROM licitaciones_versiones"
        ).fetchone()
        self.assertIsNone(fila["fecha_cambio"])
        self.assertTrue(fila["detectado_en"])

    def test_fuentes_distintas_no_colisionan(self):
        db.guardar(self.con, lic(fuente="oeffentlichevergabe", id_externo="1"))
        db.guardar(self.con, lic(fuente="ted", id_externo="1"))
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM licitaciones").fetchone()[0], 2
        )

    def test_fts_se_mantiene_sincronizado(self):
        db.guardar(self.con, lic())
        db.guardar(self.con, lic(objeto="Suministro de plataforma de phishing simulado"))
        n_fts = self.con.execute("SELECT COUNT(*) FROM licitaciones_fts").fetchone()[0]
        self.assertEqual(n_fts, 1, "actualizar no debe dejar filas huérfanas en el índice")
        hit = self.con.execute(
            "SELECT COUNT(*) FROM licitaciones_fts WHERE licitaciones_fts MATCH 'phishing'"
        ).fetchone()[0]
        self.assertEqual(hit, 1)
        viejo = self.con.execute(
            "SELECT COUNT(*) FROM licitaciones_fts WHERE licitaciones_fts MATCH 'concienciacion'"
        ).fetchone()[0]
        self.assertEqual(viejo, 0, "el texto antiguo no debe seguir indexado")

    def test_busqueda_ignora_acentos(self):
        db.guardar(self.con, lic())
        for termino in ("concienciacion", "concienciación", "CIBERSEGURIDAD"):
            with self.subTest(termino=termino):
                n = self.con.execute(
                    "SELECT COUNT(*) FROM licitaciones_fts WHERE licitaciones_fts MATCH ?",
                    (termino,),
                ).fetchone()[0]
                self.assertEqual(n, 1)


class TestMigracion(unittest.TestCase):
    """La base de cada compañero se autocorrige al arrancar.

    `CREATE TABLE IF NOT EXISTS` no toca una tabla que ya existe, así que sin la
    migración las bases en uso —con su triaje y sus notas dentro— se quedan sin las
    columnas nuevas y la aplicación falla al consultarlas.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ruta = Path(self.dir.name) / "vieja.db"

    def test_una_base_de_una_version_anterior_recibe_columna_e_indice(self):
        import sqlite3

        # El esquema de antes es el de ahora sin la columna nueva. Se construye quitando
        # sus dos líneas en lugar de con DROP COLUMN, que pide SQLite 3.35 y aquí se
        # soporta más atrás; el assert de abajo avisa si el recorte deja de aplicar.
        viejo = db.ESQUEMA.replace(
            "    -- Huella con la que se evaluaron los perfiles la última vez. "
            "Ver COLUMNAS_NUEVAS.\n"
            "    huella_evaluada           TEXT,\n",
            "",
        )
        self.assertNotIn("huella_evaluada", viejo, "el recorte del esquema ya no aplica")

        con = sqlite3.connect(self.ruta)
        con.executescript(viejo)
        con.execute(
            "INSERT INTO licitaciones (fuente, id_externo, huella, "
            "visto_primera_vez, visto_ultima_vez) VALUES ('prueba', 'X1', 'hhh', "
            "'2026-01-01', '2026-01-01')"
        )
        con.commit()
        con.close()

        con = db.conectar(self.ruta)
        self.addCleanup(con.close)
        columnas = {f["name"] for f in con.execute("PRAGMA table_info(licitaciones)")}
        self.assertIn("huella_evaluada", columnas)
        indices = {
            f[0] for f in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        self.assertIn("idx_lic_pendientes", indices)
        # Y la ficha que ya estaba queda pendiente de evaluar, no dada por evaluada:
        # darla por buena esconderla para siempre si nunca llegó a evaluarse.
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM licitaciones "
                        "WHERE huella_evaluada IS NOT huella").fetchone()[0], 1)


class TestSnapshots(unittest.TestCase):
    """El snapshot de cada versión guarda solo lo que se usa.

    Guardar una copia de todos los campos suponía 534 MB de las 269.000 versiones
    —el 46% de la base— de datos que la aplicación no lee en ningún sitio.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = db.conectar(Path(self.dir.name) / "t.db")

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def test_el_snapshot_es_compacto(self):
        db.guardar(self.con, lic(descripcion="x" * 4000, objeto="y" * 2000))
        snapshot = self.con.execute(
            "SELECT snapshot FROM licitaciones_versiones"
        ).fetchone()[0]
        self.assertLess(len(snapshot), 400, "el snapshot no debe copiar el objeto entero")
        import json
        datos = json.loads(snapshot)
        self.assertEqual(set(datos), set(db.CAMPOS_SNAPSHOT))

    def test_conserva_lo_necesario_para_auditar(self):
        db.guardar(self.con, lic())
        db.guardar(self.con, lic(estado="zuschlag", adjudicatario="Empresa X",
                                 importe_adjudicacion=50000))
        import json
        ultimo = json.loads(self.con.execute(
            "SELECT snapshot FROM licitaciones_versiones ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
        self.assertEqual(ultimo["estado"], "zuschlag")
        self.assertEqual(ultimo["adjudicatario"], "Empresa X")
        self.assertEqual(ultimo["importe_adjudicacion"], 50000)

    def test_el_recorte_de_snapshots_antiguos_es_idempotente(self):
        db.guardar(self.con, lic())
        self.assertEqual(db.recortar_snapshots(self.con), 0,
                         "los nuevos ya vienen compactos")


class TestRevisiones(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        db.guardar(self.con, lic())
        self.lic_id = self.con.execute("SELECT id FROM licitaciones").fetchone()[0]

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def _estado(self):
        f = self.con.execute(
            "SELECT estado, notas FROM revisiones WHERE licitacion_id = ?", (self.lic_id,)
        ).fetchone()
        return (f["estado"], f["notas"]) if f else (None, None)

    def test_guardar_notas_no_borra_el_estado(self):
        """Regresión: escribir una nota sacaba la licitación del seguimiento."""
        db.fijar_revision(self.con, self.lic_id, estado="siguiendo")
        self.assertEqual(self._estado()[0], "siguiendo")

        db.fijar_revision(self.con, self.lic_id, notas="llamar al CISO")
        estado, notas = self._estado()
        self.assertEqual(estado, "siguiendo", "las notas no deben tocar el estado")
        self.assertEqual(notas, "llamar al CISO")

    def test_cambiar_estado_no_borra_las_notas(self):
        db.fijar_revision(self.con, self.lic_id, notas="pendiente de hablar con compras")
        db.fijar_revision(self.con, self.lic_id, estado="presentada")
        estado, notas = self._estado()
        self.assertEqual(estado, "presentada")
        self.assertEqual(notas, "pendiente de hablar con compras")

    def test_estado_invalido_se_rechaza(self):
        with self.assertRaises(ValueError):
            db.fijar_revision(self.con, self.lic_id, estado="inventado")

    def test_el_triaje_sobrevive_a_una_actualizacion_de_la_licitacion(self):
        db.fijar_revision(self.con, self.lic_id, estado="siguiendo", notas="mía")
        db.guardar(self.con, lic(estado="zuschlag", adjudicatario="Otra Empresa"))
        self.assertEqual(self._estado(), ("siguiendo", "mía"))


class TestTriajePorExpediente(unittest.TestCase):
    """El triaje pertenece al expediente, no al anuncio.

    La bandeja colapsa los anuncios de un expediente en una tarjeta y muestra solo el
    más reciente, así que guardar el triaje únicamente en la fila que se tenía delante
    no bastaba: el filtro quitaba ese anuncio y la tarjeta volvía a salir con el
    siguiente, otra vez «sin revisar». Y la republicación del día siguiente entraba
    como fila nueva sin triaje, con el mismo efecto.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = db.conectar(Path(self.dir.name) / "t.db")

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def _anuncio(self, id_externo, expediente="EXP-1", **kw):
        db.guardar(self.con, lic(id_externo=id_externo, expediente=expediente, **kw))
        return self.con.execute(
            "SELECT id FROM licitaciones WHERE id_externo = ?", (id_externo,)
        ).fetchone()["id"]

    def _triaje(self):
        return {
            f["licitacion_id"]: f["estado"]
            for f in self.con.execute("SELECT licitacion_id, estado FROM revisiones")
        }

    def test_descartar_marca_todos_los_anuncios_del_expediente(self):
        primero = self._anuncio("A1")
        correccion = self._anuncio("A2")
        db.fijar_revision(self.con, correccion, estado="descartado",
                          motivo_descarte="fuera de nicho")
        self.assertEqual(self._triaje(), {primero: "descartado", correccion: "descartado"})

    def test_no_se_lleva_por_delante_otro_expediente(self):
        propio = self._anuncio("A1")
        ajeno = self._anuncio("B1", expediente="EXP-2")
        db.fijar_revision(self.con, propio, estado="descartado")
        self.assertEqual(self._triaje(), {propio: "descartado"})
        self.assertNotIn(ajeno, self._triaje())

    def test_un_anuncio_nuevo_hereda_el_triaje_del_expediente(self):
        """La republicación de mañana no puede devolver a la bandeja lo descartado hoy."""
        db.fijar_revision(self.con, self._anuncio("A1"), estado="descartado",
                          motivo_descarte="importe bajo")
        nuevo = self._anuncio("A2")  # el servicio nacional republica el expediente al corregirlo
        f = self.con.execute(
            "SELECT estado, motivo_descarte FROM revisiones WHERE licitacion_id = ?",
            (nuevo,),
        ).fetchone()
        self.assertEqual((f["estado"], f["motivo_descarte"]), ("descartado", "importe bajo"))

    def test_un_expediente_sin_triar_no_genera_filas_de_revision(self):
        """Heredar no significa crear triajes 'nuevo' que no dicen nada."""
        self._anuncio("A1")
        self._anuncio("A2")
        self.assertEqual(self._triaje(), {})

    def test_la_fusion_con_ted_arrastra_el_triaje(self):
        """El anuncio europeo entra con grupo propio y se fusiona después.

        Sin propagar detrás de la fusión, el expediente descartado volvía a la bandeja
        por la puerta de atrás: el anuncio de TED seguía marcado como «sin revisar».
        """
        nacional = self._anuncio(
            "P1", fuente="oeffentlichevergabe", expediente="J260047",
            organo="Junta de Contratación del Ministerio de Cultura",
            valor_estimado=1031857.23, fecha_limite_presentacion="2026-09-28T19:00:00")
        ted = self._anuncio(
            "T1", fuente="ted", expediente="548701-2026", organo="Ministerio de Cultura",
            objeto="España – Servicios – Servicio de oficina de ciberseguridad",
            valor_estimado=1031857.23,
            fecha_limite_presentacion="2026-09-28T19:00:00+02:00")
        db.fijar_revision(self.con, nacional, estado="descartado")
        self.assertNotIn(ted, self._triaje())

        db.fusionar_grupos_ted(self.con)
        self.assertEqual(db.propagar_revisiones_en_grupos(self.con), 1)
        self.assertEqual(self._triaje(), {nacional: "descartado", ted: "descartado"})

    def test_propagar_no_pisa_una_decision_ya_tomada(self):
        """Solo rellena huecos. En un empate de triajes no elige por la persona."""
        primero = self._anuncio("A1")
        segundo = self._anuncio("A2")
        # Se simula una base anterior a este cambio, con triajes distintos por anuncio.
        self.con.execute("DELETE FROM revisiones")
        for lic_id, estado in ((primero, "descartado"), (segundo, "siguiendo")):
            self.con.execute(
                """INSERT INTO revisiones (licitacion_id, estado, actualizado_en)
                   VALUES (?, ?, ?)""", (lic_id, estado, db.ahora()))
        self.assertEqual(db.propagar_revisiones_en_grupos(self.con), 0)
        self.assertEqual(self._triaje(), {primero: "descartado", segundo: "siguiendo"})


class TestFusionTedNacional(unittest.TestCase):
    """Una licitación sobre el umbral europeo se publica en el servicio nacional y en TED.

    Las dos filas tienen que acabar en el mismo grupo para no ocupar dos sitios de la
    bandeja ni pedir dos triajes.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = db.conectar(Path(self.dir.name) / "t.db")

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def _grupo(self, id_externo):
        return self.con.execute(
            "SELECT clave_grupo FROM licitaciones WHERE id_externo = ?", (id_externo,)
        ).fetchone()["clave_grupo"]

    def test_une_el_anuncio_de_ted_con_su_expediente_nacional(self):
        # Mismo contrato, órgano escrito distinto y título traducido: solo coinciden
        # el importe y el día de cierre, que es justo lo que usa la fusión.
        db.guardar(self.con, lic(
            fuente="oeffentlichevergabe", id_externo="P1", expediente="J260047",
            organo="Junta de Contratación del Ministerio de Cultura",
            objeto="Servicio de oficina de ciberseguridad en el ministerio de cultura",
            valor_estimado=1031857.23, fecha_limite_presentacion="2026-09-28T19:00:00"))
        db.guardar(self.con, lic(
            fuente="ted", id_externo="T1", expediente="548701-2026",
            organo="Ministerio de Cultura",
            objeto="España – Servicios de gestión de instalaciones – Servicio de oficina",
            valor_estimado=1031857.23, fecha_limite_presentacion="2026-09-28T19:00:00+02:00"))
        self.assertNotEqual(self._grupo("P1"), self._grupo("T1"))

        stats = db.fusionar_grupos_ted(self.con)
        self.assertEqual(stats["anuncios_fusionados"], 1)
        self.assertEqual(self._grupo("T1"), self._grupo("P1"))

        # Idempotente: volver a pasar no cambia nada ni vuelve a contar.
        self.assertEqual(db.fusionar_grupos_ted(self.con)["anuncios_fusionados"], 0)

    def test_no_fusiona_cuando_el_importe_y_la_fecha_son_ambiguos(self):
        """Dos licitaciones distintas con importe redondo y el mismo cierre.

        Se midieron 10 casos reales así sobre 629. Preferimos dos filas separadas a
        fusionar la equivocada, que mezclaría el triaje de dos expedientes.
        """
        for i in (1, 2):
            db.guardar(self.con, lic(
                fuente="oeffentlichevergabe", id_externo=f"P{i}", expediente=f"EXP{i}",
                objeto=f"Contrato distinto número {i}", valor_estimado=400000,
                fecha_limite_presentacion="2026-09-15T12:00:00"))
        db.guardar(self.con, lic(
            fuente="ted", id_externo="T1", expediente="999-2026",
            objeto="España – Servicios – Alguno de los dos, no se sabe cuál",
            valor_estimado=400000, fecha_limite_presentacion="2026-09-15T12:00:00"))

        antes = self._grupo("T1")
        stats = db.fusionar_grupos_ted(self.con)
        self.assertEqual(stats["anuncios_fusionados"], 0)
        self.assertEqual(stats["ambiguos"], 1)
        self.assertEqual(self._grupo("T1"), antes, "no debía tocar el grupo ambiguo")

    def test_no_fusiona_sin_importe_o_sin_fecha_limite(self):
        """Sin las dos mitades de la clave no hay identificación posible."""
        db.guardar(self.con, lic(
            fuente="oeffentlichevergabe", id_externo="P1", expediente="EXP1",
            valor_estimado=None, importe_sin_iva=None, importe_adjudicacion=None,
            fecha_limite_presentacion=None))
        db.guardar(self.con, lic(
            fuente="ted", id_externo="T1", expediente="999-2026",
            valor_estimado=None, importe_sin_iva=None, importe_adjudicacion=None,
            fecha_limite_presentacion=None))
        self.assertEqual(db.fusionar_grupos_ted(self.con)["anuncios_fusionados"], 0)


if __name__ == "__main__":
    unittest.main()


class TestAgrupacionPorProcedimiento(unittest.TestCase):
    """El Bekanntmachungsservice publica un identificador de procedimiento de verdad (el
    `ocid`), estable entre la licitación, sus correcciones y la adjudicación. Con eso la
    agrupación sale exacta y se acaba la heurística sobre el expediente y el título.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        self.addCleanup(self.con.close)

    def _anuncio(self, id_externo, *, ocid, objeto="Awareness-Plattform"):
        return Licitacion(fuente="oeffentlichevergabe", id_externo=id_externo,
                          organo="IT-Dienstleistungszentrum", objeto=objeto,
                          id_procedimiento=ocid)

    def test_dos_anuncios_del_mismo_procedimiento_comparten_clave(self):
        """Aunque el título cambie entre el anuncio y la adjudicación, que cambia."""
        a = self._anuncio("uuid-1", ocid="ocds-mnwr74-abc", objeto="Awareness-Plattform")
        b = self._anuncio("uuid-2", ocid="ocds-mnwr74-abc",
                          objeto="Awareness-Plattform - Zuschlag")
        self.assertEqual(a.clave_grupo, b.clave_grupo)

    def test_procedimientos_distintos_no_se_mezclan(self):
        a = self._anuncio("uuid-1", ocid="ocds-mnwr74-abc")
        b = self._anuncio("uuid-2", ocid="ocds-mnwr74-xyz")
        self.assertNotEqual(a.clave_grupo, b.clave_grupo)

    def test_recomputar_conserva_la_agrupacion(self):
        """La prueba que más vale de este fichero.

        `recomputar_claves_grupo` reconstruye la clave leyendo unas columnas concretas.
        Si a ese SELECT se le olvida `id_procedimiento`, no falla nada: recalcula claves
        perfectamente válidas pero DISTINTAS, la agrupación de la base entera se deshace
        en silencio y el triaje heredado por expediente empieza a mentir. Sin este test
        eso se descubre semanas después, viendo duplicados en la bandeja.
        """
        for i, ocid in enumerate(["ocds-a", "ocds-a", "ocds-b"]):
            db.guardar(self.con, self._anuncio(f"uuid-{i}", ocid=ocid))
        self.con.commit()
        antes = [f["clave_grupo"] for f in
                 self.con.execute("SELECT clave_grupo FROM licitaciones ORDER BY id")]

        db.recomputar_claves_grupo(self.con)

        despues = [f["clave_grupo"] for f in
                   self.con.execute("SELECT clave_grupo FROM licitaciones ORDER BY id")]
        self.assertEqual(antes, despues)
        self.assertEqual(despues[0], despues[1], "los dos del mismo ocid siguen juntos")
        self.assertNotEqual(despues[1], despues[2])

    def test_sin_id_de_procedimiento_se_cae_al_expediente(self):
        """Las fuentes que no publican identificador siguen funcionando como siempre."""
        a = Licitacion(fuente="otra", id_externo="1", organo="X", expediente="EXP-7")
        b = Licitacion(fuente="otra", id_externo="2", organo="X", expediente="EXP-7")
        self.assertEqual(a.clave_grupo, b.clave_grupo)
