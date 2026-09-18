"""Indicador de progreso de la terminal.

Lo que hay que proteger aquí no es la estética de la línea, es que el indicador
no pueda tumbar una ingesta: se llama desde el camino caliente, desde un hilo
aparte y con la terminal pudiendo desaparecer debajo. Un fallo pintando debe
costar una línea fea, nunca una descarga perdida.
"""

import io
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path

from radar import progreso


class SalidaFalsa(io.StringIO):
    """StringIO que puede fingir ser un TTY."""

    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class TestFormatos(unittest.TestCase):
    def test_los_miles_se_separan_como_en_espanol(self):
        self.assertEqual(progreso._miles(131106), "131.106")

    def test_los_tamanos_usan_coma_decimal(self):
        self.assertEqual(progreso._tam(900), "900 B")
        self.assertEqual(progreso._tam(64 << 10), "64 kB")
        self.assertEqual(progreso._tam(12 << 20), "12,0 MB")

    def test_las_duraciones_se_leen_de_un_vistazo(self):
        self.assertEqual(progreso._dur(45), "45s")
        self.assertEqual(progreso._dur(72), "1m 12s")
        self.assertEqual(progreso._dur(3780), "1h 03m")


class TestIndicador(unittest.TestCase):
    def setUp(self):
        self.ind = progreso._Indicador()
        self.addCleanup(self.ind.parar)
        # La instantánea se escribe a un temporal: con la ruta por defecto, cada test
        # dejaría un data/progreso.json dentro del repositorio.
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.estado = Path(self.dir.name) / "progreso.json"
        self.iniciar = lambda salida: self.ind.iniciar(salida, estado=self.estado)

    def test_sin_iniciar_no_escribe_nada(self):
        """El pipeline llama a estas funciones siempre; en los tests y en
        `radar.py match` no hay indicador y no deben hacer nada."""
        salida = SalidaFalsa(tty=True)
        self.ind._salida = salida
        self.ind.fuente("placsp:licitaciones")
        self.ind.sumar_bytes(1000)
        self.ind.fichas(500)
        self.ind.limpiar()
        self.assertEqual(salida.getvalue(), "")

    def test_en_terminal_repinta_una_sola_linea(self):
        salida = SalidaFalsa(tty=True)
        self.iniciar(salida)
        self.ind.fuente("placsp:licitaciones")
        self.ind.fase("descargando")
        self.ind.sumar_bytes(12 << 20)
        time.sleep(0.35)
        self.ind.parar()

        texto = salida.getvalue()
        self.assertIn("placsp:licitaciones", texto)
        self.assertIn("descargando 12,0 MB", texto)
        # Cada repintado empieza borrando: nunca se acumulan líneas nuevas.
        self.assertNotIn("\n", texto)
        self.assertGreater(texto.count(progreso.BORRAR_LINEA), 1)

    def test_el_porcentaje_aparece_solo_si_se_sabe_el_total(self):
        self.ind._fuente = "x"
        self.ind._fase = "descargando"
        self.ind._bytes = 6 << 20
        self.assertNotIn("%", self.ind._resumen())
        self.ind.bytes_totales(24 << 20)
        self.assertIn("(25%)", self.ind._resumen())

    def test_sin_terminal_suelta_lineas_sueltas_y_no_codigos_ansi(self):
        """La tarea de cada mañana escribe en data/ingest.log: ahí un \\r solo
        haría el registro ilegible."""
        salida = SalidaFalsa(tty=False)
        original = progreso.INTERVALO_LOG
        progreso.INTERVALO_LOG = 0.1
        self.addCleanup(setattr, progreso, "INTERVALO_LOG", original)

        self.iniciar(salida)
        self.ind.fuente("ted")
        self.ind.fichas(59)
        time.sleep(0.4)
        self.ind.parar()

        texto = salida.getvalue()
        self.assertIn("ted", texto)
        self.assertIn("59 Datensätze", texto)
        self.assertNotIn("\x1b", texto)
        self.assertNotIn("\r", texto)

    def test_cambiar_de_fuente_reinicia_los_contadores(self):
        self.ind.sumar_bytes(999)
        self.ind.fichas(42)
        self.ind.pagina()
        self.ind.tarea("el histórico de 2024 (año 1 de 3)")
        self.ind.fuente("catalunya")
        self.assertEqual((self.ind._bytes, self.ind._fichas, self.ind._paginas), (0, 0, 0))
        self.assertEqual(self.ind._tarea, "", "la tarea era de la fuente anterior")

    def test_la_velocidad_no_revienta_sin_nada_que_medir(self):
        """Es la señal de vida de una descarga sin tamaño anunciado, así que se calcula
        en cada tic: dividir por cero ahí tumbaría la ingesta entera."""
        self.assertEqual(self.ind._velocidad(), 0.0)
        self.ind.reiniciar_bytes()
        self.assertEqual(self.ind._velocidad(), 0.0, "sin bytes todavía")
        self.ind.sumar_bytes(5_000_000)
        # Recién arrancada la descarga no se inventa una cifra con el primer trozo.
        self.assertEqual(self.ind._velocidad(), 0.0)
        self.ind._t_bytes -= 10  # como si llevara diez segundos bajando
        self.assertAlmostEqual(self.ind._velocidad(), 500_000, delta=50_000)

    def test_la_descarga_de_un_ano_no_arrastra_los_ficheros_del_anterior(self):
        """El ZIP de un año deja el contador en «fichero 1398 de 1398»; si al empezar
        la descarga del siguiente no se reinicia, la barra se queda clavada al 100%."""
        self.ind.subtarea(1398, 1398)
        self.ind.subtarea(0, 0)
        self.assertEqual((self.ind._subtarea, self.ind._subtareas), (0, 0))

    def test_reiniciar_bytes_conserva_las_paginas(self):
        """Cada página es una petición nueva: los bytes vuelven a cero, pero el
        contador de páginas es lo que dice que se está avanzando."""
        self.ind.pagina()
        self.ind.pagina()
        self.ind.sumar_bytes(500)
        self.ind.reiniciar_bytes()
        self.assertEqual(self.ind._bytes, 0)
        self.assertEqual(self.ind._paginas, 2)

    def test_los_bytes_heredados_no_inflan_la_velocidad(self):
        """Al reanudar, lo que ya estaba en el `.parcial` no se ha descargado ahora.

        Repartir 1,2 GB heredados entre los segundos que lleva el intento nuevo daba
        «400 MB/s», que es la única cifra peor que no dar ninguna: quien mira la
        pantalla la usa para decidir si la descarga va bien o se está atascando.
        """
        self.ind.reiniciar_bytes(heredados=1_200_000_000)
        self.ind.sumar_bytes(5_000_000)
        self.ind._t_bytes -= 10  # como si llevara diez segundos bajando
        self.assertAlmostEqual(self.ind._velocidad(), 500_000, delta=50_000)
        self.assertEqual(self.ind._bytes, 1_205_000_000, "la barra sí cuenta lo heredado")

    def test_la_barra_no_vuelve_a_cero_al_reanudar(self):
        """Si el porcentaje se reiniciara, reanudar al 92% parecería empezar de nuevo."""
        self.ind._fuente = "placsp:licitaciones"
        self.ind._fase = "descargando, reanudando"
        self.ind.reiniciar_bytes(heredados=1_200_000_000)
        self.ind.bytes_totales(1_300_000_000)
        self.assertIn("(92%)", self.ind._resumen())

    def test_la_frase_de_una_reanudacion_dice_que_se_reanuda(self):
        """La fase lleva un sufijo, así que la comparación tiene que ser por prefijo:
        con un `==` esto caía en la rama de las fichas y decía «Leyendo las fichas»
        mientras descargaba."""
        self.ind._titulo = "der Bekanntmachungsservice des Bundes"
        self.ind._tarea = "Historie 2026"
        self.ind._fase = progreso.FASE_REANUDANDO
        self.ind.reiniciar_bytes(heredados=600 << 20)
        self.ind.bytes_totales(1200 << 20)
        frase = self.ind._frase()
        self.assertTrue(frase.startswith("Download wird fortgesetzt: Historie 2026"),
                        frase)
        self.assertIn("von 1200,0 MB", frase)

    def test_un_reintento_no_se_cuenta_como_lectura_de_fichas(self):
        """Con reintentos, estas dos fases pueden estar minutos en pantalla."""
        self.ind._titulo = "das Amtsblatt der Europäischen Union"
        self.ind._fichas = 500
        self.ind._fase = f"{progreso.FASE_RECONECTANDO} (2/4)"
        self.assertIn("Verbindung zum Server", self.ind._frase())
        self.ind._fase = "falló el intento 2/4, reintento en 4s"
        self.assertIn("Der Download wurde unterbrochen", self.ind._frase())

    def test_una_terminal_cerrada_no_tumba_la_ingesta(self):
        salida = SalidaFalsa(tty=True)
        self.iniciar(salida)
        self.ind.fuente("placsp:licitaciones")
        salida.close()  # escribir ahora lanza ValueError
        time.sleep(0.3)
        # El hilo se ha rendido en silencio y el trabajo puede seguir.
        self.ind.fichas(10)
        self.ind.sumar_bytes(10)

    def test_parar_dos_veces_no_falla(self):
        self.iniciar(SalidaFalsa(tty=True))
        self.ind.parar()
        self.ind.parar()

    def test_iniciar_dos_veces_no_deja_dos_hilos(self):
        self.iniciar(SalidaFalsa(tty=True))
        primero = self.ind._hilo
        self.iniciar(SalidaFalsa(tty=True))
        self.assertIs(self.ind._hilo, primero)


class TestInstantanea(unittest.TestCase):
    """La instantánea que lee la aplicación.

    La ingesta y el servidor son procesos distintos, así que esto es lo único que ve
    la aplicación de una carga que corre en segundo plano. Si se rompe, la barra de
    progreso se queda quieta y el compañero vuelve a pensar que no funciona.
    """

    def setUp(self):
        # El orden importa, y cuesta un CI en rojo aprenderlo: `addCleanup` ejecuta al
        # revés de como se registra, así que el temporal se apunta ANTES para que se
        # borre DESPUÉS de parar el hilo. Al revés —que era como estaba— el publicador
        # sigue vivo mientras `rmtree` recorre el directorio, y como `_publicar` hace
        # `mkdir(parents=True)` cada segundo, recrea la carpeta entre el borrado del
        # fichero y el `rmdir`: «Directory not empty». Fallaba solo en macOS y solo a
        # veces, que es lo peor que puede hacer una prueba.
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ind = progreso._Indicador()
        self.addCleanup(self.ind.parar)
        self.estado = Path(self.dir.name) / "progreso.json"

    def _esperar_fichero(self, plazo=2.0):
        limite = time.monotonic() + plazo
        while time.monotonic() < limite:
            if self.estado.exists():
                return json.loads(self.estado.read_text(encoding="utf-8"))
            time.sleep(0.05)
        self.fail("no se publicó la instantánea")

    def test_publica_los_campos_que_pinta_la_barra(self):
        self.ind.iniciar(SalidaFalsa(tty=False), estado=self.estado)
        self.ind.etapa(2, 3, "Histórico de plataformas agregadas",
                       "unos 15 minutos", "cubre siete comunidades")
        self.ind.fuente("placsp:agregadas", "las plataformas autonómicas")
        self.ind.tarea("el histórico de 2025 (año 2 de 3)")
        self.ind.fase("descargando")
        self.ind.bytes_totales(2172595976)
        self.ind.sumar_bytes(812000000)
        self.ind.subtarea(37, 412)
        self.ind.fichas(118133)

        d = self._esperar_fichero()
        self.assertEqual(d["etapa"], 2)
        self.assertEqual(d["etapas"], 3)
        self.assertEqual(d["etiqueta"], "Histórico de plataformas agregadas")
        self.assertEqual(d["fuente"], "placsp:agregadas")
        self.assertEqual(d["fase"], "descargando")
        self.assertEqual((d["bytes"], d["bytes_total"]), (812000000, 2172595976))
        self.assertEqual((d["subtarea"], d["subtareas"]), (37, 412))
        self.assertEqual(d["fichas"], 118133)
        # El resumen es el mismo texto de la terminal, para que las dos cuenten lo
        # mismo y la aplicación no tenga que recomponerlo.
        self.assertIn("Schritt 2/3", d["resumen"])
        self.assertIn("Datei 37/412", d["resumen"])
        # Y lo que necesita la aplicación para explicar la espera en vez de solo
        # medirla: qué año va, de qué fuente, y cuánto se supone que tarda la etapa.
        self.assertEqual(d["titulo"], "las plataformas autonómicas")
        self.assertEqual(d["tarea"], "el histórico de 2025 (año 2 de 3)")
        self.assertEqual(d["coste"], "unos 15 minutos")
        self.assertEqual(d["detalle_etapa"], "cubre siete comunidades")
        self.assertIn("2025", d["resumen"])
        self.assertIn("las plataformas autonómicas", d["frase"])
        self.assertIn("2025", d["frase"])

    def test_no_deja_temporales_ni_json_a_medias(self):
        """El servidor lee este fichero cada segundo y medio: si pillara una escritura
        a medias, la barra saltaría a cero."""
        self.ind.iniciar(SalidaFalsa(tty=False), estado=self.estado)
        self.ind.fuente("ted")
        self._esperar_fichero()
        for _ in range(40):
            self.ind._publicar()
            json.loads(self.estado.read_text(encoding="utf-8"))  # siempre válido
        self.assertEqual(list(Path(self.dir.name).iterdir()), [self.estado])

    def test_al_parar_se_borra(self):
        """Si se quedara, la aplicación mostraría la barra de la carga anterior."""
        self.ind.iniciar(SalidaFalsa(tty=False), estado=self.estado)
        self.ind.fuente("ted")
        self._esperar_fichero()
        self.ind.parar()
        self.assertFalse(self.estado.exists())

    def test_no_poder_publicar_no_tumba_la_ingesta(self):
        """Un disco lleno o un permiso raro cuesta la barra, nunca la descarga."""
        raiz = Path(self.dir.name)
        self.ind.iniciar(SalidaFalsa(tty=False),
                         estado=raiz / "no-existe" / "progreso.json")
        self.ind.fuente("ted")
        raiz.chmod(0o500)  # ni se puede crear el directorio que falta
        self.addCleanup(raiz.chmod, 0o700)
        self.ind._publicar()
        self.ind.fichas(10)  # el trabajo sigue

    def test_sin_ruta_no_escribe_nada(self):
        self.ind.iniciar(SalidaFalsa(tty=False), estado=None)
        self.ind.fuente("ted")
        time.sleep(0.3)
        self.assertEqual(list(Path(self.dir.name).iterdir()), [])


class TestManejadorLog(unittest.TestCase):
    def test_el_log_borra_la_linea_antes_de_escribir(self):
        """Sin esto cada log.info sale con restos del indicador pegados detrás."""
        salida = SalidaFalsa(tty=True)
        progreso._IND.iniciar(salida, estado=None)
        self.addCleanup(progreso._IND.parar)
        progreso._IND.fuente("placsp:licitaciones")
        time.sleep(0.2)

        manejador = progreso.ManejadorLog(salida)
        manejador.setFormatter(logging.Formatter("%(message)s"))
        manejador.emit(
            logging.LogRecord("x", logging.INFO, __file__, 1, "59 avisos", None, None)
        )

        texto = salida.getvalue()
        # El mensaje va precedido del borrado, no del final de la línea de estado.
        self.assertIn(progreso.BORRAR_LINEA + "59 avisos", texto)


if __name__ == "__main__":
    unittest.main()


class TestLasFasesSonMarcadores(unittest.TestCase):
    """Las fases cruzan de Python a JavaScript, y ningún compilador lo comprueba.

    `web/app.js` decide con ellas si pinta una barra de descarga con porcentaje o una
    indeterminada. Si alguien traduce «descargando» al alemán en `progreso.py`, no salta
    ningún error: la barra deja de avanzar y la aplicación parece colgada justo durante
    las dos horas de la carga inicial, que es cuando más se mira.
    """

    def test_app_js_conoce_las_fases_que_compara(self):
        app = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text()
        for fase in (progreso.FASE_DESCARGANDO, progreso.FASE_ZIP):
            with self.subTest(fase=fase):
                self.assertIn(f"'{fase}'", app,
                              f"web/app.js no conoce la fase «{fase}»")

    def test_ninguna_fase_se_ha_traducido(self):
        """Valen cualesquiera, pero tienen que ser las MISMAS a los dos lados."""
        self.assertEqual(progreso.FASE_REANUDANDO.split(",")[0], progreso.FASE_DESCARGANDO,
                         "la reanudación tiene que seguir empezando por la fase de descarga")
