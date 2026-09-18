"""Modelo normalizado de una licitación, común a todas las fuentes.

Cada conector traduce su formato nativo (ATOM CODICE, JSON de TED, Socrata) a esta
misma forma. Los campos son los que un comercial necesita para decidir en diez
segundos si merece la pena, más lo necesario para detectar cambios de estado.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import date, datetime

# Estados normalizados del PROCEDIMIENTO (no del triaje humano, que vive en
# `db.ESTADOS_REVISION`). Cada fuente tiene su propia nomenclatura y se mapea a estos.
#
# Los valores son ASCII y en minúsculas a propósito: viajan como parámetro de consulta
# desde la interfaz, se comparan en SQL y se pintan crudos en la ficha y en la analítica.
# Una diéresis ahí no aporta nada y sí da problemas.
ESTADO_PREVIO = "vorinformation"        # Vorinformation / Markterkundung
ESTADO_PUBLICADA = "veroeffentlicht"    # en plazo o pendiente de resolver
ESTADO_EVALUACION = "in_wertung"        # cerrado el plazo, en valoración
ESTADO_ADJUDICADA = "zuschlag"          # hay adjudicación
ESTADO_FORMALIZADA = "vergeben"         # contrato firmado
ESTADO_RESUELTA = "aufgehoben"          # Aufhebung: se anula sin adjudicar
ESTADO_ANULADA = "zurueckgezogen"       # el anuncio se retira
ESTADO_DESCONOCIDO = "unbekannt"

ESTADOS_VIVOS = {ESTADO_PREVIO, ESTADO_PUBLICADA, ESTADO_EVALUACION}


def _texto(valor) -> str | None:
    if valor is None:
        return None
    s = re.sub(r"\s+", " ", str(valor)).strip()
    return s or None


def _fecha_iso(valor) -> str | None:
    """Normaliza a ISO-8601. Acepta date, datetime y las variantes de las fuentes."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, datetime):
        return valor.isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    s = str(valor).strip()
    if not s:
        return None
    # Socrata: 2026-03-13T13:00:00.000 · TED/CODICE: 2026-03-13T13:00:00+01:00
    s = s.replace("Z", "+00:00")
    for patron in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(s, patron).isoformat()
        except ValueError:
            continue
    try:  # último recurso: el parser flexible de la stdlib
        return datetime.fromisoformat(s).isoformat()
    except ValueError:
        return None


def plegar_ss(texto: str | None) -> str:
    """`Maßnahmen` -> `Massnahmen`. Lo que hace comparable la ß en FTS5.

    `normalizar()` ya lo hace por su cuenta con `casefold()`, pero el índice de texto
    completo no pasa por ahí: se alimenta del texto crudo y lo tokeniza SQLite con
    `unicode61 remove_diacritics 2`, que pliega las diéresis pero no la ß, porque no es
    un diacrítico sino una letra. Se aplica a los dos lados —a lo que se indexa y a lo
    que se busca— para que sigan siendo simétricos.
    """
    if not texto:
        return ""
    return texto.replace("ß", "ss").replace("ẞ", "SS")


def normalizar(texto: str | None) -> str:
    """Minúsculas sin diéresis y con la ß plegada, para que 'Maßnahmen' == 'Massnahmen'.

    Vive aquí, y no en el motor de reglas, porque el resultado se guarda con cada
    licitación: normalizar sobre la marcha costaba 8,7 segundos por cada evaluación
    de los perfiles sobre 127.000 registros, y era el 100% del tiempo.

    `casefold()` en lugar de `lower()` no es cosmético: es lo único que pliega la ß. El
    NFD se lleva las diéresis —`ä`→`a`, `ü`→`u`— porque son marcas combinables, pero la
    ß es un carácter propio y sin descomposición, así que con `lower()` sobrevive y
    `Maßnahmen` y `Massnahmen` quedan como dos términos distintos. Las dos grafías
    circulan en pliegos reales (Suiza y Austria escriben siempre `ss`), y además un
    título en mayúsculas escribe `MASSNAHMEN`, que es la tercera. Sin esto habría que
    listar cada término con ß por duplicado, para siempre, y el día que a alguien se le
    olvidara se perderían anuncios sin que saltara nada.

    Lo que NO se hace es mapear `ü`→`ue`: rompería la simetría, porque quien escribiera
    `Prufung` en un perfil dejaría de casar con `Prüfung`. Eso es transliteración de
    teclado, no un diacrítico, y el Bekanntmachungsservice publica UTF-8 con las
    diéresis correctas.
    """
    if not texto:
        return ""
    descompuesto = unicodedata.normalize("NFD", texto.casefold())
    sin_acentos = "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", sin_acentos)


def titulo_util_ted(titulo: str | None) -> str:
    """Quita el prefijo genérico de los títulos de TED.

    TED titula «España – <etiqueta del CPV> – <título real>», y los dos primeros
    tramos son idénticos en cientos de anuncios. Usar el título completo para
    agrupar fusionaba licitaciones que no tienen nada que ver: se midieron 247
    grupos con objetos distintos metidos en el mismo saco, lo que **ocultaba**
    licitaciones de la bandeja. Peor que un duplicado.
    """
    if not titulo:
        return ""
    tramos = [t.strip() for t in re.split(r"\s+[–—-]\s+", titulo) if t.strip()]
    if len(tramos) >= 3:
        return " ".join(tramos[2:])
    if len(tramos) == 2:
        return tramos[1]
    return titulo.strip()


def duracion_a_meses(valor, unidad: str | None) -> float | None:
    """Convierte una duración a meses. La unidad NO es opcional interpretarla.

    PLACSP usa `unitCode` con `MON`, `DAY` y `ANN`; TED usa `MONTH` y `YEAR`. Un «48»
    sin mirar la unidad son cuatro años o mes y medio, así que si la unidad no se
    reconoce se asume meses —de largo la más frecuente— y nunca días.
    """
    if valor in (None, ""):
        return None
    try:
        n = float(str(valor).strip().replace(",", "."))
    except ValueError:
        return None
    if n <= 0:
        return None
    u = (unidad or "").strip().upper()
    if u in ("DAY", "DAYS", "D", "DIA", "DIAS"):
        return round(n / 30.44, 2)
    if u in ("ANN", "YEAR", "YEARS", "Y", "A", "ANIO", "ANIOS"):
        return n * 12
    if u in ("WEE", "WEEK", "WEEKS", "W"):
        return round(n / 4.35, 2)
    return n  # MON / MONTH / desconocida


def _sumar_meses(fecha_iso: str | None, meses: float | None) -> str | None:
    if not fecha_iso or not meses:
        return None
    try:
        base = datetime.fromisoformat(fecha_iso).date()
    except ValueError:
        try:
            base = date.fromisoformat(fecha_iso[:10])
        except ValueError:
            return None
    total = base.month - 1 + int(round(meses))
    anio = base.year + total // 12
    mes = total % 12 + 1
    dia = min(base.day, [31, 29 if anio % 4 == 0 and (anio % 100 != 0 or anio % 400 == 0)
                         else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mes - 1])
    return date(anio, mes, dia).isoformat()


def _importe(valor) -> float | None:
    if valor is None or valor == "":
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = str(valor).strip().replace("€", "").replace(" ", "")
    if not s:
        return None
    # "1.234.567,89" (es) frente a "1234567.89" (fuentes en formato máquina)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def normalizar_cpv(valor) -> list[str]:
    """Devuelve códigos CPV de 8 dígitos, sin el dígito de control tras el guion."""
    if not valor:
        return []
    bruto = valor if isinstance(valor, (list, tuple, set)) else re.split(r"[;,\s|]+", str(valor))
    salida: list[str] = []
    for item in bruto:
        if not item:
            continue
        m = re.search(r"(\d{8})", str(item))
        if m and m.group(1) not in salida:
            salida.append(m.group(1))
    return salida


@dataclass
class Licitacion:
    """Una licitación (o un lote de una licitación) ya normalizada."""

    fuente: str
    id_externo: str

    expediente: str | None = None
    organo: str | None = None
    nif_organo: str | None = None
    objeto: str | None = None
    descripcion: str | None = None
    cpv: list[str] = field(default_factory=list)

    importe_sin_iva: float | None = None
    valor_estimado: float | None = None
    procedimiento: str | None = None
    tipo_contrato: str | None = None
    estado: str = ESTADO_DESCONOCIDO
    estado_origen: str | None = None

    fecha_publicacion: str | None = None
    fecha_limite_presentacion: str | None = None
    fecha_actualizacion: str | None = None

    nuts: str | None = None
    lugar: str | None = None
    bundesland: str | None = None

    url_detalle: str | None = None
    urls_pliegos: list[str] = field(default_factory=list)

    lote_num: str | None = None
    lote_desc: str | None = None

    adjudicatario: str | None = None
    importe_adjudicacion: float | None = None
    fecha_adjudicacion: str | None = None

    # Para saber cuándo vence el contrato del incumbente y llegar antes de que se
    # renueve. `fecha_fin_prevista` queda a None si no hay forma de saberlo: más vale
    # no tener el dato que tener uno inventado sobre el que llamar a un cliente.
    duracion_meses: float | None = None
    fecha_inicio_ejecucion: str | None = None
    fecha_fin_prevista: str | None = None

    # Identificador del PROCEDIMIENTO, que no cambia entre los anuncios que genera: la
    # licitación, sus correcciones y la adjudicación llevan el mismo. El
    # Bekanntmachungsservice lo publica como `ocid` y es un identificador de verdad, así
    # que agrupar sale exacto en vez de por heurística sobre el expediente y el título.
    id_procedimiento: str | None = None
    clave_grupo: str | None = None

    raw: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.expediente = _texto(self.expediente)
        self.organo = _texto(self.organo)
        self.nif_organo = _texto(self.nif_organo)
        self.objeto = _texto(self.objeto)
        self.descripcion = _texto(self.descripcion)
        self.procedimiento = _texto(self.procedimiento)
        self.tipo_contrato = _texto(self.tipo_contrato)
        self.lugar = _texto(self.lugar)
        self.lote_desc = _texto(self.lote_desc)
        self.adjudicatario = _texto(self.adjudicatario)
        self.cpv = normalizar_cpv(self.cpv)
        self.importe_sin_iva = _importe(self.importe_sin_iva)
        self.valor_estimado = _importe(self.valor_estimado)
        self.importe_adjudicacion = _importe(self.importe_adjudicacion)
        self.fecha_publicacion = _fecha_iso(self.fecha_publicacion)
        self.fecha_limite_presentacion = _fecha_iso(self.fecha_limite_presentacion)
        self.fecha_actualizacion = _fecha_iso(self.fecha_actualizacion)
        self.fecha_adjudicacion = _fecha_iso(self.fecha_adjudicacion)
        self.fecha_inicio_ejecucion = _fecha_iso(self.fecha_inicio_ejecucion)
        self.fecha_fin_prevista = _fecha_iso(self.fecha_fin_prevista)
        self.urls_pliegos = [u for u in dict.fromkeys(self.urls_pliegos or []) if u]

        # Si la fuente no publica fecha de fin, se deduce del inicio (o, en su
        # defecto, de la adjudicación) más la duración.
        if not self.fecha_fin_prevista and self.duracion_meses:
            arranque = self.fecha_inicio_ejecucion or self.fecha_adjudicacion
            self.fecha_fin_prevista = _sumar_meses(arranque, self.duracion_meses)

        if not self.clave_grupo:
            self.clave_grupo = self._clave_grupo()

    def _clave_grupo(self) -> str:
        """Agrupa los anuncios de un mismo expediente a lo largo de su vida.

        Una licitación genera varios anuncios (licitación, corrección, adjudicación
        por lotes) y cada uno es un registro distinto.

        Cuando la fuente publica un identificador de procedimiento se usa ese y se acabó
        la heurística: es el caso del Bekanntmachungsservice, que da el `ocid`. Si no lo
        hay se cae al expediente, y en TED al título, porque su «expediente» es el número
        de publicación y es único por anuncio.
        """
        organo = re.sub(r"[^a-z0-9]+", "", (self.organo or "").lower())[:40]
        if self.id_procedimiento:
            ancla = re.sub(r"[^a-z0-9]+", "", self.id_procedimiento.lower())[:64]
        elif self.fuente.startswith("ted"):
            ancla = re.sub(r"[^a-z0-9]+", "", titulo_util_ted(self.objeto).lower())[:80]
        else:
            ancla = re.sub(r"[^a-z0-9]+", "", (self.expediente or self.objeto or "").lower())[:40]
        if not ancla:
            return f"{self.fuente}:{self.id_externo}"
        return f"{organo}|{ancla}"

    @property
    def clave(self) -> str:
        return f"{self.fuente}:{self.id_externo}"

    @property
    def texto_busqueda(self) -> str:
        """Lo que se indexa en FTS5, para la caja de búsqueda libre de la bandeja.

        Lleva el expediente y el nombre del órgano a propósito: buscar «Ayuntamiento de
        Viladecans» o un número de expediente es exactamente lo que se espera de una
        caja de búsqueda. Lo que NO puede llevarlos es el texto sobre el que corren las
        reglas; para eso está `texto_reglas`.
        """
        partes = [self.objeto, self.descripcion, self.lote_desc, self.expediente, self.organo]
        return " \n".join(p for p in partes if p)

    @property
    def texto_reglas(self) -> str:
        """Sobre lo que corre el motor de reglas: el CONTRATO, no quién lo saca.

        Durante un tiempo esto y `texto_busqueda` eran el mismo campo, y el nombre del
        organismo contratante entraba en el matching. En España los organismos se
        llaman «Instituto Nacional de CIBERSEGURIDAD», «Departament d'Educació i
        FORMACIÓ Professional», «Fundación Estatal para la FORMACIÓN en el Empleo» o
        «ENS d'Abastament d'Aigua Ter-Llobregat», así que el nombre regalaba el término
        débil, el contexto requerido, o los dos.

        Medido antes de arreglarlo: de las 535 coincidencias del perfil de
        concienciación, 125 —el 23 %, una de cada cuatro fichas de la bandeja— entraban
        solo por el nombre del órgano. Entre ellas, «Formación en Gestión de Proyectos
        Europeos y soft-skills» del INCIBE, formación en riesgos laborales de una
        empresa de aguas y obras de un módulo prefabricado. Ninguna de las 125 llevaba
        un término fuerte, así que quitar el órgano no costó ni un verdadero positivo.

        El expediente también se queda fuera: es un código, y lo que casara dentro de
        «F260000477_492» sería casualidad, no señal.
        """
        partes = [self.objeto, self.descripcion, self.lote_desc]
        return " \n".join(p for p in partes if p)

    @property
    def importe_referencia(self) -> float | None:
        """El importe más representativo disponible, para filtrar por tamaño."""
        for valor in (self.valor_estimado, self.importe_sin_iva, self.importe_adjudicacion):
            if valor:
                return valor
        return None

    def huella(self) -> str:
        """Hash del contenido relevante: si cambia, es una versión nueva.

        Deliberadamente NO incluye `fecha_actualizacion` ni `raw`. PLACSP republica
        una licitación cada vez que se toca cualquier cosa, y sin esto el equipo
        recibiría la misma alerta ocho veces por la misma licitación.
        """
        relevante = {
            k: v
            for k, v in asdict(self).items()
            if k not in {"raw", "fecha_actualizacion"}
        }
        serializado = json.dumps(relevante, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(serializado.encode("utf-8")).hexdigest()

    def a_fila(self) -> dict:
        d = asdict(self)
        d["cpv"] = " ".join(self.cpv)
        d["urls_pliegos"] = json.dumps(self.urls_pliegos, ensure_ascii=False)
        d["raw"] = json.dumps(self.raw, ensure_ascii=False, default=str)
        d["huella"] = self.huella()
        # Se calcula pero NO es una columna de `licitaciones`: `db.guardar()` lo usa
        # para alimentar el índice FTS5, donde ya se guarda entero. Duplicarlo en la
        # tabla eran 1,4 kB por fila que no leía nadie.
        d["texto_busqueda"] = self.texto_busqueda
        # Ya normalizado, que es lo que permite pasarle `ya_normalizado=True` a
        # `evaluar()`: normalizar al vuelo era el 100% del tiempo de cada evaluación.
        #
        # Solo se guarda el del motor de reglas. Hubo un segundo campo, `texto_norm`, con
        # el texto de búsqueda normalizado; lo consumía una única función —la
        # previsualización de los ajustes— y encima era un defecto, porque hacía que «Ver
        # qué cambiaría» evaluase un texto DISTINTO del que evaluaría al guardar. La
        # búsqueda libre no lo usaba: esa va por FTS5.
        d["texto_reglas_norm"] = normalizar(self.texto_reglas)
        d["importe_referencia"] = self.importe_referencia
        return d
