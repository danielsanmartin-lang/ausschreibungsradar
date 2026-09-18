"""Conector del Bekanntmachungsservice (oeffentlichevergabe.de).

Es el servicio del Beschaffungsamt del Ministerio del Interior alemán y, a efectos
prácticos, la única fuente que hace falta: desde el 25/10/2023 publicar ahí es
obligatorio por encima del umbral europeo (VgV §10a Abs. 5), y por debajo llegan además
las plataformas de los Länder —DTVP, evergabe-online, subreport, RIB, vergabe.nrw,
Autobahn, Deutsche Bahn— y el relevo de service.bund.de. No hay que escribir un conector
por región, que es lo que sí exige España.

La API es un único endpoint que devuelve **un ZIP con un fichero por anuncio**:

    GET /api/notice-exports?pubDay=2026-09-15
    GET /api/notice-exports?pubMonth=2026-08

El formato se elige por cabecera `Accept` y NO por parámetro de la URL; sin la cabecera
correcta contesta 406 (con la lista de tipos aceptables en el cuerpo, que es una ayuda
real). No admite rangos de fechas —`pubDayFrom` da 400— ni filtrar por CPV en servidor:
se descarga todo y se filtra en local.

**Se leen dos formatos, y no es por gusto.** El OCDS trae el cuerpo completo de los
anuncios pero NO la fecha límite de presentación (medido: 0 de 857 en un día). La trae el
XML eForms, y con buena cobertura (420 de 857, el 83 % de las licitaciones vivas), pero
ese no trae el cuerpo normalizado. Así que el conector baja OCDS siempre, y eForms solo
cuando se le pide (`con_eforms`), que es la ventana reciente: un plazo que ya venció no
vale para nada, y bajar eForms de dos años sería el 69 % del tráfico por un dato caducado.

Los nombres de fichero coinciden exactamente entre los dos formatos —comprobado sobre un
día entero: 857 y 857, cero huérfanos—, así que el cruce es por nombre y no hay que
interpretar ids.
"""

from __future__ import annotations

import io
import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

from .. import net, progreso, rutas
from ..model import (
    ESTADO_ADJUDICADA, ESTADO_DESCONOCIDO, ESTADO_FORMALIZADA, ESTADO_PREVIO,
    ESTADO_PUBLICADA, ESTADO_RESUELTA, Licitacion, duracion_a_meses,
)
from .base import bundesland_desde_nuts

log = logging.getLogger(__name__)

BASE = "https://oeffentlichevergabe.de/api/notice-exports"
FICHA = "https://oeffentlichevergabe.de/ui/de/search/details?noticeId="

# El formato va por `Accept`. Sin esto: 406.
ACEPTA = {
    "ocds": "application/vnd.bekanntmachungsservice.ocds.zip+zip",
    "eforms": "application/vnd.bekanntmachungsservice.eforms.zip+zip",
}

# `procurementMethodDetails` llega EN INGLÉS aunque todo lo demás venga en alemán. Se
# traduce aquí, en el conector, y no en las consultas: así sale ya en alemán en la
# bandeja, en la ficha y en la analítica, en vez de solo en el gráfico. El original queda
# igualmente guardado en `estado_origen`.
#
# Los tres últimos son de bajo umbral (UVgO / VOB/A) y su nombre legal exacto está por
# contrastar contra la lista de códigos de eForms (BT-105). Mientras tanto, lo que no
# esté en esta tabla se deja pasar en inglés a propósito: un valor en inglés a la vista es
# un aviso de que falta un mapeo; uno mal traducido es una mentira silenciosa.
PROCEDIMIENTOS = {
    "open": "Offenes Verfahren",
    "restricted": "Nichtoffenes Verfahren",
    "restricted tender": "Beschränkte Ausschreibung",
    "public announcement": "Öffentliche Ausschreibung",
    "negotiated with prior publication of a call for competition / "
    "competitive with negotiation": "Verhandlungsverfahren mit Teilnahmewettbewerb",
    "negotiated without prior call for competition":
        "Verhandlungsverfahren ohne Teilnahmewettbewerb",
    "competitive dialogue": "Wettbewerblicher Dialog",
    "innovation partnership": "Innovationspartnerschaft",
    "negotiated award with public participation competition":
        "Verhandlungsvergabe mit Teilnahmewettbewerb",
    "direct award with public participation competition":
        "Beschränkte Ausschreibung mit Teilnahmewettbewerb",
    "other single stage procedure": "Sonstiges einstufiges Verfahren",
    "other multiple stage procedure": "Sonstiges mehrstufiges Verfahren",
    "direct award": "Direktauftrag",
    "restricted tender with public participation competition":
        "Beschränkte Ausschreibung mit Teilnahmewettbewerb",
    "restricted procurement with prior call for competition":
        "Beschränkte Vergabe mit Teilnahmewettbewerb",
    "restricted procurement without prior call for competition":
        "Beschränkte Vergabe ohne Teilnahmewettbewerb",
    "competitive tendering (article 5(3) of regulation 1370/2007)":
        "Wettbewerbliches Vergabeverfahren (Art. 5 Abs. 3 VO 1370/2007)",
}

TIPOS_CONTRATO = {
    "works": "Bauleistungen",
    "services": "Dienstleistungen",
    "supplies": "Lieferleistungen",
}

# Tope de enlaces que se guardan por anuncio. Medido: la media es 0,8 documentos, pero hay
# anuncios con 136. `urls_pliegos` ya era la columna de texto más pesada de la base
# española (530 B por fila), y una lista de 136 URLs de portal no ayuda a nadie a decidir.
MAX_PLIEGOS = 5

# Tope de caracteres de la descripción. Medido sobre 22.892 anuncios: la mediana son 128
# caracteres y la media 537, pero la cola llega a 16.571 y el 8 % pasa de 1.500. El texto
# se guarda tres veces —`descripcion`, `texto_reglas_norm` y el contenido del índice
# FTS5—, así que la cola larga se paga multiplicada. A 2.000 se recorta el 5 % de los
# anuncios y se corta la cola: un término del perfil que solo aparezca a partir del
# carácter 2.000 de un pliego no es la señal que busca esta herramienta.
MAX_DESCRIPCION = 2000

# Tope del texto de los lotes juntos. Un anuncio con treinta lotes concatena las treinta
# descripciones, y medido sobre una semana real el peor llegaba a 156.773 caracteres
# (p50=297, p95=4.034). Eso se paga TRES veces —en `lote_desc`, en `texto_reglas_norm` y
# dentro del índice FTS5—, así que la cola larga multiplica. A 4.000 se recorta el 5 % de
# los anuncios con lotes y se corta la cola de raíz.
MAX_LOTES = 4000


def _hoy_berlin() -> date:
    """El día de hoy en Alemania, no en el portátil de quien lanza esto.

    Importa porque la caché guarda para siempre los tramos ya CERRADOS. Con el Mac en
    otro huso, «hoy» en local puede ser un día que en Berlín todavía no ha terminado, y
    entonces se cachearía un día incompleto y no se volvería a pedir nunca. Es la misma
    clase de fallo que tenía el ZIP del año en curso de PLACSP, con otra cara.
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Berlin")).date()
    except Exception:  # noqa: BLE001 - sin base de datos de husos (tzdata), se usa local
        return date.today()


def _recortar(texto: str | None, tope: int) -> str | None:
    """Corta por el último espacio antes del tope, para no partir una palabra."""
    if not texto:
        return None
    if len(texto) <= tope:
        return texto
    return texto[:tope].rsplit(" ", 1)[0] + "…"


def _texto_lote(lote: dict) -> str:
    partes = [lote.get("title"), lote.get("description")]
    return " · ".join(p for p in partes if p)


def _sin_desplazamiento(valor: str | None) -> str | None:
    """`2026-10-15+02:00` -> `2026-10-15`.

    Se quita a propósito, no se conserva. Por dos razones duras: `model._fecha_iso` no
    tiene patrón para `%Y-%m-%d%z` y `datetime.fromisoformat` no traga esa forma por
    debajo de Python 3.11; y mezclar en la misma columna TEXT valores con y sin
    desplazamiento rompe las comparaciones lexicográficas de fechas que hace
    `consultas.py`. Alemania es un solo huso: se guarda hora de pared.
    """
    if not valor:
        return None
    return re.sub(r"(?:Z|[+-]\d{2}:?\d{2})$", "", valor.strip()) or None


def _importe_eur(valor: dict | None) -> float | None:
    """El importe solo si está en euros.

    Medido: de 1.788 anuncios con importe, uno venía en USN. Es una rareza, pero guardar
    un número sin su moneda en una columna que después se suma y se ordena es la forma
    más barata de que la analítica mienta.
    """
    if not valor or valor.get("amount") is None:
        return None
    if (valor.get("currency") or "EUR").upper() != "EUR":
        return None
    return valor["amount"]


def _estado(release: dict) -> str:
    """El estado del procedimiento, deducido del `tag` y de los `awards`.

    `tender.status` NO se usa porque el servicio no lo publica: comprobado sobre los
    22.892 anuncios de un mes, viene vacío en todos.

    Lo que sí hay es un `tag` OCDS y, en las adjudicaciones, el estado de cada `award`.
    Un `unsuccessful` es una *Aufhebung* —el procedimiento se anula sin adjudicar— y son
    393 al mes: darlas por vivas sería el peor fallo posible aquí, porque manda a alguien
    a preparar una oferta para algo que ya no existe.

    Deliberadamente NO se deduce «en valoración» comparando el plazo con hoy. El estado
    entra en `huella()` y en `CAMPOS_HISTORIAL`, así que cada re-ingesta de un anuncio
    cuyo plazo acaba de pasar escribiría una versión en el historial que no corresponde a
    ningún cambio en origen. La bandeja ya sabe filtrar por plazo; que lo haga la vista.
    """
    tags = set(release.get("tag") or [])
    if "planning" in tags:
        return ESTADO_PREVIO

    premios = release.get("awards") or []
    estados = {a.get("status") for a in premios if a.get("status")}
    if premios and estados and estados == {"unsuccessful"}:
        return ESTADO_RESUELTA
    if release.get("contracts"):
        return ESTADO_FORMALIZADA
    if premios or "award" in tags:
        return ESTADO_ADJUDICADA
    if "tender" in tags:
        return ESTADO_PUBLICADA
    # Sin `tag` y sin adjudicación. Comprobado contra el eForms del mismo día: son
    # ContractNotice con plazo de presentación, o sea licitaciones vivas.
    if not tags:
        return ESTADO_PUBLICADA
    return ESTADO_DESCONOCIDO


def _comprador(release: dict) -> tuple[str | None, dict]:
    """Nombre del comprador y su ficha completa de `parties`.

    `buyer` en OCDS es solo una referencia (id y nombre); la entrada de `parties` con rol
    `buyer` es la que trae dirección y NUTS. Y se prefiere el comprador a
    `procuringEntity`: la central de compras que tramita el expediente no es quien compra,
    y para vender importa quién compra.
    """
    ref = release.get("buyer") or {}
    compradores = [p for p in (release.get("parties") or [])
                   if "buyer" in (p.get("roles") or [])]
    if compradores:
        # El rol manda; el id solo desempata cuando hay más de un comprador. Y se mira
        # tanto `id` como `identifier.id` porque NO siempre son la misma cosa: hay
        # anuncios donde `buyer.id` es el número de registro mercantil («HRB 20680»)
        # mientras que en `parties` el id es el local del anuncio («ORG-0001»). Cruzando
        # solo por `id`, el comprador no se encontraba y se perdía su NUTS, que es de
        # donde sale el Bundesland cuando el lugar de ejecución no lo trae.
        elegido = compradores[0]
        if len(compradores) > 1 and ref.get("id"):
            claves = {ref.get("id"), (ref.get("identifier") or {}).get("id")}
            elegido = next(
                (p for p in compradores
                 if p.get("id") in claves or (p.get("identifier") or {}).get("id") in claves),
                elegido,
            )
        return elegido.get("name") or ref.get("name"), elegido
    # Sin entrada en `parties` queda la referencia, que a veces trae dirección propia.
    if ref.get("address"):
        return ref.get("name"), ref
    entidad = (release.get("tender") or {}).get("procuringEntity") or {}
    return ref.get("name") or entidad.get("name"), {}


def _adjudicacion(release: dict) -> tuple[str | None, float | None, str | None, list[str]]:
    """Primer adjudicatario, importe, fecha, y la lista completa.

    Se guarda **el primero**, no todos unidos por un separador. Un acuerdo marco se
    adjudica a veinte empresas, y `consultas.competencia()` agrupa por la cadena exacta:
    un `"A · B"` se contaría como un proveedor distinto de `"A"` y el ranking de
    Auftragnehmer se llenaría de entradas fantasma. La lista entera va a `raw`.

    El nombre se busca en DOS sitios, y el orden importa. Lo intuitivo es
    `awards[].suppliers[]`, pero medido sobre 5.818 adjudicaciones reales solo aparece
    ahí en el 14 %: lo normal es que el `award` traiga el lote, la fecha y el estado, y
    que la empresa esté en `parties` con el rol `supplier`, que cubre el 92 %. Quedándose
    solo con lo primero, la pestaña de Auftragnehmer se llenaba al 3 % y parecía que
    Alemania no publica quién gana.
    """
    nombres: list[str] = []
    importe = 0.0
    hay_importe = False
    fechas: list[str] = []
    for premio in release.get("awards") or []:
        for proveedor in premio.get("suppliers") or []:
            nombre = proveedor.get("name")
            if nombre and nombre not in nombres:
                nombres.append(nombre)
        cantidad = _importe_eur(premio.get("value"))
        if cantidad is not None:
            importe += cantidad
            hay_importe = True
        if premio.get("date"):
            fechas.append(premio["date"])
    for contrato in release.get("contracts") or []:
        if contrato.get("dateSigned"):
            fechas.append(contrato["dateSigned"])
    if not nombres:
        for parte in release.get("parties") or []:
            if "supplier" in (parte.get("roles") or []) and parte.get("name"):
                if parte["name"] not in nombres:
                    nombres.append(parte["name"])
    return (
        nombres[0] if nombres else None,
        importe if hay_importe else None,
        _sin_desplazamiento(min(fechas)) if fechas else None,
        nombres,
    )


def parsear_release(payload: dict, *, plazo: str | None = None,
                    expediente: str | None = None,
                    valor_estimado: float | None = None,
                    importe_adjudicacion: float | None = None) -> Licitacion | None:
    """Traduce un fichero OCDS del servicio a una `Licitacion`.

    `plazo`, `expediente` y los dos importes vienen del eForms del mismo anuncio cuando se
    ha descargado. El plazo y el Geschäftszeichen no están en el OCDS en absoluto; los
    importes sí, pero solo en la mitad de los casos en que el eForms los declara. Por eso
    los de fuera solo RELLENAN: si el OCDS trae la cifra, manda el OCDS, que es la fuente
    normalizada.

    Todo se pasa al CONSTRUCTOR y no se asigna después, porque asignar después se salta
    `__post_init__` y con él la normalización de fechas y el cálculo de
    `fecha_fin_prevista`.
    """
    releases = payload.get("releases") or []
    if not releases:
        return None
    r = releases[0]
    t = r.get("tender") or {}
    if not r.get("id"):
        return None

    organo, ficha_comprador = _comprador(r)

    cpv: list[str] = []
    nuts = lugar = None
    for item in t.get("items") or []:
        clas = item.get("classification") or {}
        if (clas.get("scheme") or "").upper() == "CPV" and clas.get("id"):
            cpv.append(clas["id"])
        for extra in item.get("additionalClassifications") or []:
            if (extra.get("scheme") or "").upper() == "CPV" and extra.get("id"):
                cpv.append(extra["id"])
        direccion = item.get("deliveryAddress") or {}
        nuts = nuts or direccion.get("region")
        lugar = lugar or direccion.get("locality")
    # El 89 % de los anuncios trae la región en el lugar de ejecución; en un 3 % solo
    # está la del comprador, y un 7 % no trae ninguna.
    origen_nuts = "leistungsort"
    if not nuts:
        direccion = ficha_comprador.get("address") or {}
        nuts = direccion.get("region")
        lugar = lugar or direccion.get("locality")
        origen_nuts = "vergabestelle" if nuts else "ninguno"

    lotes = t.get("lots") or []
    inicios, finales, duraciones = [], [], []
    for lote in lotes:
        periodo = lote.get("contractPeriod") or {}
        if periodo.get("startDate"):
            inicios.append(periodo["startDate"])
        if periodo.get("endDate"):
            finales.append(periodo["endDate"])
        if periodo.get("durationInDays"):
            duraciones.append(periodo["durationInDays"])

    adjudicatario, importe_adj, fecha_adj, todos = _adjudicacion(r)
    procedimiento = t.get("procurementMethodDetails")
    descripcion = _recortar(t.get("description"), MAX_DESCRIPCION)

    estado = _estado(r)
    return Licitacion(
        # Las Markterkundungen (Vorinformationen) van con sufijo de fuente propio, igual
        # que el radar español separa las consultas preliminares. Sirve para tener un
        # perfil deliberadamente amplio aplicado SOLO a ellas: son pocas —147 de 22.892
        # en un mes— y en esa fase todavía se puede influir en el pliego, así que ahí
        # compensa verlo todo. Con una fuente única no se puede acotar y ese perfil
        # amplio inundaría la bandeja.
        fuente=("oeffentlichevergabe:markterkundung" if estado == ESTADO_PREVIO
                else "oeffentlichevergabe"),
        # El uuid del ANUNCIO, sin el número de versión: así la versión 02 actualiza la
        # fila de la 01 en vez de crear otra.
        id_externo=r["id"],
        id_procedimiento=r.get("ocid"),
        # El Geschäftszeichen solo está en el eForms, y solo en los anuncios eForms-DE
        # nativos: los relevados desde service.bund.de llegan sin él (medido: 150/150
        # frente a 0/150). Sin él la agrupación no sufre, porque va por `ocid`.
        expediente=expediente,
        organo=organo,
        objeto=t.get("title"),
        descripcion=descripcion,
        cpv=cpv,
        # Solo hay una cifra publicada y es el valor estimado; no hay presupuesto base
        # separado como en España. Y solo aparece en el 10 % de los anuncios.
        valor_estimado=_importe_eur(t.get("value")) or valor_estimado,
        procedimiento=PROCEDIMIENTOS.get((procedimiento or "").strip().lower(), procedimiento),
        tipo_contrato=TIPOS_CONTRATO.get(t.get("mainProcurementCategory")),
        estado=estado,
        estado_origen=" ".join(sorted(r.get("tag") or [])) or None,
        fecha_publicacion=_sin_desplazamiento(r.get("date")),
        fecha_limite_presentacion=plazo,
        fecha_actualizacion=_sin_desplazamiento(r.get("date")),
        nuts=nuts,
        lugar=lugar,
        bundesland=bundesland_desde_nuts(nuts),
        url_detalle=FICHA + r["id"],
        urls_pliegos=[d["url"] for d in (t.get("documents") or []) if d.get("url")][:MAX_PLIEGOS],
        lote_num=lotes[0].get("id") if len(lotes) == 1 else None,
        lote_desc=_recortar(" \n".join(filter(None, (_texto_lote(l) for l in lotes))),
                            MAX_LOTES),
        adjudicatario=adjudicatario,
        importe_adjudicacion=importe_adj or importe_adjudicacion,
        fecha_adjudicacion=fecha_adj,
        duracion_meses=duracion_a_meses(max(duraciones), "DAY") if duraciones else None,
        fecha_inicio_ejecucion=_sin_desplazamiento(min(inicios)) if inicios else None,
        fecha_fin_prevista=_sin_desplazamiento(max(finales)) if finales else None,
        # Mínimo a conciencia. Volcar el release entero son 5,5 kB por anuncio: sobre dos
        # años de histórico, más de 4 GB de base por una decisión de una línea.
        raw={
            "version": payload.get("version"),
            "tag": r.get("tag"),
            "nuts_origen": origen_nuts,
            "zuschlagsempfaenger": todos[1:] or None,
        },
    )


def plazos_y_expedientes(datos: bytes) -> dict[str, dict]:
    """Del ZIP de eForms saca, por fichero, la fecha límite y el Geschäftszeichen.

    El ZIP mezcla DOS variantes de XML: los eForms-DE nativos (prefijos `cbc:`/`cac:`) y
    los relevados desde service.bund.de, que llegan con prefijos numerados (`ns8:`,
    `ns7:`) y con menos contenido. Por eso se recorre comparando el nombre LOCAL de cada
    etiqueta, sin namespace: comprobado, así se leen las dos. Un parser atado a los
    prefijos dejaría la mitad de los plazos en NULL sin que fallara nada.
    """
    salida: dict[str, dict] = {}
    with zipfile.ZipFile(io.BytesIO(datos)) as zf:
        for nombre in sorted(zf.namelist()):
            if not nombre.lower().endswith(".xml"):
                continue
            try:
                salida[Path(nombre).stem] = _leer_eforms(zf.read(nombre))
            except (ET.ParseError, KeyError) as exc:
                log.warning("eForms ilegible en %s: %s", nombre, exc)
    return salida


# Elementos de importe del eForms que NO tienen equivalente en el export OCDS, con el
# campo del modelo al que corresponde cada uno. La distinción importa: meter un importe de
# adjudicación en `valor_estimado` haría que la analítica de «baja de adjudicación»
# comparase una cifra consigo misma y diera siempre cero.
#
# Medido sobre un día entero: el OCDS trae importe en el 9 % de los anuncios y el eForms
# en el 19 %, así que leerlo aquí duplica la cobertura. El 79 % restante no lo publica en
# ningún sitio, y eso no es un fallo: en Alemania no hay obligación general de publicar el
# valor estimado, ni sobre ni bajo el umbral.
IMPORTES_EFORMS = {
    # BT-27 y los techos de acuerdo marco: lo que se espera gastar.
    "EstimatedOverallContractAmount": "valor_estimado",
    "FrameworkMaximumAmount": "valor_estimado",
    "OverallMaximumFrameworkContractsAmount": "valor_estimado",
    "MaximumValueAmount": "valor_estimado",
    # BT-720 y BT-161: lo que de verdad se ha adjudicado.
    "PayableAmount": "importe_adjudicacion",
    "TotalAmount": "importe_adjudicacion",
}


def _leer_eforms(datos: bytes) -> dict:
    fecha = hora = expediente = None
    importes: dict[str, float] = {}
    pila: list[str] = []
    for evento, elemento in ET.iterparse(io.BytesIO(datos), events=("start", "end")):
        etiqueta = elemento.tag.rsplit("}", 1)[-1]
        if evento == "start":
            pila.append(etiqueta)
            continue
        # BT-131: el plazo de presentación. La fecha y la hora llegan en dos elementos.
        if len(pila) >= 2 and pila[-2] == "TenderSubmissionDeadlinePeriod":
            if etiqueta == "EndDate" and fecha is None:
                fecha = _sin_desplazamiento(elemento.text)
            elif etiqueta == "EndTime" and hora is None:
                hora = _sin_desplazamiento(elemento.text)
        # BT-22: el Geschäftszeichen, que es el número de expediente del comprador.
        elif (len(pila) >= 2 and pila[-2] == "ProcurementProject" and etiqueta == "ID"
              and expediente is None and (elemento.text or "").strip()):
            expediente = elemento.text.strip()
        elif etiqueta in IMPORTES_EFORMS and (elemento.text or "").strip():
            # La moneda va como atributo. Si no es euro se descarta, por lo mismo que en
            # el OCDS: un número sin su moneda en una columna que después se suma y se
            # ordena es la forma más barata de que la analítica mienta.
            if (elemento.get("currencyID") or "EUR").upper() == "EUR":
                try:
                    valor = float(elemento.text)
                except ValueError:
                    valor = 0.0
                if valor > 0:
                    campo = IMPORTES_EFORMS[etiqueta]
                    # El mayor de los que declara: un anuncio con lotes publica el techo
                    # del contrato y además el de cada lote, y lo que interesa es el
                    # tamaño de la operación, no el del lote más pequeño.
                    importes[campo] = max(importes.get(campo, 0.0), valor)
        pila.pop()
    plazo = f"{fecha}T{hora}" if fecha and hora else fecha
    return {"plazo": plazo, "expediente": expediente, **importes}


class FuenteOeffentlicheVergabe:
    """Conector del Bekanntmachungsservice.

    La unidad de trabajo es el **día cerrado**, y de ahí cuelga todo el diseño: el export
    de un día que ya terminó no vuelve a cambiar nunca, así que se puede guardar en caché
    para siempre. Con eso desaparece entera la clase de fallo del ZIP que se reescribe a
    diario y que, reaprovechado, hace creer que un periodo está completo cuando le faltan
    semanas.

    Y resulta que el propio servicio lo impone: pedir el día de HOY devuelve un 400 con
    «The specified pubDay exceeds the allowed range. It must lie in the past». O sea que
    todo lo que se puede descargar está, por definición, cerrado. Por eso el último día
    que se pide es siempre ayer —en hora de Berlín, no la del portátil— y no hay ninguna
    rama sin caché.
    """

    nombre = "oeffentlichevergabe"

    def __init__(self, *, con_eforms: bool = False, meses_atras: int | None = None,
                 dias_ventana: int = 30, dias_primera_vez: int = 7,
                 dir_cache: Path | None = None, hoy: date | None = None) -> None:
        self.con_eforms = con_eforms
        self.meses_atras = meses_atras
        self.dias_ventana = dias_ventana
        self.dias_primera_vez = dias_primera_vez
        self.dir_cache = Path(dir_cache) if dir_cache else rutas.CACHE
        self._hoy = hoy
        self._cursor_nuevo: str | None = None

    # -- utilidades -------------------------------------------------------------------

    @property
    def hoy(self) -> date:
        return self._hoy or _hoy_berlin()

    @property
    def ultimo_dia(self) -> date:
        """Ayer. Hoy todavía no existe para el servicio: contesta 400."""
        return self.hoy - timedelta(days=1)

    def cursor_nuevo(self) -> str | None:
        return self._cursor_nuevo

    def _url(self, parametro: str, valor: str) -> str:
        return f"{BASE}?{parametro}={valor}"

    def _traer(self, parametro: str, valor: str, formato: str) -> bytes:
        """Descarga un tramo y lo deja en la caché. Todo lo descargable está cerrado."""
        destino = self.dir_cache / f"ov_{formato}_{valor}.zip"
        if destino.exists() and zipfile.is_zipfile(destino):
            return destino.read_bytes()
        # Resetear los contadores antes de cada descarga: sin esto la barra se queda
        # enseñando «fichero 1398 de 1398» al 100% durante toda la descarga siguiente,
        # que desde fuera se ve igual que una aplicación colgada.
        progreso.subtarea(0, 0)
        net.descargar_a_fichero(self._url(parametro, valor), destino,
                                cabeceras={"Accept": ACEPTA[formato]})
        return destino.read_bytes()

    def _tramo(self, parametro: str, valor: str) -> Iterator[Licitacion]:
        """Un día o un mes: descarga, cruza con el eForms si toca, y va soltando fichas."""
        datos = self._traer(parametro, valor, "ocds")
        extra: dict[str, dict] = {}
        if self.con_eforms:
            try:
                extra = plazos_y_expedientes(self._traer(parametro, valor, "eforms"))
            except (net.ErrorRed, zipfile.BadZipFile, OSError) as exc:
                # Sin plazos se puede vivir —la ficha sale sin fecha de cierre—; sin el
                # cuerpo no. Así que esto avisa y sigue en vez de tumbar el tramo.
                log.warning("%s: no se han podido traer los plazos de %s (%s)",
                            self.nombre, valor, exc)

        with zipfile.ZipFile(io.BytesIO(datos)) as zf:
            # ORDENADOS. Un mismo anuncio aparece en el mismo ZIP con la versión 01 y la
            # 02 —medido: 126 casos en un mes— y con el orden de archivo la 01 puede
            # procesarse después de la 02. La última escritura gana, así que en la base
            # quedaría la versión vieja, con su estado y su plazo obsoletos, y con la
            # huella «correcta» según esa versión vieja: no se volvería a corregir.
            nombres = sorted(n for n in zf.namelist() if n.lower().endswith(".json"))
            progreso.subtarea(0, len(nombres))
            for i, nombre in enumerate(nombres, 1):
                progreso.subtarea(i, len(nombres))
                try:
                    payload = json.loads(zf.read(nombre))
                except (json.JSONDecodeError, KeyError) as exc:
                    log.warning("%s: %s ilegible (%s)", self.nombre, nombre, exc)
                    continue
                datos_extra = extra.get(Path(nombre).stem, {})
                lic = parsear_release(
                    payload,
                    plazo=datos_extra.get("plazo"),
                    expediente=datos_extra.get("expediente"),
                    valor_estimado=datos_extra.get("valor_estimado"),
                    importe_adjudicacion=datos_extra.get("importe_adjudicacion"),
                )
                if lic is not None:
                    yield lic

    # -- el contrato de `Fuente` -------------------------------------------------------

    def incremental(self, cursor: str | None) -> Iterator[Licitacion]:
        """Los días publicados desde el cursor, ambos incluidos.

        No hay ventana de solape hacia atrás: una corrección se publica como una versión
        nueva en SU día, no en el del anuncio original, así que mirar solo hacia delante
        no pierde nada.
        """
        ultimo = self.ultimo_dia
        try:
            desde = date.fromisoformat(cursor) + timedelta(days=1) if cursor else None
        except (TypeError, ValueError):
            desde = None
        if desde is None:
            # Una ventana corta a propósito: el histórico lo trae `historico()`, y la
            # primera ingesta no tiene por qué bajar un mes entero para nada.
            desde = ultimo - timedelta(days=self.dias_primera_vez - 1)

        dia = desde
        while dia <= ultimo:
            progreso.tarea(f"Neues vom {dia.isoformat()}")
            yield from self._tramo("pubDay", dia.isoformat())
            self._cursor_nuevo = dia.isoformat()
            dia += timedelta(days=1)

    def historico(self, anio: int) -> Iterator[Licitacion]:
        """Un año, mes a mes.

        La captura de errores va **por mes y no por año**, al revés que en una fuente
        donde un año es un solo fichero. Aquí un año son doce descargas: con un try por
        año, una caída en marzo se llevaría por delante de abril a diciembre. Solo se
        propaga el fallo si no se ha podido traer NINGÚN mes, que es cuando de verdad hay
        que marcar la fuente como caída.
        """
        meses = self._meses_de(anio)
        if not meses:
            # Un año enteramente fuera de la ventana no es un error: no se pide y ya.
            # Si lanzara, el pipeline marcaría la fuente como fallida.
            return

        fallos, exitos = [], 0
        for i, mes in enumerate(meses, 1):
            progreso.tarea(f"Historie {anio} · Monat {i} von {len(meses)} ({mes})")
            try:
                if mes == self.hoy.strftime("%Y-%m"):
                    # El mes en curso no está cerrado, así que su `pubMonth` se quedaría
                    # cacheado a medias para siempre. Se pide día a día: cada día
                    # terminado sí es definitivo.
                    yield from self._dias_del_mes_en_curso()
                else:
                    yield from self._tramo("pubMonth", mes)
                exitos += 1
            except (net.ErrorRed, zipfile.BadZipFile, OSError) as exc:
                if isinstance(exc, net.ErrorRed) and exc.codigo == 404:
                    log.info("%s: no hay nada publicado en %s", self.nombre, mes)
                    exitos += 1
                    continue
                log.warning("%s: %s falló (%s); se sigue con los demás meses",
                            self.nombre, mes, exc)
                fallos.append(mes)
        if fallos and not exitos:
            raise net.ErrorRed(
                f"ningún mes de {anio} se ha podido traer: {', '.join(fallos)}"
            )
        # Si este año llega hasta el mes en curso, el histórico ha traído TODO lo
        # publicado hasta ayer, así que puede dejar el cursor puesto. En una fuente donde
        # el histórico tiene fecha de corte esto sería mentira —quedaría un hueco entre el
        # corte y hoy que no traería nadie—, pero aquí el histórico y la incremental
        # hablan la misma unidad, el día, y el último mes se pide día a día.
        if not fallos and meses and meses[-1] == self.hoy.strftime("%Y-%m"):
            self._cursor_nuevo = self.ultimo_dia.isoformat()

    def _dias_del_mes_en_curso(self) -> Iterator[Licitacion]:
        ultimo = self.ultimo_dia
        dia = self.hoy.replace(day=1)
        while dia <= ultimo:
            yield from self._tramo("pubDay", dia.isoformat())
            dia += timedelta(days=1)

    def _meses_de(self, anio: int) -> list[str]:
        """Los meses de ese año que caen dentro de la ventana y no son futuros."""
        hoy = self.hoy
        limite = None
        if self.meses_atras:
            # `meses_atras=24` desde septiembre de 2026 -> desde octubre de 2024.
            total = hoy.year * 12 + (hoy.month - 1) - (self.meses_atras - 1)
            limite = (total // 12, total % 12 + 1)
        meses = []
        for mes in range(1, 13):
            if (anio, mes) > (hoy.year, hoy.month):
                break
            if limite and (anio, mes) < limite:
                continue
            meses.append(f"{anio:04d}-{mes:02d}")
        return meses
