"""Duración de contratos, vencimientos y agrupación del ciclo de vida.

Todo lo que hay aquí salió de datos reales: las unidades de duración que llegan
mezcladas de las distintas fuentes y el prefijo genérico de los títulos de TED.
"""

# Sin esto, la anotación `str | None` de `_añadir` se evalúa al definir la función y este
# fichero no se puede ni importar en Python 3.9, el mínimo que declara el proyecto.
from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from radar import consultas, db
from radar.model import Licitacion, duracion_a_meses, titulo_util_ted


class TestDuracion(unittest.TestCase):
    def test_unidades_mezcladas(self):
        """`unitCode` no es decorativo: MON, DAY y ANN aparecen mezclados en el
        mismo fichero, y un 48 sin unidad son cuatro años o mes y medio."""
        self.assertEqual(duracion_a_meses("48", "MON"), 48)
        self.assertEqual(duracion_a_meses("11", "ANN"), 132)
        self.assertAlmostEqual(duracion_a_meses("120", "DAY"), 3.94, places=1)

    def test_unidades_de_ted(self):
        self.assertEqual(duracion_a_meses("24", "MONTH"), 24)
        self.assertEqual(duracion_a_meses("3", "YEAR"), 36)

    def test_sin_unidad_asume_meses(self):
        """Es la unidad de la mayoría; asumir días convertiría 2 años en 24 días."""
        self.assertEqual(duracion_a_meses("12", None), 12)

    def test_valores_no_utilizables(self):
        for valor, unidad in [(None, "MON"), ("", "MON"), ("0", "MON"),
                              ("-5", "MON"), ("no consta", "MON")]:
            with self.subTest(valor=valor):
                self.assertIsNone(duracion_a_meses(valor, unidad))


class TestFechaFinPrevista(unittest.TestCase):
    def test_se_calcula_desde_la_adjudicacion_y_la_duracion(self):
        l = Licitacion(fuente="p", id_externo="1", fecha_adjudicacion="2026-01-15",
                       duracion_meses=12)
        self.assertEqual(l.fecha_fin_prevista, "2027-01-15")

    def test_la_fecha_explicita_manda_sobre_el_calculo(self):
        l = Licitacion(fuente="p", id_externo="1", fecha_adjudicacion="2026-01-15",
                       duracion_meses=12, fecha_fin_prevista="2026-06-30")
        self.assertTrue(l.fecha_fin_prevista.startswith("2026-06-30"))

    def test_el_inicio_de_ejecucion_tiene_prioridad_sobre_la_adjudicacion(self):
        l = Licitacion(fuente="p", id_externo="1", fecha_adjudicacion="2026-01-15",
                       fecha_inicio_ejecucion="2026-03-01", duracion_meses=6)
        self.assertEqual(l.fecha_fin_prevista, "2026-09-01")

    def test_sin_datos_no_se_inventa_una_fecha(self):
        """Peor que no tener el dato es llamar a un cliente con una fecha inventada."""
        l = Licitacion(fuente="p", id_externo="1", fecha_adjudicacion="2026-01-15")
        self.assertIsNone(l.fecha_fin_prevista)
        l2 = Licitacion(fuente="p", id_externo="2", duracion_meses=12)
        self.assertIsNone(l2.fecha_fin_prevista)

    def test_no_desborda_a_final_de_mes(self):
        l = Licitacion(fuente="p", id_externo="1", fecha_adjudicacion="2026-01-31",
                       duracion_meses=1)
        self.assertEqual(l.fecha_fin_prevista, "2026-02-28")


class TestClaveGrupo(unittest.TestCase):
    def test_quita_el_prefijo_generico_de_ted(self):
        """TED titula «España – <etiqueta CPV> – <título real>» y los dos primeros
        tramos son idénticos en cientos de anuncios."""
        self.assertEqual(
            titulo_util_ted("España – Servicios de formación informática – "
                            "Servicio de oficina de concienciación en ciberseguridad"),
            "Servicio de oficina de concienciación en ciberseguridad",
        )

    def test_no_toca_los_titulos_que_no_son_de_ted(self):
        t = "Servei integral de formació i conscienciació en ciberseguretat"
        self.assertEqual(titulo_util_ted(t), t)

    def test_dos_licitaciones_distintas_del_mismo_organo_no_se_fusionan(self):
        """El bug que ocultaba licitaciones: usar el título completo de TED metía en
        el mismo grupo contratos que solo compartían el prefijo genérico."""
        a = Licitacion(fuente="ted", id_externo="1", organo="Ayuntamiento X",
                       objeto="España – Paquetes de software y sistemas de información – "
                              "Suministro de licencias de antivirus")
        b = Licitacion(fuente="ted", id_externo="2", organo="Ayuntamiento X",
                       objeto="España – Paquetes de software y sistemas de información – "
                              "Plataforma de simulación de phishing")
        self.assertNotEqual(a.clave_grupo, b.clave_grupo)

    def test_el_anuncio_y_su_adjudicacion_caen_en_el_mismo_grupo(self):
        anuncio = Licitacion(fuente="ted", id_externo="580823-2024", organo="LANTIK",
                             objeto="España – Servicios de formación informática – "
                                    "Servicio de oficina de concienciación en ciberseguridad",
                             estado="veroeffentlicht")
        adjudicacion = Licitacion(fuente="ted", id_externo="570954-2025", organo="LANTIK",
                                  objeto="España – Servicios de seguridad – "
                                         "Servicio de oficina de concienciación en ciberseguridad",
                                  estado="zuschlag")
        self.assertEqual(anuncio.clave_grupo, adjudicacion.clave_grupo)

    def test_en_placsp_agrupa_por_expediente(self):
        a = Licitacion(fuente="placsp:licitaciones", id_externo="1", organo="Órgano",
                       expediente="EXP/2026/001", objeto="Licitación")
        b = Licitacion(fuente="placsp:licitaciones", id_externo="2", organo="Órgano",
                       expediente="EXP/2026/001", objeto="Adjudicación del expediente")
        self.assertEqual(a.clave_grupo, b.clave_grupo)


class TestVistaVencimientos(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        hoy = date.today()
        casos = [
            ("vence-pronto", (hoy + timedelta(days=40)).isoformat(), "Incumbente SL", 90000),
            ("vence-tarde", (hoy + timedelta(days=400)).isoformat(), "Otra SL", 50000),
            ("ya-vencido", (hoy - timedelta(days=30)).isoformat(), "Antigua SL", 20000),
            ("sin-fecha-fin", None, "Sin Datos SL", 70000),
        ]
        for nombre, fin, adj, importe in casos:
            db.guardar(self.con, Licitacion(
                fuente="prueba", id_externo=nombre, objeto=f"Concienciación {nombre}",
                organo="Órgano", estado="zuschlag", adjudicatario=adj,
                importe_adjudicacion=importe, fecha_fin_prevista=fin,
            ))
        for f in self.con.execute("SELECT id FROM licitaciones"):
            self.con.execute(
                """INSERT INTO matches (licitacion_id, perfil, puntuacion, motivo, creado_en)
                   VALUES (?, 'p', 3.0, 'm', ?)""", (f["id"], db.ahora()))
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def _ids(self, **kw):
        return [i["id"] for i in consultas.vencimientos(self.con, **kw)["items"]]

    def test_solo_los_que_vencen_en_la_ventana(self):
        v = consultas.vencimientos(self.con, meses=6)
        objetos = [i["objeto"] for i in v["items"]]
        self.assertEqual(len(objetos), 1)
        self.assertIn("vence-pronto", objetos[0])

    def test_ventana_mas_amplia_incluye_los_lejanos(self):
        objetos = [i["objeto"] for i in consultas.vencimientos(self.con, meses=24)["items"]]
        self.assertEqual(len(objetos), 2)

    def test_los_sin_fecha_de_fin_no_aparecen(self):
        todos = " ".join(
            i["objeto"] for i in consultas.vencimientos(self.con, meses=60)["items"]
        )
        self.assertNotIn("sin-fecha-fin", todos)

    def test_incluye_al_incumbente_y_el_importe(self):
        it = consultas.vencimientos(self.con, meses=6)["items"][0]
        self.assertEqual(it["adjudicatario"], "Incumbente SL")
        self.assertEqual(it["importe"], 90000)
        self.assertGreater(it["dias_para_vencer"], 0)

    def test_lo_descartado_no_aparece(self):
        lic_id = self.con.execute(
            "SELECT id FROM licitaciones WHERE id_externo = 'vence-pronto'"
        ).fetchone()[0]
        db.fijar_revision(self.con, lic_id, estado="descartado",
                          motivo_descarte="incumbente atado")
        self.con.commit()
        self.assertEqual(consultas.vencimientos(self.con, meses=6)["items"], [])


class TestVentanaDeVencimiento(unittest.TestCase):
    """Los bordes de la ventana, y que los recuentos cuadren con la lista.

    La condición se escribió comparando `fecha_fin_prevista` sin envolverla en
    `substr(..., 1, 10)`, para que el índice sirva. Eso obliga a cerrar la ventana
    con `< fin + 1 día`, porque las fechas reales traen hora: un contrato que vence
    el último día de la ventana a las 10:00 desaparecía con un `<= fin` a secas.
    Y los cuatro recuentos de la cabecera salen de una sola pasada, así que hay que
    comprobar que dicen lo mismo que la consulta de la lista.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = db.conectar(Path(self.dir.name) / "t.db")

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def _sqlite_fecha(self, *modificadores) -> str:
        """La frontera se pregunta al mismo motor que la calcula en la consulta;
        sumar meses a mano en Python daría un día distinto a fin de mes."""
        args = ", ".join(f"'{m}'" for m in ("now",) + modificadores)
        return self.con.execute(f"SELECT date({args})").fetchone()[0]

    def _añadir(self, nombre: str, fin: str | None, *, perfiles=("p",)):
        db.guardar(self.con, Licitacion(
            fuente="prueba", id_externo=nombre, objeto=f"Concienciación {nombre}",
            organo="Órgano", estado="zuschlag", adjudicatario="Incumbente SL",
            importe_adjudicacion=1000, fecha_fin_prevista=fin,
        ))
        lic_id = self.con.execute(
            "SELECT id FROM licitaciones WHERE id_externo = ?", (nombre,)
        ).fetchone()[0]
        for perfil in perfiles:
            self.con.execute(
                """INSERT INTO matches (licitacion_id, perfil, puntuacion, motivo, creado_en)
                   VALUES (?, ?, 3.0, 'm', ?)""", (lic_id, perfil, db.ahora()))
        self.con.commit()
        return lic_id

    def _objetos(self, meses=6):
        return [i["objeto"] for i in consultas.vencimientos(self.con, meses=meses)["items"]]

    def test_el_que_vence_hoy_entra(self):
        self._añadir("hoy", self._sqlite_fecha() + "T09:00:00")
        self.assertEqual(len(self._objetos()), 1)

    def test_el_ultimo_dia_de_la_ventana_con_hora_entra(self):
        """El caso que justifica el `+1 day`: cerrar en `<= date('now','+6 months')`
        dejaba fuera lo que vence ese mismo día a cualquier hora."""
        self._añadir("justo-al-borde", self._sqlite_fecha("+6 months") + "T10:00:00")
        self.assertEqual(len(self._objetos()), 1)

    def test_el_dia_siguiente_al_borde_no_entra(self):
        self._añadir("pasado-el-borde",
                     self._sqlite_fecha("+6 months", "+1 day") + "T00:00:00")
        self.assertEqual(self._objetos(), [])

    def test_las_ventanas_cuadran_con_la_lista(self):
        """Los cuatro recuentos salen de una sola consulta con agregación
        condicional; si se desincronizan de la lista, la cabecera miente."""
        self._añadir("en-3-meses", self._sqlite_fecha("+2 months"))
        self._añadir("en-6-meses", self._sqlite_fecha("+5 months") + "T10:00:00")
        self._añadir("en-12-meses", self._sqlite_fecha("+11 months"))
        self._añadir("en-24-meses", self._sqlite_fecha("+23 months"))
        self._añadir("muy-lejano", self._sqlite_fecha("+40 months"))
        self._añadir("ya-vencido", self._sqlite_fecha("-2 months"))
        self._añadir("sin-fecha", None)

        por_ventana = {v["meses"]: v for v in
                       consultas.vencimientos(self.con)["por_ventana"]}
        self.assertEqual([por_ventana[m]["total"] for m in (3, 6, 12, 24)], [1, 2, 3, 4])

        for meses in (3, 6, 12, 24):
            with self.subTest(meses=meses):
                v = consultas.vencimientos(self.con, meses=meses)
                self.assertEqual(v["total"], por_ventana[meses]["total"])
                self.assertEqual(v["importe_total"], por_ventana[meses]["importe"])
                self.assertEqual(len(v["items"]), v["total"])

    def test_dos_perfiles_no_cuentan_dos_veces(self):
        """Sin deduplicar por licitación, la de dos perfiles inflaba el recuento y
        el importe de las ventanas."""
        self._añadir("doble", self._sqlite_fecha("+2 months"), perfiles=("p", "q"))
        v = consultas.vencimientos(self.con)
        self.assertEqual(v["total"], 1)
        self.assertEqual(v["importe_total"], 1000)
        self.assertEqual(v["por_ventana"][0]["total"], 1)
        self.assertEqual(v["por_ventana"][0]["importe"], 1000)

    def test_lo_descartado_tampoco_cuenta_en_las_ventanas(self):
        lic_id = self._añadir("descartada", self._sqlite_fecha("+2 months"))
        db.fijar_revision(self.con, lic_id, estado="descartado",
                          motivo_descarte="incumbente atado")
        self.con.commit()
        v = consultas.vencimientos(self.con)
        self.assertEqual(v["items"], [])
        self.assertEqual([x["total"] for x in v["por_ventana"]], [0, 0, 0, 0])
        self.assertEqual([x["importe"] for x in v["por_ventana"]], [0, 0, 0, 0])


class TestMotivoDescarte(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        db.guardar(self.con, Licitacion(fuente="p", id_externo="1", objeto="Concienciación"))
        self.lic_id = self.con.execute("SELECT id FROM licitaciones").fetchone()[0]

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def test_se_guarda_y_se_agrega(self):
        db.fijar_revision(self.con, self.lic_id, estado="descartado",
                          motivo_descarte="importe bajo")
        self.con.commit()
        resumen = consultas.motivos_descarte(self.con)
        self.assertEqual(resumen, [{"motivo": "importe bajo", "total": 1}])

    def test_motivo_invalido_se_rechaza(self):
        with self.assertRaises(ValueError):
            db.fijar_revision(self.con, self.lic_id, estado="descartado",
                              motivo_descarte="porque no me gusta")

    def test_el_motivo_sobrevive_a_un_cambio_de_notas(self):
        db.fijar_revision(self.con, self.lic_id, estado="descartado",
                          motivo_descarte="fuera de nicho")
        db.fijar_revision(self.con, self.lic_id, notas="revisar el año que viene")
        self.con.commit()
        f = self.con.execute(
            "SELECT motivo_descarte, notas, estado FROM revisiones"
        ).fetchone()
        self.assertEqual(f["motivo_descarte"], "fuera de nicho")
        self.assertEqual(f["estado"], "descartado")


class TestNovedades(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = db.conectar(Path(self.dir.name) / "t.db")

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def _añadir(self, nombre):
        db.guardar(self.con, Licitacion(fuente="p", id_externo=nombre,
                                        objeto=f"Concienciación {nombre}"))
        lic_id = self.con.execute(
            "SELECT id FROM licitaciones WHERE id_externo = ?", (nombre,)
        ).fetchone()[0]
        self.con.execute(
            """INSERT INTO matches (licitacion_id, perfil, puntuacion, motivo, creado_en)
               VALUES (?, 'p', 3.0, 'm', ?)""", (lic_id, db.ahora()))
        self.con.commit()

    def _marcar_visita(self):
        """Marca por id, no por hora: las marcas de tiempo tienen resolución de
        segundo y una ingesta en el mismo segundo se quedaría fuera."""
        ultimo = self.con.execute("SELECT COALESCE(MAX(id), 0) FROM licitaciones").fetchone()[0]
        db.escribir_preferencia(self.con, "ultima_visita_id", str(ultimo))
        self.con.commit()

    def test_sin_visita_previa_no_hay_novedades(self):
        self._añadir("a")
        self.assertEqual(consultas.resumen(self.con)["novedades"], 0)

    def test_cuenta_lo_llegado_despues_de_la_visita(self):
        self._añadir("a")
        self._marcar_visita()
        self.assertEqual(consultas.resumen(self.con)["novedades"], 0)
        self._añadir("b")
        self._añadir("c")
        self.assertEqual(consultas.resumen(self.con)["novedades"], 2)

    def test_el_filtro_devuelve_solo_las_novedades(self):
        self._añadir("a")
        self._marcar_visita()
        self._añadir("b")
        ids = [i["id_externo"] for i in
               consultas.bandeja(self.con, solo_vivas=False, solo_novedades=True)["items"]]
        self.assertEqual(ids, ["b"])


if __name__ == "__main__":
    unittest.main()
