"""Cuándo se vuelven a aplicar los perfiles a la base, y cuánto cuesta hacerlo.

Aquí se protegen dos cosas que tiran en direcciones opuestas.

La primera es el coste. La ingesta de cada mañana reevaluaba las 673.755 fichas de la
base —39 s de Python— y emitía un DELETE por cada ficha y cada perfil que no casaba:
2,7 millones de sentencias contra una tabla `matches` de 3.705 filas, casi todas para
borrar algo que no existía.

La segunda es la corrección, que es la que importa de verdad: una reevaluación parcial
que se salte algo deja la bandeja mintiendo en silencio, y eso es mucho peor que tardar
39 s. De ahí que casi todas las pruebas de abajo sean sobre CUÁNDO hay que volver a
mirarlo todo aunque se haya pedido lo contrario.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radar import db, matching
from radar.model import Licitacion


def perfil(**kw) -> matching.Perfil:
    base = {
        "name": "Prueba",
        "starke_begriffe": ["phishing"],
        "schwache_begriffe": ["concienci"],
        "erforderlicher_kontext": ["ciberseguridad"],
        "ausschluss": ["seguridad vial"],
    }
    base.update(kw)
    return matching.Perfil(**base).preparar()


class EspiaSQL:
    """Envoltorio de la conexión que apunta las sentencias que se ejecutan.

    Es la única forma de comprobar lo que este cambio venía a arreglar: que no se
    emitan millones de DELETE inútiles. Contar filas no lo detecta, porque el resultado
    final era correcto; lo que estaba mal era el camino.
    """

    def __init__(self, con):
        self.con = con
        self.sentencias: list[str] = []

    def execute(self, sql, params=()):
        self.sentencias.append(sql)
        return self.con.execute(sql, params)

    def executemany(self, sql, seq):
        self.sentencias.append(sql)
        return self.con.executemany(sql, seq)

    def commit(self):
        return self.con.commit()

    def cuantas(self, trozo: str) -> int:
        return sum(1 for s in self.sentencias if trozo in s)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        self.addCleanup(self.con.close)

    def guardar(self, n: int, **kw) -> Licitacion:
        """Una licitación que casa con el perfil de prueba."""
        base = {
            "fuente": "placsp:licitaciones",
            "id_externo": f"licitaciones:{n}",
            "objeto": "Servicio de simulación de phishing",
            "organo": "Ayuntamiento de Prueba",
            "expediente": f"EXP/{n}",
            "valor_estimado": 120000,
        }
        base.update(kw)
        lic = Licitacion(**base)
        estado = db.guardar(self.con, lic)
        self.con.commit()
        self.estado_ultimo_guardado = estado
        return lic

    def pendientes(self) -> int:
        return self.con.execute(
            "SELECT COUNT(*) FROM licitaciones WHERE huella_evaluada IS NOT huella"
        ).fetchone()[0]

    def matches(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]


class TestAmbitoIncremental(Base):
    def test_una_republicacion_identica_no_se_vuelve_a_evaluar(self):
        """PLACSP republica el mismo expediente muchas veces. `visto_ultima_vez` no
        servía para distinguirlo: `guardar()` lo actualiza también cuando la ficha llega
        igual, así que todas las fichas parecían recientes todas las mañanas."""
        lic = self.guardar(1)
        matching.reevaluar(self.con, [perfil()])

        db.guardar(self.con, lic)  # la misma ficha, otra vez
        self.con.commit()
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM licitaciones WHERE visto_ultima_vez "
                             "IS NOT NULL").fetchone()[0], 1)

        stats = matching.reevaluar(self.con, [perfil()], incremental=True)
        self.assertEqual(stats["evaluadas"], 0, "una republicación idéntica no cambia nada")
        self.assertFalse(stats["completa"])

    def test_un_cambio_real_vuelve_a_la_cola(self):
        self.guardar(1)
        matching.reevaluar(self.con, [perfil()])
        antes = self.con.execute("SELECT puntuacion FROM matches").fetchone()[0]

        self.guardar(1, valor_estimado=90)  # el importe cambia: huella nueva
        self.assertEqual(self.estado_ultimo_guardado, "actualizada")

        stats = matching.reevaluar(self.con, [perfil()], incremental=True)
        self.assertEqual(stats["evaluadas"], 1)
        despues = self.con.execute("SELECT puntuacion FROM matches").fetchone()[0]
        self.assertLess(despues, antes, "ya no puntúa por importe relevante")

    def test_lo_que_entra_nuevo_se_evalua_sin_mirar_el_resto(self):
        for i in range(5):
            self.guardar(i)
        matching.reevaluar(self.con, [perfil()])

        self.guardar(99)
        stats = matching.reevaluar(self.con, [perfil()], incremental=True)
        self.assertEqual(stats["evaluadas"], 1)
        self.assertEqual(stats["creados"], 1)
        self.assertEqual(self.matches(), 6)

    def test_despues_de_cada_pasada_no_queda_nada_pendiente(self):
        for i in range(3):
            self.guardar(i)
        self.assertEqual(self.pendientes(), 3)
        matching.reevaluar(self.con, [perfil()])
        self.assertEqual(self.pendientes(), 0)
        self.guardar(50)
        self.assertEqual(self.pendientes(), 1)
        matching.reevaluar(self.con, [perfil()], incremental=True)
        self.assertEqual(self.pendientes(), 0)

    def test_la_primera_pasada_de_una_base_antigua_es_completa(self):
        """Una base que viene de la versión anterior no tiene ni la columna rellena ni
        la preferencia: hay que mirarlo todo, no dar por evaluado lo que nunca lo fue."""
        for i in range(4):
            self.guardar(i)
        matching.reevaluar(self.con, [perfil()])

        self.con.execute("UPDATE licitaciones SET huella_evaluada = NULL")
        self.con.execute("DELETE FROM preferencias WHERE clave = ?",
                         (matching.CLAVE_HUELLA_PERFILES,))
        self.con.commit()

        stats = matching.reevaluar(self.con, [perfil()], incremental=True)
        self.assertTrue(stats["completa"])
        self.assertEqual(stats["evaluadas"], 4)
        self.assertIn("nicht vermerkt", stats["motivo"])


class TestHuellaDeLosPerfiles(Base):
    def test_cambiar_los_perfiles_fuerza_pasada_completa_aunque_se_pida_incremental(self):
        """Sin esto, «he cambiado un término y la bandeja no se ha movido» sería el
        comportamiento normal de la ingesta diaria."""
        for i in range(3):
            self.guardar(i)
        matching.reevaluar(self.con, [perfil()])

        stats = matching.reevaluar(
            self.con, [perfil(starke_begriffe=["dmarc"])], incremental=True
        )
        self.assertTrue(stats["completa"])
        self.assertEqual(stats["motivo"], "die Profile haben sich geändert")
        self.assertEqual(stats["evaluadas"], 3)
        self.assertEqual(self.matches(), 0, "ya no casa ninguna")
        self.assertEqual(stats["retirados"], 3)

    def test_el_mismo_fichero_de_perfiles_no_fuerza_nada(self):
        """Dos objetos Perfil distintos con los mismos campos tienen que dar la misma
        huella; si no, cada ingesta haría una pasada completa sin motivo."""
        self.guardar(1)
        matching.reevaluar(self.con, [perfil()])
        stats = matching.reevaluar(self.con, [perfil()], incremental=True)
        self.assertFalse(stats["completa"])
        self.assertEqual(stats["evaluadas"], 0)

    def test_los_terminos_de_consulta_no_fuerzan_una_pasada_completa(self):
        """`abfrage_begriffe` solo cambia lo que se PREGUNTA a TED y a Cataluña; sobre
        lo ya descargado no mueve ni un match."""
        self.guardar(1)
        matching.reevaluar(self.con, [perfil()])
        stats = matching.reevaluar(
            self.con, [perfil(abfrage_begriffe=["phishing", "dmarc"])], incremental=True
        )
        self.assertFalse(stats["completa"])
        self.assertEqual(stats["evaluadas"], 0)

    def test_reordenar_los_terminos_si_fuerza_la_pasada(self):
        """El motivo que se guarda cita los tres primeros términos, así que el orden
        cambia lo que la bandeja explica en «Por qué ha entrado»."""
        self.guardar(1)
        matching.reevaluar(self.con, [perfil(starke_begriffe=["phishing", "dmarc"])])
        stats = matching.reevaluar(
            self.con, [perfil(starke_begriffe=["dmarc", "phishing"])], incremental=True
        )
        self.assertTrue(stats["completa"])

    def test_subir_la_version_del_motor_fuerza_una_pasada_completa(self):
        """Es el incidente de `patron()`: cambió la forma de casar, había que retirar
        612 de 943 matches y ni los perfiles ni las fichas habían cambiado."""
        self.guardar(1)
        matching.reevaluar(self.con, [perfil()])
        with mock.patch.object(matching, "VERSION_MATCHING", "99"):
            stats = matching.reevaluar(self.con, [perfil()], incremental=True)
        self.assertTrue(stats["completa"])
        self.assertEqual(stats["evaluadas"], 1)

    def test_desactivar_un_perfil_cuenta_como_cambio(self):
        self.guardar(1)
        matching.reevaluar(self.con, [perfil()])
        stats = matching.reevaluar(self.con, [perfil(aktiv=False)], incremental=True)
        self.assertTrue(stats["completa"])

    def test_borrar_un_perfil_ya_desactivado_no_fuerza_otra_pasada(self):
        """La huella retrata lo que se aplica, no lo que hay escrito en el fichero.

        Quitar de `suchprofile.json` un perfil que ya estaba desactivado no puede mover
        ni un match —desde que se desactivó ya no se evaluaba—, así que cobrar por
        ello otra pasada completa sobre 673.755 fichas sería trabajo regalado."""
        self.guardar(1)
        matching.reevaluar(self.con, [perfil(), perfil(name="Amplio", aktiv=False)])
        stats = matching.reevaluar(self.con, [perfil()], incremental=True)
        self.assertFalse(stats["completa"])
        self.assertEqual(stats["evaluadas"], 0)


class TestPerfilesDesactivados(Base):
    """Un perfil desactivado no se evalúa y no deja nada en la bandeja.

    Es la decisión que faltaba por tomar, y hacía falta porque los dos caminos que
    aplican los perfiles no le daban la lista a `reevaluar` con el mismo criterio:
    `cargar_perfiles` —`radar.py match`— filtra los inactivos, y `validar_perfiles`
    —el botón «Guardar» de la pestaña «Términos de búsqueda»— los devuelve todos,
    porque esa misma lista es la que se escribe en el fichero. Con `reevaluar`
    iterando la lista tal cual, desactivar desde la aplicación no vaciaba nada:
    las coincidencias del perfil apagado seguían ahí hasta que alguien relanzaba
    `match` a mano.
    """

    def amplio(self, **kw) -> matching.Perfil:
        """Un segundo perfil que casa por su cuenta con la ficha de prueba."""
        return perfil(name="Amplio", starke_begriffe=["simulacion"], **kw)

    def test_desactivar_un_perfil_retira_sus_coincidencias(self):
        self.guardar(1)
        matching.reevaluar(self.con, [perfil(), self.amplio()])
        self.assertEqual(self.matches(), 2)

        stats = matching.reevaluar(self.con, [perfil(), self.amplio(aktiv=False)])
        self.assertEqual(stats["por_perfil"], {"Prueba": 1})
        self.assertEqual(stats["huerfanos"], 1, "se va por el camino de los huérfanos")
        self.assertEqual(self.matches(), 1)

    def test_una_ficha_nueva_no_entra_por_un_perfil_desactivado(self):
        """No basta con retirar lo que ya había: lo que llegue después tampoco entra."""
        perfiles = [perfil(starke_begriffe=["dmarc"], schwache_begriffe=[],
                           erforderlicher_kontext=[]), self.amplio(aktiv=False)]
        matching.reevaluar(self.con, perfiles)

        self.guardar(1)
        stats = matching.reevaluar(self.con, perfiles, incremental=True)
        self.assertFalse(stats["completa"], "los perfiles no han cambiado")
        self.assertEqual(stats["evaluadas"], 1)
        self.assertEqual(self.matches(), 0)

    def test_desactivar_no_toca_lo_que_casa_por_otro_perfil(self):
        """La ficha sigue en la bandeja si algún perfil activo la sostiene."""
        self.guardar(1)
        matching.reevaluar(self.con, [perfil(), self.amplio()])
        matching.reevaluar(self.con, [perfil(), self.amplio(aktiv=False)])
        self.assertEqual(
            [f["perfil"] for f in self.con.execute("SELECT perfil FROM matches")],
            ["Prueba"],
        )

    def test_desactivar_no_borra_el_triaje_de_una_persona(self):
        """Mismo criterio que un perfil endurecido: lo que decide alguien se queda."""
        self.guardar(1)
        matching.reevaluar(self.con, [self.amplio()])
        lic_id = self.con.execute("SELECT id FROM licitaciones").fetchone()[0]
        db.fijar_revision(self.con, lic_id, estado="descartado", notas="ya lo miré")
        self.con.commit()

        matching.reevaluar(self.con, [perfil(), self.amplio(aktiv=False)])
        fila = self.con.execute(
            "SELECT estado, notas FROM revisiones WHERE licitacion_id = ?", (lic_id,)
        ).fetchone()
        self.assertEqual((fila["estado"], fila["notas"]), ("descartado", "ya lo miré"))


class TestElNombreDelOrganoNoCasa(Base):
    """El nombre del organismo contratante no puede entrar en las reglas.

    Los organismos se llaman «Bundesamt für SICHERHEIT in der Informationstechnik» o
    «Landesamt für AUSBILDUNG», así que mientras el matcher leyó el texto de búsqueda
    —que lleva el órgano dentro— el nombre regalaba el término débil, el contexto
    requerido, o los dos. Medido sobre la base española antes de separarlos: 125 de las
    535 coincidencias, el 23 %, y ninguna con un término fuerte.
    """

    def test_un_termino_solo_en_el_nombre_del_organo_no_hace_casar(self):
        """El caso exacto que se midió: «Formación en ... soft-skills» del INCIBE.

        El objeto trae el término débil («formacion») y el contexto («ciberseguridad»)
        lo pone el nombre del órgano. Antes entraba; no es una licitación de
        concienciación en ciberseguridad, es formación en gestión de proyectos.
        """
        self.guardar(
            1,
            objeto="Formación en Gestión de Proyectos Europeos y soft-skills",
            organo="Dirección General del Instituto Nacional de Ciberseguridad (INCIBE)",
        )
        matching.reevaluar(self.con, [perfil()])
        self.assertEqual(self.matches(), 0)

    def test_ni_siquiera_un_termino_fuerte_cuenta_si_solo_esta_en_el_organo(self):
        """Sin señal en el contrato no hay match, venga de donde venga el nombre."""
        self.guardar(
            1,
            objeto="Suministro de mobiliario de oficina",
            organo="Agència de Ciberseguretat i Simulació de Phishing de Prueba",
        )
        matching.reevaluar(self.con, [perfil()])
        self.assertEqual(self.matches(), 0)

    def test_lo_que_casa_por_el_objeto_sigue_casando(self):
        """La otra mitad del contrato: quitar el órgano no podía costar precisión.

        Sobre la base real no costó ni un verdadero positivo —0 de 535—, y esta es la
        prueba de que el camino bueno sigue abierto.
        """
        self.guardar(1, objeto="Servicio de simulación de phishing",
                     organo="Ayuntamiento sin nada que delate el sector")
        matching.reevaluar(self.con, [perfil()])
        self.assertEqual(self.matches(), 1)

    def test_el_contexto_tambien_vale_si_esta_en_la_descripcion_o_en_el_lote(self):
        """`texto_reglas` son tres columnas, no una: el lote es donde aparece la señal
        en los contratos grandes. El caso que lo justifica está en el README: un
        mantenimiento de hardware de Viladecans cuyo lote 10 era «programa de
        conscienciació en CIBERSEGURETAT»."""
        self.guardar(1, objeto="Mantenimiento de hardware",
                     organo="Ayuntamiento de Prueba",
                     lote_desc="Lote 10: programa de concienciación en ciberseguridad")
        matching.reevaluar(self.con, [perfil()])
        self.assertEqual(self.matches(), 1)

    def test_la_busqueda_libre_sigue_encontrando_por_organo(self):
        """Lo que NO se ha roto: buscar la vergabestelle tiene que seguir funcionando.

        El órgano se quita del texto de las REGLAS, no del índice de búsqueda: son dos
        cosas con dos usos, y ese es el motivo de separarlas en lugar de quitar el órgano
        de todas partes. Se comprueba contra FTS5, que es por donde va de verdad la caja
        de búsqueda libre."""
        self.guardar(1, organo="Stadt Viladecans")
        (encontrados,) = self.con.execute(
            "SELECT COUNT(*) FROM licitaciones_fts WHERE licitaciones_fts MATCH ?",
            ("viladecans",),
        ).fetchone()
        self.assertEqual(encontrados, 1, "la caja de búsqueda tiene que hallar por órgano")

        fila = self.con.execute("SELECT texto_reglas_norm FROM licitaciones").fetchone()
        self.assertNotIn("viladecans", fila["texto_reglas_norm"])

    def test_una_base_vieja_se_rellena_al_migrar(self):
        """`texto_reglas_norm` no existía: la base de cada compañero llega sin ella.

        Si la migración no la rellenara, `COALESCE(texto_reglas_norm, '')` dejaría la
        bandeja vacía al actualizar, que es peor que el ruido que se venía a quitar.
        """
        self.guardar(1)
        self.con.execute("UPDATE licitaciones SET texto_reglas_norm = NULL")
        db.escribir_preferencia(self.con, "version_texto_reglas", "0")
        self.con.commit()

        aplicadas = db.migrar(self.con)
        self.assertTrue(any("Regeltext" in a for a in aplicadas), aplicadas)
        fila = self.con.execute(
            "SELECT texto_reglas_norm FROM licitaciones").fetchone()
        self.assertIn("phishing", fila["texto_reglas_norm"])


class TestEscrituras(Base):
    def test_no_emite_un_solo_delete_cuando_nada_ha_dejado_de_casar(self):
        """El incidente: 673.755 fichas × 4 perfiles = 2,7 millones de DELETE contra
        una tabla de 3.705 filas, cada mañana, para no borrar nada."""
        for i in range(20):
            self.guardar(i)
        matching.reevaluar(self.con, [perfil()])

        espia = EspiaSQL(self.con)
        matching.reevaluar(espia, [perfil()])
        self.assertEqual(espia.cuantas("DELETE FROM matches WHERE licitacion_id"), 0)

    def test_las_escrituras_van_en_bloque_y_no_una_por_fila(self):
        for i in range(30):
            self.guardar(i)
        espia = EspiaSQL(self.con)
        matching.reevaluar(espia, [perfil()])
        self.assertLessEqual(espia.cuantas("INSERT INTO matches"), 1,
                             "30 fichas tienen que caber en un solo executemany")
        self.assertEqual(self.matches(), 30)

    def test_un_perfil_endurecido_retira_el_match_y_conserva_el_triaje(self):
        """Lo que decide una persona no lo puede borrar un cambio de términos."""
        self.guardar(1)
        matching.reevaluar(self.con, [perfil()])
        lic_id = self.con.execute("SELECT id FROM licitaciones").fetchone()[0]
        db.fijar_revision(self.con, lic_id, estado="descartado",
                          motivo_descarte="importe bajo", notas="hablado con el órgano")
        self.con.commit()

        stats = matching.reevaluar(self.con, [perfil(starke_begriffe=["dmarc"])])
        self.assertEqual(stats["retirados"], 1)
        self.assertEqual(self.matches(), 0)
        fila = self.con.execute(
            "SELECT estado, notas FROM revisiones WHERE licitacion_id = ?", (lic_id,)
        ).fetchone()
        self.assertEqual(fila["estado"], "descartado")
        self.assertEqual(fila["notas"], "hablado con el órgano")

    def test_los_matches_de_un_perfil_renombrado_se_limpian_de_una_vez(self):
        self.guardar(1)
        matching.reevaluar(self.con, [perfil()])
        stats = matching.reevaluar(self.con, [perfil(name="Otro nombre")])
        self.assertEqual(stats["huerfanos"], 1)
        self.assertEqual(
            [f["perfil"] for f in self.con.execute("SELECT DISTINCT perfil FROM matches")],
            ["Otro nombre"],
        )


class TestEstadisticas(Base):
    def test_las_estadisticas_dicen_el_total_y_no_lo_de_esta_pasada(self):
        """La cifra que se enseña —en la terminal y en «Guardado · N coincidencias»—
        tiene que ser cuántas coincidencias hay, no cuántas se han tocado."""
        for i in range(7):
            self.guardar(i)
        matching.reevaluar(self.con, [perfil()])

        stats = matching.reevaluar(self.con, [perfil()], incremental=True)
        self.assertEqual(stats["evaluadas"], 0)
        self.assertEqual(stats["matches"], 7)
        self.assertEqual(stats["por_perfil"], {"Prueba": 7})

    def test_distingue_lo_creado_de_lo_reescrito(self):
        self.guardar(1)
        primera = matching.reevaluar(self.con, [perfil()])
        self.assertEqual((primera["creados"], primera["actualizados"]), (1, 0))
        segunda = matching.reevaluar(self.con, [perfil()])
        self.assertEqual((segunda["creados"], segunda["actualizados"]), (0, 1))


class TestPlanDeConsulta(Base):
    """El índice parcial es lo que evita recorrer 673.755 filas cada mañana.

    SQLite solo usa un índice parcial cuando el WHERE de la consulta coincide
    LITERALMENTE con el suyo, así que reescribir `IS NOT` como `IS NULL OR <>` —que
    parece lo mismo— devuelve el recorrido completo sin que falle nada visible. Estas
    dos pruebas son la única alarma que quedaría.
    """

    def _plan(self, sql: str) -> str:
        return "\n".join(
            str(f[3]) for f in self.con.execute("EXPLAIN QUERY PLAN " + sql)
        )

    def test_la_seleccion_de_pendientes_usa_el_indice_parcial(self):
        plan = self._plan(matching._SELECT_EVAL + matching._PENDIENTES)
        self.assertIn("idx_lic_pendientes", plan, plan)

    def test_el_marcado_de_evaluadas_tambien(self):
        plan = self._plan(
            f"SELECT id FROM licitaciones{matching._PENDIENTES} LIMIT 5000"
        )
        self.assertIn("idx_lic_pendientes", plan, plan)


if __name__ == "__main__":
    unittest.main()
