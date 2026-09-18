"""La pestaña Analítica: los patrones del mercado, no lo que hay hoy.

Aquí se protegen dos cosas distintas.

La primera es la aritmética. Cada bloque agrega por su cuenta sobre la misma base, así que
si uno cuenta anuncios donde los demás cuentan expedientes, la pestaña se contradice
consigo misma y no hay forma de saber a cuál creer. Es el mismo fallo que `_condiciones`
existe para no repetir: la cabecera decía 945 sin revisar donde la lista mostraba 57.

La segunda, y es la que importa de verdad, es que ninguna cifra sea una cifra inventada.
Tres de las pruebas de abajo están escritas contra un error concreto que ya se cometió
midiendo esta base: comparar la adjudicación con el valor estimado en vez de con el
presupuesto base (daba un 30% de baja donde hay un 13%), aceptar como recorrido una ficha
que nació adjudicada (el 45% de los expedientes salía con cero días) y promediar un mes a
medias con los meses cerrados.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from radar import consultas, db
from radar.model import Licitacion


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        self.addCleanup(self.con.close)
        # El memo es de módulo y sobrevive entre pruebas: sin limpiarlo, la segunda
        # comparte respuesta con la primera porque una base recién creada tiene la misma
        # firma (MAX(id) 1, sin matches, sin revisiones) que otra base recién creada.
        consultas._MEMO.clear()
        self.addCleanup(consultas._MEMO.clear)

    def _sqlite_fecha(self, *modificadores) -> str:
        """La frontera se pregunta al mismo motor que la calcula en la consulta."""
        args = ", ".join(f"'{m}'" for m in ("now",) + modificadores)
        return self.con.execute(f"SELECT date({args})").fetchone()[0]

    def _añadir(self, nombre: str, *, perfiles=("p",), puntuacion=3.0, **campos) -> int:
        """Un anuncio con su match. `campos` va tal cual a `Licitacion`."""
        base = dict(
            fuente="prueba", id_externo=nombre, objeto=f"Concienciación {nombre}",
            organo="Órgano", estado="veroeffentlicht", fecha_publicacion="2025-03-10",
        )
        base.update(campos)
        db.guardar(self.con, Licitacion(**base))
        lic_id = self.con.execute(
            "SELECT id FROM licitaciones WHERE id_externo = ?", (nombre,)
        ).fetchone()[0]
        for perfil in perfiles:
            self.con.execute(
                "INSERT INTO matches (licitacion_id, perfil, puntuacion, motivo, creado_en)"
                " VALUES (?, ?, ?, 'm', ?)", (lic_id, perfil, puntuacion, db.ahora()))
        self.con.commit()
        return lic_id

    def _version(self, lic_id: int, estado: str, fecha: str, *, anterior=None) -> None:
        """Una versión en el historial, con la fecha que da la FUENTE.

        Se inserta a mano porque lo que se prueba es la consulta, no `db.guardar`; y
        `detectado_en` se deja aparte a propósito, que es justo lo que el bloque del ciclo
        no debe mirar.
        """
        self.con.execute(
            "INSERT INTO licitaciones_versiones (licitacion_id, huella, estado,"
            " estado_anterior, detectado_en, fecha_cambio, snapshot)"
            " VALUES (?, ?, ?, ?, ?, ?, '{}')",
            (lic_id, f"{estado}-{fecha}", estado, anterior, db.ahora(), fecha))
        self.con.commit()


class TestBaseDeExpedientes(Base):
    def test_los_anuncios_de_un_expediente_cuentan_una_vez(self):
        """PLACSP republica el mismo expediente por cada cambio, y cada republicación es
        una fila. Contando anuncios, esta base da un 19% de más que no es mercado."""
        for i in range(3):
            self._añadir(f"a{i}", expediente="EXP/1", importe_sin_iva=100_000)
        d = consultas.analitica(self.con)
        self.assertEqual(d["generado_para"]["expedientes"], 1)
        self.assertEqual(d["generado_para"]["anuncios"], 3)
        self.assertEqual(d["importes"]["expedientes"], 1)

    def test_dos_perfiles_no_cuentan_dos_veces(self):
        self._añadir("uno", perfiles=("p", "q"), importe_sin_iva=50_000)
        d = consultas.analitica(self.con)
        self.assertEqual(d["generado_para"]["expedientes"], 1)
        self.assertEqual(d["cartera"]["expedientes"], 1)

    def test_lo_descartado_no_entra_en_ningun_bloque(self):
        """El fallo típico es que seis bloques respeten el filtro y el séptimo no."""
        vivo = self._añadir("vivo", expediente="EXP/1", importe_sin_iva=100_000)
        fuera = self._añadir("fuera", expediente="EXP/2", importe_sin_iva=999_000,
                             puntuacion=7.0)
        db.fijar_revision(self.con, fuera, estado="descartado", motivo_descarte="otro")
        self.con.commit()
        self.assertTrue(vivo)

        d = consultas.analitica(self.con)
        for bloque, clave in (("generado_para", "expedientes"),
                              ("importes", "expedientes"),
                              ("cartera", "expedientes"),
                              ("cpv", "expedientes")):
            with self.subTest(bloque=bloque):
                self.assertEqual(d[bloque][clave], 1)
        self.assertEqual(d["cartera"]["lista_corta"], 0, "la descartada puntuaba 7,0")
        self.assertEqual(d["importes"]["mediana"], 100_000)

    def test_el_filtro_de_perfil_no_pierde_los_expedientes_multiperfil(self):
        self._añadir("ambos", perfiles=("p", "q"), expediente="EXP/1")
        self._añadir("solo-q", perfiles=("q",), expediente="EXP/2")
        self.assertEqual(
            consultas.analitica(self.con, perfil="p")["generado_para"]["expedientes"], 1)
        self.assertEqual(
            consultas.analitica(self.con, perfil="q")["generado_para"]["expedientes"], 2)


class TestCalendario(Base):
    def _preparar_dos_anios(self) -> None:
        """2024 y 2025 completos, más dos meses de 2026: el año en curso a medias."""
        for anio in ("2024", "2025"):
            for mes in range(1, 13):
                self._añadir(f"{anio}-{mes}", expediente=f"E/{anio}/{mes}",
                             fecha_publicacion=f"{anio}-{mes:02d}-05")
        for dia in ("01", "02", "03"):
            self._añadir(f"2026-01-{dia}", expediente=f"E/2026/{dia}",
                         fecha_publicacion=f"2026-01-{dia}")

    def test_el_expediente_cuenta_en_su_primera_publicacion(self):
        """Un expediente «sale» cuando aparece su primer anuncio, no cuando se publica su
        adjudicación seis meses después."""
        self._añadir("licitacion", expediente="EXP/1", fecha_publicacion="2025-02-01")
        self._añadir("adjudicacion", expediente="EXP/1", fecha_publicacion="2025-09-01")
        meses = consultas.analitica(self.con)["calendario"]["meses"]
        self.assertEqual(meses, [{"mes": "2025-02", "expedientes": 1}])

    def test_el_anio_en_curso_no_entra_en_la_media(self):
        """Promediar enero de 2026 con los eneros cerrados hundiría el mes: el corte
        de datos está en el día 3."""
        self._preparar_dos_anios()
        c = consultas.analitica(self.con)["calendario"]
        self.assertEqual(c["anios_completos"], ["2024", "2025"])
        self.assertEqual(c["anio_en_curso"], "2026")
        enero = next(m for m in c["media_por_mes"] if m["mes"] == "01")
        self.assertEqual(enero["media"], 1.0, "un expediente en cada enero cerrado")

    def test_la_cobertura_sale_de_la_ultima_publicacion_y_no_de_hoy(self):
        """Si el usuario no ingesta en una semana, el número de días con datos tiene que
        bajar solo. Con `date('now')` diría siempre que el mes está al día."""
        self._añadir("uno", fecha_publicacion="2026-01-03")
        c = consultas.analitica(self.con)["calendario"]
        self.assertTrue(c["corte"].startswith("2026-01-03"))
        self.assertEqual(c["mes_en_curso"], "2026-01")
        self.assertEqual((c["dias_con_datos"], c["dias_del_mes"]), (3, 31))

    def test_los_dias_del_mes_salen_del_calendario_de_verdad(self):
        self._añadir("febrero-bisiesto", fecha_publicacion="2024-02-10")
        c = consultas.analitica(self.con)["calendario"]
        self.assertEqual(c["dias_del_mes"], 29)

    def test_una_fecha_con_desplazamiento_no_se_va_al_mes_anterior(self):
        """`strftime` convierte a UTC y mandaría esto a octubre. Por eso el mes se saca
        con `substr(fecha, 1, 7)`."""
        self._añadir("madrugada", fecha_publicacion="2025-11-01T00:30:00+01:00")
        meses = consultas.analitica(self.con)["calendario"]["meses"]
        self.assertEqual([m["mes"] for m in meses], ["2025-11"])

    def test_el_rango_acota_por_el_mes_de_la_primera_publicacion(self):
        self._añadir("vieja", expediente="E/1", fecha_publicacion="2023-06-01")
        self._añadir("nueva", expediente="E/2", fecha_publicacion="2025-06-01")
        d = consultas.analitica(self.con, desde="2024-01")
        self.assertEqual(d["generado_para"]["expedientes"], 1)
        self.assertEqual([m["mes"] for m in d["calendario"]["meses"]], ["2025-06"])

    def test_por_debajo_del_minimo_de_meses_el_bloque_se_marca_insuficiente(self):
        """Una serie de dos meses no es una serie, y la pestaña no debe pintarla."""
        self._añadir("uno", fecha_publicacion="2025-01-05")
        self.assertFalse(consultas.analitica(self.con)["calendario"]["suficiente"])


class TestImportes(Base):
    def test_los_tramos_son_una_particion_de_los_que_tienen_importe(self):
        for i, importe in enumerate((10_000, 50_000, 200_000, 700_000, 5_000_000)):
            self._añadir(f"i{i}", expediente=f"E/{i}", importe_sin_iva=importe)
        self._añadir("sin-importe", expediente="E/9")
        d = consultas.analitica(self.con)["importes"]
        self.assertEqual(d["expedientes"], 6)
        self.assertEqual((d["con_importe"], d["sin_importe"]), (5, 1))
        self.assertEqual([t["expedientes"] for t in d["tramos"]], [1, 1, 1, 1, 1])
        self.assertEqual(sum(t["expedientes"] for t in d["tramos"]), d["con_importe"])

    def test_el_expediente_sin_importe_no_cae_en_el_tramo_de_cero(self):
        """Contarlo como «menos de 25.000 €» sería inventarse que es una operación
        pequeña cuando lo que pasa es que no se sabe."""
        self._añadir("sin-importe")
        d = consultas.analitica(self.con)["importes"]
        self.assertEqual(d["tramos"][0]["expedientes"], 0)
        self.assertIsNone(d["mediana"])

    def test_el_importe_del_expediente_es_el_mayor_de_sus_anuncios(self):
        """Un acuerdo marco con ocho lotes tiene anuncios de 195.000 y de 61 millones. Si
        se pinta el del anuncio más reciente, el comercial no lo abre."""
        self._añadir("lote", expediente="EXP/1", importe_sin_iva=195_000)
        self._añadir("marco", expediente="EXP/1", importe_sin_iva=61_000_000)
        d = consultas.analitica(self.con)["importes"]
        self.assertEqual(d["mediana"], 61_000_000)
        self.assertEqual(d["mayores"][0]["imp"], 61_000_000)

    def test_la_mediana_no_es_la_media(self):
        """Con un outlier, la media miente y la mediana no. En la base real la media son
        4.052.863 € y la mediana 169.288."""
        for i, importe in enumerate((1_000, 2_000, 3_000, 900_000_000)):
            self._añadir(f"i{i}", expediente=f"E/{i}", importe_sin_iva=importe)
        self.assertEqual(consultas.analitica(self.con)["importes"]["mediana"], 2_500)

    def test_no_se_publica_ninguna_suma_de_importes(self):
        """`clave_grupo` no deduplica entre fuentes: en la base real hay 126 expedientes
        repetidos que arrastran 1.038 M€, un 9,7% de la suma. Cualquier total sería un
        número inventado con cara de dato.

        La única excepción es `comunidades`, y no es un agujero en la regla sino un sitio
        donde el razonamiento de arriba no se sostiene: TED no publica región, así que la
        copia de TED de cada duplicado cae en «sin comunidad» y no en el total de ninguna.
        Eso lo prueba `TestComunidades.test_el_duplicado_de_ted_no_infla_ninguna_comunidad`,
        y por eso ese bloque se salta aquí en vez de esquivar el guardia con un nombre de
        clave distinto. Para todos los demás la prohibición sigue entera.
        """
        self._añadir("uno", importe_sin_iva=100_000)
        # Nombres exactos y no subcadenas: `mediana` contiene «media» y es justo lo que
        # sí se publica. Lo prohibido es la media aritmética y cualquier total.
        prohibidas = {"media", "suma", "importe_total", "total_importe", "suma_importes"}
        d = consultas.analitica(self.con)
        for bloque, contenido in d.items():
            if bloque == "comunidades" or not isinstance(contenido, dict):
                continue
            for clave in contenido:
                with self.subTest(bloque=bloque, clave=clave):
                    self.assertNotIn(clave, prohibidas)
                    self.assertNotIn("suma", clave)
                    self.assertNotIn("importe_total", clave)


class TestBajaDeAdjudicacion(Base):
    def _adjudicada(self, nombre, pres, adj, **campos):
        return self._añadir(nombre, expediente=f"E/{nombre}", estado="zuschlag",
                            importe_sin_iva=pres, importe_adjudicacion=adj,
                            fecha_adjudicacion="2025-06-01", **campos)

    def test_se_compara_con_el_presupuesto_base_y_no_con_el_valor_estimado(self):
        """`importe_referencia` prefiere `valor_estimado`, y el valor estimado incluye
        prórrogas y modificaciones: su p90 es 2,65 veces el presupuesto base. Comparar la
        adjudicación contra él daba una baja mediana del 30% donde la real es del 13%, y
        un comercial que fuera con ese 30% perdería margen por un error de columna."""
        self._adjudicada("con-prorrogas", 100_000, 90_000, valor_estimado=300_000)
        d = consultas.analitica(self.con)["baja"]
        self.assertEqual(d["mediana"], 10.0, "10.000 sobre 100.000, no 210.000/300.000")

    def test_la_adjudicacion_igual_al_presupuesto_no_es_una_baja_del_cero(self):
        """Son 270 expedientes de la base real donde la fuente repitió el presupuesto en
        lugar de publicar el precio. Contarlos como bajas del 0% hundiría la mediana."""
        self._adjudicada("repetido", 100_000, 100_000)
        self._adjudicada("real", 100_000, 80_000)
        d = consultas.analitica(self.con)["baja"]
        self.assertEqual(d["comparables"], 1)
        self.assertEqual(d["mediana"], 20.0)
        repetidos = next(e for e in d["excluidos"] if "wiederholt" in e["motivo"])
        self.assertEqual(repetidos["expedientes"], 1)

    def test_una_baja_imposible_no_entra_en_la_mediana(self):
        """Una baja del 90% no es una baja: es un lote comparado con el total del acuerdo
        marco, o una anualidad contra tres años."""
        self._adjudicada("lote-contra-total", 1_000_000, 50_000)
        self._adjudicada("normal", 100_000, 85_000)
        d = consultas.analitica(self.con)["baja"]
        self.assertEqual(d["comparables"], 1)
        self.assertEqual(d["mediana"], 15.0)
        escalas = next(e for e in d["excluidos"] if "vergleichbare" in e["motivo"])
        self.assertEqual(escalas["expedientes"], 1)

    def test_el_sobrecoste_se_cuenta_aparte(self):
        self._adjudicada("sobrecoste", 100_000, 120_000)
        d = consultas.analitica(self.con)["baja"]
        self.assertEqual(d["comparables"], 0)
        sobre = next(e for e in d["excluidos"] if "über dem" in e["motivo"])
        self.assertEqual(sobre["expedientes"], 1)

    def test_los_dos_importes_salen_del_mismo_anuncio(self):
        """Cruzar el presupuesto de un anuncio con la adjudicación de otro es la fábrica
        de bajas del 90%: aquí el anuncio de licitación no publica adjudicación y el de
        adjudicación no publica presupuesto, así que el expediente no es comparable."""
        self._añadir("licitacion", expediente="EXP/1", importe_sin_iva=1_000_000)
        self._añadir("adjudicacion", expediente="EXP/1", estado="zuschlag",
                     importe_adjudicacion=100_000, fecha_adjudicacion="2025-06-01")
        d = consultas.analitica(self.con)["baja"]
        self.assertEqual(d["con_ambos_importes"], 0)
        self.assertIsNone(d["mediana"])

    def test_los_excluidos_cuadran_con_los_que_tienen_ambos_importes(self):
        self._adjudicada("repetido", 100_000, 100_000)
        self._adjudicada("escalas", 1_000_000, 10_000)
        self._adjudicada("sobrecoste", 100_000, 110_000)
        self._adjudicada("bueno", 100_000, 90_000)
        d = consultas.analitica(self.con)["baja"]
        self.assertEqual(
            d["comparables"] + sum(e["expedientes"] for e in d["excluidos"]),
            d["con_ambos_importes"])


class TestCiclo(Base):
    def test_mide_el_recorrido_con_la_fecha_de_la_fuente(self):
        lic = self._añadir("normal", fecha_publicacion="2025-01-01")
        self._version(lic, "veroeffentlicht", "2025-01-01")
        self._version(lic, "zuschlag", "2025-04-01", anterior="veroeffentlicht")
        d = consultas.analitica(self.con)["ciclo"]
        self.assertEqual(d["expedientes"], 1)
        self.assertEqual(d["mediana_dias"], 90)

    def test_una_ficha_que_nace_adjudicada_no_recorrio_nada(self):
        """El anuncio de adjudicación se publica como ficha propia y su primera versión
        ya lleva la fecha de la adjudicación. Aceptándolo, el 45,3% de los expedientes
        salía con cero días o menos y la mediana no significaba nada."""
        lic = self._añadir("nacio-adjudicada", estado="zuschlag",
                           fecha_publicacion="2025-04-01")
        self._version(lic, "zuschlag", "2025-04-01")
        d = consultas.analitica(self.con)["ciclo"]
        self.assertEqual(d["expedientes"], 0)

    def test_no_usa_detectado_en(self):
        """Las 1,5 millones de versiones de la base real tienen todas la misma
        `detectado_en`, porque el histórico entró en una sola carga."""
        lic = self._añadir("con-fechas", fecha_publicacion="2025-01-01")
        self._version(lic, "veroeffentlicht", "2025-01-01")
        self._version(lic, "zuschlag", "2025-03-02", anterior="veroeffentlicht")
        # Todas las versiones se detectaron hoy: si el bloque mirara ahí, daría 0 días.
        detectados = {f[0] for f in self.con.execute(
            "SELECT detectado_en FROM licitaciones_versiones")}
        self.assertEqual(len(detectados), 1)
        self.assertEqual(consultas.analitica(self.con)["ciclo"]["mediana_dias"], 60)


class TestCpv(Base):
    def test_un_codigo_repetido_en_varios_anuncios_cuenta_una_vez(self):
        self._añadir("uno", expediente="EXP/1", cpv="72000000 79417000")
        self._añadir("dos", expediente="EXP/1", cpv="72000000")
        d = consultas.analitica(self.con)["cpv"]
        self.assertEqual(d["con_cpv"], 1)
        divisiones = {x["division"]: x["expedientes"] for x in d["divisiones"]}
        self.assertEqual(divisiones["72"], 1)
        producto = {x["codigo"]: x["expedientes"] for x in d["del_producto"]}
        self.assertEqual(producto["79417000"], 1)

    def test_el_sufijo_de_control_del_cpv_no_crea_un_codigo_nuevo(self):
        """En los datos reales el código llega a veces como «80533100-3»."""
        self._añadir("con-sufijo", cpv="79417000-3")
        producto = {x["codigo"]: x["expedientes"]
                    for x in consultas.analitica(self.con)["cpv"]["del_producto"]}
        self.assertEqual(producto["79417000"], 1)

    def test_el_expediente_sin_cpv_se_cuenta_aparte(self):
        self._añadir("sin-cpv")
        d = consultas.analitica(self.con)["cpv"]
        self.assertEqual((d["con_cpv"], d["sin_cpv"]), (0, 1))


class TestCartera(Base):
    def test_las_fronteras_de_puntuacion_parten_el_total(self):
        for i, punt in enumerate((2.0, 4.0, 4.5, 5.0, 6.5)):
            self._añadir(f"p{i}", expediente=f"E/{i}", puntuacion=punt)
        d = consultas.analitica(self.con)["cartera"]
        self.assertEqual(d["lista_corta"] + d["intermedios"] + d["resto"], d["expedientes"])
        self.assertEqual(d["lista_corta"], 1, "solo el 6,5 pasa de 5,0")

    def test_el_plazo_abierto_cuadra_con_el_contador_de_la_cabecera(self):
        """Es el invariante que impide que la pestaña y la bandeja cuenten distinto."""
        self._añadir("abierta", expediente="E/1", estado="veroeffentlicht",
                     fecha_limite_presentacion=self._sqlite_fecha("+10 days"))
        self._añadir("vencida", expediente="E/2", estado="veroeffentlicht",
                     fecha_limite_presentacion=self._sqlite_fecha("-10 days"))
        self._añadir("zuschlag", expediente="E/3", estado="zuschlag")
        d = consultas.analitica(self.con)["cartera"]
        self.assertEqual(d["con_plazo_abierto"],
                         consultas.contar(self.con, solo_vivas=True))
        self.assertEqual(d["con_plazo_abierto"], 1)

    def test_el_estado_es_el_del_anuncio_mas_reciente(self):
        """Contando todos los anuncios, un expediente adjudicado aparecería además como
        publicado y los estados sumarían más que el total."""
        self._añadir("licitacion", expediente="EXP/1", estado="veroeffentlicht",
                     fecha_publicacion="2025-01-01")
        self._añadir("adjudicacion", expediente="EXP/1", estado="zuschlag",
                     fecha_publicacion="2025-06-01")
        d = consultas.analitica(self.con)["cartera"]
        self.assertEqual(d["estados"], [{"estado": "zuschlag", "expedientes": 1}])
        self.assertEqual(sum(e["expedientes"] for e in d["estados"]), d["expedientes"])

    def test_dice_si_es_un_archivo_o_un_pipeline_segun_el_dato(self):
        """La frase no puede estar escrita a mano: el día que el usuario ingeste a diario
        durante meses, «esto es un archivo» dejará de ser verdad."""
        for i in range(30):
            self._añadir(f"cerrada{i}", expediente=f"E/{i}", estado="aufgehoben")
        self.assertTrue(consultas.analitica(self.con)["cartera"]["es_archivo_historico"])

        consultas._MEMO.clear()
        for i in range(10):
            self._añadir(f"abierta{i}", expediente=f"A/{i}", estado="veroeffentlicht",
                         fecha_limite_presentacion=self._sqlite_fecha("+10 days"))
        self.assertFalse(consultas.analitica(self.con)["cartera"]["es_archivo_historico"])


class TestRenovaciones(Base):
    def test_cuenta_los_que_vencen_en_la_ventana_y_los_que_tienen_incumbente(self):
        self._añadir("con-titular", expediente="E/1", estado="zuschlag",
                     adjudicatario="Incumbente SL",
                     fecha_fin_prevista=self._sqlite_fecha("+2 months"))
        self._añadir("sin-titular", expediente="E/2", estado="zuschlag",
                     fecha_fin_prevista=self._sqlite_fecha("+3 months"))
        self._añadir("lejano", expediente="E/3", estado="zuschlag",
                     adjudicatario="Otro SL",
                     fecha_fin_prevista=self._sqlite_fecha("+20 months"))
        d = consultas.analitica(self.con)["renovaciones"]
        self.assertEqual((d["expedientes"], d["con_incumbente"]), (2, 1))

    def test_el_rango_temporal_no_afecta_a_lo_que_es_de_hoy(self):
        """«Vence en seis meses» es de ahora: filtrarlo por «publicados en 2026» daría un
        número que no significa nada, y un filtro que se ignora en silencio es peor que
        un filtro que falta."""
        self._añadir("vieja-que-vence", estado="zuschlag",
                     fecha_publicacion="2024-01-01", adjudicatario="Incumbente SL",
                     fecha_fin_prevista=self._sqlite_fecha("+2 months"))
        d = consultas.analitica(self.con, desde="2026-01")
        self.assertEqual(d["generado_para"]["expedientes"], 0)
        self.assertEqual(d["renovaciones"]["expedientes"], 1)
        self.assertTrue(d["renovaciones"]["siempre_a_fecha_de_hoy"])
        self.assertTrue(d["cartera"]["siempre_a_fecha_de_hoy"])


class TestComunidades(Base):
    """El reparto territorial: el único bloque que suma dinero, y por qué puede.

    Las dos primeras pruebas protegen la excepción a la regla de «ninguna suma»; la
    tercera protege que el gráfico se pueda leer, que es lo que se perdía cuando un
    sistema dinámico de 2.646 M€ entraba en la misma escala que un contrato de 70.000 €.
    """

    def _adjudicada(self, nombre, bundesland, importe, **campos):
        return self._añadir(nombre, expediente=f"E/{nombre}", estado="zuschlag",
                            bundesland=bundesland, importe_adjudicacion=importe, **campos)

    def test_suma_por_comunidad_y_ordena_de_mas_a_menos(self):
        self._adjudicada("a", "Madrid", 300_000)
        self._adjudicada("b", "Madrid", 200_000)
        self._adjudicada("c", "Galicia", 400_000)
        d = consultas.analitica(self.con)["comunidades"]["adjudicadas"]
        self.assertEqual([(c["bundesland"], c["importe"], c["expedientes"])
                          for c in d["comunidades"]],
                         [("Madrid", 500_000, 2), ("Galicia", 400_000, 1)])

    def test_el_duplicado_de_ted_no_infla_ninguna_comunidad(self):
        """La prueba que sostiene la excepción a «ninguna cifra de dinero total».

        El mismo contrato real llega dos veces con `clave_grupo` distinta —una por PLACSP
        y otra por TED— y eso es lo que hace inventada cualquier suma del mercado. Pero
        TED no expone territorio, así que su copia va a «sin comunidad»: el total de
        Madrid sigue siendo el de un solo contrato, no el de dos.
        """
        self._adjudicada("placsp", "Madrid", 100_000, fuente="prueba")
        self._añadir("ted", expediente="E/otro", estado="zuschlag", bundesland=None,
                     importe_adjudicacion=100_000, fuente="ted")
        d = consultas.analitica(self.con)["comunidades"]["adjudicadas"]
        self.assertEqual([c["bundesland"] for c in d["comunidades"]], ["Madrid"])
        self.assertEqual(d["comunidades"][0]["importe"], 100_000)
        self.assertEqual(d["sin_comunidad"], 1)

    def test_el_macro_contrato_sale_de_la_barra_pero_no_del_pie(self):
        """Se aparta de la escala y se enseña con nombre y con el total de su comunidad.

        Con los 2.646 M€ del sistema dinámico dentro, diecisiete comunidades pintaban una
        barra de un píxel. Apartarlo sin decirlo sería peor: el pie da el total de verdad.
        """
        grande = consultas.IMPORTE_MAXIMO_COMPARABLE + 1
        self._adjudicada("normal", "Madrid", 400_000)
        self._adjudicada("marco", "Madrid", grande, organo="Racionalización")
        d = consultas.analitica(self.con)["comunidades"]["adjudicadas"]

        self.assertEqual(d["comunidades"][0]["importe"], 400_000, "la barra, sin el marco")
        self.assertEqual(d["importe_en_barras"], 400_000)
        self.assertEqual(d["importe_excluido"], grande)
        self.assertEqual(d["importe_con_excluidos"], 400_000 + grande)
        self.assertEqual(len(d["excluidos"]), 1)
        self.assertEqual(d["excluidos"][0]["organo"], "Racionalización")
        self.assertEqual(d["excluidos"][0]["total_de_su_comunidad"], 400_000 + grande)

    def test_los_lotes_de_un_expediente_se_suman(self):
        """Cataluña emite una fila POR LOTE y PLACSP una por licitación.

        Con un `MAX` a secas, de un contrato de tres lotes se contaba el mayor y se
        tiraban los otros dos. Medido sobre la base real, Cataluña era la única comunidad
        afectada: 51,9 M€ contra los 61,7 M€ reales.
        """
        for i, imp in enumerate((100_000, 60_000, 40_000)):
            self._añadir(f"lote{i}", expediente="E/multi", estado="zuschlag",
                         bundesland="Cataluña", importe_adjudicacion=imp, lote_num=str(i + 1))
        d = consultas.analitica(self.con)["comunidades"]["adjudicadas"]
        self.assertEqual(d["comunidades"][0]["importe"], 200_000)
        self.assertEqual(d["comunidades"][0]["expedientes"], 1, "sigue siendo un expediente")

    def test_la_republicacion_de_un_lote_no_se_suma_dos_veces(self):
        """El otro lado de la moneda, y la razón de que la agregación sea de dos niveles.

        Sumar en bruto daría 76,7 M€ para Cataluña en vez de 61,7: hay 59 pares
        (expediente, lote) con más de un anuncio. Dentro del lote manda el MAX.
        """
        self._añadir("v1", expediente="E/rep", estado="zuschlag", bundesland="Cataluña",
                     importe_adjudicacion=50_000, lote_num="1")
        self._añadir("v2", expediente="E/rep", estado="zuschlag", bundesland="Cataluña",
                     importe_adjudicacion=55_000, lote_num="1")
        self._añadir("otro", expediente="E/rep", estado="zuschlag", bundesland="Cataluña",
                     importe_adjudicacion=30_000, lote_num="2")
        d = consultas.analitica(self.con)["comunidades"]["adjudicadas"]
        self.assertEqual(d["comunidades"][0]["importe"], 85_000,
                         "55.000 del lote 1 (el mayor de sus dos anuncios) + 30.000 del 2")

    def test_un_expediente_sin_lotes_se_agrega_como_siempre(self):
        """PLACSP no publica lote, así que su republicación tiene que seguir dando el MAX."""
        self._añadir("a", expediente="E/uno", estado="zuschlag", bundesland="Madrid",
                     importe_adjudicacion=100_000)
        self._añadir("b", expediente="E/uno", estado="zuschlag", bundesland="Madrid",
                     importe_adjudicacion=120_000)
        d = consultas.analitica(self.con)["comunidades"]["adjudicadas"]
        self.assertEqual(d["comunidades"][0]["importe"], 120_000)

    def test_el_recuento_incluye_los_macro_contratos_que_el_importe_aparta(self):
        """Es la diferencia que hace que Madrid diga 14 por número y 12 por dinero.

        Un recuento no lo desequilibra un contrato grande, y apartarlo escondería una
        licitación que existe y a la que se puede ir.
        """
        self._añadir("normal", expediente="E/1", estado="zuschlag", bundesland="Madrid",
                     importe_adjudicacion=400_000)
        self._añadir("marco", expediente="E/2", estado="zuschlag", bundesland="Madrid",
                     importe_adjudicacion=consultas.IMPORTE_MAXIMO_COMPARABLE + 1)
        d = consultas.analitica(self.con)["comunidades"]["adjudicadas"]
        self.assertEqual(d["comunidades"][0]["expedientes"], 1, "en las barras de dinero")
        self.assertEqual(d["recuento"][0]["expedientes"], 2, "en el recuento, los dos")
        self.assertEqual(d["expedientes_contados"], 2)

    def test_el_recuento_ordena_por_numero_y_no_por_dinero(self):
        """Los dos ordenes no coinciden, y ese desajuste es el motivo del gráfico."""
        self._añadir("caro", expediente="E/caro", estado="zuschlag", bundesland="Madrid",
                     importe_adjudicacion=900_000)
        for i in range(3):
            self._añadir(f"barato{i}", expediente=f"E/b{i}", estado="zuschlag",
                         bundesland="Cataluña", importe_adjudicacion=10_000)
        d = consultas.analitica(self.con)["comunidades"]["adjudicadas"]
        self.assertEqual(d["comunidades"][0]["bundesland"], "Madrid", "por dinero manda Madrid")
        self.assertEqual(d["recuento"][0]["bundesland"], "Cataluña", "por número, Cataluña")

    def test_el_pie_siempre_cuadra(self):
        for i, imp in enumerate((1_000, 90_000_000, 500_000, 60_000_000)):
            self._adjudicada(f"x{i}", "Galicia", imp)
        d = consultas.analitica(self.con)["comunidades"]["adjudicadas"]
        self.assertEqual(d["importe_en_barras"] + d["importe_excluido"],
                         d["importe_con_excluidos"])

    def test_las_adjudicadas_se_acotan_por_el_rango_y_las_activas_no(self):
        """Las activas hablan de hoy, como la cartera y las renovaciones, y lo declaran.

        Un pliego abierto que se publicó en 2024 sigue abierto hoy: acotarlo por el rango
        lo escondería justo cuando es lo único que se puede pelear.
        """
        self._adjudicada("vieja", "Madrid", 100_000, fecha_publicacion="2024-05-10")
        self._añadir("viva", expediente="E/viva", estado="veroeffentlicht", bundesland="Madrid",
                     importe_sin_iva=70_000, fecha_publicacion="2024-05-10",
                     fecha_limite_presentacion=self._sqlite_fecha("+10 days"))

        d = consultas.analitica(self.con, desde="2025-01")["comunidades"]
        self.assertEqual(d["adjudicadas"]["comunidades"], [], "la de 2024 queda fuera")
        self.assertEqual(d["activas"]["comunidades"][0]["expedientes"], 1)
        self.assertTrue(d["activas"]["siempre_a_fecha_de_hoy"])

    def test_una_adjudicada_no_cuenta_como_dinero_vivo(self):
        """La fuente a veces deja el estado en vivo después de publicar la adjudicación,
        y ese dinero ya no está en juego."""
        self._añadir("cerrada", expediente="E/c", estado="veroeffentlicht", bundesland="Madrid",
                     importe_sin_iva=90_000, importe_adjudicacion=80_000,
                     fecha_limite_presentacion=self._sqlite_fecha("+10 days"))
        d = consultas.analitica(self.con)["comunidades"]["activas"]
        self.assertEqual(d["comunidades"], [])

    def test_el_plazo_vencido_no_es_dinero_vivo(self):
        self._añadir("vencida", expediente="E/v", estado="veroeffentlicht", bundesland="Madrid",
                     importe_sin_iva=90_000,
                     fecha_limite_presentacion=self._sqlite_fecha("-1 day"))
        self.assertEqual(
            consultas.analitica(self.con)["comunidades"]["activas"]["comunidades"], [])


class TestPlazo(Base):
    def test_las_dos_fechas_salen_del_mismo_anuncio(self):
        """Cruzar la publicación de un anuncio con el plazo de otro fabrica plazos que
        nadie tuvo. Aquí el mínimo de cada columna por separado daría 14 días."""
        self._añadir("a", expediente="E/1", fecha_publicacion="2025-03-01",
                     fecha_limite_presentacion="2025-04-30")
        self._añadir("b", expediente="E/1", fecha_publicacion="2025-04-01",
                     fecha_limite_presentacion="2025-03-15")
        d = consultas.analitica(self.con)["plazo"]
        self.assertEqual(d["expedientes"], 1)
        self.assertEqual(d["mediana_dias"], 60)

    def test_el_plazo_ya_vencido_no_entra_como_un_cero(self):
        """159 expedientes de la base repiten un plazo anterior a su publicación. Como
        ceros hundirían la mediana, así que se cuentan aparte y se enseñan."""
        self._añadir("bien", expediente="E/1", fecha_publicacion="2025-03-01",
                     fecha_limite_presentacion="2025-03-21")
        self._añadir("mal", expediente="E/2", fecha_publicacion="2025-03-01",
                     fecha_limite_presentacion="2025-02-01")
        d = consultas.analitica(self.con)["plazo"]
        self.assertEqual(d["expedientes"], 1)
        self.assertEqual(d["mediana_dias"], 20)
        self.assertEqual(d["con_ambas_fechas"], 2)
        motivos = {e["motivo"]: e["expedientes"] for e in d["excluidos"]}
        self.assertEqual(motivos["die Frist war bei Veröffentlichung schon abgelaufen"], 1)

    def test_los_tramos_son_una_particion_de_los_usables(self):
        for i, dias in enumerate((3, 12, 25, 40, 90)):
            self._añadir(f"p{i}", expediente=f"E/{i}", fecha_publicacion="2025-01-01",
                         fecha_limite_presentacion=self._sqlite_fecha())
            self.con.execute(
                "UPDATE licitaciones SET fecha_limite_presentacion ="
                " date('2025-01-01', ?) WHERE id_externo = ?", (f"+{dias} days", f"p{i}"))
        self.con.commit()
        d = consultas.analitica(self.con)["plazo"]
        self.assertEqual(sum(t["expedientes"] for t in d["tramos"]), d["expedientes"])
        self.assertEqual(d["expedientes"], 5)

    def test_por_debajo_del_minimo_el_bloque_se_marca_insuficiente(self):
        self._añadir("uno", expediente="E/1", fecha_publicacion="2025-03-01",
                     fecha_limite_presentacion="2025-03-21")
        self.assertFalse(consultas.analitica(self.con)["plazo"]["suficiente"])


class TestProcedimiento(Base):
    def test_lo_que_llega_sin_traducir_no_parte_la_barra_en_dos(self):
        """El conector ya traduce al ingerir, pero si un valor nuevo del servicio se cuela
        en inglés, «Open» y «Offenes Verfahren» saldrían como dos barras de lo mismo."""
        self._añadir("de", expediente="E/1", procedimiento="Offenes Verfahren")
        self._añadir("en", expediente="E/2", procedimiento="Open")
        self._añadir("en2", expediente="E/3", procedimiento="Restricted")
        d = consultas.analitica(self.con)["procedimiento"]
        reparto = {x["procedimiento"]: x["expedientes"] for x in d["procedimientos"]}
        self.assertEqual(reparto["Offenes Verfahren"], 2)
        self.assertEqual(reparto["Nichtoffenes Verfahren"], 1)

    def test_el_reparto_mas_los_sin_dato_cuadran_con_el_total(self):
        self._añadir("con", expediente="E/1", procedimiento="Offenes Verfahren")
        self._añadir("sin", expediente="E/2", procedimiento=None)
        d = consultas.analitica(self.con)["procedimiento"]
        self.assertEqual(
            sum(x["expedientes"] for x in d["procedimientos"]) + d["sin_dato"],
            d["expedientes"])
        self.assertEqual(d["sin_dato"], 1)

    def test_lo_que_no_cabe_en_el_top_se_agrupa_sin_perderse(self):
        for i in range(consultas.TOP_PROCEDIMIENTOS + 3):
            self._añadir(f"p{i}", expediente=f"E/{i}", procedimiento=f"Tipo {i}")
        d = consultas.analitica(self.con)["procedimiento"]
        self.assertEqual(d["procedimientos"][-1]["procedimiento"], "übrige")
        self.assertEqual(sum(x["expedientes"] for x in d["procedimientos"]),
                         d["expedientes"])


class TestOrganos(Base):
    def test_cuenta_expedientes_y_no_anuncios(self):
        """Tres republicaciones del mismo expediente son un comprador, no tres."""
        for i in range(3):
            self._añadir(f"a{i}", expediente="E/1", organo="INCIBE")
        self._añadir("otro", expediente="E/2", organo="AMTEGA")
        d = consultas.analitica(self.con)["organos"]
        reparto = {x["organo"]: x["expedientes"] for x in d["organos"]}
        self.assertEqual(reparto, {"INCIBE": 1, "AMTEGA": 1})
        self.assertEqual(d["distintos"], 2)

    def test_se_queda_con_los_primeros_y_lo_dice_en_distintos(self):
        for i in range(consultas.TOP_ORGANOS + 4):
            self._añadir(f"o{i}", expediente=f"E/{i}", organo=f"Órgano {i}")
        d = consultas.analitica(self.con)["organos"]
        self.assertEqual(len(d["organos"]), consultas.TOP_ORGANOS)
        self.assertEqual(d["distintos"], consultas.TOP_ORGANOS + 4)


class TestCoherencia(Base):
    def test_los_agregados_cuadran_con_los_contadores_de_la_cabecera(self):
        """Cada bloque agrega por su cuenta sobre la misma base; si uno se desvía del
        contador de arriba, la pestaña se contradice consigo misma y no hay forma de saber
        a cuál creer.

        La referencia es `contar()`, no una constante: es la función que alimenta la
        bandeja, así que la Analítica y la bandeja miden lo mismo por construcción.
        """
        self._añadir("dos-anuncios-a", expediente="EXP/1", importe_sin_iva=100_000,
                     cpv="72000000")
        self._añadir("dos-anuncios-b", expediente="EXP/1", importe_sin_iva=120_000)
        self._añadir("dos-perfiles", expediente="EXP/2", perfiles=("p", "q"),
                     importe_sin_iva=30_000, cpv="79417000")
        self._añadir("sin-importe", expediente="EXP/3", cpv="48000000")
        self._añadir("sin-cpv", expediente="EXP/4", importe_sin_iva=2_000_000)
        descartado = self._añadir("descartado", expediente="EXP/5",
                                  importe_sin_iva=500_000)
        db.fijar_revision(self.con, descartado, estado="descartado",
                          motivo_descarte="fuera de nicho")
        self.con.commit()

        esperado = consultas.contar(self.con, solo_vivas=False)
        d = consultas.analitica(self.con)
        self.assertEqual(esperado, 4, "cinco expedientes menos el descartado")

        for bloque in ("generado_para", "importes", "cartera", "cpv"):
            with self.subTest(bloque=bloque):
                self.assertEqual(d[bloque]["expedientes"], esperado)

        # Los tramos son una partición: ni pierden ni duplican.
        importes = d["importes"]
        self.assertEqual(sum(t["expedientes"] for t in importes["tramos"]),
                         importes["con_importe"])
        self.assertEqual(importes["con_importe"] + importes["sin_importe"], esperado)

        # El calendario cuenta cada expediente en un mes y solo en uno.
        self.assertEqual(sum(m["expedientes"] for m in d["calendario"]["meses"]), esperado)

        # El CPV también reparte el total, con los que no traen ninguno aparte.
        self.assertEqual(d["cpv"]["con_cpv"] + d["cpv"]["sin_cpv"], esperado)

        # Y la puntuación parte el total en tres sin solapes.
        cartera = d["cartera"]
        self.assertEqual(
            cartera["lista_corta"] + cartera["intermedios"] + cartera["resto"], esperado)

    def test_una_base_vacia_no_revienta_ningun_bloque(self):
        """La pestaña se abre en una instalación recién hecha, antes de la primera
        ingesta."""
        d = consultas.analitica(self.con)
        self.assertEqual(d["generado_para"]["expedientes"], 0)
        self.assertIsNone(d["importes"]["mediana"])
        self.assertIsNone(d["baja"]["mediana"])
        self.assertIsNone(d["ciclo"]["mediana_dias"])
        self.assertEqual(d["calendario"]["meses"], [])
        self.assertEqual(d["calendario"]["media_por_mes"], [])
        for bloque in ("calendario", "baja", "ciclo"):
            with self.subTest(bloque=bloque):
                self.assertFalse(d[bloque]["suficiente"])

    def test_el_memo_se_invalida_cuando_entra_algo_nuevo(self):
        """Si la caché no viera la ingesta de la mañana, la pestaña enseñaría las cifras
        de ayer sin avisar."""
        self._añadir("uno", importe_sin_iva=100_000)
        primera = consultas.analitica(self.con)
        self.assertEqual(primera["generado_para"]["expedientes"], 1)
        self._añadir("dos", expediente="E/2", importe_sin_iva=200_000)
        self.assertEqual(consultas.analitica(self.con)["generado_para"]["expedientes"], 2)

    def test_el_memo_se_invalida_cuando_el_usuario_tria(self):
        lic = self._añadir("uno", importe_sin_iva=100_000)
        self.assertEqual(consultas.analitica(self.con)["generado_para"]["expedientes"], 1)
        db.fijar_revision(self.con, lic, estado="descartado", motivo_descarte="otro")
        self.con.commit()
        self.assertEqual(consultas.analitica(self.con)["generado_para"]["expedientes"], 0)


if __name__ == "__main__":
    unittest.main()
