"""Conector de TED. El fixture es una respuesta real de la Search API v3."""

import json
import unittest
from datetime import date
from pathlib import Path

from radar.sources.ted import CAMPOS, construir_consulta, parsear_aviso

FIXTURES = Path(__file__).parent / "fixtures"


class TestConsulta(unittest.TestCase):
    def test_fechas_sin_guiones(self):
        """TED rechaza `publication-date>=2024-01-01`; solo acepta `20240101`."""
        c = construir_consulta(cpv=["80533100"], terminos=[], desde=date(2024, 1, 1))
        self.assertIn("publication-date>=20240101", c)
        self.assertNotIn("2024-01-01", c)

    def test_cpv_y_texto_van_en_or(self):
        """Filtrar solo por CPV pierde la mayoría de este nicho: los pliegos de
        concienciación se clasifican con CPV dispares o sin ninguno."""
        c = construir_consulta(cpv=["80533100"], terminos=["phishing"], desde=None)
        self.assertIn("classification-cpv IN (80533100)", c)
        self.assertIn('FT~"phishing"', c)
        self.assertIn(" OR ", c)

    def test_acota_el_pais(self):
        self.assertIn("(buyer-country=DEU)", construir_consulta(cpv=[], terminos=[], desde=None))

    def test_sin_criterios_no_pide_el_mundo_entero(self):
        c = construir_consulta(cpv=[], terminos=[], desde=None)
        self.assertEqual(c, "(buyer-country=DEU)")

    def test_los_campos_pedidos_son_los_validados(self):
        """`scope` reduce la lista de campos admitidos, así que no se envía.
        Estos nombres se comprobaron uno a uno contra la API."""
        for campo in ("publication-number", "notice-title", "deadline-receipt-request",
                      "classification-cpv", "total-value", "procedure-type"):
            self.assertIn(campo, CAMPOS)


class TestParseo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        datos = json.loads((FIXTURES / "ted_muestra.json").read_text(encoding="utf-8"))
        cls.avisos = {a["publication-number"]: parsear_aviso(a) for a in datos["notices"]}

    def test_lantik_es_el_caso_de_referencia(self):
        """Servicio de oficina de concienciación en ciberseguridad, 915.000 €."""
        l = self.avisos["580823-2024"]
        self.assertIn("concienciación en ciberseguridad", l.objeto)
        self.assertIn("LANTIK", l.organo)
        self.assertEqual(l.importe_referencia, 915000)
        self.assertIn("80533100", l.cpv)

    def test_toma_el_texto_en_castellano(self):
        """notice-title llega como diccionario por idioma con listas dentro."""
        l = self.avisos["580823-2024"]
        self.assertNotIn("{", l.objeto)
        self.assertNotIn("'spa'", l.objeto)

    def test_traduce_los_codigos_al_castellano(self):
        for l in self.avisos.values():
            with self.subTest(id=l.id_externo):
                self.assertNotEqual(l.procedimiento, "open")
                self.assertNotEqual(l.tipo_contrato, "services")

    def test_no_repite_valores_duplicados(self):
        """contract-nature llega como ["services","services"] y se mostraba
        "services · services"."""
        for l in self.avisos.values():
            if l.tipo_contrato:
                partes = [p.strip() for p in l.tipo_contrato.split("·")]
                self.assertEqual(len(partes), len(set(partes)))

    def test_expediente_es_el_numero_de_publicacion(self):
        """`notice-identifier` es un UUID interno que no sirve para buscar nada."""
        for numero, l in self.avisos.items():
            self.assertEqual(l.expediente, numero)

    def test_enlace_a_la_ficha(self):
        for l in self.avisos.values():
            self.assertTrue(l.url_detalle.startswith("https://"))
            self.assertIn("ted.europa.eu", l.url_detalle)

    def test_fecha_limite_normalizada(self):
        l = self.avisos["355940-2016"]
        self.assertTrue(l.fecha_limite_presentacion.startswith("2016-11-16"))


if __name__ == "__main__":
    unittest.main()
