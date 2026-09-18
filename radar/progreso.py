"""Indicador de actividad en la terminal: qué está haciendo la ingesta ahora.

Por qué existe: la ingesta se pasa la mayor parte del tiempo esperando a que
conteste PLACSP —timeout de 120 s y hasta cuatro intentos por URL, con esperas
de 2, 4 y 8 s entre medias— y durante ese rato no imprimía absolutamente nada.
Desde fuera es indistinguible de un cuelgue, y quien lanza start.command acaba
matando con Ctrl+C una descarga que iba bien.

El módulo no cambia lo que hace la ingesta: solo la cuenta en voz alta. Lo hace
desde un hilo aparte que repinta cada 100 ms, porque los tramos mudos son
justamente aquellos en los que el hilo principal está bloqueado leyendo de un
socket y no puede imprimir nada por su cuenta.

Con terminal (stdout es un TTY) pinta una sola línea que se sobrescribe:

    ⠹ etapa 3/4 · placsp:agregadas · el histórico de 2025 (año 2 de 3) · descargando 84,2 MB/133,0 MB (63%) a 1,4 MB/s · 2.950 fichas · 1m 12s

Cuando la salida es un fichero —la tarea de cada mañana escribe en
data/ingest.log— no hay repintado posible, así que suelta una línea cada 30 s.
El registro queda legible y además dice en qué punto exacto se atascó.

Todas las funciones son inocuas si nadie ha llamado a `iniciar()`: los módulos
de red y el pipeline las invocan siempre, y en los tests o en `radar.py match`
simplemente no hacen nada.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from . import rutas

RAIZ = rutas.CODIGO

RUEDA = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
INTERVALO_PINTADO = 0.1   # s entre repintados cuando hay terminal
INTERVALO_LOG = 30.0      # s entre líneas sueltas cuando la salida es un fichero
INTERVALO_ESTADO = 1.0    # s entre instantáneas escritas a disco
BORRAR_LINEA = "\r\x1b[K"

# La ingesta y el servidor son procesos distintos, así que los contadores en memoria
# no le sirven a la aplicación: se publica una instantánea aquí y el servidor la lee.
# Es lo que permite que la carga inicial se vea avanzar en la pantalla mientras la
# base sigue creciendo por detrás.
ESTADO = rutas.DIR_DATOS / "progreso.json"


def _miles(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _tam(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} kB"
    return f"{n / (1024 * 1024):.1f} MB".replace(".", ",")


def _dur(segundos: float) -> str:
    s = int(segundos)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def _ritmo(bytes_por_s: float) -> str:
    """En MB por segundo, que es como se mide una conexión en todas partes.

    Con un decimal porque estas descargas se mueven por debajo de 2 MB/s y sin él se
    quedarían todas en «1 MB/s», que no dice si va bien o si se está atascando.
    """
    return f"{bytes_por_s / (1024 * 1024):.1f} MB/s".replace(".", ",")


# Las FASES son marcadores, no texto. Se comparan por prefijo aquí, en `net.py` y en
# `web/app.js` (que decide con ellas si pinta barra de descarga o barra indeterminada).
# Traducirlas no da un error: deja la barra de descarga muerta y nadie se entera. Por eso
# van como constantes y en un idioma que ya no se toca.
FASE_CONECTANDO = "conectando"
FASE_RECONECTANDO = "reconectando"
FASE_DESCARGANDO = "descargando"
FASE_REANUDANDO = "descargando, reanudando"
FASE_ZIP = "leyendo el zip"
FASE_FALLO = "falló el intento"
FASE_PROCESANDO = "procesando"


class _Indicador:
    """Estado compartido entre el hilo que trabaja y el hilo que lo cuenta."""

    def __init__(self) -> None:
        # Reentrante: el manejador de logging borra la línea con el cerrojo ya
        # tomado por `imprimir()`.
        self._cerrojo = threading.RLock()
        self._parada = threading.Event()
        self._hilo: threading.Thread | None = None
        self._salida = sys.stdout
        self._tty = False
        self._pintada = False
        self._rueda = itertools.cycle(RUEDA)

        self._inicio = 0.0
        self._ultimo_log = 0.0
        self._ultimo_estado = 0.0
        self._estado: Path | None = None
        self._etapa = 0
        self._etapas = 0
        self._etiqueta = ""
        self._coste = ""
        self._detalle_etapa = ""
        self._fuente = ""
        self._titulo = ""
        self._tarea = ""
        self._fase = ""
        # Instante en que arrancó la descarga en curso, para poder dar la velocidad.
        # Sin ella, una descarga sin `Content-Length` no tiene ninguna señal de vida:
        # ni porcentaje, ni ficheros, ni fichas. Solo los MB, y no se sabe si suben.
        self._t_bytes = 0.0
        # Contadores del camino caliente (uno por ficha procesada): se leen y
        # escriben sin cerrojo a propósito. Asignar un int es atómico y no
        # merece la pena pagar un lock 130.000 veces por una cifra decorativa.
        self._paginas = 0
        self._bytes = 0
        # Los bytes que ha leído ESTE intento, que no son los mismos que `_bytes`
        # cuando una descarga se reanuda: allí `_bytes` arranca con lo que ya había
        # en el `.parcial` para que la barra no vuelva a cero, y repartir esos 1,2 GB
        # heredados entre los tres segundos que lleva el intento nuevo daba
        # «400 MB/s». Dos contadores porque son dos preguntas: cuánto hay y a qué
        # ritmo llega.
        self._bytes_medidos = 0
        self._bytes_total = 0
        self._fichas = 0
        self._subtarea = 0
        self._subtareas = 0

    # -- ciclo de vida ----------------------------------------------------

    def iniciar(self, salida=None, estado: Path | None = ESTADO) -> None:
        """Arranca el hilo que pinta. Se puede llamar dos veces sin romper nada."""
        if os.environ.get("RADAR_SIN_PROGRESO"):
            return
        with self._cerrojo:
            if self._hilo is not None:
                return
            self._estado = Path(estado) if estado else None
            self._salida = salida or sys.stdout
            try:
                self._tty = self._salida.isatty()
            except (AttributeError, ValueError):
                self._tty = False
            self._inicio = time.monotonic()
            self._ultimo_log = self._inicio
            self._ultimo_estado = 0.0  # que el primer tic publique ya
            self._parada.clear()
            self._hilo = threading.Thread(
                target=self._bucle, name="progreso", daemon=True
            )
            self._hilo.start()

    def parar(self) -> None:
        hilo = self._hilo
        self._parada.set()
        if hilo is not None:
            hilo.join(timeout=1.0)
        with self._cerrojo:
            self.limpiar()
            # La instantánea se borra al terminar: si se quedara, la aplicación
            # mostraría la barra de la carga anterior mientras la siguiente todavía
            # no ha escrito la suya.
            if self._estado is not None:
                self._estado.unlink(missing_ok=True)
            self._hilo = None
            self._fuente = ""
            self._titulo = ""
            self._tarea = ""
            self._fase = ""
            self._etapa = self._etapas = 0
            self._etiqueta = self._coste = self._detalle_etapa = ""

    @property
    def activo(self) -> bool:
        return self._hilo is not None

    # -- lo que reporta quien trabaja -------------------------------------

    def etapa(self, numero: int, total: int, etiqueta: str = "",
              coste: str = "", detalle: str = "") -> None:
        """Etapa de la carga inicial: «2 de 3, histórico de plataformas agregadas».

        Es lo único que da una idea de cuánto queda en total, porque el avance dentro
        de una fuente no dice nada de las que vienen después.

        `coste` y `detalle` son los textos que ya describen cada etapa en
        `ETAPAS_PRIMERA_CARGA` («unos 5 GB de descarga y 15 min de proceso»). Se guardan
        para que la aplicación pueda decir que la espera es larga *por diseño*: es la
        diferencia entre parecer colgada y parecer ocupada.
        """
        with self._cerrojo:
            self._etapa = numero
            self._etapas = total
            self._etiqueta = etiqueta
            self._coste = coste
            self._detalle_etapa = detalle

    def fuente(self, nombre: str, titulo: str = "") -> None:
        """Empieza una fuente nueva: reinicia los contadores de esta etapa.

        `titulo` es el nombre de la fuente en castellano, para la interfaz;
        `nombre` es el técnico, que es el que sirve para buscar en el log.
        """
        with self._cerrojo:
            self._fuente = nombre
            self._titulo = titulo
            self._tarea = ""
            self._fase = ""
            self._paginas = 0
            self._bytes = 0
            self._bytes_medidos = 0
            self._bytes_total = 0
            self._fichas = 0
            self._subtarea = 0
            self._subtareas = 0

    def tarea(self, texto: str) -> None:
        """Qué trozo de la fuente se está haciendo: «histórico de 2025 (año 2 de 3)».

        Sin esto, la etapa más larga —tres años de la Plataforma del Estado— se ve
        desde fuera como una sola cosa interminable, sin manera de saber por dónde va.
        """
        self._tarea = texto

    def fase(self, texto: str) -> None:
        self._fase = texto

    def subtarea(self, indice: int, total: int) -> None:
        """Avance dentro de una tarea cuyo tamaño se conoce: fichero N de M del ZIP."""
        self._subtarea = indice
        self._subtareas = total

    def procesando(self) -> None:
        """Marca 'procesando' sin tocar nada si ya lo estaba (camino caliente)."""
        if self._fase != FASE_PROCESANDO:
            self._fase = FASE_PROCESANDO

    def pagina(self) -> None:
        self._paginas += 1

    def fichas(self, n: int) -> None:
        self._fichas = n

    def bytes_totales(self, n: int | None) -> None:
        self._bytes_total = n or 0

    def sumar_bytes(self, n: int) -> None:
        self._bytes += n
        self._bytes_medidos += n

    def reiniciar_bytes(self, heredados: int = 0) -> None:
        """Nueva petición: el contador vuelve a `heredados`, las páginas no.

        `heredados` son los bytes que una descarga reanudada ya trae en el `.parcial`.
        Cuentan para la barra —si no, reanudar al 95% la haría volver a cero y
        parecería que se empieza otra vez— pero NO para la velocidad, que se mide
        aparte: ver `_bytes_medidos`.
        """
        self._bytes = heredados
        self._bytes_medidos = 0
        self._bytes_total = 0
        self._t_bytes = time.monotonic()

    def _velocidad(self) -> float:
        """Bytes por segundo de la descarga en curso. 0 si aún no hay qué medir.

        El medio segundo de guarda evita la cifra absurda del primer tic, cuando el
        primer trozo de un mega se ha leído en milésimas.

        Se mide sobre `_bytes_medidos` y no sobre `_bytes` justamente por las
        reanudaciones: lo que ya estaba en disco no se ha descargado ahora.
        """
        if not self._bytes_medidos or not self._t_bytes:
            return 0.0
        transcurrido = time.monotonic() - self._t_bytes
        return self._bytes_medidos / transcurrido if transcurrido > 0.5 else 0.0

    # -- pintado ----------------------------------------------------------

    def limpiar(self) -> None:
        """Borra la línea de estado para que otro pueda escribir donde estaba."""
        with self._cerrojo:
            if self._pintada and self._tty:
                self._salida.write(BORRAR_LINEA)
                self._salida.flush()
                self._pintada = False

    def imprimir(self, texto: str = "") -> None:
        """print() que no se pisa con la línea de estado."""
        with self._cerrojo:
            self.limpiar()
            print(texto, file=self._salida, flush=True)

    def _resumen(self) -> str:
        partes = []
        if self._etapas:
            partes.append(f"Schritt {self._etapa}/{self._etapas}")
        partes.append(self._fuente or "Vorbereitung")
        if self._tarea:
            partes.append(self._tarea)
        if self._paginas:
            partes.append(f"S. {self._paginas}")
        if self._fase:
            fase = self._fase
            if self._bytes and fase.startswith(FASE_DESCARGANDO):
                if self._bytes_total:
                    pct = 100 * self._bytes / self._bytes_total
                    fase += f" {_tam(self._bytes)}/{_tam(self._bytes_total)} ({pct:.0f}%)"
                else:
                    fase += f" {_tam(self._bytes)}"
                vel = self._velocidad()
                if vel:
                    fase += f" mit {_ritmo(vel)}"
            partes.append(fase)
        if self._subtareas:
            partes.append(f"Datei {self._subtarea}/{self._subtareas}")
        if self._fichas:
            partes.append(f"{_miles(self._fichas)} Datensätze")
        partes.append(_dur(time.monotonic() - self._inicio))
        return " · ".join(partes)

    def _frase(self) -> str:
        """Lo mismo que `_resumen()`, en alemán llano y para la interfaz.

        Se compone aquí y no en el JavaScript por la misma razón que `resumen`: que la
        terminal y la aplicación cuenten lo mismo. La línea de la terminal se recorta al
        ancho disponible y tiene que ser telegráfica; esta no, y es la que responde a la
        única pregunta que se hace quien mira la pantalla diez minutos: ¿esto sigue vivo
        y qué está haciendo?

        Todas las frases tienen la forma «Qué se está haciendo: sobre qué». Es a
        propósito y no es pereza: mete el nombre de la fuente y el de la tarea como
        aposición, así que no hay que declinarlos ni buscarles el artículo correcto. La
        versión española contraía «de el» en «del» a mano y eso, con nombres alemanes,
        habría producido mezclas del tipo «Descargando der Bekanntmachungsservice».
        """
        quien = self._titulo or self._fuente or "die Quellen"
        qué = f"{self._tarea} · {quien}" if self._tarea else quien

        # Se compara por PREFIJO y no por igualdad: la fase de una descarga reanudada es
        # «descargando, reanudando», y con un `==` se colaba hasta la rama de los
        # registros y decía que estaba leyendo fichas mientras bajaba el ZIP.
        if self._fase.startswith(FASE_DESCARGANDO):
            detalle = _tam(self._bytes)
            if self._bytes_total:
                detalle += f" von {_tam(self._bytes_total)}"
            vel = self._velocidad()
            if vel:
                detalle += f" · {_ritmo(vel)}"
            if "reanudando" in self._fase:
                # Decirlo importa: quien vio cortarse la descarga no entiende por qué la
                # barra sale ya casi llena si nadie le cuenta que se está reanudando.
                frase = f"Download wird fortgesetzt: {qué} — {detalle}."
            else:
                frase = f"Wird geladen: {qué} — {detalle}."
            if self._fichas:
                frase += f" Bereits {_miles(self._fichas)} Datensätze aus dieser Quelle."
            return frase
        # Los reintentos dejaron de ser un parpadeo el día que las descargas grandes
        # empezaron a reintentar: estas fases pueden estar en pantalla minutos, y sin
        # rama propia caían en la de los registros, que no viene a cuento.
        if self._fase.startswith((FASE_CONECTANDO, FASE_RECONECTANDO)):
            return f"Verbindung zum Server wird aufgebaut: {qué}."
        if self._fase.startswith(FASE_FALLO):
            return f"Der Download wurde unterbrochen, neuer Versuch läuft: {qué}."
        if self._fase == FASE_ZIP:
            return f"Archiv wird geöffnet: {qué}."
        if self._subtareas:
            return (f"Datensätze werden einzeln gelesen: {qué} — Datei "
                    f"{self._subtarea} von {self._subtareas} · "
                    f"{_miles(self._fichas)} gelesen.")
        if self._fichas:
            return f"Datensätze werden gelesen: {qué} — {_miles(self._fichas)} gelesen."
        return f"Wird vorbereitet: {qué}."

    # -- instantánea para la aplicación -----------------------------------

    def instantanea(self) -> dict:
        """Lo que se publica a disco. Se llama con el cerrojo tomado."""
        return {
            "etapa": self._etapa,
            "etapas": self._etapas,
            "etiqueta": self._etiqueta,
            "coste": self._coste,
            "detalle_etapa": self._detalle_etapa,
            "fuente": self._fuente,
            "titulo": self._titulo,
            "tarea": self._tarea,
            "fase": self._fase,
            "paginas": self._paginas,
            "bytes": self._bytes,
            "bytes_total": self._bytes_total,
            "bytes_por_s": round(self._velocidad()),
            "subtarea": self._subtarea,
            "subtareas": self._subtareas,
            "fichas": self._fichas,
            "segundos": round(time.monotonic() - self._inicio, 1),
            # El mismo texto que se ve en la terminal, para que la aplicación no
            # tenga que recomponerlo y las dos cuenten lo mismo.
            "resumen": self._resumen(),
            # Y la versión en castellano llano, que es la que se lee en pantalla.
            "frase": self._frase(),
        }

    def _publicar(self) -> None:
        """Escribe la instantánea de forma atómica.

        Con el cerrojo tomado. El `replace` sobre un temporal del mismo directorio es
        lo que evita que el servidor lea un JSON a medio escribir; sin él, la barra
        parpadearía a cero cada vez que coincidieran lectura y escritura.
        """
        if self._estado is None:
            return
        tmp = self._estado.with_suffix(self._estado.suffix + ".tmp")
        try:
            self._estado.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.instantanea()), encoding="utf-8")
            tmp.replace(self._estado)
        except OSError:
            # No poder publicar el progreso no es motivo para tumbar una ingesta que
            # va bien; en la terminal se sigue viendo.
            tmp.unlink(missing_ok=True)

    def _bucle(self) -> None:
        while not self._parada.is_set():
            try:
                self._tic()
            except (OSError, ValueError):
                # La terminal se ha cerrado debajo. Callar es mejor que reventar
                # la ingesta por no poder pintar una línea decorativa.
                return
            # Sin terminal basta con despertarse una vez por segundo: quien
            # decide si toca escribir es `_tic` comparando contra INTERVALO_LOG.
            self._parada.wait(
                INTERVALO_PINTADO if self._tty else min(1.0, INTERVALO_LOG)
            )

    def _tic(self) -> None:
        with self._cerrojo:
            if not self._fuente:
                return

            # Se publica pase lo que pase con la terminal: cuando la ingesta corre en
            # segundo plano no hay terminal que mirar, y es justo entonces cuando la
            # aplicación necesita la instantánea.
            ahora = time.monotonic()
            if ahora - self._ultimo_estado >= INTERVALO_ESTADO:
                self._ultimo_estado = ahora
                self._publicar()

            if self._tty:
                # Se recorta al ancho de la terminal: si la línea envuelve, el \r
                # vuelve al principio de la última fila y deja basura pegada.
                ancho = shutil.get_terminal_size((80, 24)).columns
                linea = f"{next(self._rueda)} {self._resumen()}"[: max(ancho - 1, 20)]
                self._salida.write(f"{BORRAR_LINEA}{linea}")
                self._salida.flush()
                self._pintada = True
                return

            if ahora - self._ultimo_log >= INTERVALO_LOG:
                self._ultimo_log = ahora
                print(f"  ... {self._resumen()}", file=self._salida, flush=True)


_IND = _Indicador()


class ManejadorLog(logging.StreamHandler):
    """StreamHandler que borra la línea de estado antes de escribir.

    Sin esto, cada `log.info` se mezcla con el indicador y deja restos de la
    línea anterior colgando al final del mensaje.
    """

    def emit(self, record: logging.LogRecord) -> None:
        with _IND._cerrojo:
            _IND.limpiar()
            super().emit(record)


# Fachada del módulo: el resto del código llama a estas y no al objeto.
iniciar = _IND.iniciar
parar = _IND.parar
etapa = _IND.etapa
fuente = _IND.fuente
tarea = _IND.tarea
fase = _IND.fase
subtarea = _IND.subtarea
instantanea = _IND.instantanea
procesando = _IND.procesando
pagina = _IND.pagina
fichas = _IND.fichas
bytes_totales = _IND.bytes_totales
sumar_bytes = _IND.sumar_bytes
reiniciar_bytes = _IND.reiniciar_bytes
limpiar = _IND.limpiar
imprimir = _IND.imprimir
