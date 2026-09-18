"""Consultas de la bandeja: qué se considera "abierta" y en qué orden se muestra."""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from radar import consultas, db
from radar.model import Licitacion


def dias(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat() + "T13:00:00"


class TestBandeja(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        casos = [
            ("cierra-en-3", dias(3), "veroeffentlicht", 50000),
            ("cierra-en-40", dias(40), "veroeffentlicht", 90000),
            ("vencida-hace-mucho", dias(-1400), "veroeffentlicht", 200000),
            ("sin-plazo", None, "veroeffentlicht", 30000),
            ("ya-adjudicada", dias(-10), "zuschlag", 70000),
        ]
        for i, (nombre, limite, estado, importe) in enumerate(casos):
            db.guardar(self.con, Licitacion(
                fuente="prueba", id_externo=nombre,
                objeto=f"Concienciación en ciberseguridad {nombre}",
                organo="Órgano de prueba", estado=estado,
                valor_estimado=importe, fecha_limite_presentacion=limite,
            ))
        for fila in self.con.execute("SELECT id FROM licitaciones"):
            self.con.execute(
                """INSERT INTO matches (licitacion_id, perfil, puntuacion, motivo, creado_en)
                   VALUES (?, 'Perfil de prueba', 3.0, 'motivo de prueba', ?)""",
                (fila["id"], db.ahora()),
            )
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def _ids(self, **kw):
        return [it["id_externo"] for it in consultas.bandeja(self.con, **kw)["items"]]

    def test_abiertas_excluye_vencidas_y_adjudicadas(self):
        """Hay licitaciones en estado 'veroeffentlicht' con plazo de hace cuatro años:
        filtrar solo por estado las colaba como si estuvieran abiertas."""
        abiertas = self._ids(solo_vivas=True)
        self.assertIn("cierra-en-3", abiertas)
        self.assertIn("cierra-en-40", abiertas)
        self.assertIn("sin-plazo", abiertas)
        self.assertNotIn("vencida-hace-mucho", abiertas)
        self.assertNotIn("ya-adjudicada", abiertas)

    def test_orden_urgencia_pone_lo_vencido_al_final(self):
        orden = self._ids(solo_vivas=False, orden="urgencia")
        self.assertEqual(orden[0], "cierra-en-3")
        self.assertLess(orden.index("cierra-en-3"), orden.index("cierra-en-40"))
        self.assertLess(orden.index("cierra-en-40"), orden.index("sin-plazo"),
                        "sin plazo va después de lo que tiene plazo abierto")
        self.assertEqual(orden[-1], "vencida-hace-mucho")

    def test_dias_restantes_se_calcula(self):
        items = consultas.bandeja(self.con, solo_vivas=False)["items"]
        por_id = {it["id_externo"]: it["dias_restantes"] for it in items}
        self.assertEqual(por_id["cierra-en-3"], 3)
        self.assertEqual(por_id["cierra-en-40"], 40)
        self.assertIsNone(por_id["sin-plazo"])

    def test_descartadas_no_estorban_por_defecto(self):
        lic_id = self.con.execute(
            "SELECT id FROM licitaciones WHERE id_externo = 'cierra-en-3'"
        ).fetchone()[0]
        db.fijar_revision(self.con, lic_id, estado="descartado")
        self.con.commit()
        self.assertNotIn("cierra-en-3", self._ids(solo_vivas=True))
        self.assertIn("cierra-en-3", self._ids(solo_vivas=True, estado_revision="descartado"))

    def test_filtro_por_importe(self):
        self.assertEqual(
            sorted(self._ids(solo_vivas=True, importe_min=80000)), ["cierra-en-40"]
        )

    def test_busqueda_libre(self):
        self.assertIn("cierra-en-3", self._ids(solo_vivas=True, busqueda="ciberseguridad"))
        self.assertEqual(self._ids(solo_vivas=True, busqueda="alcantarillado"), [])

    def test_csv_lleva_las_columnas_que_usa_un_comercial(self):
        columnas, filas = consultas.para_csv(self.con, solo_vivas=False)
        for esperada in ("organo", "objeto", "importe_referencia",
                         "fecha_limite_presentacion", "url_detalle", "motivo"):
            self.assertIn(esperada, columnas)
        self.assertEqual(len(filas), 5)


class TestElTriajeProtegeLaFicha(unittest.TestCase):
    """Lo que sigues no se pierde al estrechar los perfiles.

    El incidente: al desactivar la red amplia de ciberseguridad, un contrato de 6,25 M€
    de Canal de Isabel II que estaba en seguimiento —con 23 días de plazo— se fue de la
    bandeja. Su fila de `revisiones` seguía intacta, pero no había forma de verlo: ni el
    filtro de estado ni la búsqueda libre pasan por fuera de `matches`, así que la ficha
    era invisible en las tres vistas y el contador de la cabecera decía 1 donde había 2.

    Marcar algo es una decisión explícita y pesa más que el filtro automático.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        self.addCleanup(self.con.close)

    def _guardar(self, nombre, *, con_match: bool, estado_triaje=None):
        db.guardar(self.con, Licitacion(
            fuente="prueba", id_externo=nombre, expediente=f"E/{nombre}",
            objeto=f"Servicios de ciberseguridad {nombre}",
            organo="Órgano de prueba", estado="veroeffentlicht",
            valor_estimado=6_250_000, fecha_limite_presentacion=dias(23),
        ))
        lic = self.con.execute(
            "SELECT id FROM licitaciones WHERE id_externo = ?", (nombre,)).fetchone()["id"]
        if con_match:
            self.con.execute(
                "INSERT INTO matches (licitacion_id, perfil, puntuacion, motivo, creado_en)"
                " VALUES (?, 'Perfil activo', 3.0, 'motivo', ?)", (lic, db.ahora()))
        if estado_triaje:
            db.fijar_revision(self.con, lic, estado=estado_triaje,
                              motivo_descarte="fuera de nicho"
                              if estado_triaje == "descartado" else None)
        self.con.commit()
        return lic

    def _ids(self, **kw):
        return [it["id"] for it in consultas.bandeja(self.con, **kw)["items"]]

    def test_lo_que_sigues_se_queda_aunque_deje_de_casar(self):
        seguida = self._guardar("seguida", con_match=False, estado_triaje="siguiendo")
        self.assertIn(seguida, self._ids(solo_vivas=False))
        self.assertIn(seguida, self._ids(solo_vivas=True),
                      "y también en la vista de cada día, «solo abiertas»")

    def test_lo_presentado_tambien(self):
        """Si se ha llegado a presentar oferta, esconderlo sería aún peor."""
        p = self._guardar("presentada", con_match=False, estado_triaje="presentada")
        self.assertIn(p, self._ids(solo_vivas=False))

    def test_lo_descartado_que_deja_de_casar_no_resucita(self):
        """La otra mitad del contrato. Aquí el filtro y la decisión dicen lo mismo, y
        traerlo de vuelta sería justo el ruido que se venía a quitar."""
        d = self._guardar("descartada", con_match=False, estado_triaje="descartado")
        self.assertNotIn(d, self._ids(solo_vivas=False))
        self.assertNotIn(d, self._ids(solo_vivas=False, estado_revision="descartado"))

    def test_lo_que_nunca_se_toco_y_no_casa_sigue_fuera(self):
        """Sin triaje no hay nada que proteger: la bandeja no es la base entera."""
        suelta = self._guardar("sin-tocar", con_match=False)
        self.assertNotIn(suelta, self._ids(solo_vivas=False))

    def test_el_filtro_de_estado_la_encuentra(self):
        seguida = self._guardar("seguida", con_match=False, estado_triaje="siguiendo")
        self._guardar("normal", con_match=True)
        self.assertEqual(self._ids(solo_vivas=False, estado_revision="siguiendo"), [seguida])

    def test_el_contador_de_la_cabecera_cuadra_con_la_lista(self):
        """El síntoma que delató el problema: la cabecera decía una cifra y la lista otra.

        `resumen()` calcula sus KPI con `contar()`, así que las dos tienen que moverse
        juntas; esto lo fija.
        """
        self._guardar("seguida", con_match=False, estado_triaje="siguiendo")
        self._guardar("seguida-2", con_match=True, estado_triaje="siguiendo")
        r = consultas.resumen(self.con, en_marcha=False)
        self.assertEqual(r["siguiendo"], 2)
        self.assertEqual(
            r["siguiendo"],
            consultas.bandeja(self.con, solo_vivas=False,
                              estado_revision="siguiendo")["total"])

    def test_la_ficha_protegida_no_finge_tener_perfil(self):
        """Llega con `perfil` a nulo, que es lo que hace que la ficha diga «está en la
        base pero no casa con ningún perfil activo» en lugar de inventarse un motivo."""
        self._guardar("seguida", con_match=False, estado_triaje="siguiendo")
        ficha = consultas.bandeja(self.con, solo_vivas=False)["items"][0]
        self.assertIsNone(ficha["perfil"])
        self.assertIsNone(ficha["motivo"])
        self.assertEqual(ficha["estado_revision"], "siguiendo")

    def test_filtrar_por_perfil_no_la_arrastra(self):
        """Si se pide un perfil concreto, se quiere ese perfil: una ficha que no casa
        con ninguno no tiene nada que hacer en esa lista."""
        seguida = self._guardar("seguida", con_match=False, estado_triaje="siguiendo")
        normal = self._guardar("normal", con_match=True)
        ids = self._ids(solo_vivas=False, perfil="Perfil activo")
        self.assertIn(normal, ids)
        self.assertNotIn(seguida, ids)


class TestNuevasYOrdenPorPublicacion(unittest.TestCase):
    """La etiqueta «Nueva» y el orden por fecha de publicación.

    Las dos miran la PRIMERA publicación del expediente y no la del anuncio que se
    enseña en la tarjeta, que es el más reciente del grupo. La diferencia no es teórica:
    sobre la base real, 5 de los 16 expedientes que el criterio ingenuo marcaba como
    recientes eran pliegos viejos con una adjudicación publicada esta semana, y uno de
    ellos llevaba adjudicado desde junio.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        self.addCleanup(self.con.close)

    def _añadir(self, nombre, publicacion, *, expediente=None, estado="veroeffentlicht"):
        db.guardar(self.con, Licitacion(
            fuente="prueba", id_externo=nombre, expediente=expediente or nombre,
            objeto=f"Concienciación en ciberseguridad {nombre}",
            organo="Órgano de prueba", estado=estado,
            fecha_publicacion=publicacion, valor_estimado=50_000,
        ))
        lic = self.con.execute(
            "SELECT id FROM licitaciones WHERE id_externo = ?", (nombre,)).fetchone()["id"]
        self.con.execute(
            "INSERT INTO matches (licitacion_id, perfil, puntuacion, motivo, creado_en)"
            " VALUES (?, 'p', 3.0, 'm', ?)", (lic, db.ahora()))
        self.con.commit()

    def _items(self, **kw):
        return {it["expediente"]: it for it in consultas.bandeja(self.con, **kw)["items"]}

    def test_lo_publicado_esta_semana_sale_marcado(self):
        self._añadir("hoy", dias(0), expediente="E/hoy")
        self._añadir("hace6", dias(-6), expediente="E/hace6")
        items = self._items(solo_vivas=False)
        self.assertTrue(items["E/hoy"]["es_nueva"])
        self.assertEqual(items["E/hoy"]["dias_desde_publicacion"], 0)
        self.assertTrue(items["E/hace6"]["es_nueva"])

    def test_la_etiqueta_caduca_a_los_siete_dias(self):
        self._añadir("justo", dias(-consultas.DIAS_NUEVA), expediente="E/justo")
        self._añadir("pasada", dias(-consultas.DIAS_NUEVA - 1), expediente="E/pasada")
        items = self._items(solo_vivas=False)
        self.assertTrue(items["E/justo"]["es_nueva"], "el séptimo día todavía cuenta")
        self.assertFalse(items["E/pasada"]["es_nueva"])

    def test_una_adjudicacion_reciente_no_convierte_en_nuevo_un_pliego_viejo(self):
        """El caso que justifica todo lo demás: dos anuncios del mismo expediente."""
        self._añadir("viejo", dias(-90), expediente="E/1")
        self._añadir("adjudicacion", dias(-1), expediente="E/1", estado="zuschlag")
        items = self._items(solo_vivas=False)
        self.assertEqual(len(items), 1, "los dos anuncios son un expediente")
        ficha = items["E/1"]
        self.assertEqual(ficha["fecha_publicacion"][:10], dias(-1)[:10],
                         "la tarjeta enseña el anuncio más reciente")
        self.assertFalse(ficha["es_nueva"], "pero el expediente no es nuevo")
        self.assertEqual(ficha["dias_desde_publicacion"], 90)

    def test_sin_fecha_de_publicacion_no_se_marca_nada(self):
        self._añadir("sinfecha", None, expediente="E/sf")
        ficha = self._items(solo_vivas=False)["E/sf"]
        self.assertFalse(ficha["es_nueva"])
        self.assertIsNone(ficha["dias_desde_publicacion"])

    def test_el_orden_por_publicacion_va_en_los_dos_sentidos(self):
        self._añadir("a", dias(-30), expediente="E/a")
        self._añadir("b", dias(-2), expediente="E/b")
        self._añadir("c", dias(-15), expediente="E/c")
        recientes = [i["expediente"] for i in
                     consultas.bandeja(self.con, solo_vivas=False, orden="reciente")["items"]]
        antiguas = [i["expediente"] for i in
                    consultas.bandeja(self.con, solo_vivas=False, orden="antigua")["items"]]
        self.assertEqual(recientes, ["E/b", "E/c", "E/a"])
        self.assertEqual(antiguas, ["E/a", "E/c", "E/b"])

    def test_las_sin_fecha_no_encabezan_las_mas_antiguas(self):
        """SQLite ordena NULL antes que cualquier valor, así que un ASC a secas abría
        «las más antiguas» con las que no tienen fecha: desconocidas, no antiguas."""
        self._añadir("vieja", dias(-900), expediente="E/vieja")
        self._añadir("media", dias(-100), expediente="E/media")
        self._añadir("sinfecha", None, expediente="E/sf")
        orden = [i["expediente"] for i in
                 consultas.bandeja(self.con, solo_vivas=False, orden="antigua")["items"]]
        self.assertEqual(orden, ["E/vieja", "E/media", "E/sf"])

    def test_la_bandeja_abre_por_lo_ultimo_publicado(self):
        """El orden de fábrica, fijado aquí para que no se deshaga sin querer.

        Hasta la 1.3.0 era «urgencia», y poner arriba lo que cierra antes deja lo
        primero de la lista fuera de plazo para preparar nada. El valor vive en
        `consultas.ORDEN_POR_DEFECTO` y lo leen también el servidor y el `<select>` de
        la interfaz; si alguien cambia uno de los tres y no los otros, la API y la
        pantalla dejan de decir lo mismo y nadie se enterará hasta que un filtro
        devuelva un orden que nadie pidió.
        """
        self.assertEqual(consultas.ORDEN_POR_DEFECTO, "reciente")
        self._añadir("a", dias(-30), expediente="E/a")
        self._añadir("b", dias(-2), expediente="E/b")
        self._añadir("c", dias(-15), expediente="E/c")
        sin_pedir_orden = [i["expediente"] for i in
                           consultas.bandeja(self.con, solo_vivas=False)["items"]]
        self.assertEqual(sin_pedir_orden, ["E/b", "E/c", "E/a"])

    def test_un_orden_que_no_existe_cae_en_el_de_fabrica(self):
        """El respaldo de `ORDENES.get` tiene que ser el mismo valor por defecto.

        Estuvo apuntando a «urgencia» cuando el de fábrica ya era otro: una petición
        con `orden=loquesea` devolvía un orden distinto del que da no pedir nada.
        """
        self._añadir("a", dias(-30), expediente="E/a")
        self._añadir("b", dias(-2), expediente="E/b")
        inventado = [i["expediente"] for i in
                     consultas.bandeja(self.con, solo_vivas=False, orden="no-existe")["items"]]
        por_defecto = [i["expediente"] for i in
                       consultas.bandeja(self.con, solo_vivas=False)["items"]]
        self.assertEqual(inventado, por_defecto)

    def test_ordenar_por_reciente_usa_la_primera_publicacion(self):
        """Si mirara el anuncio mostrado, el expediente viejo con adjudicación de ayer
        se colocaría por delante del que salió esta semana."""
        self._añadir("viejo", dias(-90), expediente="E/viejo")
        self._añadir("su-adjudicacion", dias(-1), expediente="E/viejo", estado="zuschlag")
        self._añadir("nuevo", dias(-3), expediente="E/nuevo")
        orden = [i["expediente"] for i in
                 consultas.bandeja(self.con, solo_vivas=False, orden="reciente")["items"]]
        self.assertEqual(orden, ["E/nuevo", "E/viejo"])


class TestCoherenciaContadores(unittest.TestCase):
    """Cada cifra de la cabecera tiene que ser la misma que sale al pulsarla.

    Es la regresión que motivó este trabajo: la cabecera decía 945 «sin revisar» y
    al aplicar ese filtro la lista mostraba 57, porque la lista agrupa los anuncios
    de un mismo expediente y los contadores contaban anuncios sueltos.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        hoy = date.today()
        # Dos anuncios del MISMO expediente: la lista los agrupa en una fila.
        casos = [
            ("exp1-anuncio", (hoy + timedelta(days=10)).isoformat() + "T13:00:00",
             "veroeffentlicht", "EXP/1"),
            ("exp1-adjudicacion", None, "zuschlag", "EXP/1"),
            ("exp2", (hoy + timedelta(days=3)).isoformat() + "T13:00:00", "veroeffentlicht", "EXP/2"),
            ("exp3-vencido", (hoy - timedelta(days=90)).isoformat() + "T13:00:00",
             "veroeffentlicht", "EXP/3"),
        ]
        for nombre, limite, estado, exp in casos:
            db.guardar(self.con, Licitacion(
                fuente="placsp:licitaciones", id_externo=nombre, expediente=exp,
                objeto=f"Concienciación en ciberseguridad {nombre}",
                organo="Mismo Órgano", estado=estado, valor_estimado=50000,
                fecha_limite_presentacion=limite,
            ))
        for f in self.con.execute("SELECT id FROM licitaciones"):
            self.con.execute(
                """INSERT INTO matches (licitacion_id, perfil, puntuacion, motivo, creado_en)
                   VALUES (?, 'p', 3.0, 'm', ?)""", (f["id"], db.ahora()))
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    # El filtro que aplica cada contador de la cabecera. Debe coincidir con KPIS
    # en web/app.js.
    FILTROS = {
        "coincidencias": dict(solo_vivas=False),
        "en_plazo": dict(solo_vivas=True),
        "cierran_7_dias": dict(solo_vivas=True, cierran_en_dias=7),
        "sin_revisar": dict(solo_vivas=False, estado_revision="nuevo"),
        "siguiendo": dict(solo_vivas=False, estado_revision="siguiendo"),
        "presentadas": dict(solo_vivas=False, estado_revision="presentada"),
    }

    def test_cada_contador_coincide_con_su_lista(self):
        r = consultas.resumen(self.con)
        for clave, filtros in self.FILTROS.items():
            with self.subTest(contador=clave):
                self.assertEqual(
                    r[clave], consultas.bandeja(self.con, limite=500, **filtros)["total"],
                    f"la cabecera y la lista no cuentan lo mismo en «{clave}»",
                )

    def test_los_contadores_cuentan_expedientes_no_anuncios(self):
        """EXP/1 tiene dos anuncios y debe contar como uno."""
        r = consultas.resumen(self.con)
        anuncios = self.con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        self.assertEqual(anuncios, 4)
        self.assertEqual(r["coincidencias"], 3, "EXP/1 debe contar una sola vez")

    def test_sigue_coincidiendo_tras_triar(self):
        lic_id = self.con.execute(
            "SELECT id FROM licitaciones WHERE id_externo = 'exp2'"
        ).fetchone()[0]
        db.fijar_revision(self.con, lic_id, estado="siguiendo")
        self.con.commit()
        r = consultas.resumen(self.con)
        self.assertEqual(r["siguiendo"], 1)
        for clave, filtros in self.FILTROS.items():
            with self.subTest(contador=clave):
                self.assertEqual(
                    r[clave], consultas.bandeja(self.con, limite=500, **filtros)["total"]
                )

    def test_el_desglose_por_perfil_tambien_cuadra(self):
        """Contaba anuncios mientras el total contaba expedientes: el desglose sumaba
        921 y arriba ponía 640."""
        r = consultas.resumen(self.con)
        for p in r["por_perfil"]:
            with self.subTest(perfil=p["perfil"]):
                self.assertEqual(
                    p["total"],
                    consultas.bandeja(self.con, solo_vivas=False, perfil=p["perfil"],
                                      limite=500)["total"],
                )

    def test_la_lista_dice_de_cuantas_esta_filtrando(self):
        d = consultas.bandeja(self.con, solo_vivas=True)
        self.assertEqual(d["total_sin_filtros"], 3)
        self.assertLess(d["total"], d["total_sin_filtros"])


class TestResumen(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = db.conectar(Path(self.dir.name) / "t.db")

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def test_resumen_con_base_vacia_no_revienta(self):
        r = consultas.resumen(self.con)
        self.assertEqual(r["coincidencias"], 0)
        self.assertEqual(r["en_plazo"], 0)
        self.assertEqual(r["sin_revisar"], 0)
        self.assertEqual(r["fuentes"], [])

    def test_no_publica_el_recuento_de_lo_que_no_cumple_criterios(self):
        """Un número que no se puede consultar en ninguna vista solo despista."""
        r = consultas.resumen(self.con)
        for clave in ("licitaciones", "descargadas", "total_base"):
            self.assertNotIn(clave, r)

    def test_avisa_cuando_una_fuente_falla(self):
        """Una fuente rota y una fuente sin novedades se ven igual si no se avisa."""
        log_id = db.abrir_ingest(self.con, "fuente_rota")
        db.cerrar_ingest(self.con, log_id, vistos=0, error="ErrorRed: timeout")
        log_id2 = db.abrir_ingest(self.con, "fuente_vacia")
        db.cerrar_ingest(self.con, log_id2, vistos=0)
        self.con.commit()

        avisos = {f["fuente"]: f["aviso"] for f in consultas.resumen(self.con)["fuentes"]}
        self.assertEqual(avisos["fuente_rota"], "der letzte Abruf ist fehlgeschlagen")
        self.assertEqual(
            avisos["fuente_vacia"], "der letzte Abruf hat nichts gebracht"
        )

    def test_una_fuente_descargando_ahora_no_es_una_fuente_rota(self):
        """Mientras la ingesta corre, su fila del registro está abierta: `terminado_en`
        a NULL y `ok` a 0, exactamente igual que un fallo. Sin distinguirlo, la carga
        inicial en segundo plano pintaba «la última ingesta falló» en rojo durante las
        dos horas que dura, que es justo el mensaje contrario al que toca."""
        db.abrir_ingest(self.con, "descargando")
        self.con.commit()

        (f,) = consultas.resumen(self.con, en_marcha=True)["fuentes"]
        self.assertTrue(f["en_curso"])
        self.assertIsNone(f["aviso"])

    def test_una_ingesta_cortada_a_lo_bruto_si_es_un_fallo(self):
        """La fila abierta se queda abierta para siempre si se mata el proceso. Con
        `en_marcha` en falso —nadie está descargando— eso sí hay que contarlo."""
        db.abrir_ingest(self.con, "interrumpida")
        self.con.commit()

        (f,) = consultas.resumen(self.con, en_marcha=False)["fuentes"]
        self.assertFalse(f["en_curso"])
        self.assertEqual(f["aviso"], "der letzte Abruf ist fehlgeschlagen")


class TestCompetencia(unittest.TestCase):
    """Agrupar variantes de razón social. Sin esto el ranking no sirve: el mismo
    proveedor aparecía tres veces por llevar la coma en distinto sitio."""

    def test_variantes_de_la_misma_empresa_se_agrupan(self):
        """El «&» es lo que distingue el caso alemán: está en la mitad de las razones
        sociales, y sin tratarlo como puntuación «GmbH & Co. KG» no se recorta y la misma
        empresa sale tres veces."""
        variantes = [
            "Bechtle GmbH & Co. KG",
            "Bechtle GmbH & Co KG",
            "BECHTLE GMBH & CO. KG",
            "bechtle gmbh co kg",
        ]
        claves = {consultas.normalizar_empresa(v) for v in variantes}
        self.assertEqual(len(claves), 1, f"no se agruparon: {claves}")

    def test_empresas_distintas_no_se_confunden(self):
        a = consultas.normalizar_empresa("Bechtle GmbH")
        b = consultas.normalizar_empresa("Lavette GmbH")
        self.assertNotEqual(a, b)

    def test_tolera_vacios(self):
        self.assertIsNone(consultas.normalizar_empresa(None))
        self.assertIsNone(consultas.normalizar_empresa(""))
        self.assertIsNone(consultas.normalizar_empresa("  GmbH  "))

    def test_el_ranking_cuadra_con_su_desglose(self):
        """Regresión: una licitación que casaba con dos perfiles se contaba dos
        veces en el ranking, así que al abrir la empresa salían menos contratos de
        los anunciados."""
        dir_tmp = tempfile.TemporaryDirectory()
        con = db.conectar(Path(dir_tmp.name) / "t.db")
        db.guardar(con, Licitacion(
            fuente="prueba", id_externo="doble", objeto="Concienciación y correo",
            organo="Órgano", estado="zuschlag",
            adjudicatario="Doppel GmbH & Co. KG", importe_adjudicacion=10000,
        ))
        lic_id = con.execute("SELECT id FROM licitaciones").fetchone()[0]
        for perfil in ("Security Awareness und Phishing", "E-Mail-Sicherheit"):
            con.execute(
                """INSERT INTO matches (licitacion_id, perfil, puntuacion, motivo, creado_en)
                   VALUES (?, ?, 3.0, 'm', ?)""", (lic_id, perfil, db.ahora()))
        con.commit()

        ranking = consultas.competencia(con)
        self.assertEqual(ranking[0]["contratos"], 1, "no debe contarse una vez por perfil")
        self.assertEqual(ranking[0]["importe"], 10000)
        desglose = consultas.contratos_de(con, ranking[0]["empresa"])
        self.assertEqual(len(desglose), ranking[0]["contratos"])
        con.close()
        dir_tmp.cleanup()

    def test_desglose_encuentra_las_variantes_con_acentos(self):
        """El filtro fino va en Python; un LIKE contra la columna sin normalizar perdía
        «GEBÄUDESERVICE» al buscar «gebaudeservice»."""
        dir_tmp = tempfile.TemporaryDirectory()
        con = db.conectar(Path(dir_tmp.name) / "t.db")
        for i, nombre in enumerate(["WESTFALIA GEBÄUDESERVICE GMBH & CO. KG",
                                    "Westfalia Gebäudeservice GmbH"]):
            db.guardar(con, Licitacion(
                fuente="prueba", id_externo=f"a{i}", objeto="Concienciación",
                organo=f"Órgano {i}", estado="zuschlag",
                adjudicatario=nombre, importe_adjudicacion=1000,
            ))
        for f in con.execute("SELECT id FROM licitaciones"):
            con.execute(
                """INSERT INTO matches (licitacion_id, perfil, puntuacion, motivo, creado_en)
                   VALUES (?, 'p', 1.0, 'm', ?)""", (f["id"], db.ahora()))
        con.commit()
        ranking = consultas.competencia(con)
        self.assertEqual(len(ranking), 1, "las dos variantes son la misma empresa")
        self.assertEqual(len(consultas.contratos_de(con, ranking[0]["empresa"])), 2)
        con.close()
        dir_tmp.cleanup()

    def test_ranking_suma_contratos_e_importes(self):
        dir_tmp = tempfile.TemporaryDirectory()
        con = db.conectar(Path(dir_tmp.name) / "t.db")
        for i, (nombre, importe) in enumerate([
            ("Bechtle GmbH & Co. KG", 100000),
            ("Bechtle GmbH", 50000),
            ("Andere Firma GmbH", 10000),
        ]):
            db.guardar(con, Licitacion(
                fuente="prueba", id_externo=f"a{i}", objeto="Concienciación",
                organo=f"Órgano {i}", estado="zuschlag",
                adjudicatario=nombre, importe_adjudicacion=importe,
            ))
        for fila in con.execute("SELECT id FROM licitaciones"):
            con.execute(
                """INSERT INTO matches (licitacion_id, perfil, puntuacion, motivo, creado_en)
                   VALUES (?, 'p', 1.0, 'm', ?)""", (fila["id"], db.ahora()))
        con.commit()

        ranking = consultas.competencia(con)
        self.assertEqual(ranking[0]["contratos"], 2)
        self.assertEqual(ranking[0]["importe"], 150000)
        self.assertEqual(ranking[0]["organos"], 2)
        con.close()
        dir_tmp.cleanup()

class TestDescarteDeExpedienteAgrupado(unittest.TestCase):
    """Regresión: descartar una tarjeta con varios anuncios no la quitaba de la bandeja.

    El filtro «lo descartado no estorba» se aplica ANTES de agrupar, así que al quitar
    el anuncio descartado su hueco lo ocupaba el siguiente anuncio del mismo
    expediente y la tarjeta seguía en la lista, ahora como «sin revisar». Sobre una
    base real le pasaba al 21% de los expedientes con coincidencia (423 de 2.057).
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        # Dos anuncios del mismo expediente: la licitación y su corrección.
        for id_externo, publicacion in (("A1", "2026-07-01"), ("A2", "2026-07-20")):
            db.guardar(self.con, Licitacion(
                fuente="placsp:licitaciones", id_externo=id_externo,
                expediente="EXP-1", organo="Ayuntamiento de Prueba",
                objeto="Concienciación en ciberseguridad", estado="veroeffentlicht",
                valor_estimado=120000, fecha_publicacion=publicacion,
                fecha_limite_presentacion=dias(20),
            ))
        for fila in self.con.execute("SELECT id FROM licitaciones"):
            self.con.execute(
                """INSERT INTO matches (licitacion_id, perfil, puntuacion, motivo, creado_en)
                   VALUES (?, 'Perfil de prueba', 3.0, 'motivo de prueba', ?)""",
                (fila["id"], db.ahora()),
            )
        self.con.commit()
        # El que ve quien usa la herramienta: el anuncio más reciente del grupo.
        self.visible = consultas.bandeja(self.con)["items"][0]["id"]

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def test_la_bandeja_colapsa_los_dos_anuncios_en_una_tarjeta(self):
        datos = consultas.bandeja(self.con)
        self.assertEqual(datos["total"], 1)
        self.assertEqual(datos["items"][0]["anuncios"], 2)

    def test_descartar_la_tarjeta_saca_el_expediente_entero(self):
        db.fijar_revision(self.con, self.visible, estado="descartado",
                          motivo_descarte="fuera de nicho")
        self.con.commit()
        self.assertEqual(consultas.bandeja(self.con)["items"], [],
                         "el otro anuncio del expediente no debe ocupar su hueco")
        self.assertEqual(consultas.contar(self.con), 0)

    def test_sigue_estando_bajo_el_filtro_de_descartadas(self):
        db.fijar_revision(self.con, self.visible, estado="descartado")
        self.con.commit()
        datos = consultas.bandeja(self.con, estado_revision="descartado")
        self.assertEqual(datos["total"], 1, "un expediente descartado, no dos anuncios")

    def test_el_motivo_de_descarte_cuenta_expedientes_no_anuncios(self):
        db.fijar_revision(self.con, self.visible, estado="descartado",
                          motivo_descarte="importe bajo")
        self.con.commit()
        self.assertEqual(consultas.motivos_descarte(self.con),
                         [{"motivo": "importe bajo", "total": 1}])


if __name__ == "__main__":
    unittest.main()


class TestCajaDeBusquedaLibre(unittest.TestCase):
    """El guion es un OPERADOR en FTS5, no una letra.

    En castellano casi no se nota: «concienciación», «phishing», «ciberseguridad». En
    alemán es el primer término que teclea cualquiera —`IT-Sicherheit`,
    `E-Mail-Sicherheit`, `Awareness-Plattform`— y sin entrecomillar `MATCH` no devuelve
    cero resultados: lanza `OperationalError: no such column: Mail`, que el servidor
    traduce a un 400 y la bandeja pinta como «búsqueda no válida».
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        self.addCleanup(self.con.close)

    def _guardar(self, objeto):
        db.guardar(self.con, Licitacion(
            fuente="prueba", id_externo=objeto[:24], objeto=objeto,
            organo="Vergabestelle", estado="veroeffentlicht",
        ))
        for fila in self.con.execute(
            "SELECT id FROM licitaciones WHERE id NOT IN (SELECT licitacion_id FROM matches)"
        ).fetchall():
            self.con.execute(
                "INSERT INTO matches (licitacion_id, perfil, puntuacion, motivo, creado_en)"
                " VALUES (?, 'p', 3.0, 'm', ?)", (fila["id"], db.ahora()))
        self.con.commit()

    def _buscar(self, texto):
        return consultas.bandeja(self.con, busqueda=texto, solo_vivas=False)["items"]

    def test_una_palabra_con_guion_no_revienta(self):
        self._guardar("Beschaffung einer Awareness-Plattform")
        self.assertEqual(len(self._buscar("Awareness-Plattform")), 1)

    def test_dos_guiones_tampoco(self):
        self._guardar("Konzept zur E-Mail-Sicherheit")
        self.assertEqual(len(self._buscar("E-Mail-Sicherheit")), 1)

    def test_varias_palabras_siguen_siendo_un_Y(self):
        self._guardar("Schulung zur IT-Sicherheit")
        self._guardar("Schulung zum Brandschutz")
        self.assertEqual(len(self._buscar("Schulung IT-Sicherheit")), 1)

    def test_la_esszett_encuentra_las_dos_grafias(self):
        """Las dos circulan, y un título en mayúsculas escribe siempre SS."""
        self._guardar("Sensibilisierungsmaßnahmen für Beschäftigte")
        self.assertEqual(len(self._buscar("Sensibilisierungsmassnahmen")), 1)
        self.assertEqual(len(self._buscar("Sensibilisierungsmaßnahmen")), 1)

    def test_las_dieresis_siguen_siendo_indiferentes(self):
        self._guardar("Prüfung der Netzwerksicherheit")
        self.assertEqual(len(self._buscar("Prufung")), 1)

    def test_se_sigue_encontrando_por_vergabestelle(self):
        self._guardar("Lieferung von Druckern")
        self.assertEqual(len(self._buscar("Vergabestelle")), 1)


class TestLaBandejaNoRecorreLaTablaEntera(unittest.TestCase):
    """El plan de la bandeja, atado. Esto se rompe en silencio: sigue dando el resultado
    correcto, solo que tarda ocho segundos con la pantalla en blanco.

    El caso que lo motiva, medido sobre 575.230 anuncios reales: la entrada era un
    `LEFT JOIN matches` con un `WHERE (m.licitacion_id IS NOT NULL OR protegida)`, y ese
    OR impide a SQLite entrar por `matches`. Elegía `idx_lic_estado`... y «vivas» abarca
    371.195 de las 575.230 filas, porque un anuncio publicado en 2024 se queda en
    «veröffentlicht» para siempre: su adjudicación llega como OTRA fila, no como un
    cambio de estado. Contar costaba 12,2 s y `resumen()` llama a contar varias veces.

    Con la entrada por el conjunto visible —unos mil identificadores— son 0,02 s.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.con = db.conectar(Path(self.dir.name) / "t.db")
        self.addCleanup(self.con.close)

    def _plan(self, sql, params):
        return [f[-1] for f in self.con.execute("EXPLAIN QUERY PLAN " + sql, params)]

    def test_se_entra_por_las_coincidencias_y_no_por_las_licitaciones(self):
        sql = (f"SELECT COUNT(*) FROM (SELECT {consultas._GRUPO} AS g "
               f"{consultas._DESDE_BANDEJA} WHERE {consultas._VISIBLE} "
               "AND 1=1 GROUP BY g)")
        plan = self._plan(sql, list(consultas.ESTADOS_PROTEGIDOS))
        texto = " · ".join(plan)
        self.assertIn("SEARCH l USING INTEGER PRIMARY KEY", texto,
                      f"se ha dejado de entrar por el conjunto visible:\n  " + "\n  ".join(plan))
        self.assertNotIn("SEARCH l USING INDEX idx_lic_estado", texto,
                         "ha vuelto a entrar por el estado, que abarca media tabla")

    def test_el_cross_join_sigue_estando(self):
        """En SQLite `CROSS JOIN` no es otro tipo de join: es la forma de decir «no
        reordenes». Sin él el planificador vuelve a preferir el índice de estado."""
        self.assertIn("CROSS JOIN licitaciones", consultas._DESDE_BANDEJA)
