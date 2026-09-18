"""Orquestación de la ingesta: construir fuentes, recorrerlas y guardar.

Cada fuente se ingiere de forma aislada. Si una falla, se registra el error en
`ingest_log` y se sigue con las demás: perder TED una mañana no debe impedir ver lo
que ha publicado el Bekanntmachungsservice.
"""

from __future__ import annotations

import logging
import sqlite3
import zipfile
from datetime import datetime, timezone

from . import db, net, progreso
from .matching import Perfil, terminos_para_consultas
from .sources.oeffentlichevergabe import FuenteOeffentlicheVergabe
from .sources.ted import FuenteTED

log = logging.getLogger(__name__)

# Nombre corto en la CLI -> constructor. Cada entrada recibe los CPV y términos de los
# perfiles (solo los usan las fuentes que filtran en servidor) y un dict de opciones que
# las etapas de la carga inicial rellenan.
CONSTRUCTORES = {
    "oeffentlichevergabe": lambda cpv, terminos, opciones: [
        FuenteOeffentlicheVergabe(
            con_eforms=opciones.get("con_eforms", False),
            meses_atras=opciones.get("meses_atras"),
            dias_ventana=opciones.get("dias_ventana", 30),
        )
    ],
    "ted": lambda cpv, terminos, opciones: [
        FuenteTED(cpv, terminos, pais="DEU",
                  dias_ventana=opciones.get("dias_ventana", 30))
    ],
}

FUENTES_DISPONIBLES = tuple(CONSTRUCTORES)

# Lo que se ingiere cuando no se pide nada. TED queda FUERA a propósito: desde el
# 25/10/2023 todo lo que supera el umbral europeo pasa por el Bekanntmachungsservice
# (VgV §10a Abs. 5), así que para Alemania TED no aporta registros nuevos y sí obliga a
# reconciliar dos fuentes que se solapan casi del todo. Se mantiene disponible como red
# de seguridad y para comprobar cobertura.
FUENTES_POR_DEFECTO = ("oeffentlichevergabe",)

# El mismo nombre, dicho para quien no ha visto nunca esta base. El técnico sigue siendo
# el de `fuente.nombre`, que es el que hay que poder buscar en el log; este es el que se
# lee en la pantalla mientras la carga inicial trabaja.
TITULOS_FUENTE = {
    "oeffentlichevergabe": "der Bekanntmachungsservice des Bundes",
    "ted": "das Amtsblatt der Europäischen Union",
}

# Meses de histórico que se guardan. Es la ventana que recorta el conector dentro de los
# años naturales que pide la carga inicial.
MESES_HISTORICO = 24

# Hasta dónde se puede ampliar desde la aplicación. Tres años y no más porque a partir de
# ahí deja de ser una decisión y pasa a ser un problema: cada año extra son ~1,6 GB de
# descarga, ~275.000 anuncios y ~1,8 GB más de base.
MESES_HISTORICO_MAX = 36

# La ventana elegida se guarda, y no se deduce de los datos: «tengo anuncios de 2024» no
# distingue «pedí tres años» de «pedí dos y ese mes cayó dentro».
CLAVE_MESES = "meses_historico"


def meses_historico(con) -> int:
    """La ventana de histórico de esta instalación, en meses."""
    guardado = db.leer_preferencia(con, CLAVE_MESES)
    try:
        return max(1, min(int(guardado), MESES_HISTORICO_MAX))
    except (TypeError, ValueError):
        return MESES_HISTORICO


def fijar_meses_historico(con, meses: int) -> int:
    """Guarda la ventana y devuelve la que ha quedado, ya acotada."""
    meses = max(MESES_HISTORICO, min(int(meses), MESES_HISTORICO_MAX))
    db.escribir_preferencia(con, CLAVE_MESES, str(meses))
    con.commit()
    return meses

# Etapas de la carga inicial, de la más barata a la más cara. El orden no es estético: la
# primera deja la herramienta usable en minutos, y la que cuesta un rato largo se queda
# corriendo por detrás mientras ya se puede trabajar.
ETAPAS_PRIMERA_CARGA = [
    {
        "etiqueta": "Was gerade läuft",
        "fuentes": ["oeffentlichevergabe"],
        "historico": True,
        "opciones": {"meses_atras": 2, "con_eforms": True},
        "coste": "rund 260 MB Download und etwa eine Minute Verarbeitung",
        "detalle": "der laufende und der vorige Monat: alles, worauf man sich jetzt "
                   "noch bewerben kann",
    },
    {
        "etiqueta": "Zwei Jahre Historie",
        "fuentes": ["oeffentlichevergabe"],
        "historico": True,
        # `ventana_completa` marca cuál es LA etapa del archivo: es la que recorta a la
        # ventana elegida, y la que hay que relanzar si alguien la amplía a tres años.
        # `meses_atras` se rellena al lanzarla, leyendo la preferencia.
        "ventana_completa": True,
        "opciones": {"con_eforms": True},
        # No se promete un total de descarga a secas: lo que tarda lo manda la línea de
        # cada uno y varía dos órdenes de magnitud. El proceso sí depende solo de la
        # máquina y sí se puede prometer.
        "coste": "rund 3,3 GB Download, die Dauer hängt von deiner Leitung ab, "
                 "und 15 bis 25 Minuten Verarbeitung",
        "detalle": "rund 500.000 Bekanntmachungen; das ist, was Vertragsenden, "
                   "Auftragnehmer und die Auswertung füllt",
        # Cuánto cuesta cada año adicional, para poder decirlo antes de empezar.
        "coste_por_anio": "rund 1,6 GB und 8 bis 12 Minuten",
    },
]

# Por qué las DOS etapas piden eForms, aunque cueste 2,3 GB más:
#
# El export OCDS no trae ni la Angebotsfrist, ni el Geschäftszeichen, ni —lo que más
# duele— el precio de adjudicación. Medido sobre un día entero: de 314 adjudicaciones,
# el OCDS publica importe en 50 (15 %) y el eForms en 231 (73 %), en `PayableAmount`.
# Sin eForms, la pestaña de Auftragnehmer sale casi entera a 0 € y el bloque de
# Preisabschlag no tiene con qué comparar.
#
# Hubo una versión con tres etapas donde solo la primera y la última pedían eForms, para
# ahorrarse esos 2,3 GB. Salió mal de dos formas: la del medio reingería los mismos meses
# sin eForms y dejaba los plazos a NULL —la base entera acabó sin una sola fecha de
# cierre—, y el histórico profundo se quedaba igualmente sin precios de adjudicación, que
# es justo donde viven, porque las adjudicaciones se acumulan a lo largo de los dos años.
# Pedirlo siempre cuesta más disco y elimina las dos clases de fallo.


# Marcador en `preferencias` de cada etapa terminada. Hace falta llevar la cuenta porque
# la aplicación decide sola, al arrancar, qué le queda por descargar: sin esto habría que
# adivinarlo mirando los datos, y «¿tengo dos años de histórico?» no se puede responder
# sin confundir «la etapa no se ha hecho» con «la etapa se hizo y ese mes no publicó nada».
CLAVE_ETAPA = "primera_carga_etapa_{n}"

# A partir de cuántas horas sin una ingesta correcta se considera que toca otra. Son 20 y
# no 24 a propósito: quien abre la aplicación cada mañana a la misma hora la abriría a
# veces cinco minutos antes, y con 24 se saltaría el día entero.
HORAS_FRESCURA = 20


def marcar_etapa(con, numero: int, hecha: bool = True) -> None:
    db.escribir_preferencia(con, CLAVE_ETAPA.format(n=numero), "1" if hecha else None)
    con.commit()


def etapas_pendientes(con) -> list[int]:
    """Las etapas de la primera carga que todavía no han terminado bien."""
    return [
        n for n in range(1, len(ETAPAS_PRIMERA_CARGA) + 1)
        if db.leer_preferencia(con, CLAVE_ETAPA.format(n=n)) != "1"
    ]


def _horas_desde_la_ultima_ingesta(con, ahora: datetime) -> float | None:
    """None si no hay ninguna ingesta correcta todavía."""
    fechas = []
    for f in db.salud_fuentes(con):
        if f["ok"] and f["terminado_en"]:
            try:
                fechas.append(datetime.fromisoformat(f["terminado_en"]))
            except ValueError:
                continue
    if not fechas:
        return None
    ultima = max(fechas)
    if ultima.tzinfo is None:
        ultima = ultima.replace(tzinfo=timezone.utc)
    return (ahora - ultima).total_seconds() / 3600


def trabajo_pendiente(con, *, ahora: datetime | None = None) -> dict | None:
    """Qué le falta a esta instalación. `None` si está al día.

    Se consulta al arrancar el servidor para que la aplicación se ponga al día sola, sin
    depender de que alguien se acuerde de lanzar la ingesta o de instalar la tarea diaria.

    Las etapas pendientes mandan sobre la ingesta del día: no tiene sentido pedir las
    novedades de ayer mientras faltan dos años de histórico.
    """
    pendientes = etapas_pendientes(con)
    if pendientes:
        return {"tipo": "primera_carga", "etapas": pendientes}

    horas = _horas_desde_la_ultima_ingesta(con, ahora or datetime.now(timezone.utc))
    if horas is None or horas >= HORAS_FRESCURA:
        return {"tipo": "incremental", "horas": horas}
    return None


def anios_primera_carga(hoy, meses: int | None = None) -> list[int]:
    """Los años naturales que hay que pedir para cubrir la ventana. `hoy` se fija en tests.

    Son años NATURALES porque es lo que recorre `ingerir()`; el recorte fino al mes lo
    hace el conector con `meses_atras`. Por eso hace falta uno más de la cuenta: 24 meses
    contados desde septiembre empiezan en octubre de hace dos años, así que hay que pedir
    tres años naturales para que quepan.
    """
    meses = meses or MESES_HISTORICO
    # +1 por el año en curso, que casi nunca está completo.
    anios = (meses + 11) // 12 + 1
    return list(range(hoy.year - anios + 1, hoy.year + 1))


def opciones_de_etapa(etapa: dict, meses: int) -> dict:
    """Las opciones de una etapa, con la ventana del archivo ya resuelta."""
    opciones = dict(etapa.get("opciones") or {})
    if etapa.get("ventana_completa"):
        opciones["meses_atras"] = meses
    return opciones


def construir_fuentes(nombres: list[str] | None, perfiles: list[Perfil],
                      *, dias_ventana: int = 30,
                      opciones: dict | None = None) -> list:
    """Instancia los conectores pedidos.

    A las fuentes que filtran en servidor (hoy solo TED) se les pasan los CPV y términos
    de los perfiles para no descargar el continente entero. El filtrado fino se hace
    después en local.

    `opciones` es lo que cada etapa de la carga inicial quiere del conector —la ventana
    de meses, si trae las fechas límite— y llega tal cual al constructor.
    """
    cpv, terminos = terminos_para_consultas(perfiles)
    opciones = dict(opciones or {})
    opciones.setdefault("dias_ventana", dias_ventana)

    if not nombres:
        nombres = list(FUENTES_POR_DEFECTO)

    fuentes = []
    for nombre in nombres:
        constructor = CONSTRUCTORES.get(nombre)
        if constructor is None:
            raise ValueError(
                f"Fuente desconocida: {nombre}. Disponibles: {', '.join(FUENTES_DISPONIBLES)}"
            )
        fuentes.extend(constructor(cpv, terminos, opciones))
    return fuentes


def ingerir(con: sqlite3.Connection, fuentes: list, *, anios: list[int] | None = None,
            reiniciar_cursor: bool = False) -> dict:
    """Ejecuta la ingesta. Devuelve un resumen por fuente.

    `reiniciar_cursor` olvida por dónde se quedó la última vez y vuelve a pedir la
    ventana completa. Hace falta cuando cambia la forma de interpretar los datos:
    los registros ya guardados no se reprocesan solos, porque la ingesta
    incremental —con razón— solo pide lo publicado desde la última ejecución.
    """
    resumen = {}

    for fuente in fuentes:
        progreso.fuente(fuente.nombre, TITULOS_FUENTE.get(fuente.nombre, fuente.nombre))
        if reiniciar_cursor:
            db.escribir_cursor(con, fuente.nombre, None)
            con.commit()
        log_id = db.abrir_ingest(con, fuente.nombre)
        vistos = nuevos = actualizados = 0
        error = None
        try:
            if anios:
                for i, anio in enumerate(anios, 1):
                    progreso.tarea(f"Historie {anio} (Jahr {i} von {len(anios)})")
                    # Cada año va en su propio try. No todas las fuentes publican todos
                    # los años, y con un try único un 404 en uno se llevaba por delante
                    # los que sí existen. Por el mismo motivo se recogen los fallos de
                    # fichero y no solo los de red: un ZIP de la caché que no se puede
                    # leer —iCloud vacía los ficheros grandes de las carpetas
                    # sincronizadas— dejaba los años siguientes sin intentar siquiera.
                    try:
                        for lic in fuente.historico(anio):
                            vistos += 1
                            progreso.procesando()
                            progreso.fichas(vistos)
                            estado = db.guardar(con, lic)
                            nuevos += estado == "nueva"
                            actualizados += estado == "actualizada"
                            if vistos % 2000 == 0:
                                con.commit()
                                log.info("  %s %s: %d procesadas...",
                                         fuente.nombre, anio, vistos)
                    except (net.ErrorRed, zipfile.BadZipFile, OSError) as exc:
                        if isinstance(exc, net.ErrorRed) and exc.codigo == 404:
                            # No es una avería: ese año no existe para esta fuente. Si se
                            # contara como error, una carga inicial que fue bien acabaría
                            # diciendo «alguna fuente ha fallado».
                            log.info("%s: no hay histórico publicado de %s",
                                     fuente.nombre, anio)
                            continue
                        log.warning("%s %s falló: %s; se sigue con los demás años",
                                    fuente.nombre, anio, exc)
                        error = error or f"{type(exc).__name__}: {exc}"
                    con.commit()
                # También tras el histórico, no solo tras la incremental. El radar español
                # no lo hacía porque su histórico venía en ZIP anuales con fecha de corte,
                # y dar el cursor por puesto habría dejado un hueco que no traía nadie.
                # Aquí el conector solo lo ofrece cuando de verdad ha llegado hasta el
                # último día publicado, y solo si el año entró sin errores.
                if not error:
                    nuevo_cursor = fuente.cursor_nuevo()
                    if nuevo_cursor:
                        db.escribir_cursor(con, fuente.nombre, nuevo_cursor)
            else:
                progreso.tarea("Neues seit dem letzten Abruf")
                cursor_fila = db.leer_cursor(con, fuente.nombre)
                cursor = cursor_fila["cursor"] if cursor_fila else None
                for lic in fuente.incremental(cursor):
                    vistos += 1
                    progreso.procesando()
                    progreso.fichas(vistos)
                    estado = db.guardar(con, lic)
                    nuevos += estado == "nueva"
                    actualizados += estado == "actualizada"
                    if vistos % 2000 == 0:
                        con.commit()
                nuevo_cursor = fuente.cursor_nuevo()
                if nuevo_cursor:
                    db.escribir_cursor(con, fuente.nombre, nuevo_cursor)
        except Exception as exc:  # noqa: BLE001 - se aísla la fuente a propósito
            error = f"{type(exc).__name__}: {exc}"
            log.error("Fuente %s falló: %s", fuente.nombre, error)

        con.commit()
        db.cerrar_ingest(
            con, log_id, vistos=vistos, nuevos=nuevos, actualizados=actualizados, error=error
        )
        con.commit()
        resumen[fuente.nombre] = {
            "vistos": vistos, "nuevos": nuevos, "actualizados": actualizados, "error": error
        }
        estado_txt = f"ERROR: {error}" if error else f"{nuevos} nuevas, {actualizados} actualizadas"
        log.info("%-28s %6d vistas · %s", fuente.nombre, vistos, estado_txt)

    # Un anuncio de TED y el del servicio alemán del mismo procedimiento solo se pueden
    # reconocer comparándolos entre sí, así que no puede hacerse al guardar cada fila. Va
    # aquí, una vez terminadas todas las fuentes: si se hiciera por fuente, el anuncio de
    # TED que llega antes que su pareja se quedaría suelto hasta la siguiente ingesta.
    #
    # Solo se ejecuta si TED está entre las fuentes. Con TED apagado no puede fusionar
    # nada, y la consulta paga un GROUP BY por importe y fecha de cierre —sin índice que
    # lo apoye— sobre cientos de miles de filas para devolver cero.
    #
    # No entra en `resumen`: quien lo consume espera una entrada por fuente con su campo
    # `error` (radar.py lo recorre para decidir el código de salida).
    if any(f.nombre == "ted" for f in fuentes):
        fusion = db.fusionar_grupos_ted(con)
        if fusion["anuncios_fusionados"]:
            log.info(
                "%-28s %6d anuncios unidos a su procedimiento "
                "(%d grupos ambiguos, sin tocar)",
                "dedup ted", fusion["anuncios_fusionados"], fusion["ambiguos"],
            )

    # Va después de la fusión, no antes: un anuncio que acaba de entrar en el grupo de
    # un expediente ya triado tiene que heredar ese triaje aquí. Si no, lo descartado
    # reaparecería en la bandeja al día siguiente por la puerta de atrás.
    heredados = db.propagar_revisiones_en_grupos(con)
    if heredados:
        log.info(
            "%-28s %6d anuncios heredan el triaje de su expediente",
            "triaje por expediente", heredados,
        )

    return resumen
