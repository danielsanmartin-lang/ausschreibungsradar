"""Conector de TED (Tenders Electronic Daily), la publicación oficial de la UE.

Usa la Search API v3, que no requiere autenticación. Detalles que cuestan tiempo
descubrir y que conviene no volver a averiguar:

- Endpoint: POST https://api.ted.europa.eu/v3/notices/search
- El parámetro `scope` REDUCE la lista de campos admitidos (con `scope: ACTIVE`
  casi todos dan HTTP 400). Se omite a propósito.
- No existe parámetro `sort`; el orden se controla acotando `publication-date`.
- Las fechas en expert search van sin guiones: `publication-date>=20240101`.
- `notice-title` y `buyer-name` son diccionarios por idioma (`spa`, `eng`, ...) y
  sus valores son listas.
- Máximo 250 resultados por página y 15.000 por consulta.

TED se solapa bastante con PLACSP para España (lo de encima de umbral europeo sale
en los dos). Se mantiene porque cubre organismos cuyo registro en PLACSP es pobre
y porque sirve de red de seguridad si la sindicación de PLACSP se rompe.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterator

from .. import net, progreso
from ..model import (
    ESTADO_ADJUDICADA, ESTADO_DESCONOCIDO, ESTADO_PREVIO, ESTADO_PUBLICADA,
    Licitacion, duracion_a_meses,
)
from .base import bundesland_desde_nuts

log = logging.getLogger(__name__)

URL = "https://api.ted.europa.eu/v3/notices/search"
POR_PAGINA = 250
MAX_RESULTADOS = 15000

CAMPOS = [
    "publication-number", "notice-identifier", "notice-title", "description-proc",
    "description-lot", "buyer-name", "buyer-country", "classification-cpv",
    "publication-date", "dispatch-date", "deadline-receipt-request", "procedure-type",
    "contract-nature", "notice-type", "total-value", "estimated-value-proc",
    "estimated-value-cur-proc", "winner-name", "winner-decision-date", "links",
    # Duración: viene por lote y en listas paralelas valor/unidad. Solo la trae
    # alrededor del 25% de los avisos, así que es un extra, no algo con lo que contar.
    "duration-period-value-lot", "duration-period-unit-lot",
]


def _duracion(aviso: dict) -> float | None:
    """Duración del contrato en meses, la más larga de sus lotes.

    `duration-period-value-lot` y `duration-period-unit-lot` son listas paralelas, una
    entrada por lote. Se toma la mayor porque es la que marca cuándo queda libre el
    contrato. Cuando falta la unidad se asume meses: es la unidad de la gran mayoría
    de los avisos, y asumir días convertiría un contrato de 2 años en 24 días.
    """
    valores = aviso.get("duration-period-value-lot")
    if not valores:
        return None
    if not isinstance(valores, list):
        valores = [valores]
    unidades = aviso.get("duration-period-unit-lot") or []
    if not isinstance(unidades, list):
        unidades = [unidades]

    meses = []
    for i, valor in enumerate(valores):
        unidad = unidades[i] if i < len(unidades) else (unidades[0] if unidades else None)
        convertido = duracion_a_meses(valor, unidad)
        if convertido:
            meses.append(convertido)
    return max(meses) if meses else None

# TED responde en inglés aunque se pidan los textos en español. Se traduce lo que
# se muestra en la bandeja para no mezclar idiomas en la misma columna.
PROCEDIMIENTOS = {
    "open": "Abierto",
    "restricted": "Restringido",
    "neg-w-call": "Negociado con publicidad",
    "neg-wo-call": "Negociado sin publicidad",
    "comp-dial": "Diálogo competitivo",
    "innovation": "Asociación para la innovación",
    "comp-tend": "Licitación con negociación",
    "oth-mult": "Otro (varias fases)",
    "oth-single": "Otro (fase única)",
    "negotiated": "Negociado",
}

TIPOS_CONTRATO = {
    "services": "Servicios",
    "supplies": "Suministros",
    "works": "Obras",
    "combined": "Mixto",
}


def _traducir(valor: str | None, tabla: dict) -> str | None:
    if not valor:
        return None
    partes = [tabla.get(p.strip(), p.strip()) for p in valor.split(" · ")]
    return " · ".join(dict.fromkeys(partes))


# notice-type -> estado normalizado. Los tipos de TED son muchos; se agrupan por
# prefijo, que es lo que distingue anuncio previo / licitación / adjudicación.
def _estado(notice_type: str | None) -> str:
    if not notice_type:
        return ESTADO_DESCONOCIDO
    t = notice_type.lower()
    if t.startswith("pin") or "prior" in t:
        return ESTADO_PREVIO
    if t.startswith("can") or "award" in t or t.startswith("car"):
        return ESTADO_ADJUDICADA
    if t.startswith("cn") or t.startswith("subco") or "competition" in t:
        return ESTADO_PUBLICADA
    return ESTADO_DESCONOCIDO


def _idioma(valor, preferidos=("spa", "eng")) -> str | None:
    """Extrae texto de los campos multilingües de TED."""
    if valor is None:
        return None
    if isinstance(valor, str):
        return valor
    if isinstance(valor, list):
        # dict.fromkeys deduplica manteniendo el orden: contract-nature llega a
        # veces como ["services", "services"] y se mostraba "services · services".
        unicos = list(dict.fromkeys(str(v) for v in valor if v))
        return " · ".join(unicos) or None
    if isinstance(valor, dict):
        for clave in preferidos:
            if valor.get(clave):
                return _idioma(valor[clave], preferidos)
        for v in valor.values():
            texto = _idioma(v, preferidos)
            if texto:
                return texto
    return None


def _primero(valor):
    if isinstance(valor, list):
        return valor[0] if valor else None
    return valor


def construir_consulta(
    *, cpv: list[str], terminos: list[str], desde: date | None, pais: str = "DEU"
) -> str:
    """Consulta en la sintaxis de expert search de TED.

    Se pregunta por CPV **o** por texto libre a propósito: en este nicho el CPV
    está mal puesto la mayoría de las veces, así que restringir solo por CPV
    perdería la mayor parte de lo interesante.
    """
    partes = [f"(buyer-country={pais})"]
    if desde:
        partes.append(f"(publication-date>={desde.strftime('%Y%m%d')})")

    alternativas = []
    if cpv:
        alternativas.append(f"(classification-cpv IN ({' '.join(cpv)}))")
    if terminos:
        ors = " OR ".join(f'FT~"{t}"' for t in terminos)
        alternativas.append(f"({ors})")
    if alternativas:
        partes.append("(" + " OR ".join(alternativas) + ")")

    return " AND ".join(partes)


def parsear_aviso(aviso: dict) -> Licitacion | None:
    numero = aviso.get("publication-number")
    if not numero:
        return None

    enlaces = aviso.get("links") or {}
    url_detalle = None
    if isinstance(enlaces, dict):
        html = enlaces.get("html") or {}
        if isinstance(html, dict):
            url_detalle = html.get("SPA") or html.get("ENG") or next(iter(html.values()), None)
    if not url_detalle:
        url_detalle = f"https://ted.europa.eu/es/notice/-/detail/{numero}"

    pdfs = []
    if isinstance(enlaces, dict) and isinstance(enlaces.get("pdf"), dict):
        pdf_es = enlaces["pdf"].get("SPA") or enlaces["pdf"].get("ENG")
        if pdf_es:
            pdfs.append(pdf_es)

    nuts = None  # TED no expone place-performance en los campos admitidos de v3

    return Licitacion(
        fuente="ted",
        id_externo=numero,
        # El número de publicación (p.ej. 580823-2024) es la referencia que sirve
        # para buscar el aviso; `notice-identifier` es un UUID interno.
        expediente=numero,
        organo=_idioma(aviso.get("buyer-name")),
        objeto=_idioma(aviso.get("notice-title")),
        descripcion=_idioma(aviso.get("description-proc")) or _idioma(aviso.get("description-lot")),
        cpv=aviso.get("classification-cpv") or [],
        importe_sin_iva=aviso.get("total-value"),
        valor_estimado=_primero(aviso.get("estimated-value-proc")),
        procedimiento=_traducir(_idioma(aviso.get("procedure-type")), PROCEDIMIENTOS),
        tipo_contrato=_traducir(_idioma(aviso.get("contract-nature")), TIPOS_CONTRATO),
        estado=_estado(aviso.get("notice-type")),
        estado_origen=aviso.get("notice-type"),
        fecha_publicacion=(aviso.get("publication-date") or "").split("+")[0].rstrip("Z") or None,
        fecha_limite_presentacion=_primero(aviso.get("deadline-receipt-request")),
        nuts=nuts,
        bundesland=bundesland_desde_nuts(nuts),
        url_detalle=url_detalle,
        urls_pliegos=pdfs,
        adjudicatario=_idioma(aviso.get("winner-name")),
        fecha_adjudicacion=_primero(aviso.get("winner-decision-date")),
        duracion_meses=_duracion(aviso),
        raw={
            "notice_type": aviso.get("notice-type"),
            "notice_identifier": _idioma(aviso.get("notice-identifier")),
        },
    )


class FuenteTED:
    nombre = "ted"

    def __init__(self, cpv: list[str], terminos: list[str], *, pais: str = "DEU",
                 dias_ventana: int = 30):
        self.cpv = cpv
        self.terminos = terminos
        self.pais = pais
        self.dias_ventana = dias_ventana
        self._cursor_nuevo: str | None = None

    def _consultar(self, desde: date | None) -> Iterator[Licitacion]:
        consulta = construir_consulta(
            cpv=self.cpv, terminos=self.terminos, desde=desde, pais=self.pais
        )
        log.info("TED: %s", consulta)
        pagina, traidos = 1, 0
        while True:
            progreso.pagina()
            respuesta = net.post_json(
                URL,
                {
                    "query": consulta,
                    "fields": CAMPOS,
                    "limit": POR_PAGINA,
                    "page": pagina,
                    "onlyLatestVersions": True,
                },
            )
            avisos = respuesta.get("notices") or []
            total = respuesta.get("totalNoticeCount", 0)
            if pagina == 1:
                log.info("TED: %d avisos coinciden", total)
                if total > MAX_RESULTADOS:
                    log.warning(
                        "TED devuelve %d avisos y el máximo recuperable es %d: "
                        "acota la ventana de fechas o los términos.",
                        total, MAX_RESULTADOS,
                    )
            if not avisos:
                break

            for aviso in avisos:
                lic = parsear_aviso(aviso)
                if lic is not None:
                    yield lic
            traidos += len(avisos)

            if traidos >= min(total, MAX_RESULTADOS):
                break
            pagina += 1

    def incremental(self, cursor: str | None) -> Iterator[Licitacion]:
        if cursor:
            try:
                desde = date.fromisoformat(cursor) - timedelta(days=3)  # solape de seguridad
            except ValueError:
                desde = date.today() - timedelta(days=self.dias_ventana)
        else:
            desde = date.today() - timedelta(days=self.dias_ventana)
        self._cursor_nuevo = date.today().isoformat()
        yield from self._consultar(desde)

    def historico(self, anio: int) -> Iterator[Licitacion]:
        self._cursor_nuevo = date.today().isoformat()
        # La consulta acota por año usando el mismo campo de fecha.
        consulta = construir_consulta(
            cpv=self.cpv, terminos=self.terminos, desde=date(anio, 1, 1), pais=self.pais
        ) + f" AND (publication-date<={anio}1231)"
        log.info("TED histórico %s: %s", anio, consulta)
        pagina, traidos = 1, 0
        while True:
            progreso.pagina()
            respuesta = net.post_json(
                URL,
                {"query": consulta, "fields": CAMPOS, "limit": POR_PAGINA,
                 "page": pagina, "onlyLatestVersions": True},
            )
            avisos = respuesta.get("notices") or []
            if not avisos:
                break
            for aviso in avisos:
                lic = parsear_aviso(aviso)
                if lic is not None:
                    yield lic
            traidos += len(avisos)
            if traidos >= min(respuesta.get("totalNoticeCount", 0), MAX_RESULTADOS):
                break
            pagina += 1

    def cursor_nuevo(self) -> str | None:
        return self._cursor_nuevo
