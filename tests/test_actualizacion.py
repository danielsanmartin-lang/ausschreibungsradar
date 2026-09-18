"""Traer una versión nueva sin llevarse por delante los datos de nadie.

Este módulo sustituye ficheros del propio programa a partir de algo descargado de
internet, así que lo que se prueba aquí no es tanto que funcione como que **se niegue a
funcionar** cuando algo no cuadra: si el paquete no es lo que dice ser, lo correcto es
quedarse en la versión vieja, no instalar media.

Y hay dos cosas que jamás puede tocar, porque son el trabajo de la persona y no del
programa: `data/` (la base, el triaje, las notas) y `config/suchprofile.json` (los términos
de búsqueda que cada uno ha ido afinando).
"""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from radar import actualizacion, net


def zip_de_release(destino: Path, version: str, sin=(), extra=None) -> Path:
    """Un zipball como el que sirve GitHub: todo dentro de una carpeta «repo-sha»."""
    base = "danielsanmartin-lang-ausschreibungsradar-abc1234"
    contenido = {
        "radar/__init__.py": f'__version__ = "{version}"\n',
        "radar/db.py": "# nuevo\n",
        "web/app.js": "// nuevo\n",
        "radar.py": "# nuevo\n",
        "config/certs/ca-bundle.pem": "# certificados nuevos\n",
        # Ojo: el zip TRAE suchprofile.json, como el repositorio real. Que no se instale es
        # justamente lo que hay que comprobar.
        "config/suchprofile.json": '{"perfiles": "los de fábrica"}',
    }
    for ruta in sin:
        contenido.pop(ruta, None)
    contenido.update(extra or {})
    with zipfile.ZipFile(destino, "w") as zf:
        for ruta, texto in contenido.items():
            zf.writestr(f"{base}/{ruta}", texto)
    return destino


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.raiz = Path(self.dir.name) / "proyecto"
        (self.raiz / "radar").mkdir(parents=True)
        (self.raiz / "radar" / "__init__.py").write_text('__version__ = "1.0.0"\n')
        (self.raiz / "radar" / "db.py").write_text("# viejo\n")
        (self.raiz / "web").mkdir()
        (self.raiz / "web" / "app.js").write_text("// viejo\n")
        (self.raiz / "radar.py").write_text("# viejo\n")
        (self.raiz / "config").mkdir()
        (self.raiz / "config" / "suchprofile.json").write_text('{"mios": "afinados a mano"}')
        (self.raiz / "data").mkdir()
        (self.raiz / "data" / "ausschreibungen.db").write_text("mi base de 3 GB")

        self.zip = Path(self.dir.name) / "release.zip"
        parches = [
            mock.patch.object(actualizacion, "RAIZ", self.raiz),
            mock.patch.object(actualizacion, "__version__", "1.0.0"),
            # Que no haya ninguna ingesta en marcha: eso tiene su propia prueba.
            mock.patch("radar.busqueda.en_marcha", return_value=None),
        ]
        for p in parches:
            p.start()
            self.addCleanup(p.stop)

    def _release(self, version="1.1.0", notas=""):
        return {"tag_name": version, "body": notas,
                "zipball_url": f"https://api.github.com/x/{version}"}

    def _aplicar(self, release=None, version_del_zip=None, **kw):
        """Simula la descarga entregando el zip que se le haya preparado."""
        release = release or self._release()
        zip_de_release(self.zip, version_del_zip or release["tag_name"], **kw)

        def descarga(url, destino, **_):
            destino.write_bytes(self.zip.read_bytes())
            return destino

        with mock.patch.object(net, "descargar_json", return_value=release), \
             mock.patch.object(net, "descargar_a_fichero", side_effect=descarga) as bajar:
            self.bajar = bajar
            return actualizacion.aplicar()


class TestComparacionDeVersiones(unittest.TestCase):
    def test_se_comparan_como_numeros_y_no_como_texto(self):
        """El fallo clásico: como texto, «1.10» es anterior a «1.9» y el actualizador se
        quedaría clavado para siempre en la versión vieja."""
        self.assertGreater(actualizacion._tupla("1.10.0"), actualizacion._tupla("1.9.0"))

    def test_la_v_de_la_etiqueta_da_igual(self):
        self.assertEqual(actualizacion._tupla("v2.0.1"), actualizacion._tupla("2.0.1"))

    def test_una_version_rara_no_revienta(self):
        self.assertEqual(actualizacion._tupla("1.0-beta"), (1, 0))


class TestComprobar(Base):
    def test_avisa_cuando_hay_una_mas_nueva(self):
        with mock.patch.object(net, "descargar_json", return_value=self._release("1.2.0")):
            info = actualizacion.comprobar()
        self.assertTrue(info["hay_nueva"])
        self.assertEqual(info["version_nueva"], "1.2.0")

    def test_la_misma_version_no_es_una_nueva(self):
        with mock.patch.object(net, "descargar_json", return_value=self._release("1.0.0")):
            self.assertFalse(actualizacion.comprobar()["hay_nueva"])

    def test_una_release_mas_vieja_no_cuenta(self):
        """Si alguien publica una release antigua, no se degrada la instalación."""
        with mock.patch.object(net, "descargar_json", return_value=self._release("0.9.0")):
            self.assertFalse(actualizacion.comprobar()["hay_nueva"])

    def test_un_404_se_explica_en_aleman(self):
        """Es el caso de hoy: repositorio privado o sin releases. La interfaz llama a
        esto al abrirse, así que no puede lanzar excepciones ni soltar un traceback."""
        error = net.ErrorRed("no existe", codigo=404)
        with mock.patch.object(net, "descargar_json", side_effect=error):
            info = actualizacion.comprobar()
        self.assertFalse(info["hay_nueva"])
        self.assertIn("privat", info["error"])

    def test_sin_internet_devuelve_error_no_excepcion(self):
        with mock.patch.object(net, "descargar_json",
                              side_effect=net.ErrorRed("falló tras 2 intentos")):
            info = actualizacion.comprobar()
        self.assertIsNotNone(info["error"])
        self.assertFalse(info["hay_nueva"])


class TestAplicar(Base):
    def test_sustituye_el_codigo_y_guarda_lo_anterior(self):
        r = self._aplicar(self._release("1.1.0"))
        self.assertTrue(r["ok"], r["mensaje"])
        self.assertEqual((self.raiz / "radar" / "db.py").read_text(), "# nuevo\n")
        self.assertEqual((self.raiz / "web" / "app.js").read_text(), "// nuevo\n")
        # Poder volver atrás importa más que ahorrar 600 kB.
        self.assertEqual((self.raiz / "radar.anterior" / "db.py").read_text(), "# viejo\n")

    def test_no_toca_la_base_ni_el_triaje(self):
        self._aplicar()
        self.assertEqual((self.raiz / "data" / "ausschreibungen.db").read_text(), "mi base de 3 GB")

    def test_el_zipball_se_baja_con_dos_intentos_y_no_con_cuatro(self):
        """`aplicar_en_subproceso` mata el proceso a los 900 s. Con los cuatro intentos
        por defecto de `descargar_a_fichero` (600 s de timeout cada uno) el peor caso se
        come el plazo y la actualización muere a mitad."""
        self._aplicar()
        self.assertEqual(self.bajar.call_args.kwargs["intentos"], 2)

    def test_no_toca_los_terminos_de_busqueda(self):
        """La prueba que justifica la lista blanca: el zip trae `suchprofile.json` con los
        valores de fábrica, y aun así los del usuario tienen que seguir ahí. Perderlos
        sería borrarle meses de ajuste fino a cada compañero."""
        self._aplicar()
        self.assertEqual(json.loads((self.raiz / "config" / "suchprofile.json").read_text()),
                         {"mios": "afinados a mano"})

    def test_los_certificados_si_se_actualizan(self):
        """Caducan, y si se quedan atrás PLACSP deja de validar."""
        self._aplicar()
        self.assertIn("nuevos",
                      (self.raiz / "config" / "certs" / "ca-bundle.pem").read_text())

    def test_estar_al_dia_no_toca_nada(self):
        with mock.patch.object(net, "descargar_json", return_value=self._release("1.0.0")):
            r = actualizacion.aplicar()
        self.assertTrue(r["ok"])
        self.assertTrue(r["sin_cambios"])
        self.assertEqual((self.raiz / "radar" / "db.py").read_text(), "# viejo\n")

    def test_si_la_etiqueta_y_el_codigo_no_coinciden_no_se_instala(self):
        """Defensa contra un paquete que no es lo que dice ser: la release anuncia 1.1.0
        y dentro viene otra cosa."""
        r = self._aplicar(self._release("1.1.0"), version_del_zip="9.9.9")
        self.assertFalse(r["ok"])
        self.assertIn("es wird nichts geändert", r["mensaje"].lower())
        self.assertEqual((self.raiz / "radar" / "db.py").read_text(), "# viejo\n")

    def test_un_zip_al_que_le_falta_media_aplicacion_no_se_instala(self):
        r = self._aplicar(sin=("web/app.js",))
        self.assertFalse(r["ok"])
        self.assertIn("web", r["mensaje"])
        self.assertEqual((self.raiz / "web" / "app.js").read_text(), "// viejo\n")

    def test_si_el_sha_publicado_no_cuadra_no_se_instala(self):
        """Una descarga corrompida a medio camino no llega a sustituir nada."""
        falso = "0" * 64
        r = self._aplicar(self._release("1.1.0", notas=f"Cambios varios\nSHA-256: {falso}"))
        self.assertFalse(r["ok"])
        self.assertIn("SHA-256", r["mensaje"])
        self.assertEqual((self.raiz / "radar" / "db.py").read_text(), "# viejo\n")

    def test_el_sha_correcto_deja_pasar(self):
        zip_de_release(self.zip, "1.1.0")
        sha = actualizacion._sha256(self.zip)

        def descarga(url, destino, **_):
            destino.write_bytes(self.zip.read_bytes())
            return destino

        with mock.patch.object(net, "descargar_json",
                              return_value=self._release("1.1.0", notas=f"sha256 `{sha}`")), \
             mock.patch.object(net, "descargar_a_fichero", side_effect=descarga):
            r = actualizacion.aplicar()
        self.assertTrue(r["ok"], r["mensaje"])

    def test_lo_descargado_que_no_es_un_zip_no_se_instala(self):
        def descarga(url, destino, **_):
            destino.write_text("<html>error del proxy</html>")
            return destino

        with mock.patch.object(net, "descargar_json", return_value=self._release()), \
             mock.patch.object(net, "descargar_a_fichero", side_effect=descarga):
            r = actualizacion.aplicar()
        self.assertFalse(r["ok"])
        self.assertEqual((self.raiz / "radar" / "db.py").read_text(), "# viejo\n")

    def test_no_se_actualiza_con_una_descarga_en_marcha(self):
        """Cambiar el código por debajo de una carga inicial de dos horas es pedir que
        la mitad de la ingesta corra con una versión y la otra mitad con otra."""
        with mock.patch("radar.busqueda.en_marcha",
                        return_value={"pid": 123, "iniciada": "2026-08-17T09:34:06+00:00"}):
            r = actualizacion.aplicar()
        self.assertFalse(r["ok"])
        self.assertIn("läuft ein Abruf", r["mensaje"])

    def test_si_falla_a_mitad_se_deshace_lo_hecho(self):
        """Media instalación es peor que una versión vieja: si el intercambio revienta
        con `web` ya movido, hay que devolver todo a su sitio."""
        real = actualizacion.shutil.move
        llamadas = []

        def move_que_falla(origen, destino):
            llamadas.append(destino)
            if len(llamadas) == 2:
                raise OSError("disco lleno")
            return real(origen, destino)

        zip_de_release(self.zip, "1.1.0")

        def descarga(url, destino, **_):
            destino.write_bytes(self.zip.read_bytes())
            return destino

        with mock.patch.object(net, "descargar_json", return_value=self._release("1.1.0")), \
             mock.patch.object(net, "descargar_a_fichero", side_effect=descarga), \
             mock.patch.object(actualizacion.shutil, "move", side_effect=move_que_falla):
            r = actualizacion.aplicar()

        self.assertFalse(r["ok"])
        self.assertIn("wie es war", r["mensaje"])
        self.assertEqual((self.raiz / "radar" / "db.py").read_text(), "# viejo\n")
        self.assertEqual((self.raiz / "web" / "app.js").read_text(), "// viejo\n")
        self.assertFalse((self.raiz / "radar.anterior").exists(),
                         "el respaldo tiene que volver a su sitio, no quedarse suelto")


if __name__ == "__main__":
    unittest.main()
