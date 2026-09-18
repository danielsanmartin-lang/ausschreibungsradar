"""Casos reales verificados contra la fuente. Son el criterio de aceptación:
si estos dejan de pasar, el filtro se ha roto o se ha vuelto ruidoso.

Los positivos con nombre propio salen del Bekanntmachungsservice; los negativos, de
medir el filtro sobre los 22.892 anuncios de un mes entero y mirar qué colaba.
"""
import json
import unittest

from radar import matching
from radar.matching import cargar_perfiles, evaluar, patron, prefijo_cpv

# (debe_casar, texto, cpv, importe, fuente, etiqueta)
CASOS = [
    # --- Deben entrar ---------------------------------------------------------------
    (True, "Awareness-Plattform",
     ["72200000"], None, "oeffentlichevergabe",
     "IT-Dienstleistungszentrum des Saarlandes: diana real de agosto"),
    (True, "Projekt 1009: KI-Detektion und -abwehr von Phishing-Emails (KIDAP)",
     ["72000000"], None, "oeffentlichevergabe",
     "BSI, anti-phishing con DMARC y DKIM en el pliego"),
    (True, "Einführung von DMARC, DKIM und SPF für die E-Mail-Infrastruktur des Landes",
     ["72500000"], 250000, "oeffentlichevergabe", "protección del correo"),
    (True, "Secure E-Mail Gateway als Schutz vor Phishing und Schadsoftware",
     ["48730000"], 400000, "oeffentlichevergabe", "pasarela de correo"),
    (True, "Sensibilisierungsmaßnahmen im Bereich Cybersicherheit für die Beschäftigten",
     ["79416000"], 90000, "oeffentlichevergabe", "débil + contexto"),
    (True, "Sensibilisierungsmassnahmen im Bereich Cybersicherheit für die Beschäftigten",
     ["79416000"], 90000, "oeffentlichevergabe", "la misma con ss en vez de ß"),
    (True, "SENSIBILISIERUNGSMASSNAHMEN IM BEREICH CYBERSICHERHEIT",
     ["79416000"], 90000, "oeffentlichevergabe", "y la misma en mayúsculas"),
    (True, "Schulungen zur IT-Sicherheit für Mitarbeitende der Stadtverwaltung",
     ["80500000"], 60000, "oeffentlichevergabe", "el guion hace de límite de palabra"),
    (True, "E-Learning-Plattform Informationssicherheit nach BSI IT-Grundschutz",
     ["80533100"], 120000, "oeffentlichevergabe", "e-learning + IT-Grundschutz"),
    (True, "Security Awareness Training für Beschäftigte",
     [], 45000, "oeffentlichevergabe", "sin CPV: el texto se basta"),
    (True, "Rahmenvertrag Schulungen: Awareness-Schulung IT-Sicherheit, Brandschutz "
           "und Erste Hilfe",
     ["80500000"], 300000, "oeffentlichevergabe",
     "Sammelausschreibung: por esto `ausschluss` va casi vacío"),
    (True, "Beratung zu Ausfallsicherheit und Phishing-Simulationen",
     ["72500000"], 80000, "oeffentlichevergabe",
     "excluir «ausfallsicherheit» se habría llevado este acierto"),
    (True, "Virtuelle Poststelle: Signatur- und Verschlüsselungsgateway für E-Mail "
           "nach BSI TR",
     ["72500000"], 500000, "oeffentlichevergabe",
     "«poststelle» NO puede estar en ausschluss: aquí es producto"),

    # --- NO deben entrar: falsos positivos medidos sobre un mes entero ---------------
    (False, "Absicherung des Bahnübergangs an der B27",
     [], 800000, "oeffentlichevergabe",
     "«bsi» dentro de «Absicherung»: 208 falsos positivos sin el ancla de palabra"),
    (False, "Erhöhung der Ausfallsicherheit der Serverlandschaft",
     ["72500000"], 400000, "oeffentlichevergabe", "resiliencia, no ciberseguridad"),
    (False, "Fachkraft für Arbeitssicherheit und Gesundheitsschutz",
     ["71317200"], 200000, "oeffentlichevergabe", "prevención de riesgos laborales"),
    (False, "Prüfung nach Betriebssicherheitsverordnung",
     [], 60000, "oeffentlichevergabe", "seguridad industrial"),
    (False, "Hochwassersicherheit der Deichanlagen",
     ["45240000"], 2000000, "oeffentlichevergabe", "diques"),
    (False, "Gruppenschulung für Pflegekräfte",
     ["80500000"], 50000, "oeffentlichevergabe", "formación sanitaria"),
    (False, "Trinkwasser-Hygieneschulung nach TrinkwV",
     ["80500000"], 30000, "oeffentlichevergabe", "higiene del agua"),
    (False, "Beschulung von Kindern mit Förderbedarf",
     ["80500000"], 400000, "oeffentlichevergabe", "escolarización"),
    (False, "Schulung in Microsoft Excel und Office-Anwendungen",
     ["80533100"], 45000, "oeffentlichevergabe",
     "el CPV de formación informática NO vale como contexto"),
    (False, "Kampagne zur Verkehrssicherheit an Schulen",
     ["79341000"], 150000, "oeffentlichevergabe", "seguridad vial"),
    (False, "Fortbildung zum Brandschutzhelfer",
     ["80500000"], 40000, "oeffentlichevergabe", "protección contra incendios"),
    (False, "Schulungen zur Datenschutz-Grundverordnung für Beschäftigte",
     ["80500000"], 70000, "oeffentlichevergabe",
     "por esto «datenschutz» no está en el contexto: es formación RGPD, no producto"),
    (False, "Bewachungsdienstleistungen für Liegenschaften, Kontakt per E-Mail an die "
           "Poststelle",
     ["79713000"], 5000000, "oeffentlichevergabe",
     "vigilantes: el débil «e-mail» con el contexto «schutz» colaría sin el ausschluss"),
    (False, "Postdienstleistungen: Briefpost, Frankierung und Kurierdienst",
     ["64110000"], 900000, "oeffentlichevergabe", "servicio postal"),
    (False, "Lieferung von Microsoft 365 Lizenzen",
     ["48000000"], 300000, "oeffentlichevergabe", "licencias, sin señal de seguridad"),
    (False, "Sozialpädagogische Familienhilfe SPF im Landkreis",
     ["85311300"], 600000, "oeffentlichevergabe", "SPF que no es el del correo"),
    (False, "Prävention von Cybermobbing an Schulen",
     ["80000000"], 80000, "oeffentlichevergabe",
     "lo único que justifica tener `ausschluss`"),
]


class TestMatching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.perfiles = cargar_perfiles()

    def test_casos_reales(self):
        """Corre SIEMPRE, también en integración continua.

        En el radar español estos casos se saltaban cuando no había un perfil propio, y
        como el fichero afinado no se versiona, en la práctica no se ejecutaban nunca: el
        criterio de aceptación de la herramienta estaba apagado. Aquí el ejemplo que
        viaja en el repositorio es el perfil alemán de verdad, así que cada cambio en
        `patron()`, en `evaluar()` o en los términos ve enseguida si se ha cargado una de
        las dianas."""
        for esperado, texto, cpv, importe, fuente, etiqueta in CASOS:
            with self.subTest(caso=etiqueta):
                aciertos = [
                    (p.name, evaluar(p, texto, cpv, importe, None, fuente))
                    for p in self.perfiles
                ]
                casa = [(n, r) for n, r in aciertos if r.casa]
                if esperado:
                    self.assertTrue(casa, f"debía casar y no casó: {etiqueta}")
                else:
                    self.assertFalse(
                        casa,
                        f"no debía casar: {etiqueta} -> "
                        + "; ".join(f"{n}: {r.motivo}" for n, r in casa),
                    )

    def test_todo_match_explica_su_motivo(self):
        """Sin traza no se puede afinar el ruido."""
        for esperado, texto, cpv, importe, fuente, etiqueta in CASOS:
            if not esperado:
                continue
            for p in self.perfiles:
                r = evaluar(p, texto, cpv, importe, None, fuente)
                if r.casa:
                    with self.subTest(caso=etiqueta, perfil=p.name):
                        self.assertTrue(r.motivo.strip(), "motivo vacío")
                        self.assertGreater(r.puntuacion, 0)

    def test_los_terminos_casan_a_principio_de_palabra_pero_siguen_siendo_raices(self):
        """Las dos mitades del contrato de `patron()`, que se contrapesan.

        Anclar solo el INICIO no es un detalle de implementación: cerrar también el
        final con `\\b` haría fallar la segunda mitad de estos casos, que es el diseño
        de raíces del que depende media configuración.
        """
        # En alemán el ancla de inicio hace un trabajo que en castellano no hacía: la
        # palabra que discrimina va al FINAL del compuesto sólido, y ahí es justo donde
        # están los falsos amigos. Medido: el 13 % de las apariciones de «sicherheit» y
        # el 9 % de las de «schulung» están dentro de un compuesto, y todas son ruido.
        no_casa = [("sicherheit", "erhohung der ausfallsicherheit"),
                   ("sicherheit", "fachkraft fur arbeitssicherheit"),
                   ("sicherheit", "prufung nach betriebssicherheitsverordnung"),
                   ("schulung", "gruppenschulung fur pflegekrafte"),
                   ("schulung", "beschulung von kindern"),
                   ("bsi ", "absicherung des bahnubergangs"),
                   ("spf ", "sozialpadagogische familienhilfe spfim landkreis")]
        for termino, texto in no_casa:
            with self.subTest(termino=termino, texto=texto):
                self.assertIsNone(patron(termino).search(texto),
                                  "casa dentro de otra palabra")

        # Y lo que sí tiene que seguir casando: el guion es un límite de palabra gratis,
        # y en cabeza de compuesto la raíz sigue creciendo hacia la derecha.
        casa = [("sicherheit", "konzept zur it-sicherheit"),
                ("sicherheit", "beratung cyber-sicherheit"),
                ("schulung", "awareness-schulung fur mitarbeitende"),
                ("schulung", "schulungskonzept fur die verwaltung"),
                ("cyber", "cybersicherheit im landesnetz"),
                ("cyber", "abwehr von cyberangriffen"),
                ("sensibilisier", "sensibilisierungsmassnahmen fur beschaftigte"),
                ("informationssicherheit", "informationssicherheitsmanagementsystem"),
                ("phishing", "spear-phishing-simulation"),
                ("bsi ", "nach bsi it-grundschutz")]
        for termino, texto in casa:
            with self.subTest(termino=termino, texto=texto):
                self.assertIsNotNone(patron(termino).search(texto),
                                     "la raíz debería seguir casando")

    def test_los_cpv_se_comparan_como_familia_no_como_codigo_exacto(self):
        """`cpv_praefixe` se declara con ceros de relleno y debe acotar la familia.

        Con la comparación literal, "72500000" solo casaba consigo mismo y la oficina
        de ciberseguridad del Ministerio de Cultura (72514300) no recibía puntos del
        grupo 725 al que pertenece.
        """
        self.assertEqual(prefijo_cpv("72500000"), "725")
        self.assertEqual(prefijo_cpv("80533100"), "805331")
        self.assertEqual(prefijo_cpv("48730000"), "4873")
        self.assertEqual(prefijo_cpv("72514300"), "725143")
        # Nunca por debajo de dos dígitos: "3" acotaría media taxonomía, mientras que
        # "30" es exactamente la división que declara 30000000.
        self.assertEqual(prefijo_cpv("30000000"), "30")
        self.assertEqual(prefijo_cpv("72"), "72")

        self.assertTrue("72514300".startswith(prefijo_cpv("72500000")))
        self.assertFalse("72514300".startswith("72500000"), "el bug original")
        # Y no debe colarse una familia distinta.
        self.assertFalse("80533100".startswith(prefijo_cpv("72500000")))

    def test_perfiles_bien_formados(self):
        for p in self.perfiles:
            with self.subTest(perfil=p.name):
                self.assertTrue(p.starke_begriffe or p.schwache_begriffe,
                                "un perfil sin términos acepta o rechaza todo")
                if p.schwache_begriffe:
                    self.assertTrue(
                        p.erforderlicher_kontext,
                        "los términos ambiguos necesitan contexto o generan ruido",
                    )


if __name__ == "__main__":
    unittest.main()
