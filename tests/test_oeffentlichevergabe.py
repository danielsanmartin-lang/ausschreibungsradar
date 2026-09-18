"""Conector del Bekanntmachungsservice: parseo, troceado y caché.

Las fixtures son descargas REALES del servicio (datos CC0), una por cada caso que el
conector tiene que saber leer. Lo que se protege aquí no es que el parseo funcione —eso
se ve enseguida— sino tres cosas que fallan en silencio:

- que los dos sabores de XML eForms den el plazo (si un cambio de namespaces rompe uno,
  la mitad de las fichas se quedan sin fecha de cierre y nadie ve un error);
- que los ficheros de un ZIP se recorran ORDENADOS, para que la versión 02 no la pise la
  01;
- que un tramo del mes en curso no se guarde en caché como si estuviera cerrado.
"""

import io
import json
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock

from radar import net, progreso
from radar.sources import oeffentlichevergabe as ov
from radar.sources.oeffentlichevergabe import (
    FuenteOeffentlicheVergabe, parsear_release, plazos_y_expedientes,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def casos():
    datos = json.loads((FIXTURES / "ov_ocds_muestra.json").read_text())
    return {c["caso"]: c["payload"] for c in datos["casos"]}


class TestParseo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.casos = casos()
        cls.lics = {k: parsear_release(v) for k, v in cls.casos.items()}

    def test_ninguna_muestra_real_se_cae(self):
        for nombre, lic in self.lics.items():
            with self.subTest(caso=nombre):
                self.assertIsNotNone(lic)

    def test_los_campos_que_decide_un_comercial_en_diez_segundos(self):
        lic = self.lics["tender completo (CPV + documentos + lotes + importe)"]
        self.assertTrue(lic.objeto)
        self.assertTrue(lic.organo)
        self.assertTrue(lic.cpv)
        self.assertTrue(lic.urls_pliegos)
        self.assertTrue(lic.url_detalle.startswith("https://oeffentlichevergabe.de/ui/de/"))
        self.assertEqual(lic.fuente, "oeffentlichevergabe")

    def test_ningun_procedimiento_se_queda_en_ingles(self):
        """`procurementMethodDetails` llega en inglés aunque el resto venga en alemán.
        Un valor sin traducir a la vista es un aviso de que falta un mapeo; peor sería
        traducirlo mal, así que lo que no está en la tabla se deja pasar y salta aquí."""
        ingleses = {"Open", "Restricted", "Negotiated", "Public", "Direct", "Other"}
        for nombre, lic in self.lics.items():
            if not lic.procedimiento:
                continue
            with self.subTest(caso=nombre):
                self.assertNotIn(lic.procedimiento.split()[0], ingleses, lic.procedimiento)

    def test_el_ocid_es_lo_que_agrupa(self):
        lic = self.lics["tender completo (CPV + documentos + lotes + importe)"]
        self.assertTrue(lic.id_procedimiento)
        self.assertIn(
            lic.id_procedimiento.replace("-", "")[-16:].lower(),
            lic.clave_grupo.replace("-", "").lower(),
        )

    def test_una_aufhebung_no_se_da_por_viva(self):
        """`awards[].status == unsuccessful` es una Aufhebung: el procedimiento se anula
        sin adjudicar. Darla por viva manda a alguien a preparar una oferta para algo que
        ya no existe, y son 393 al mes."""
        lic = self.lics["Aufhebung: award unsuccessful"]
        self.assertEqual(lic.estado, "aufgehoben")

    def test_una_vorinformation_es_estado_previo(self):
        self.assertEqual(self.lics["planning / Vorinformation"].estado, "vorinformation")

    def test_un_anuncio_sin_tag_pero_con_contrato_esta_formalizado(self):
        self.assertEqual(self.lics["sin tag (relevado de service.bund.de)"].estado,
                         "vergeben")

    def test_el_adjudicatario_es_uno_solo_aunque_haya_veinte(self):
        """Un acuerdo marco se adjudica a muchas empresas. `consultas.competencia()`
        agrupa por la cadena exacta, así que un «A · B» sería un proveedor fantasma
        distinto de «A» y el ranking se llenaría de basura."""
        lic = self.lics["adjudicación con VARIOS proveedores (acuerdo marco)"]
        self.assertTrue(lic.adjudicatario)
        self.assertNotIn(" · ", lic.adjudicatario)
        self.assertTrue(lic.raw.get("zuschlagsempfaenger"), "el resto se guarda en raw")

    def test_un_importe_en_otra_moneda_no_se_guarda_como_si_fueran_euros(self):
        """Solo uno de cada 1.788 viene en otra moneda, pero un número sin su moneda en
        una columna que después se suma y se ordena es la forma más barata de que la
        analítica mienta."""
        self.assertIsNone(self.lics["moneda que no es EUR"].valor_estimado)

    def test_el_bundesland_sale_del_lugar_de_ejecucion(self):
        lic = self.lics["tender completo (CPV + documentos + lotes + importe)"]
        self.assertEqual(lic.raw["nuts_origen"], "leistungsort")
        self.assertTrue(lic.bundesland)

    def test_si_no_hay_lugar_de_ejecucion_vale_el_de_la_vergabestelle(self):
        """El cruce del comprador NO puede hacerse solo por `buyer.id`: hay anuncios
        donde eso es el número de registro mercantil («HRB 20680») mientras que en
        `parties` el id es el local del anuncio («ORG-0001»). Cruzando solo por id, el
        comprador no se encontraba y se perdía su NUTS."""
        lic = self.lics["region solo del comprador, no del item"]
        self.assertEqual(lic.raw["nuts_origen"], "vergabestelle")
        self.assertEqual(lic.bundesland, "Nordrhein-Westfalen")

    def test_raw_se_queda_pequeno(self):
        """Volcar el release entero son 5,5 kB por anuncio: sobre dos años de histórico,
        más de 4 GB de base por una decisión de una línea."""
        for nombre, lic in self.lics.items():
            with self.subTest(caso=nombre):
                self.assertLess(len(json.dumps(lic.raw, ensure_ascii=False)), 1000)

    def test_un_payload_vacio_no_revienta(self):
        self.assertIsNone(parsear_release({}))
        self.assertIsNone(parsear_release({"releases": []}))
        self.assertIsNone(parsear_release({"releases": [{"tender": {}}]}))


class TestEForms(unittest.TestCase):
    """El ZIP mezcla dos sabores de XML y hay que leer los dos.

    Los eForms-DE nativos usan prefijos `cbc:`/`cac:`; los relevados desde
    service.bund.de llegan con prefijos numerados (`ns8:`, `ns7:`) y con menos
    contenido. Un parser atado a los prefijos dejaría la mitad de los plazos en NULL sin
    que fallara nada: la bandeja se llenaría de fichas «sin plazo» y parecería que
    Alemania no publica las fechas de cierre.
    """

    def _leer(self, nombre):
        return ov._leer_eforms((FIXTURES / nombre).read_bytes())

    def test_el_plazo_sale_del_eforms_nativo(self):
        self.assertEqual(self._leer("ov_eforms_estandar.xml")["plazo"],
                         "2026-10-20T12:00:00")

    def test_y_tambien_del_relevado_de_service_bund_de(self):
        self.assertEqual(self._leer("ov_eforms_relevado.xml")["plazo"],
                         "2026-10-06T10:00:00")

    def test_la_fecha_y_la_hora_llegan_separadas_y_sin_desplazamiento(self):
        """`EndDate` es `2026-10-20+02:00` y `EndTime` es `12:00:00+02:00`. Se combinan y
        se les quita el desplazamiento: `model._fecha_iso` no tiene patrón para
        `%Y-%m-%d%z`, y mezclar en la misma columna valores con y sin desplazamiento
        rompe las comparaciones de fechas de las consultas."""
        plazo = self._leer("ov_eforms_estandar.xml")["plazo"]
        self.assertNotIn("+", plazo)
        self.assertRegex(plazo, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

    def test_el_geschaeftszeichen_sale_del_nativo(self):
        """No está en el OCDS: el `tender.id` de ahí es un uuid sintético."""
        self.assertEqual(self._leer("ov_eforms_estandar.xml")["expediente"], "MPE 09/2026")

    def test_el_relevado_no_trae_geschaeftszeichen_y_no_se_inventa(self):
        self.assertIsNone(self._leer("ov_eforms_relevado.xml")["expediente"])

    def test_se_cruza_por_nombre_de_fichero(self):
        """Los nombres coinciden exactamente entre los dos formatos —comprobado sobre un
        día entero: 857 y 857, cero huérfanos— así que no hay que interpretar ids."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("abc-01.xml", (FIXTURES / "ov_eforms_estandar.xml").read_bytes())
            zf.writestr("123-1.xml", (FIXTURES / "ov_eforms_relevado.xml").read_bytes())
        datos = plazos_y_expedientes(buffer.getvalue())
        self.assertEqual(set(datos), {"abc-01", "123-1"})
        self.assertTrue(all(d["plazo"] for d in datos.values()))


def zip_ocds(*pares) -> bytes:
    """Un ZIP de mentira con (nombre, objeto) por anuncio."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for nombre, objeto in pares:
            zf.writestr(nombre, json.dumps({"releases": [{
                "id": nombre.rsplit("-", 1)[0], "ocid": "ocds-x-" + nombre.rsplit("-", 1)[0],
                "date": "2026-08-01T00:00:00+02:00", "tag": ["tender"],
                "buyer": {"name": "Vergabestelle"},
                "tender": {"title": objeto},
            }]}))
    return buffer.getvalue()


class BaseTramos(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cache = Path(self.dir.name)
        self.pedidos = []

    def _fuente(self, **kwargs):
        kwargs.setdefault("hoy", date(2026, 9, 17))
        return FuenteOeffentlicheVergabe(dir_cache=self.cache, **kwargs)

    def _bajar_a_fichero(self, url, destino, **kwargs):
        self.pedidos.append(url)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(zip_ocds(("a-01.json", "Awareness-Plattform")))
        return destino

    def _descargar(self, url, **kwargs):
        self.pedidos.append(url)
        return zip_ocds(("b-01.json", "Phishing-Simulation")), {}, 200

    def _consumir(self, generador):
        with mock.patch.object(net, "descargar_a_fichero", self._bajar_a_fichero), \
             mock.patch.object(net, "descargar", self._descargar):
            return list(generador)


class TestCache(BaseTramos):
    def test_un_mes_cerrado_se_baja_una_vez_y_se_reaprovecha(self):
        f = self._fuente()
        self._consumir(f._tramo("pubMonth", "2026-03"))
        self.assertEqual(len(self.pedidos), 1)
        self._consumir(f._tramo("pubMonth", "2026-03"))
        self.assertEqual(len(self.pedidos), 1, "la segunda vez sale de la caché")
        self.assertTrue((self.cache / "ov_ocds_2026-03.zip").exists())

    def test_el_ultimo_dia_que_se_pide_es_ayer(self):
        """No es una precaución nuestra: el servicio devuelve 400 para el día de hoy
        —«The specified pubDay exceeds the allowed range. It must lie in the past»—, así
        que todo lo descargable está cerrado por definición y se puede cachear."""
        f = self._fuente()
        self.assertEqual(f.ultimo_dia, date(2026, 9, 16))
        self._consumir(f.incremental("2026-09-14"))
        self.assertNotIn("2026-09-17", " ".join(self.pedidos))

    def test_el_mes_en_curso_se_pide_dia_a_dia(self):
        """Un mes a medias no se puede cachear entero, pero sus días terminados sí."""
        f = self._fuente(meses_atras=None)
        self._consumir(f.historico(2026))
        de_septiembre = [u for u in self.pedidos if "pubDay=2026-09" in u]
        self.assertEqual(len(de_septiembre), 16, "del 1 al 16; el 17 es hoy y aún no existe")
        self.assertNotIn("pubMonth=2026-09", " ".join(self.pedidos),
                         "el mes a medias no se cachea entero")
        self.assertEqual(len(list(self.cache.glob("ov_ocds_2026-09-*.zip"))), 16)


class TestCursor(BaseTramos):
    def test_el_cursor_se_queda_en_el_ultimo_dia_traido(self):
        """Todos los días que se pueden pedir están cerrados, así que el cursor puede
        avanzar sin miedo: no hay ningún día a medias que haya que volver a pedir."""
        f = self._fuente()
        self._consumir(f.incremental("2026-09-13"))
        self.assertEqual(f.cursor_nuevo(), "2026-09-16")

    def test_pide_desde_el_dia_siguiente_al_cursor(self):
        f = self._fuente()
        self._consumir(f.incremental("2026-09-14"))
        dias = [u.split("=")[-1] for u in self.pedidos]
        self.assertEqual(dias, ["2026-09-15", "2026-09-16"])

    def test_sin_cursor_pide_una_ventana_corta(self):
        """El histórico lo trae `historico()`; la primera ingesta no tiene por qué bajar
        un mes entero para nada."""
        f = self._fuente(dias_primera_vez=3)
        self._consumir(f.incremental(None))
        self.assertEqual([u.split("=")[-1] for u in self.pedidos],
                         ["2026-09-14", "2026-09-15", "2026-09-16"])

    def test_un_cursor_corrupto_no_tumba_la_ingesta(self):
        f = self._fuente(dias_primera_vez=2)
        self._consumir(f.incremental("lo-que-sea"))
        self.assertEqual(len(self.pedidos), 2)


class TestHistoricoPorMeses(BaseTramos):
    def test_un_mes_caido_no_se_lleva_los_otros_once(self):
        """Donde un año es UN fichero, el try del pipeline basta. Aquí un año son doce
        descargas: sin try por mes, una caída en marzo se llevaría de abril a diciembre.
        """
        def falla_en_marzo(url, destino, **kwargs):
            self.pedidos.append(url)
            if "2025-03" in url:
                raise net.ErrorRed("se cortó la conexión")
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(zip_ocds(("a-01.json", "x")))
            return destino

        f = self._fuente()
        with mock.patch.object(net, "descargar_a_fichero", falla_en_marzo):
            fichas = list(f.historico(2025))
        self.assertEqual(len(fichas), 11, "los otros once meses entran igual")

    def test_si_no_entra_ningun_mes_la_fuente_se_marca_como_caida(self):
        def siempre_falla(url, destino, **kwargs):
            raise net.ErrorRed("caída")

        f = self._fuente()
        with mock.patch.object(net, "descargar_a_fichero", siempre_falla):
            with self.assertRaises(net.ErrorRed):
                list(f.historico(2025))

    def test_un_mes_sin_nada_publicado_no_es_una_averia(self):
        def no_existe(url, destino, **kwargs):
            self.pedidos.append(url)
            if "2025-03" in url:
                raise net.ErrorRed("no hay nada", codigo=404)
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(zip_ocds(("a-01.json", "x")))
            return destino

        f = self._fuente()
        with mock.patch.object(net, "descargar_a_fichero", no_existe):
            fichas = list(f.historico(2025))
        self.assertEqual(len(fichas), 11)

    def test_un_anio_fuera_de_la_ventana_devuelve_cero_y_NO_lanza(self):
        """Si lanzara, el pipeline marcaría la fuente entera como fallida por pedir un
        año que sencillamente no se quiere."""
        f = self._fuente(meses_atras=24)
        self.assertEqual(self._consumir(f.historico(2020)), [])
        self.assertEqual(self.pedidos, [])

    def test_la_ventana_recorta_dentro_del_anio(self):
        """`meses_atras=24` desde septiembre de 2026 empieza en octubre de 2024."""
        f = self._fuente(meses_atras=24)
        self._consumir(f.historico(2024))
        meses = sorted(u.split("=")[-1] for u in self.pedidos)
        self.assertEqual(meses, ["2024-10", "2024-11", "2024-12"])

    def test_no_se_piden_meses_futuros(self):
        f = self._fuente(meses_atras=None)
        self._consumir(f.historico(2026))
        self.assertNotIn("2026-10", " ".join(self.pedidos))


class TestOrdenDeLosFicheros(BaseTramos):
    def test_la_version_02_no_la_pisa_la_01(self):
        """Medido: 126 uuids aparecen con varias versiones dentro de un mismo ZIP
        mensual. Con el orden de archivo, la 01 puede procesarse DESPUÉS de la 02: la
        última escritura gana y en la base queda la versión vieja, con su estado y su
        plazo obsoletos, y con la huella «correcta» según esa versión vieja, así que no
        se vuelve a corregir nunca."""
        def desordenado(url, destino, **kwargs):
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(zip_ocds(("x-02.json", "corregido"),
                                         ("x-01.json", "original")))
            return destino

        f = self._fuente()
        with mock.patch.object(net, "descargar_a_fichero", desordenado):
            fichas = list(f._tramo("pubMonth", "2026-03"))
        self.assertEqual([l.objeto for l in fichas], ["original", "corregido"],
                         "la última en procesarse tiene que ser la versión más alta")


class TestProgreso(BaseTramos):
    def test_la_barra_sabe_cuantos_ficheros_van_de_cuantos(self):
        f = self._fuente()
        with mock.patch.object(progreso, "subtarea") as subtarea:
            self._consumir(f._tramo("pubMonth", "2026-03"))
        llamadas = [c.args for c in subtarea.call_args_list]
        self.assertIn((0, 0), llamadas, "se resetea antes de descargar")
        self.assertIn((1, 1), llamadas, "y luego cuenta con total conocido")


if __name__ == "__main__":
    unittest.main()


class TestImportesDelEForms(unittest.TestCase):
    """El OCDS trae importe en el 9 % de los anuncios y el eForms en el 19 %: leerlo aquí
    duplica la cobertura, medido sobre un día entero. El 79 % restante no lo publica en
    ningún sitio, y eso no es un fallo sino el mercado: en Alemania no hay obligación
    general de publicar el valor estimado."""

    def _leer(self, nombre):
        return ov._leer_eforms((FIXTURES / nombre).read_bytes())

    def test_lo_que_declara_el_eforms_se_recoge(self):
        datos = self._leer("ov_eforms_estandar.xml")
        alguno = datos.get("valor_estimado") or datos.get("importe_adjudicacion")
        self.assertTrue(alguno is None or alguno > 0)

    def test_un_valor_estimado_no_se_confunde_con_uno_de_adjudicacion(self):
        """Si se mezclaran, la analítica de «Preisabschlag» compararía una cifra consigo
        misma y daría siempre cero."""
        self.assertEqual(ov.IMPORTES_EFORMS["EstimatedOverallContractAmount"], "valor_estimado")
        self.assertEqual(ov.IMPORTES_EFORMS["PayableAmount"], "importe_adjudicacion")

    def test_el_ocds_manda_sobre_el_eforms(self):
        """El de fuera solo RELLENA: el OCDS es la fuente normalizada."""
        payload = {"releases": [{
            "id": "x", "ocid": "ocds-x", "date": "2026-09-01T00:00:00+02:00",
            "tag": ["tender"], "buyer": {"name": "V"},
            "tender": {"title": "t", "value": {"amount": 1000.0, "currency": "EUR"}},
        }]}
        lic = parsear_release(payload, valor_estimado=999999.0)
        self.assertEqual(lic.valor_estimado, 1000.0)

    def test_pero_lo_rellena_cuando_el_ocds_no_lo_trae(self):
        payload = {"releases": [{
            "id": "x", "ocid": "ocds-x", "date": "2026-09-01T00:00:00+02:00",
            "tag": ["tender"], "buyer": {"name": "V"}, "tender": {"title": "t"},
        }]}
        lic = parsear_release(payload, valor_estimado=250000.0)
        self.assertEqual(lic.valor_estimado, 250000.0)

    def test_una_moneda_que_no_es_euro_se_descarta(self):
        import io as _io
        xml = ('<X xmlns="urn:x"><PayableAmount currencyID="USD">50000</PayableAmount></X>')
        self.assertIsNone(ov._leer_eforms(xml.encode()).get("importe_adjudicacion"))
