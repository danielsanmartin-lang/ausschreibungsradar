"""Consultas de lectura para la bandeja y el export.

Se aísla del servidor para poder probarlas sin levantar HTTP.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime

from .model import ESTADO_ADJUDICADA, ESTADOS_VIVOS, plegar_ss

ORDENES = {
    # "Cierran antes" tiene que poner primero lo que cierra pronto y AÚN está
    # abierto. Ordenar solo por días restantes ascendente dejaba arriba lo vencido
    # hace cuatro años, que es lo último que le interesa a nadie.
    # Tres grupos: primero lo abierto con plazo (lo que cierra antes arriba),
    # después lo que no publica plazo, y al final lo vencido. Dentro de lo vencido
    # se invierte el orden: lo que cerró la semana pasada aún sirve para seguir la
    # adjudicación; lo de hace cuatro años no.
    "urgencia": (
        "CASE WHEN orden_urgencia < 0 THEN 2 "
        "     WHEN orden_urgencia = 9999 THEN 1 "
        "     ELSE 0 END ASC, "
        "CASE WHEN orden_urgencia < 0 THEN -orden_urgencia ELSE orden_urgencia END ASC, "
        "puntuacion DESC"
    ),
    "puntuacion": "puntuacion DESC, primera_publicacion DESC",
    # Las dos de fecha ordenan por la PRIMERA publicación del expediente, no por la del
    # anuncio que se enseña. Si fuera por el anuncio, una adjudicación publicada ayer
    # subiría a lo más alto un expediente de junio que ya está cerrado: medido, 5 de las
    # 16 que salían arriba eran eso. Es el mismo criterio con el que la Analítica decide
    # en qué mes «sale» un expediente, y el mismo que enciende la etiqueta «Nueva», así
    # que ordenar por «más recientes» deja justo las nuevas arriba.
    "reciente": "primera_publicacion DESC",
    # El `IS NULL` primero no es adorno: SQLite considera NULL más pequeño que
    # cualquier valor, así que un ASC a secas abre «las más antiguas» con las que no
    # traen fecha de publicación, que no son antiguas sino desconocidas. En DESC caen
    # solas al final y no hace falta.
    "antigua": "primera_publicacion IS NULL, primera_publicacion ASC",
    "importe": "l.importe_referencia DESC",
}

# El de fábrica es «reciente», y no «urgencia» como lo fue hasta la 1.3.0. Ordenar por
# lo que cierra antes pone arriba plazos que ya no se pueden cazar: lo primero que hay
# que ver al abrir es lo último publicado, que es donde queda margen para preparar una
# oferta. Está escrito aquí, en el respaldo de `bandeja()` y en el `<select>` de la
# interfaz, y los tres tienen que decir lo mismo: un valor por defecto que no coincide
# entre la API y la pantalla es una avería con fecha.
ORDEN_POR_DEFECTO = "reciente"

# Todas las ordenaciones terminan en `l.id`. Sin un criterio único al final, SQLite
# no garantiza el mismo orden entre dos consultas, y con LIMIT/OFFSET eso significa
# que la página 2 puede repetir filas de la página 1 y perder otras. Hay cientos de
# licitaciones empatadas a 3.0 de puntuación, así que no es un caso teórico.
ORDENES = {clave: f"{expr}, l.id ASC" for clave, expr in ORDENES.items()}

# Clave con la que se agrupan los anuncios de un mismo expediente. Los registros
# que vienen de la versión anterior tienen `clave_grupo` a NULL hasta que se
# vuelven a ingerir, y el COALESCE hace que entonces cada uno sea su propio grupo:
# la vista funciona igual, solo que sin agrupar todavía.
_GRUPO = "COALESCE(l.clave_grupo, CAST(l.id AS TEXT))"


# Estados de triaje que mantienen una ficha en la bandeja aunque deje de casar con
# ningún perfil activo.
#
# Marcar algo como «siguiendo» o «presentada» es una decisión explícita, y tiene que
# pesar más que el filtro automático. El caso que lo motiva: al estrechar los perfiles
# —quitando la red amplia de ciberseguridad— se fue de la bandeja un contrato de 6,25 M€
# de Canal de Isabel II que estaba en seguimiento, con 23 días de plazo por delante. La
# fila de `revisiones` seguía ahí, intacta, pero la interfaz no tenía por dónde
# enseñarla: ni el filtro de estado ni la búsqueda libre pasan por fuera de `matches`.
# Ajustar los términos no puede esconderte algo en lo que estás trabajando.
#
# «descartado» NO está aquí a propósito: ahí el filtro y la decisión dicen lo mismo, y
# resucitar lo descartado sería justo el ruido que se venía a quitar.
ESTADOS_PROTEGIDOS = ("siguiendo", "presentada")

# El FROM de la bandeja. Lo comparten la lista y los contadores por el mismo motivo por
# el que comparten `_condiciones()`: cuando cada uno tenía el suyo, acabaron contando
# cosas distintas.
#
# `LEFT JOIN matches` y no `JOIN`: una ficha protegida por su triaje puede no tener
# ninguna coincidencia. Eso deja `perfil`, `puntuacion` y `motivo` a NULL, que es
# exactamente lo que la ficha ya sabe contar —«está en la base pero no casa con ningún
# perfil activo»—.
# El conjunto visible se calcula ANTES y se usa para entrar en `licitaciones`, en vez de
# recorrer la tabla entera y quedarse con las que cumplen.
#
# Antes esto era un `LEFT JOIN matches` con un `WHERE (m.licitacion_id IS NOT NULL OR
# protegida)`. Ese OR impide a SQLite entrar por `matches`, así que el plan era un
# recorrido completo: medido, 12,2 s para contar sobre 575.230 filas, y `resumen()` llama
# a `contar()` varias veces, así que abrir la bandeja costaba ocho segundos con la
# pantalla en blanco. Entrando por aquí son unos mil identificadores y una búsqueda por
# clave primaria cada uno.
#
# El UNION (y no UNION ALL) es el que deduplica: una ficha puede estar en las dos mitades.
#
# Y `CROSS JOIN` no es un tipo de join distinto: en SQLite es la forma de decir «no
# reordenes». Sin él, el planificador seguía prefiriendo entrar por `idx_lic_estado`, y
# «vivas» abarca 371.195 de las 575.230 filas —un anuncio publicado en 2024 se queda en
# «veröffentlicht» para siempre, porque la adjudicación llega como OTRA fila—, así que
# volvía a recorrer media tabla. Medido sobre la base real: 12,2 s entrando por el estado,
# 0,02 s entrando por aquí.
_marcas_protegidos = ", ".join("?" * len(ESTADOS_PROTEGIDOS))
_DESDE_BANDEJA = f"""FROM (SELECT licitacion_id AS id FROM matches
                            UNION
                           SELECT licitacion_id FROM revisiones
                            WHERE estado IN ({_marcas_protegidos})) vis
              CROSS JOIN licitaciones l ON l.id = vis.id
              LEFT JOIN matches m ON m.licitacion_id = l.id
              LEFT JOIN revisiones r ON r.licitacion_id = l.id"""

# Ya lo acota el FROM. Se deja como constante para no reescribir los WHERE que la usan.
_VISIBLE = "1=1"


# Cuántos días luce la etiqueta «Nueva». Una semana es lo que tarda alguien en dar una
# vuelta a la bandeja sin sentir que se le ha escapado algo, y coincide con el plazo más
# corto que publica esta administración.
DIAS_NUEVA = 7


def _a_fecha(valor: str | None) -> date | None:
    """Una fecha de la base a `date`, acepte o no hora y zona horaria."""
    if not valor:
        return None
    try:
        return datetime.fromisoformat(valor).date()
    except ValueError:
        try:
            return date.fromisoformat(valor[:10])
        except ValueError:
            return None


def _dias_restantes(limite: str | None) -> int | None:
    fecha = _a_fecha(limite)
    return None if fecha is None else (fecha - date.today()).days


def _dias_desde(publicacion: str | None) -> int | None:
    fecha = _a_fecha(publicacion)
    return None if fecha is None else (date.today() - fecha).days


def consulta_fts(texto: str) -> str:
    """Convierte lo que teclea una persona en una consulta que FTS5 acepte.

    Dos cosas, las dos obligatorias en alemán y casi invisibles en castellano:

    1. **El guion es un operador en FTS5**, no una letra. `MATCH 'E-Mail-Sicherheit'` no
       devuelve nada: lanza `OperationalError: no such column: Mail`. En castellano casi
       no se nota —«concienciación», «phishing»— pero aquí el primer término que teclea
       cualquiera lleva guion: `IT-Sicherheit`, `E-Mail-Gateway`, `Awareness-Plattform`.
       Cada palabra se entrecomilla y pasa a ser una frase literal.
    2. **La ß se pliega**, igual que al indexar: el tokenizador quita las diéresis pero
       no la ß, así que sin esto «Massnahmen» no encontraría «Maßnahmen».

    Se pierden los operadores de FTS5 (`OR`, `NEAR`, `*`), y es a propósito: esto es la
    caja de búsqueda de una bandeja, no una consola de consultas, y un `OperationalError`
    delante de quien está trabajando vale mucho menos que un `OR`.
    """
    palabras = [p.replace('"', "") for p in plegar_ss(texto).split()]
    return " ".join(f'"{p}"' for p in palabras if p)


def _condiciones(
    con: sqlite3.Connection,
    *,
    perfil: str | None = None,
    estado_revision: str | None = None,
    solo_vivas: bool = True,
    cierran_en_dias: int | None = None,
    bundesland: str | None = None,
    fuente: str | None = None,
    importe_min: float | None = None,
    busqueda: str | None = None,
    solo_novedades: bool = False,
) -> tuple[list[str], list]:
    """Construye el WHERE de la bandeja. Lo comparten la lista y los contadores.

    Está extraído a propósito: los contadores de la cabecera tenían sus propias
    consultas y acabaron contando otra cosa que la lista (decían 945 sin revisar
    donde la lista mostraba 57). Con un solo sitio donde se deciden los filtros,
    no pueden volver a divergir.
    """
    where = ["1=1"]
    params: list = []

    if perfil:
        where.append("m.perfil = ?")
        params.append(perfil)
    if estado_revision:
        where.append("COALESCE(r.estado, 'nuevo') = ?")
        params.append(estado_revision)
    else:
        # Por defecto, lo descartado no estorba.
        where.append("COALESCE(r.estado, 'nuevo') != 'descartado'")
    if solo_vivas:
        # "Abierta" son dos condiciones, no una: que el estado siga vivo Y que el
        # plazo no haya pasado. Hay anuncios que se quedan en «veröffentlicht»
        # con fecha límite de hace cuatro años; filtrar solo por estado las colaba.
        marcas = ", ".join("?" * len(ESTADOS_VIVOS))
        where.append(f"l.estado IN ({marcas})")
        params.extend(sorted(ESTADOS_VIVOS))
        where.append(
            "(l.fecha_limite_presentacion IS NULL"
            " OR substr(l.fecha_limite_presentacion, 1, 10) >= date('now'))"
        )
    if cierran_en_dias is not None:
        where.append(
            "substr(l.fecha_limite_presentacion, 1, 10) "
            "BETWEEN date('now') AND date('now', ?)"
        )
        params.append(f"+{int(cierran_en_dias)} day")
    if bundesland:
        where.append("l.bundesland = ?")
        params.append(bundesland)
    if fuente:
        where.append("l.fuente LIKE ?")
        params.append(f"{fuente}%")
    if importe_min:
        where.append("l.importe_referencia >= ?")
        params.append(importe_min)
    if busqueda:
        where.append(
            "l.id IN (SELECT rowid FROM licitaciones_fts WHERE licitaciones_fts MATCH ?)"
        )
        params.append(consulta_fts(busqueda))
    if solo_novedades:
        marca = _marca_visita(con)
        if marca is not None:
            where.append("l.id > ?")
            params.append(marca)

    return where, params


def contar(con: sqlite3.Connection, **filtros) -> int:
    """Cuántos expedientes cumplen esos filtros. La misma unidad que la lista."""
    where, params = _condiciones(con, **filtros)
    return con.execute(
        f"""SELECT COUNT(*) FROM (
                SELECT {_GRUPO} AS g
                  {_DESDE_BANDEJA}
                 WHERE {_VISIBLE} AND {" AND ".join(where)}
                 GROUP BY g
            )""",
        [*ESTADOS_PROTEGIDOS, *params],
    ).fetchone()[0]


def bandeja(
    con: sqlite3.Connection,
    *,
    perfil: str | None = None,
    estado_revision: str | None = None,
    solo_vivas: bool = True,
    cierran_en_dias: int | None = None,
    bundesland: str | None = None,
    fuente: str | None = None,
    importe_min: float | None = None,
    busqueda: str | None = None,
    solo_novedades: bool = False,
    orden: str = ORDEN_POR_DEFECTO,
    limite: int = 200,
    offset: int = 0,
) -> dict:
    """Licitaciones que han casado con algún perfil, con su estado de triaje.

    Y también lo que sigues o has presentado aunque ya no case: ver
    `ESTADOS_PROTEGIDOS`.
    """
    filtros = dict(
        perfil=perfil, estado_revision=estado_revision, solo_vivas=solo_vivas,
        cierran_en_dias=cierran_en_dias, bundesland=bundesland, fuente=fuente,
        importe_min=importe_min, busqueda=busqueda, solo_novedades=solo_novedades,
    )
    where, params = _condiciones(con, **filtros)
    orden_sql = ORDENES.get(orden, ORDENES[ORDEN_POR_DEFECTO])

    # Dos agrupaciones encadenadas, cada una arreglando un problema distinto:
    #
    # 1. `GROUP BY l.id` — una licitación puede casar con varios perfiles y el JOIN
    #    devolvía una fila por cada uno, así que salía repetida mientras `total`
    #    contaba licitaciones distintas. Eso descuadraba también la paginación,
    #    porque `offset` avanza contando filas.
    # 2. `PARTITION BY clave_grupo` — una misma licitación genera varios anuncios a
    #    lo largo de su vida (licitación, corrección, adjudicación por lotes) y cada
    #    uno es un registro: LANTIK aparecía dos veces y CSIRT Canarias cuatro. Se
    #    queda el anuncio más reciente de cada expediente, con el número de anuncios
    #    del grupo para poder decirlo en la tarjeta.
    sql = f"""
        SELECT * FROM (
            SELECT l.*,
                   group_concat(DISTINCT m.perfil) AS perfil,
                   MAX(m.puntuacion) AS puntuacion,
                   group_concat(m.motivo, '  ||  ') AS motivo,
                   COALESCE(r.estado, 'nuevo') AS estado_revision,
                   r.asignado_a, r.notas, r.motivo_descarte,
                   CASE
                     WHEN l.fecha_limite_presentacion IS NULL THEN 9999
                     ELSE CAST(julianday(substr(l.fecha_limite_presentacion, 1, 10))
                               - julianday('now') AS INTEGER)
                   END AS orden_urgencia,
                   COUNT(*) OVER (PARTITION BY {_GRUPO}) AS anuncios,
                   MIN(l.fecha_publicacion) OVER (PARTITION BY {_GRUPO})
                       AS primera_publicacion,
                   ROW_NUMBER() OVER (
                       PARTITION BY {_GRUPO}
                       ORDER BY l.fecha_publicacion DESC, l.id DESC
                   ) AS _rn
              {_DESDE_BANDEJA}
             WHERE {_VISIBLE} AND {" AND ".join(where)}
             GROUP BY l.id
        ) l
         WHERE _rn = 1
         ORDER BY {orden_sql}
         LIMIT ? OFFSET ?
    """
    filas = con.execute(
        sql, [*ESTADOS_PROTEGIDOS, *params, limite, offset]
    ).fetchall()

    total = contar(con, **filtros)

    # Cuántas habría sin los filtros del usuario, para poder decir "57 de 668" y
    # que se entienda por qué no salen todas.
    total_sin_filtros = contar(con, solo_vivas=False)

    items = []
    for f in filas:
        d = dict(f)
        d["cpv"] = (d.get("cpv") or "").split()
        try:
            d["urls_pliegos"] = json.loads(d.get("urls_pliegos") or "[]")
        except json.JSONDecodeError:
            d["urls_pliegos"] = []
        d.pop("raw", None)
        d.pop("texto_busqueda", None)
        d["dias_restantes"] = _dias_restantes(d.get("fecha_limite_presentacion"))
        # «Nueva» se decide aquí y no en la interfaz: la regla vive en un sitio, y así
        # el CSV y la pantalla no pueden discrepar sobre qué es reciente.
        dias = _dias_desde(d.get("primera_publicacion"))
        d["dias_desde_publicacion"] = dias
        d["es_nueva"] = dias is not None and 0 <= dias <= DIAS_NUEVA
        items.append(d)

    return {"total": total, "total_sin_filtros": total_sin_filtros, "items": items}


def resumen(con: sqlite3.Connection, *, en_marcha: bool = False) -> dict:
    """Cifras de cabecera y salud de las fuentes.

    Cada cifra se calcula con `contar()`, que es la misma función que usa la lista.
    Antes cada contador tenía su propia consulta y acabaron midiendo cosas
    distintas: la cabecera decía 945 «sin revisar» y al pulsar el filtro salían 57,
    porque la lista agrupa los anuncios de un expediente y los contadores no.

    No se publica el recuento de lo descargado que no cumple los criterios: es un
    número que no se puede consultar en ninguna vista y solo despista.

    `en_marcha` dice si hay una ingesta corriendo ahora mismo. Lo sabe quien llama —el
    servidor tiene el cerrojo a mano— y sirve para no confundir una fila de registro
    todavía abierta con una fuente rota.
    """
    # También por expedientes agrupados: si aquí se contaran anuncios, el desglose
    # sumaría 921 mientras el total de arriba dice 640, que es justo la incoherencia
    # que había que quitar.
    nombres = [
        f["perfil"] for f in con.execute("SELECT DISTINCT perfil FROM matches ORDER BY perfil")
    ]
    perfiles = sorted(
        (
            {
                "perfil": nombre,
                "total": contar(con, solo_vivas=False, perfil=nombre),
                "nuevos": contar(
                    con, solo_vivas=False, perfil=nombre, estado_revision="nuevo"
                ),
            }
            for nombre in nombres
        ),
        key=lambda p: -p["total"],
    )

    return {
        # Todas las coincidencias, sin filtrar por plazo ni por triaje. Es el
        # denominador de "57 de 668".
        "coincidencias": contar(con, solo_vivas=False),
        "en_plazo": contar(con, solo_vivas=True),
        "cierran_7_dias": contar(con, solo_vivas=True, cierran_en_dias=7),
        "sin_revisar": contar(con, solo_vivas=False, estado_revision="nuevo"),
        "siguiendo": contar(con, solo_vivas=False, estado_revision="siguiendo"),
        "presentadas": contar(con, solo_vivas=False, estado_revision="presentada"),
        "novedades": _novedades(con),
        "ultima_visita": _ultima_visita(con),
        "ultima_busqueda": _ultima_busqueda(con),
        "por_perfil": perfiles,
        "bundesland": [
            dict(f)
            for f in con.execute(
                """SELECT l.bundesland, COUNT(DISTINCT l.id) AS total
                     FROM licitaciones l JOIN matches m ON m.licitacion_id = l.id
                    WHERE l.bundesland IS NOT NULL
                    GROUP BY l.bundesland ORDER BY total DESC"""
            )
        ],
        "fuentes": salud(con, en_marcha),
        "cobertura": cobertura(con),
        "historico": ventana_historico(con),
    }


def ventana_historico(con) -> dict:
    """Cuánto histórico hay pedido, cuánto se puede pedir y qué cuesta ampliarlo.

    Viaja dentro de `/api/resumen` en lugar de tener endpoint propio porque la pantalla
    lo pinta en el mismo bloque que la cobertura, que ya sale de ahí.
    """
    from . import pipeline

    meses = pipeline.meses_historico(con)
    return {
        "meses": meses,
        "maximo": pipeline.MESES_HISTORICO_MAX,
        "ampliable": meses < pipeline.MESES_HISTORICO_MAX,
        # El precio del salto, dicho antes de darlo y no después.
        "coste_ampliar": next(
            (e.get("coste_por_anio") for e in pipeline.ETAPAS_PRIMERA_CARGA
             if e.get("ventana_completa")),
            None,
        ),
    }


def cobertura(con) -> dict:
    """Los agujeros de esta base, en cifras, para que la pantalla no tenga que inventarlas.

    Solo lo que cambia con los datos: cuántos expedientes se quedan sin comunidad —y por
    tanto fuera del reparto territorial y del filtro— y desde cuándo tiene histórico cada
    fuente, que es muy desigual. El resto de las limitaciones son prosa fija y viven en la
    interfaz, porque no dependen de la base.

    Se calcula sobre expedientes y entrando por `matches`, como la Analítica, para que las
    dos pestañas no digan cosas distintas.
    """
    entrada, params = _entrada_expedientes()
    fila = con.execute(
        "SELECT COUNT(*) AS expedientes,"
        "       COUNT(CASE WHEN cc IS NULL THEN 1 END) AS sin_comunidad,"
        "       COUNT(CASE WHEN cc IS NULL AND fu LIKE '%ted%' THEN 1 END) AS sin_comunidad_ted"
        "  FROM ("
        f"   SELECT {_GRUPO} AS g, MAX(l.bundesland) AS cc,"
        "          GROUP_CONCAT(DISTINCT l.fuente) AS fu"
        f"  {entrada} GROUP BY g)", params
    ).fetchone()
    fuentes = [
        {"fuente": f["fuente"], "desde": f["desde"], "hasta": f["hasta"]}
        for f in con.execute(
            "SELECT l.fuente,"
            "       MIN(substr(l.fecha_publicacion, 1, 7)) AS desde,"
            "       MAX(substr(l.fecha_publicacion, 1, 7)) AS hasta"
            f"{entrada} AND l.fecha_publicacion IS NOT NULL"
            " GROUP BY l.fuente ORDER BY l.fuente", params)
    ]
    datos = {k: fila[k] for k in fila.keys()}
    datos["fuentes"] = fuentes
    return datos


def _ultima_busqueda(con) -> str | None:
    """Cuándo terminó la última ingesta que fue bien, para poder mostrarla."""
    fila = con.execute(
        """SELECT MAX(terminado_en) AS cuando FROM ingest_log
            WHERE ok = 1 AND terminado_en IS NOT NULL"""
    ).fetchone()
    return fila["cuando"] if fila else None


def _preferencia(con, clave: str) -> str | None:
    fila = con.execute("SELECT valor FROM preferencias WHERE clave = ?", (clave,)).fetchone()
    return fila["valor"] if fila else None


def _ultima_visita(con) -> str | None:
    return _preferencia(con, "ultima_visita")


def _marca_visita(con) -> int | None:
    """Último id de licitación visto en la visita anterior.

    Se usa el id y no la hora a propósito: las marcas de tiempo tienen resolución de
    segundo, así que una ingesta que termina en el mismo segundo en que se marca la
    visita dejaría sus licitaciones fuera de las novedades. Los ids son monotónicos
    y no tienen ese problema.
    """
    valor = _preferencia(con, "ultima_visita_id")
    try:
        return int(valor) if valor is not None else None
    except ValueError:
        return None


def _novedades(con) -> int:
    """Coincidencias que han entrado después de la última visita."""
    marca = _marca_visita(con)
    if marca is None:
        return 0
    return con.execute(
        """SELECT COUNT(DISTINCT l.id)
             FROM licitaciones l
             JOIN matches m ON m.licitacion_id = l.id
             LEFT JOIN revisiones r ON r.licitacion_id = l.id
            WHERE l.id > ?
              AND COALESCE(r.estado, 'nuevo') != 'descartado'""",
        (marca,),
    ).fetchone()[0]


def salud(con, en_marcha: bool = False) -> list[dict]:
    filas = con.execute(
        """SELECT l.fuente, l.iniciado_en, l.terminado_en, l.vistos, l.nuevos,
                  l.actualizados, l.ok, l.error
             FROM ingest_log l
             JOIN (SELECT fuente, MAX(id) AS ult FROM ingest_log GROUP BY fuente) u
               ON u.ult = l.id
            ORDER BY l.fuente"""
    ).fetchall()
    salida = []
    for f in filas:
        d = dict(f)
        # Una fuente que no trae nada y una fuente rota se ven igual si no se avisa.
        #
        # Pero mientras la ingesta corre su fila está abierta (`terminado_en` a NULL y
        # `ok` a 0), y eso es indistinguible de un fallo si solo se mira `ok`. Sin
        # `en_marcha`, una carga inicial en segundo plano pintaba «la última ingesta
        # falló» en rojo durante las dos horas que dura.
        #
        # Hace falta el dato de fuera, no basta con `terminado_en`: una ingesta cortada
        # a lo bruto deja la fila abierta para siempre, y ésa sí es un fallo que hay
        # que contar.
        d["en_curso"] = d["terminado_en"] is None and en_marcha
        d["aviso"] = None
        if not d["en_curso"]:
            if not d["ok"]:
                d["aviso"] = "der letzte Abruf ist fehlgeschlagen"
            elif d["vistos"] == 0:
                d["aviso"] = "der letzte Abruf hat nichts gebracht"
        salida.append(d)
    return salida


# Ordenados de más largo a más corto: hay que quitar "gmbh & co. kg" antes de que
# "gmbh" lo parta por la mitad y deje un "co kg" suelto.
# Van en la forma que tienen DESPUÉS de quitar la puntuación, porque ese paso va antes:
# «GmbH & Co. KG» llega aquí como «gmbh co kg».
_SUFIJOS = (
    "gesellschaft mit beschrankter haftung",
    "ug haftungsbeschrankt", "gmbh co kgaa", "gmbh co kg", "gmbh und co kg",
    "ag co kg", "gmbh co", "partg mbb", "e kfm", "e kfr",
    "gmbh", "mbh", "kgaa", "gbr", "ohg", "partg", "partmbb",
    "kg", "ag", "ug", "se", "ev", "ek", "arge", "bietergemeinschaft",
)


def normalizar_empresa(nombre: str | None) -> str | None:
    """Agrupa variantes de la misma razón social.

    "Bechtle GmbH & Co. KG", "Bechtle GmbH & Co KG" y "BECHTLE GMBH & CO. KG" son el
    mismo proveedor y aparecerían como tres. Sin esto, un ranking de adjudicatarios no
    sirve para nada.

    El «&» es la diferencia con el castellano: forma parte de la mitad de las razones
    sociales alemanas y, si no se trata como puntuación, "GmbH & Co. KG" no se recorta.
    """
    if not nombre:
        return None
    import re
    import unicodedata

    t = unicodedata.normalize("NFD", nombre.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    # El & entra aquí y no es un detalle: forma parte de la mitad de las razones
    # sociales alemanas («GmbH & Co. KG»), y sin tratarlo como puntuación el sufijo no
    # se recorta y la misma empresa sale tres veces en el ranking.
    t = re.sub(r"[.,;:()&\"']", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    for sufijo in _SUFIJOS:
        t = re.sub(rf"(^|\s){re.escape(sufijo)}(\s|$)", " ", t)
    return re.sub(r"\s+", " ", t).strip() or None


def competencia(con: sqlite3.Connection, *, limite: int = 20) -> list[dict]:
    """Quién se está llevando estos contratos y por cuánto.

    Sale gratis del historial y probablemente vale más que las alertas de lo nuevo:
    dice contra quién se compite, a qué precios y en qué organismos.

    En la aplicación esto es la pestaña «Adjudicatarios» (`/api/adjudicatarios`); el
    nombre de aquí se quedó del original y no lo ve nadie desde fuera.
    """
    # Deduplicar por licitación no es opcional: sin ello, una que casa con dos
    # perfiles se contaba dos veces y el ranking no cuadraba con su propio
    # desglose (Vodafone salía con 6 contratos y al abrirlo mostraba 4). Se hace
    # con un DISTINCT sobre `matches` en lugar de un `GROUP BY l.id` sobre el
    # cruce, porque además de deduplicar es lo que decide por dónde entra la
    # consulta: partiendo de las ~3.000 filas de `matches` y buscando cada
    # licitación por clave primaria, en vez de recorrer las 450.000 de
    # `licitaciones`. Es la misma lección que `contratos_de`: 4,6 s -> 0,03 s.
    filas = con.execute(
        """SELECT l.adjudicatario,
                  COALESCE(l.importe_adjudicacion, l.importe_referencia) AS importe,
                  l.organo
             FROM (SELECT DISTINCT licitacion_id FROM matches) m
             JOIN licitaciones l ON l.id = m.licitacion_id
            WHERE l.adjudicatario IS NOT NULL"""
    ).fetchall()

    agrupado: dict[str, dict] = {}
    for f in filas:
        clave = normalizar_empresa(f["adjudicatario"])
        if not clave:
            continue
        entrada = agrupado.setdefault(
            clave, {"empresa": f["adjudicatario"], "contratos": 0, "importe": 0.0,
                    "organos": set(), "variantes": set()}
        )
        entrada["contratos"] += 1
        entrada["importe"] += f["importe"] or 0
        if f["organo"]:
            entrada["organos"].add(f["organo"])
        entrada["variantes"].add(f["adjudicatario"])

    salida = []
    for datos in agrupado.values():
        # Se muestra la variante más larga: suele ser la razón social completa.
        datos["empresa"] = max(datos["variantes"], key=len)
        datos["organos"] = len(datos["organos"])
        datos.pop("variantes")
        salida.append(datos)
    salida.sort(key=lambda d: (-d["contratos"], -d["importe"]))
    return salida[:limite]


def contratos_de(con: sqlite3.Connection, empresa: str, *, limite: int = 60) -> list[dict]:
    """Contratos de un adjudicatario, agrupando sus variantes de razón social.

    El filtro fino tiene que hacerse en Python porque la agrupación de razones
    sociales no se puede expresar en SQL. Traer las 99.000 filas adjudicadas de la
    tabla entera para descartarlas aquí tardaba 2,2 segundos; entrando por
    `matches` baja a milisegundos, porque son unos cientos de filas.

    Se probó además a acotar con un `LIKE` sobre el nombre, y era un error: el LIKE
    va contra la columna sin normalizar, que lleva acentos ("INFORMACIÓN"), mientras
    el término salía de la clave ya normalizada. Perdía variantes de la misma
    empresa y el desglose no cuadraba con el ranking.
    """
    clave = normalizar_empresa(empresa)
    if not clave:
        return []

    filas = con.execute(
        """SELECT l.id, l.objeto, l.organo, l.adjudicatario, l.fecha_adjudicacion,
                  l.fecha_fin_prevista, l.url_detalle,
                  COALESCE(l.importe_adjudicacion, l.importe_referencia) AS importe
             FROM licitaciones l
            WHERE l.adjudicatario IS NOT NULL
              AND l.id IN (SELECT licitacion_id FROM matches)
            ORDER BY l.fecha_adjudicacion DESC, l.id ASC"""
    ).fetchall()
    salida = [dict(f) for f in filas if normalizar_empresa(f["adjudicatario"]) == clave]
    return salida[:limite]


def motivos_descarte(con: sqlite3.Connection) -> list[dict]:
    """Por qué se descarta, para afinar los perfiles con datos en vez de a ojo.

    Cuenta expedientes, como el resto de la aplicación. El triaje se guarda en todos
    los anuncios del expediente (ver `db.ids_del_grupo`), así que contar filas de
    `revisiones` diría "4 descartadas por importe bajo" donde hubo una sola decisión
    sobre una licitación con cuatro anuncios.
    """
    return [
        dict(f)
        for f in con.execute(
            f"""SELECT motivo, COUNT(*) AS total FROM (
                    SELECT COALESCE(r.motivo_descarte, '(sin motivo)') AS motivo,
                           {_GRUPO} AS g
                      FROM revisiones r
                      JOIN licitaciones l ON l.id = r.licitacion_id
                     WHERE r.estado = 'descartado'
                     GROUP BY g, motivo
                )
                GROUP BY motivo ORDER BY total DESC"""
        )
    ]


# Ventana de vencimiento. La columna se compara DESNUDA a propósito: envolverla en
# `substr(..., 1, 10)` —como estaba— impide que SQLite use `idx_lic_fin` y convierte
# cada consulta en un recorrido completo de las 450.000 licitaciones (4,7 s la lista
# y 19,2 s las cuatro ventanas, medido). El `+1 day` del extremo superior es lo que
# conserva la semántica que daba el substr, porque las fechas pueden traer hora:
# '2027-02-11T10:00' vence el 11 de febrero y tiene que entrar en la ventana que
# acaba ese día. El `>= date('now')` descarta además los nulos por sí solo.
EN_VENTANA = (
    "l.fecha_fin_prevista >= date('now')"
    " AND l.fecha_fin_prevista < date('now', ?, '+1 day')"
)


def vencimientos(con: sqlite3.Connection, *, meses: int = 6, limite: int = 200) -> dict:
    """Contratos ya adjudicados cuyo plazo termina pronto.

    Es la lista para llamar ANTES de que salga el pliego, cuando el incumbente aún no
    ha renovado. Solo aparecen los que tienen fecha de fin conocida: si la fuente no
    la publica y no se puede deducir de la duración, la licitación no entra aquí en
    lugar de entrar con una fecha inventada.
    """
    filas = con.execute(
        f"""SELECT l.id, l.objeto, l.organo, l.bundesland, l.fuente, l.url_detalle,
                   l.adjudicatario, l.fecha_adjudicacion, l.duracion_meses,
                   l.fecha_inicio_ejecucion, l.fecha_fin_prevista,
                   COALESCE(l.importe_adjudicacion, l.importe_referencia) AS importe,
                   group_concat(DISTINCT m.perfil) AS perfil,
                   CAST(julianday(substr(l.fecha_fin_prevista, 1, 10))
                        - julianday('now') AS INTEGER) AS dias_para_vencer
              FROM licitaciones l
              JOIN matches m ON m.licitacion_id = l.id
              LEFT JOIN revisiones r ON r.licitacion_id = l.id
             WHERE {EN_VENTANA}
               AND COALESCE(r.estado, 'nuevo') != 'descartado'
             GROUP BY l.id
             ORDER BY l.fecha_fin_prevista ASC, importe DESC, l.id ASC
             LIMIT ?""",
        (f"+{int(meses)} months", limite),
    ).fetchall()

    agregado = con.execute(
        f"""SELECT COUNT(*) AS total, COALESCE(SUM(importe), 0) AS importe FROM (
                SELECT COALESCE(l.importe_adjudicacion, l.importe_referencia) AS importe
                  FROM licitaciones l
                  JOIN matches m ON m.licitacion_id = l.id
                  LEFT JOIN revisiones r ON r.licitacion_id = l.id
                 WHERE {EN_VENTANA}
                   AND COALESCE(r.estado, 'nuevo') != 'descartado'
                 GROUP BY l.id
            )""",
        (f"+{int(meses)} months",),
    ).fetchone()

    return {
        "total": agregado["total"],
        "importe_total": agregado["importe"],
        "meses": meses,
        "items": [dict(f) for f in filas],
        # Recuento de cada ventana, para poder comparar de un vistazo sin cambiar
        # de selección una por una.
        "por_ventana": ventanas_vencimiento(con),
    }


def ventanas_vencimiento(con: sqlite3.Connection,
                         ventanas: tuple[int, ...] = (3, 6, 12, 24)) -> list[dict]:
    """Cuántos contratos vencen en cada ventana y por cuánto importe.

    Una sola pasada, no una consulta por ventana: las ventanas estrechas están
    contenidas en la más ancha, así que sus recuentos salen de la misma lectura con
    agregación condicional. Cuatro consultas casi idénticas sobre 450.000
    licitaciones tardaban 19,2 s; así tarda 0,04 s.
    """
    meses = [int(m) for m in ventanas]
    if not meses:
        return []

    # El orden de los parámetros es el de aparición en el texto de la consulta:
    # primero los de cada ventana, y el de la ventana ancha al final, dentro de
    # EN_VENTANA.
    columnas, params = [], []
    for i, m in enumerate(meses):
        columnas.append(
            f"COUNT(CASE WHEN fin < date('now', ?, '+1 day') THEN 1 END) AS n{i}"
        )
        columnas.append(
            "COALESCE(SUM(CASE WHEN fin < date('now', ?, '+1 day') THEN importe END), 0)"
            f" AS i{i}"
        )
        params += [f"+{m} months", f"+{m} months"]
    params.append(f"+{max(meses)} months")

    f = con.execute(
        f"""SELECT {', '.join(columnas)} FROM (
                SELECT l.fecha_fin_prevista AS fin,
                       COALESCE(l.importe_adjudicacion, l.importe_referencia) AS importe
                  FROM licitaciones l
                  JOIN matches m ON m.licitacion_id = l.id
                  LEFT JOIN revisiones r ON r.licitacion_id = l.id
                 WHERE {EN_VENTANA}
                   AND COALESCE(r.estado, 'nuevo') != 'descartado'
                 GROUP BY l.id
            )""",
        params,
    ).fetchone()

    return [
        {"meses": m, "total": f[f"n{i}"], "importe": f[f"i{i}"]}
        for i, m in enumerate(meses)
    ]


def historial(con: sqlite3.Connection, licitacion_id: int) -> list[dict]:
    """Las versiones de una licitación, en el orden en que ocurrieron.

    Se ordena por la fecha de la fuente y no por `detectado_en`, que en el histórico
    es la misma para todas. El desempate por `id` no es decorativo: PLACSP publica
    fechas sin hora, y dos cambios del mismo día empatarían dejando el orden a merced
    del planificador de SQLite.
    """
    return [
        dict(f)
        for f in con.execute(
            """SELECT estado, estado_anterior, importe_referencia, adjudicatario,
                      detectado_en, fecha_cambio
                 FROM licitaciones_versiones
                WHERE licitacion_id = ?
                ORDER BY COALESCE(fecha_cambio, detectado_en), id""",
            (licitacion_id,),
        )
    ]


def para_csv(con: sqlite3.Connection, **filtros) -> tuple[list[str], list[list]]:
    filtros.setdefault("limite", 100000)
    datos = bandeja(con, **filtros)
    columnas = [
        "perfil", "puntuacion", "estado_revision", "dias_restantes", "organo", "objeto",
        "importe_referencia", "fecha_limite_presentacion", "fecha_publicacion", "estado",
        "procedimiento", "bundesland", "expediente", "cpv", "fuente", "url_detalle", "motivo",
        "adjudicatario", "notas",
    ]
    filas = []
    for item in datos["items"]:
        filas.append([
            " ".join(item[c]) if isinstance(item.get(c), list) else item.get(c, "")
            for c in columnas
        ])
    return columnas, filas


# --- Analítica -------------------------------------------------------------
#
# Los bloques de la pestaña «Analítica». No responden a «qué hay hoy» —eso es la
# bandeja— sino a las preguntas de patrón: cuándo publica este mercado, a cuánto cierra,
# cuánto tarda en decidirse. Salen del histórico.
#
# Tres decisiones gobiernan todo lo que hay aquí abajo, y las tres se pagaron midiendo:
#
# 1. **La entrada por `matches` se escribe, no se confía al planificador.** Con un filtro
#    de fecha en el WHERE, SQLite prefiere `idx_lic_pub` y recorre las 673.755 filas: 40
#    ms frente a 5 ms, y 486 frente a 335 en frío. Por eso todos los bloques arrancan en
#    `_entrada_expedientes()` y el filtro de perfil va DENTRO del subquery de `matches`,
#    donde usa `idx_match_perfil` (5 ms -> 1 ms).
# 2. **Ninguna cifra de dinero total.** `clave_grupo` no deduplica entre fuentes: medido,
#    126 expedientes (4,6%) están repetidos entre PLACSP y TED y arrastran 1.038 M€, un
#    9,7% de la suma. Y los cinco mayores importes son el 52,4% del total. Cualquier
#    «importe del mercado» sería un número inventado con cara de dato, así que aquí solo
#    hay medianas, tramos y recuentos.
# 3. **Los tramos y las medianas se calculan en Python** sobre la lista de valores que
#    devuelve UNA consulta, no con una consulta por tramo. Es la misma lección que
#    `ventanas_vencimiento` (una pasada en vez de cuatro), y entrando por `matches` la
#    lista son unos miles de filas: la parte caliente es el disco, no el bucle.

# Por debajo de estos mínimos un bloque NO se pinta: en su lugar la pestaña dice qué
# hacer para tener datos. Una mediana de doce casos presentada como una mediana es peor
# que un hueco, y esa es la doctrina del proyecto (ver `vencimientos`).
MINIMO_MESES_CALENDARIO = 12
MINIMO_COMPARABLES_BAJA = 50
MINIMO_EXPEDIENTES_CICLO = 50

# Tramos de importe del expediente, en euros. Los cortes no son redondos por gusto:
# sobre la base real el 54% de los expedientes cae entre 25.000 y 500.000, que es el
# tamaño de operación que este producto pelea de verdad.
TRAMOS_IMPORTE = (25_000, 100_000, 500_000, 1_000_000)

# Tramos de baja de adjudicación (%) y de días de publicación a adjudicación.
TRAMOS_BAJA = (5, 15, 30, 50)
TRAMOS_CICLO = (30, 60, 90, 180)

# Una baja por encima de esto no es una baja: son escalas distintas comparadas entre sí
# —un lote contra el total del acuerdo marco, una anualidad contra tres años—. Medido:
# 262 de 1.739 expedientes con los dos importes caen aquí, y su mediana es del 78%.
BAJA_MAXIMA_CREIBLE = 50.0

# Fronteras de puntuación. La moda de la base es 3,5 con el 41% de las coincidencias, así
# que 5,0 es la única frontera que separa algo: por encima quedan 77 expedientes, que es
# una lista corta que alguien se puede leer.
PUNTUACION_LISTA_CORTA = 5.0
PUNTUACION_INTERMEDIA = 4.0

# Los CPV que son literalmente el producto, no una familia donde podría caer. Se sacan
# aparte porque son los únicos sobre los que merece la pena afinar los términos.
CPV_DEL_PRODUCTO = {
    "79417000": "Sicherheitsberatung",
    "80510000": "Fachschulungen",
    "80500000": "Schulungsdienstleistungen",
}

MESES_RENOVACION = 6

# Por encima de esto no es un contrato, es un continente: sistemas dinámicos de
# adquisición y acuerdos marco de los que solo se gana un lote suelto. Medido sobre la
# base real: el mayor de los expedientes vivos son 2.646 M€ de un sistema dinámico de
# Racionalización y Centralización, el 86% de todo el dinero abierto de Madrid. Con él
# dentro del gráfico, diecisiete comunidades pintan una barra de un píxel y el reparto
# territorial deja de decir nada. Se dejan fuera de las barras y se enseñan con nombre
# —y con el total de su comunidad—, que es lo mismo que hace el bloque de importes con
# los cinco mayores: el outlier se ve, no se esconde.
IMPORTE_MAXIMO_COMPARABLE = 50_000_000

# Días entre la publicación y el cierre del plazo. Los cortes salen de la propia base:
# la mediana son 20 días y la mitad de los expedientes caen entre 15 y 31, así que un
# corte en 10 y otro en 20 son los que separan «no me da tiempo» de «me da».
TRAMOS_PLAZO = (10, 20, 30, 45)
MINIMO_EXPEDIENTES_PLAZO = 50

# Un plazo de más de un año no es un plazo de presentación: son anuncios previos con la
# fecha del contrato futuro, o erratas de la fuente. Medido: 11 de 1.876.
PLAZO_MAXIMO_CREIBLE = 400

# Cuántas filas se pintan en los dos bloques de nombres propios. Ocho órganos caben en
# una tarjeta sin que haya que hacer scroll dentro de ella.
TOP_ORGANOS = 8
TOP_PROCEDIMIENTOS = 7

# La fuente catalana publica el procedimiento en catalán, así que sin esto «Obert» y
# «Abierto» salen como dos barras distintas de lo que es el mismo procedimiento. La clave
# va en minúsculas porque es como se compara; el valor es lo que se pinta.
# Red de seguridad: el conector ya traduce `procurementMethodDetails` al ingerir, así que
# esto solo recoge lo que llegue sin mapear —variantes de caja, o un valor nuevo del
# servicio— para que el gráfico no parta en dos la misma verfahrensart.
PROCEDIMIENTOS_NORMALIZADOS = {
    "open": "Offenes Verfahren",
    "offenes verfahren": "Offenes Verfahren",
    "restricted": "Nichtoffenes Verfahren",
    "nichtoffenes verfahren": "Nichtoffenes Verfahren",
    "public announcement": "Öffentliche Ausschreibung",
    "öffentliche ausschreibung": "Öffentliche Ausschreibung",
    "beschränkte ausschreibung": "Beschränkte Ausschreibung",
    "verhandlungsvergabe": "Verhandlungsvergabe",
    "direktauftrag": "Direktauftrag",
}


def _condicion_viva() -> tuple[str, list]:
    """«Está en plazo»: estado vivo Y fecha límite sin vencer.

    Extraída porque la comparten el bloque de cartera y el de comunidades activas, y son
    exactamente las dos condiciones de `_condiciones(solo_vivas=True)`. Si cada bloque la
    escribe por su cuenta, uno se deja la mitad y la pestaña empieza a contar una cosa
    distinta de la cabecera: es justo el fallo que `_condiciones` existe para no repetir.
    """
    marcas = ", ".join("?" * len(ESTADOS_VIVOS))
    return (
        f"l.estado IN ({marcas})"
        " AND (l.fecha_limite_presentacion IS NULL"
        "      OR substr(l.fecha_limite_presentacion, 1, 10) >= date('now'))",
        sorted(ESTADOS_VIVOS),
    )


def _entrada_expedientes(perfil: str | None = None,
                         joins: str = "") -> tuple[str, list]:
    """El FROM/WHERE que comparten todos los bloques de la Analítica.

    Está extraído por el mismo motivo que `_condiciones`: si cada bloque escribe su
    propia entrada, uno se deja el `!= 'descartado'` o el filtro de perfil y la pestaña
    empieza a contradecirse consigo misma.

    La forma importa tanto como el contenido. Entrar por `(SELECT ... FROM matches)` hay
    que escribirlo: confiado al planificador, cualquier filtro de fecha en el WHERE le
    hace preferir `idx_lic_pub` y recorrer las 673.755 licitaciones en vez de las 3.705
    de `matches`. Y el perfil va dentro de ese subquery para que siga entrando por ahí.

    La puntuación se pre-agrega aquí (`MAX(puntuacion) GROUP BY licitacion_id`) y no con
    un `MAX(m.puntuacion)` sobre el cruce: así un expediente que casa con dos perfiles no
    duplica filas.

    `joins` existe para el único bloque que necesita una tercera tabla —el del ciclo, que
    lee `licitaciones_versiones`—. Se pasa en lugar de duplicar la entrada porque duplicarla
    es exactamente cómo un bloque se deja el `!= 'descartado'` y empieza a contar de más.
    """
    sub = "SELECT licitacion_id AS lid, MAX(puntuacion) AS punt FROM matches"
    params: list = []
    if perfil:
        sub += " WHERE perfil = ?"
        params.append(perfil)
    sub += " GROUP BY licitacion_id"
    return (
        f" FROM ({sub}) mm"
        " JOIN licitaciones l ON l.id = mm.lid"
        " LEFT JOIN revisiones r ON r.licitacion_id = l.id"
        f"{joins}"
        " WHERE COALESCE(r.estado, 'nuevo') != 'descartado'"
    ), params


def _rango(desde: str | None, hasta: str | None) -> tuple[str, list]:
    """El HAVING que acota por el mes de PRIMERA publicación del expediente.

    Va en HAVING y no en WHERE porque el mes del expediente es un agregado: un
    expediente «sale» cuando aparece su primer anuncio, no cuando se publica su
    adjudicación. Y de paso fija la semántica del filtro temporal de la pestaña:
    «expedientes que aparecieron por primera vez dentro del rango».
    """
    trozos, params = [], []
    if desde:
        trozos.append("mes >= ?")
        params.append(desde)
    if hasta:
        trozos.append("mes <= ?")
        params.append(hasta)
    return (" HAVING " + " AND ".join(trozos)) if trozos else "", params


def _expedientes(perfil: str | None, desde: str | None, hasta: str | None,
                 columnas: str = "") -> tuple[str, list]:
    """Un expediente por fila, con su mes, su importe y su puntuación.

    El importe del expediente es el MAYOR de sus anuncios, no el del anuncio más reciente
    que muestra la bandeja. Comparadas las dos opciones sobre los 483 expedientes con más
    de un anuncio, el último anuncio falla en la dirección peligrosa: en un acuerdo marco
    de ocho lotes que va de 195.000 € a 61,2 M€ dejaría pintados 195.000 y el comercial
    no lo abre. El MAX falla en la dirección segura —enseña el total cuando el hueco real
    es un lote— y entonces sí lo abre. El coste de esa decisión es que el euro de este
    bloque no coincide con el de la tarjeta, y por eso la métrica se etiqueta «el mayor
    importe publicado del expediente» y los tramos no son clicables.
    """
    entrada, params = _entrada_expedientes(perfil)
    having, p_rango = _rango(desde, hasta)
    extra = f", {columnas}" if columnas else ""
    sql = (
        f"SELECT {_GRUPO} AS g,"
        " MIN(substr(l.fecha_publicacion, 1, 7)) AS mes,"
        " MAX(COALESCE(l.importe_adjudicacion, l.importe_referencia)) AS imp,"
        " MAX(mm.punt) AS punt"
        f"{extra}"
        f"{entrada} GROUP BY g{having}"
    )
    return sql, params + p_rango


def _solo_en_rango(perfil: str | None, desde: str | None,
                   hasta: str | None) -> tuple[str, list]:
    """Acota una consulta por FILAS a los expedientes que están en el rango temporal.

    Los bloques que necesitan dos columnas del mismo anuncio (la baja) o una tabla
    distinta (el ciclo) no pueden agrupar por expediente y filtrar por el `MIN` a la vez,
    así que preguntan por los grupos que sí están dentro. Cuando no hay rango no se añade
    nada: el filtro más barato es el que no se escribe.
    """
    if not desde and not hasta:
        return "", []
    base, params = _expedientes(perfil, desde, hasta)
    return f" AND {_GRUPO} IN (SELECT g FROM ({base}))", params


def _mediana(valores: list) -> float | None:
    """La mediana de una lista YA ORDENADA. SQLite no la trae y `statistics` no se usa.

    Se da la mediana y nunca la media: sobre los importes reales la media son 4.052.863 €
    y la mediana 169.288 €, veinticuatro veces menos, porque cinco contratos son el 52%
    de la suma. Publicar la media sería dar por normal lo que es excepcional.
    """
    if not valores:
        return None
    mitad = len(valores) // 2
    if len(valores) % 2:
        return float(valores[mitad])
    return (valores[mitad - 1] + valores[mitad]) / 2


def _percentil(valores: list, p: int) -> float | None:
    """El percentil `p` de una lista ya ordenada, por el método más simple que hay.

    Sin interpolación a propósito: con dos mil valores la diferencia es de céntimos y un
    percentil interpolado invita a leer precisión que el dato no tiene.
    """
    if not valores:
        return None
    i = min(len(valores) - 1, max(0, int(round(p / 100 * (len(valores) - 1)))))
    return float(valores[i])


def _tramos(valores: list, cortes: tuple) -> list[dict]:
    """Reparte una lista ordenada en tramos. Es una PARTICIÓN: ni pierde ni duplica.

    En un solo recorrido y no con una consulta por tramo, que es la lección de
    `ventanas_vencimiento` (19,2 s -> 0,04 s) aplicada del lado de Python.
    """
    salida = [{"desde": 0 if i == 0 else cortes[i - 1],
               "hasta": corte, "expedientes": 0}
              for i, corte in enumerate(cortes)]
    salida.append({"desde": cortes[-1], "hasta": None, "expedientes": 0})
    for v in valores:
        for tramo in salida:
            if tramo["hasta"] is None or v < tramo["hasta"]:
                tramo["expedientes"] += 1
                break
    return salida


# Nombre en castellano de las divisiones CPV que aparecen en este nicho. No es la lista
# oficial completa —son 45 divisiones— sino las que salen de verdad; el resto cae en
# «otros», que es más honesto que enseñar un código a pelo.
DIVISIONES_CPV = {
    "72": "IT-Dienstleistungen",
    "48": "Softwarepakete",
    "79": "Unternehmens- und Sicherheitsdienste",
    "80": "Bildung und Schulung",
    "30": "Büro- und EDV-Ausstattung",
    "32": "Telekommunikationsausrüstung",
    "50": "Reparatur und Wartung",
    "64": "Telekommunikationsdienste",
    "71": "Architektur und Ingenieurwesen",
    "35": "Sicherheit und Verteidigung",
    "51": "Installationsleistungen",
    "75": "Öffentliche Verwaltung",
    "45": "Bauleistungen",
    "90": "Umwelt und Entsorgung",
    "33": "Medizinische Ausstattung",
    "98": "Sonstige Dienstleistungen",
}


def _bloque_calendario(con, perfil, desde, hasta) -> dict:
    """En qué meses sale el trabajo. El bloque más sólido de la pestaña.

    Medido sobre la base real: diciembre publica 2,4 veces más que agosto (97,5 contra 41
    expedientes de media). Eso es lo que decide cuándo hay que estar preparado, y sale
    del mes de PRIMERA publicación del expediente, no del último anuncio.

    El corte de cobertura sale de `MAX(fecha_publicacion)` y NO de `date('now')`: si el
    usuario no ingesta durante una semana, el número de días con datos baja solo y la
    pestaña lo confiesa sin que nadie tenga que programarlo.
    """
    base, params = _expedientes(perfil, desde, hasta)
    meses = [
        {"mes": f["mes"], "expedientes": f["expedientes"]}
        for f in con.execute(
            f"SELECT mes, COUNT(*) AS expedientes FROM ({base})"
            " WHERE mes IS NOT NULL GROUP BY mes ORDER BY mes", params)
    ]
    entrada, p_entrada = _entrada_expedientes(perfil)
    corte = con.execute(
        f"SELECT MAX(l.fecha_publicacion){entrada}", p_entrada).fetchone()[0]

    por_anio: dict = {}
    for m in meses:
        por_anio.setdefault(m["mes"][:4], {})[m["mes"][5:7]] = m["expedientes"]

    # Un año cuenta para la media solo si están sus doce meses Y no es el año en curso:
    # promediar un diciembre a medias hundiría justo el mes más fuerte.
    anio_en_curso = corte[:4] if corte else None
    completos = sorted(a for a, ms in por_anio.items()
                       if len(ms) == 12 and a != anio_en_curso)
    media = [
        {"mes": f"{i:02d}",
         "media": round(sum(por_anio[a].get(f"{i:02d}", 0) for a in completos)
                        / len(completos), 1)}
        for i in range(1, 13)
    ] if completos else []

    dias_del_mes = dias_con_datos = None
    if corte:
        anio, mes = int(corte[:4]), int(corte[5:7])
        primero = date(anio, mes, 1)
        siguiente = date(anio + 1, 1, 1) if mes == 12 else date(anio, mes + 1, 1)
        dias_del_mes = (siguiente - primero).days
        dias_con_datos = int(corte[8:10])

    return {
        "meses": meses,
        "media_por_mes": media,
        "anios_completos": completos,
        "anio_en_curso": anio_en_curso,
        "mes_en_curso": corte[:7] if corte else None,
        "corte": corte,
        "dias_con_datos": dias_con_datos,
        "dias_del_mes": dias_del_mes,
        "suficiente": len(meses) >= MINIMO_MESES_CALENDARIO,
        "minimo_meses": MINIMO_MESES_CALENDARIO,
    }


def _bloque_importes(con, perfil, desde, hasta) -> dict:
    """De qué tamaño son estas operaciones de verdad.

    Mediana y tramos, nunca media ni suma: la media son 4.052.863 € contra una mediana de
    169.288 —veinticuatro veces— porque cinco contratos son el 52,4% del total. Y de esos
    cinco, tres son el mismo acuerdo marco repetido en dos fuentes, que es por lo que
    tampoco hay ninguna cifra de dinero agregada en toda la pestaña.

    Los cinco mayores se devuelven CON NOMBRE. Una nota que diga «cuidado, hay outliers»
    no sirve de nada; una lista donde se ve que el 3.º, el 4.º y el 5.º son el mismo
    contrato enseña a desconfiar de las sumas para siempre.
    """
    base, params = _expedientes(perfil, desde, hasta)
    valores = [f[0] for f in con.execute(
        f"SELECT imp FROM ({base}) WHERE imp IS NOT NULL ORDER BY imp", params)]
    total = con.execute(f"SELECT COUNT(*) FROM ({base})", params).fetchone()[0]

    # Los cinco mayores, a nivel de anuncio y quedándose el primero de cada expediente:
    # así el que se enseña es exactamente el anuncio del que sale el importe del bloque.
    # El LIMIT holgado es el colchón para los expedientes con varios anuncios.
    entrada, p_entrada = _entrada_expedientes(perfil)
    filtro, p_rango = _solo_en_rango(perfil, desde, hasta)
    vistos, mayores = set(), []
    for f in con.execute(
        f"SELECT {_GRUPO} AS g, l.id, l.organo, l.objeto, l.fuente,"
        " COALESCE(l.importe_adjudicacion, l.importe_referencia) AS imp"
        f"{entrada}{filtro}"
        " AND COALESCE(l.importe_adjudicacion, l.importe_referencia) IS NOT NULL"
        " ORDER BY imp DESC, l.id LIMIT 60", p_entrada + p_rango
    ):
        if f["g"] in vistos:
            continue
        vistos.add(f["g"])
        mayores.append({k: f[k] for k in ("id", "organo", "objeto", "fuente", "imp")})
        if len(mayores) == 5:
            break

    return {
        "expedientes": total,
        "con_importe": len(valores),
        "sin_importe": total - len(valores),
        "mediana": _mediana(valores),
        "p25": _percentil(valores, 25),
        "p75": _percentil(valores, 75),
        "tramos": _tramos(valores, TRAMOS_IMPORTE),
        "mayores": mayores,
    }


def _bloque_baja(con, perfil, desde, hasta) -> dict:
    """Cuánto por debajo del presupuesto se están cerrando estos contratos.

    Se compara contra `importe_sin_iva` —el presupuesto base de licitación— y NO contra
    `importe_referencia`, que es una propiedad calculada que prefiere el valor estimado
    (ver `model.Licitacion.importe_referencia`). El valor estimado incluye prórrogas y
    modificaciones: su p90 es 2,65 veces el presupuesto base, así que compararlo con la
    adjudicación inventa bajas. Medido: daba una baja mediana del 30,0% donde la real es
    del 15,0%, y un comercial que fuera con ese 30% en la cabeza perdería margen.

    Los dos importes salen SIEMPRE del mismo anuncio. Cruzar el presupuesto de uno con la
    adjudicación de otro es la fábrica de bajas del 90%.

    Y casi la mitad de la muestra no sirve, así que se dice: 346 expedientes repiten el
    presupuesto como precio de adjudicación —la fuente no publicó la cifra— y 262 comparan
    escalas distintas (un lote contra el total, una anualidad contra tres años).
    """
    entrada, params = _entrada_expedientes(perfil)
    filtro, p_rango = _solo_en_rango(perfil, desde, hasta)
    filas = con.execute(
        "SELECT pres, adj FROM ("
        "  SELECT l.importe_sin_iva AS pres, l.importe_adjudicacion AS adj,"
        f"         ROW_NUMBER() OVER (PARTITION BY {_GRUPO}"
        "                             ORDER BY l.fecha_adjudicacion DESC, l.id DESC) AS rn"
        f"  {entrada}{filtro}"
        "    AND l.importe_sin_iva > 0 AND l.importe_adjudicacion > 0"
        ") WHERE rn = 1", params + p_rango
    ).fetchall()

    repetido = escalas = sobrecoste = 0
    bajas = []
    for f in filas:
        if f["adj"] == f["pres"]:
            repetido += 1
            continue
        baja = 100.0 * (f["pres"] - f["adj"]) / f["pres"]
        if baja < 0:
            sobrecoste += 1
        elif baja > BAJA_MAXIMA_CREIBLE:
            escalas += 1
        else:
            bajas.append(baja)
    bajas.sort()

    def pct(v):
        # Un decimal: la baja se lee «un 14%», no «un 13,884186928780824%».
        return round(v, 1) if v is not None else None

    return {
        "con_ambos_importes": len(filas),
        "comparables": len(bajas),
        "mediana": pct(_mediana(bajas)),
        "p25": pct(_percentil(bajas, 25)),
        "p75": pct(_percentil(bajas, 75)),
        "tramos": _tramos(bajas, TRAMOS_BAJA),
        "excluidos": [
            {"motivo": "die Quelle hat den Wert als Preis wiederholt",
             "expedientes": repetido},
            {"motivo": f"nicht vergleichbare Größen (Abschlag über "
                       f"{BAJA_MAXIMA_CREIBLE:.0f}%)", "expedientes": escalas},
            {"motivo": "über dem veröffentlichten Wert zugeschlagen", "expedientes": sobrecoste},
        ],
        "suficiente": len(bajas) >= MINIMO_COMPARABLES_BAJA,
        "minimo_comparables": MINIMO_COMPARABLES_BAJA,
    }


def _bloque_renovaciones(con, perfil) -> dict:
    """Cuántos contratos se acaban antes de que salga el pliego nuevo.

    Es el momento comercial bueno: cuando el contrato del incumbente se está acabando y
    todavía no hay pliego que discutir. Aquí solo va el RECUENTO —la lista ya existe en la
    pestaña Vencimientos, con su tarjeta y sus botones de ventana—: la Analítica dice
    cuánto hay, Vencimientos dice cuáles.

    Sin importe a propósito: es una suma, y las sumas de esta base arrastran un 10% de
    duplicados entre fuentes. Lo que cuenta aquí son puertas a las que llamar.

    Ignora el rango temporal de la pestaña, porque «vence en seis meses» es de AHORA:
    filtrarlo por «publicados en 2024» daría un número que no significa nada.
    """
    entrada, params = _entrada_expedientes(perfil)
    f = con.execute(
        "SELECT COUNT(*) AS expedientes, COUNT(inc) AS con_incumbente FROM ("
        f"  SELECT {_GRUPO} AS g, MAX(l.adjudicatario) AS inc"
        f"  {entrada} AND {EN_VENTANA}"
        "   GROUP BY g)", params + [f"+{MESES_RENOVACION} months"]
    ).fetchone()
    return {"expedientes": f["expedientes"], "con_incumbente": f["con_incumbente"],
            "meses": MESES_RENOVACION, "siempre_a_fecha_de_hoy": True}


def _bloque_ciclo(con, perfil, desde, hasta) -> dict:
    """Cuánto tarda una licitación en decidirse, para saber cuándo entra en el forecast.

    Se mide sobre `licitaciones_versiones.fecha_cambio` y NUNCA sobre `detectado_en`: las
    1,5 millones de versiones de esta base tienen todas la misma fecha, porque el
    histórico entró en una sola carga (está explicado en el esquema, `db.ESQUEMA`).

    Tampoco se mide restando las columnas de `licitaciones`: en PLACSP el anuncio de
    adjudicación es una fila propia con su propia `fecha_publicacion`, así que esa resta
    sale negativa en el 38% de los casos.

    La adjudicación tiene que ser una TRANSICIÓN (`estado_anterior IS NOT NULL`) y no
    cualquier versión adjudicada. La diferencia es enorme y medida: aceptando cualquiera,
    el 45,3% de los expedientes salía con cero o menos días, porque una ficha que nace ya
    adjudicada —el anuncio de adjudicación publicado como ficha propia— no recorrió nada y
    su primera versión lleva la fecha de la adjudicación. Exigiendo la transición se cae al
    0,1% (1 caso de 981) y la mediana queda en 78 días. Lo que se mide es un recorrido
    observado, no la resta de dos fichas que no se hablan.
    """
    entrada, params = _entrada_expedientes(
        perfil, joins=" JOIN licitaciones_versiones v ON v.licitacion_id = l.id")
    filtro, p_rango = _solo_en_rango(perfil, desde, hasta)
    filas = con.execute(
        "SELECT CAST(julianday(substr(adj, 1, 10))"
        "          - julianday(substr(pub, 1, 10)) AS INTEGER) AS dias FROM ("
        f"  SELECT {_GRUPO} AS g,"
        "          MIN(CASE WHEN v.estado_anterior IS NULL THEN v.fecha_cambio END) AS pub,"
        f"          MIN(CASE WHEN v.estado = '{ESTADO_ADJUDICADA}'"
        "                    AND v.estado_anterior IS NOT NULL"
        "                   THEN v.fecha_cambio END) AS adj"
        f"  {entrada}{filtro} AND v.fecha_cambio IS NOT NULL"
        "   GROUP BY g"
        ") WHERE pub IS NOT NULL AND adj IS NOT NULL"
        " ORDER BY dias", params + p_rango
    ).fetchall()

    dias = sorted(f["dias"] for f in filas if f["dias"] is not None and f["dias"] > 0)
    return {
        "expedientes": len(dias),
        "descartados_sin_recorrido": len(filas) - len(dias),
        "mediana_dias": round(_mediana(dias)) if dias else None,
        "p25": round(_percentil(dias, 25)) if dias else None,
        "p75": round(_percentil(dias, 75)) if dias else None,
        "tramos": _tramos(dias, TRAMOS_CICLO),
        "suficiente": len(dias) >= MINIMO_EXPEDIENTES_CICLO,
        "minimo_expedientes": MINIMO_EXPEDIENTES_CICLO,
    }


def _bloque_cpv(con, perfil, desde, hasta) -> dict:
    """En qué epígrafes cae esto, y cuáles son literalmente el producto.

    Es el único bloque que produce una acción sobre la configuración y no sobre un
    cliente: los tres CPV de `CPV_DEL_PRODUCTO` son los que merece la pena vigilar en los
    términos de búsqueda.

    Recuentos absolutos y NINGÚN porcentaje: un expediente tiene 2,05 códigos de media, así
    que los porcentajes sumarían más de 100 y no se pueden apilar en una tarta. El `set`
    por expediente es lo que evita contar tres veces el mismo código cuando el expediente
    tiene tres anuncios.
    """
    base, params = _expedientes(perfil, desde, hasta,
                               columnas="GROUP_CONCAT(l.cpv, ' ') AS cpvs")
    divisiones: dict = {}
    codigos: dict = {}
    con_cpv = total = 0
    for fila in con.execute(f"SELECT cpvs FROM ({base})", params):
        total += 1
        propios = {c for c in (fila["cpvs"] or "").split() if c}
        if not propios:
            continue
        con_cpv += 1
        for division in {c[:2] for c in propios}:
            divisiones[division] = divisiones.get(division, 0) + 1
        for codigo in {c.split("-")[0] for c in propios}:
            codigos[codigo] = codigos.get(codigo, 0) + 1

    return {
        "expedientes": total,
        "con_cpv": con_cpv,
        "sin_cpv": total - con_cpv,
        "divisiones": [
            {"division": d, "nombre": DIVISIONES_CPV.get(d, "Sonstige"), "expedientes": n}
            for d, n in sorted(divisiones.items(), key=lambda x: -x[1])[:6]
        ],
        "del_producto": [
            {"codigo": c, "nombre": nombre, "expedientes": codigos.get(c, 0)}
            for c, nombre in CPV_DEL_PRODUCTO.items()
        ],
    }


def _bloque_cartera(con, perfil) -> dict:
    """Si esto es un pipeline o un archivo histórico. Va último, y es el que más importa.

    Un tablero que enseña 2.716 coincidencias y no dice que solo 45 tienen plazo abierto
    es un tablero que engaña. Que lo diga el propio producto es lo que compra credibilidad
    para los otros seis bloques.

    `con_plazo_abierto` usa exactamente las mismas dos condiciones que `_condiciones` con
    `solo_vivas` —estado vivo Y plazo sin vencer— para que cuadre al expediente con el
    contador «abiertas» de la cabecera. Si se separan, la pestaña y la bandeja empiezan a
    contar cosas distintas, que es el fallo que `_condiciones` existe para no repetir.

    Ignora el rango temporal: «con plazo abierto» es de ahora, no de 2024.
    """
    entrada, params = _entrada_expedientes(perfil)
    viva, p_viva = _condicion_viva()
    f = con.execute(
        "SELECT COUNT(*) AS expedientes,"
        "       COUNT(CASE WHEN punt > ? THEN 1 END) AS lista_corta,"
        "       COUNT(CASE WHEN punt > ? AND punt <= ? THEN 1 END) AS intermedios,"
        "       COUNT(CASE WHEN punt <= ? THEN 1 END) AS resto,"
        "       COUNT(CASE WHEN abierta = 1 THEN 1 END) AS con_plazo_abierto,"
        "       COUNT(CASE WHEN consulta = 1 AND imp IS NULL THEN 1 END)"
        "           AS consultas_previas_sin_importe"
        "  FROM ("
        f"   SELECT {_GRUPO} AS g, MAX(mm.punt) AS punt,"
        "          MAX(COALESCE(l.importe_adjudicacion, l.importe_referencia)) AS imp,"
        f"         MAX(CASE WHEN {viva} THEN 1 ELSE 0 END) AS abierta,"
        "          MIN(CASE WHEN l.fuente = 'placsp:consultas_previas' THEN 1 ELSE 0 END)"
        "              AS consulta"
        f"  {entrada} GROUP BY g)",
        # El orden es el de APARICIÓN en el texto, no el lógico: las cuatro fronteras de
        # puntuación del SELECT de fuera, luego los estados vivos —que van dentro del
        # subquery, en la columna `abierta`— y al final el perfil, que entra con `entrada`.
        # Es la misma trampa que ya documenta `ventanas_vencimiento`.
        [PUNTUACION_LISTA_CORTA, PUNTUACION_INTERMEDIA, PUNTUACION_LISTA_CORTA,
         PUNTUACION_INTERMEDIA] + p_viva + params
    ).fetchone()

    # El estado del anuncio más reciente de cada expediente, el mismo criterio que la
    # tarjeta de la bandeja: si se contaran todos los anuncios, un expediente adjudicado
    # aparecería también como publicado.
    estados = [
        {"estado": e["estado"], "expedientes": e["expedientes"]}
        for e in con.execute(
            "SELECT estado, COUNT(*) AS expedientes FROM ("
            "  SELECT l.estado AS estado,"
            f"        ROW_NUMBER() OVER (PARTITION BY {_GRUPO}"
            "                            ORDER BY l.fecha_publicacion DESC, l.id DESC) AS rn"
            f"  {entrada}"
            ") WHERE rn = 1 GROUP BY estado ORDER BY expedientes DESC", params)
    ]

    datos = {k: f[k] for k in f.keys()}
    datos["estados"] = estados
    datos["puntuacion_lista_corta"] = PUNTUACION_LISTA_CORTA
    datos["siempre_a_fecha_de_hoy"] = True
    # La frase sale del dato y no escrita a mano: el día que el usuario ingeste a diario
    # durante meses, «esto es un archivo» dejará de ser verdad y tiene que dejar de decirse.
    abiertas = datos["con_plazo_abierto"]
    total = datos["expedientes"] or 1
    datos["es_archivo_historico"] = abiertas / total < 0.05
    return datos


def _reparto_por_comunidad(filas: list, campo: str) -> dict:
    """Reparte una lista de expedientes por comunidad, apartando los macro-contratos.

    Devuelve las dos lecturas a la vez, y a propósito: las barras con los contratos
    comparables, y al pie el total CON los grandes dentro. Dar solo lo primero escondería
    que en los expedientes vivos el 96% del dinero está en cuatro operaciones; dar solo
    lo segundo pintaría un gráfico de una sola barra. Las dos cifras juntas son el dato.
    """
    con_comunidad = [f for f in filas if f["bundesland"]]
    dentro = [f for f in con_comunidad if f[campo] <= IMPORTE_MAXIMO_COMPARABLE]
    fuera = [f for f in con_comunidad if f[campo] > IMPORTE_MAXIMO_COMPARABLE]

    por: dict = {}
    for f in dentro:
        d = por.setdefault(f["bundesland"], {"importe": 0.0, "expedientes": 0})
        d["importe"] += f[campo]
        d["expedientes"] += 1

    # El total de verdad de cada comunidad afectada, macro-contratos incluidos. Va en la
    # tabla de excluidos porque es la cifra que cambia la lectura del gráfico: Madrid
    # pasa de 70,6 M€ en la barra a 3.070,1 M€ reales.
    total_real = {c: v["importe"] for c, v in por.items()}
    for f in fuera:
        total_real[f["bundesland"]] = total_real.get(f["bundesland"], 0.0) + f[campo]

    # El recuento va aparte y SIN umbral. Un recuento no lo distorsiona un contrato
    # grande, así que apartarlo solo escondería licitaciones que existen y a las que se
    # puede ir. Es la diferencia que hace que Madrid diga 14 aquí y 12 en las barras de
    # dinero, y por eso el bloque de número lo explica al pie en lugar de disimularlo.
    cuantos: dict = {}
    for f in con_comunidad:
        cuantos[f["bundesland"]] = cuantos.get(f["bundesland"], 0) + 1

    en_barras = sum(v["importe"] for v in por.values())
    excluido = sum(f[campo] for f in fuera)
    return {
        "recuento": [
            {"bundesland": c, "expedientes": n}
            for c, n in sorted(cuantos.items(), key=lambda x: (-x[1], x[0]))
        ],
        "expedientes_contados": len(con_comunidad),
        "comunidades": [
            {"bundesland": c, "importe": v["importe"], "expedientes": v["expedientes"]}
            for c, v in sorted(por.items(), key=lambda x: -x[1]["importe"])
        ],
        "expedientes": len(dentro),
        "sin_comunidad": len(filas) - len(con_comunidad),
        "excluidos": [
            {"bundesland": f["bundesland"], "organo": f["organo"], "objeto": f["objeto"],
             "importe": f[campo], "total_de_su_comunidad": total_real[f["bundesland"]]}
            for f in sorted(fuera, key=lambda x: -x[campo])
        ],
        "importe_maximo": IMPORTE_MAXIMO_COMPARABLE,
        "importe_en_barras": en_barras,
        "importe_excluido": excluido,
        "importe_con_excluidos": en_barras + excluido,
    }


def _bloque_comunidades(con, perfil, desde, hasta) -> dict:
    """Dónde se adjudica el dinero, y dónde hay dinero vivo ahora mismo.

    Es el ÚNICO sitio de la pestaña donde se suman importes, y hace falta explicar por
    qué no contradice a los otros bloques. La suma del mercado sería un número inventado
    porque `clave_grupo` no cruza fuentes: 126 expedientes están repetidos entre PLACSP y
    TED. Pero TED no publica región —no expone ningún campo de territorio usable—, así
    que de cada pareja duplicada la copia de TED cae en «sin comunidad» y no en el total
    de ninguna comunidad. El reparto territorial es, justamente, el único corte de esta
    base que se puede sumar sin contar dos veces lo mismo.

    Lo que sí rompía el gráfico eran los macro-contratos, y de eso se encarga
    `_reparto_por_comunidad` con `IMPORTE_MAXIMO_COMPARABLE`.

    **El importe se agrega en dos niveles, y esto no es un capricho.** Las fuentes no
    publican igual: PLACSP emite una fila por licitación y Cataluña una fila POR LOTE
    (`catalunya.py`, `id_externo = f"{interno}#{lote}"`). Con un `MAX` a secas, de un
    contrato catalán de ocho lotes se cuenta el lote mayor y se tiran los otros siete.
    Medido: Cataluña es la ÚNICA comunidad donde `MAX` y suma difieren —51,9 M€ contra
    61,7 M€, un 19%— y por eso salía cuarta cuando por actividad es la segunda. Pero
    sumar en bruto tampoco vale: hay 59 pares (expediente, lote) con más de un anuncio,
    que son republicaciones del mismo lote, y sumarlas daría 76,7 M€. Así que `MAX`
    dentro de cada lote —mata la republicación— y `SUMA` entre lotes —recompone el
    contrato—. Las demás comunidades no se mueven ni un céntimo.

    Una sola consulta para las dos vistas, y el reparto en Python, como todo lo demás
    aquí. El rango temporal se aplica en Python y no con un HAVING porque las dos vistas
    no lo quieren igual: las adjudicaciones son historia y se acotan, y las activas son
    de hoy —un pliego abierto publicado en 2024 sigue abierto hoy— y lo ignoran, igual
    que la cartera y las renovaciones.
    """
    entrada, params = _entrada_expedientes(perfil)
    viva, p_viva = _condicion_viva()
    # El orden de los parámetros es el de APARICIÓN en el texto: `viva` va en el SELECT,
    # que se escribe antes del FROM donde entra el perfil. La misma trampa que ya
    # documentan `ventanas_vencimiento` y `_bloque_cartera`.
    por_lote = (
        f"SELECT {_GRUPO} AS g, COALESCE(l.lote_num, '') AS lote,"
        " MIN(substr(l.fecha_publicacion, 1, 7)) AS mes,"
        " MAX(l.bundesland) AS bundesland,"
        " MAX(l.importe_adjudicacion) AS adj,"
        " MAX(l.importe_referencia) AS pres,"
        " MAX(l.organo) AS organo,"
        " MAX(l.objeto) AS objeto,"
        f" MAX(CASE WHEN {viva} THEN 1 ELSE 0 END) AS abierta,"
        " MAX(CASE WHEN l.importe_adjudicacion IS NOT NULL THEN 1 ELSE 0 END) AS adjudicada"
        f"{entrada} GROUP BY g, lote"
    )
    filas = list(con.execute(
        "SELECT g, MIN(mes) AS mes, MAX(bundesland) AS bundesland,"
        " SUM(adj) AS adj, SUM(pres) AS pres,"
        " MAX(organo) AS organo, MAX(objeto) AS objeto,"
        " MAX(abierta) AS abierta, MAX(adjudicada) AS adjudicada"
        f" FROM ({por_lote}) GROUP BY g",
        p_viva + params,
    ))

    def en_rango(f) -> bool:
        """Lo mismo que el HAVING de `_rango`, incluido que un mes NULL no pasa el filtro."""
        if f["mes"] is None:
            return not desde and not hasta
        if desde and f["mes"] < desde:
            return False
        if hasta and f["mes"] > hasta:
            return False
        return True

    adjudicadas = [f for f in filas
                   if f["adjudicada"] and f["adj"] is not None and en_rango(f)]
    # «No adjudicada» además de «en plazo»: la fuente a veces deja el estado en vivo
    # después de publicar la adjudicación, y ese dinero ya no está en juego.
    activas = [f for f in filas
               if f["abierta"] and not f["adjudicada"] and f["pres"] is not None]

    datos = {
        "adjudicadas": _reparto_por_comunidad(adjudicadas, "adj"),
        "activas": _reparto_por_comunidad(activas, "pres"),
    }
    datos["activas"]["siempre_a_fecha_de_hoy"] = True
    return datos


def _bloque_plazo(con, perfil, desde, hasta) -> dict:
    """Cuántos días hay desde que sale el anuncio hasta que se cierra el plazo.

    No es lo mismo que el ciclo: aquél mide cuándo se DECIDE —para el forecast— y éste
    mide cuánto tiempo hay para ESCRIBIR la oferta, que es lo que decide si un expediente
    se puede pelear o ya nació perdido. Medido: la mediana son 20 días.

    Las dos fechas salen del MISMO anuncio, el primero del expediente, por el mismo
    motivo que en la baja: cruzar la publicación de uno con el plazo de otro fabrica
    plazos que nadie tuvo. Aun así quedan 159 expedientes con el plazo antes de la
    publicación —anuncios que repiten un plazo ya vencido— y se cuentan aparte en vez de
    colarlos como ceros, que hundirían la mediana.
    """
    entrada, params = _entrada_expedientes(perfil)
    filtro, p_rango = _solo_en_rango(perfil, desde, hasta)
    filas = con.execute(
        "SELECT pub, lim FROM ("
        "  SELECT l.fecha_publicacion AS pub, l.fecha_limite_presentacion AS lim,"
        f"        ROW_NUMBER() OVER (PARTITION BY {_GRUPO}"
        "                            ORDER BY l.fecha_publicacion ASC, l.id ASC) AS rn"
        f"  {entrada}{filtro}"
        ") WHERE rn = 1 AND pub IS NOT NULL AND lim IS NOT NULL",
        params + p_rango,
    ).fetchall()

    dias: list = []
    ya_vencido = disparatado = ilegible = 0
    for f in filas:
        try:
            d = (date.fromisoformat(f["lim"][:10]) - date.fromisoformat(f["pub"][:10])).days
        except ValueError:
            ilegible += 1
            continue
        if d < 0:
            ya_vencido += 1
        elif d > PLAZO_MAXIMO_CREIBLE:
            disparatado += 1
        else:
            dias.append(d)
    dias.sort()

    excluidos = [
        {"motivo": "die Frist war bei Veröffentlichung schon abgelaufen", "expedientes": ya_vencido},
        {"motivo": f"über {PLAZO_MAXIMO_CREIBLE} Tage (Vorinformationen)",
         "expedientes": disparatado},
    ]
    if ilegible:
        excluidos.append({"motivo": "fecha ilegible", "expedientes": ilegible})

    return {
        "con_ambas_fechas": len(filas),
        "expedientes": len(dias),
        "mediana_dias": round(_mediana(dias)) if dias else None,
        "p25": round(_percentil(dias, 25)) if dias else None,
        "p75": round(_percentil(dias, 75)) if dias else None,
        "tramos": _tramos(dias, TRAMOS_PLAZO),
        "excluidos": excluidos,
        "suficiente": len(dias) >= MINIMO_EXPEDIENTES_PLAZO,
        "minimo_expedientes": MINIMO_EXPEDIENTES_PLAZO,
    }


def _bloque_procedimiento(con, perfil, desde, hasta) -> dict:
    """Por qué puerta se entra: abierto, simplificado, restringido o por invitación.

    Importa porque no todas se pueden pelear igual. En un negociado sin publicidad no se
    puja: o te han invitado o no existes, y verlo contado es lo que dice cuánta parte de
    este mercado no se gana con una oferta sino antes.

    Los nombres se normalizan al castellano porque la fuente catalana publica en catalán
    y «Obert» y «Abierto» son el mismo procedimiento partido en dos barras.
    """
    base, params = _expedientes(perfil, desde, hasta,
                                columnas="MAX(l.procedimiento) AS proc")
    cuenta: dict = {}
    total = sin_dato = 0
    for f in con.execute(f"SELECT proc FROM ({base})", params):
        total += 1
        crudo = (f["proc"] or "").strip()
        if not crudo:
            sin_dato += 1
            continue
        nombre = PROCEDIMIENTOS_NORMALIZADOS.get(crudo.lower(), crudo)
        cuenta[nombre] = cuenta.get(nombre, 0) + 1

    orden = sorted(cuenta.items(), key=lambda x: (-x[1], x[0]))
    procedimientos = [{"procedimiento": k, "expedientes": n}
                      for k, n in orden[:TOP_PROCEDIMIENTOS]]
    cola = orden[TOP_PROCEDIMIENTOS:]
    if cola:
        # «el resto» y no «otros»: «Otros» es un valor que la propia fuente publica, y
        # dos filas con el mismo nombre y distinto significado no las distingue nadie.
        procedimientos.append({"procedimiento": "übrige",
                               "expedientes": sum(n for _, n in cola)})

    return {
        "expedientes": total,
        "sin_dato": sin_dato,
        "distintos": len(cuenta),
        "procedimientos": procedimientos,
    }


def _bloque_organos(con, perfil, desde, hasta) -> dict:
    """Quién compra esto de verdad, y cuáles repiten.

    Se agrupa por NOMBRE y no por `nif_organo`, aunque el NIF parezca lo correcto: 947
    expedientes no traen NIF, así que agrupar por ahí perdería más de lo que junta (865
    nombres distintos frente a 622 NIF, con un tercio de la base fuera).

    El nombre es el del órgano que firma, no el del organismo, así que la misma agencia
    puede salir tres veces con tres firmantes. No se fusiona a mano: inventar una regla
    de nombres parecidos junta cosas que no son la misma, y la pestaña lo advierte en vez
    de arreglarlo por su cuenta.
    """
    base, params = _expedientes(perfil, desde, hasta, columnas="MAX(l.organo) AS organo")
    cuenta: dict = {}
    total = sin_organo = 0
    for f in con.execute(f"SELECT organo FROM ({base})", params):
        total += 1
        nombre = (f["organo"] or "").strip()
        if not nombre:
            sin_organo += 1
            continue
        cuenta[nombre] = cuenta.get(nombre, 0) + 1

    orden = sorted(cuenta.items(), key=lambda x: (-x[1], x[0]))
    return {
        "expedientes": total,
        "sin_organo": sin_organo,
        "distintos": len(cuenta),
        "organos": [{"organo": k, "expedientes": n} for k, n in orden[:TOP_ORGANOS]],
    }


# Lo calculado, guardado hasta que la base cambie. NO es una caché por tiempo: un TTL
# pagaría el recálculo sin motivo y no lo pagaría cuando de verdad hace falta.
#
# Existe porque la primera visita cuesta ~970 ms y las siguientes 110: los 860 ms de
# diferencia son caché de página del sistema operativo —los 6.603 registros de versiones
# que toca el perfil están dispersos en un fichero de 3,4 GB—, no plan de ejecución. La
# misma consulta pasa de 881 ms a 14 ms en la segunda vuelta con el mismo plan, así que no
# hay SQL que lo arregle.
#
# El servidor es multihilo y esto se comparte entre hilos sin cerrojo: lo peor que puede
# pasar es que dos peticiones simultáneas calculen lo mismo, que cuesta un segundo y no
# corrompe nada.
_MEMO: dict = {}
_MEMO_MAXIMO = 8


def _firma_de_la_base(con) -> tuple:
    """Lo que tiene que cambiar para que la Analítica cambie. Cuesta 1,5 ms.

    Tres cosas pueden mover estas cifras y solo tres: que entren licitaciones nuevas, que
    se reevalúen los perfiles y que el usuario tríe algo. Una columna por cada una.
    """
    return tuple(con.execute(
        "SELECT (SELECT MAX(id) FROM licitaciones),"
        "       (SELECT COUNT(*) FROM matches),"
        "       (SELECT MAX(actualizado_en) FROM revisiones)").fetchone())


def analitica(con: sqlite3.Connection, *, perfil: str | None = None,
              desde: str | None = None, hasta: str | None = None) -> dict:
    """Qué patrones tiene este mercado: cuándo publica, a cuánto cierra, contra quién.

    Es la pestaña que contesta a las preguntas que no son «qué hay hoy»: en qué meses hay
    que estar preparado, con qué precio se entra, cuánto tarda una licitación en decidirse
    y en qué epígrafes cae el producto. Sale del histórico, no del pipeline.

    Todo se cuenta por EXPEDIENTE (`clave_grupo`), como `contar()`: a nivel de anuncio los
    mismos datos salen un 19% más altos —2.716 expedientes contra 3.365 anuncios— y ese
    19% no es mercado, son republicaciones y adjudicaciones por lotes del mismo contrato.

    Cada bloque declara su propia cobertura (`con_importe`, `comparables`, `excluidos`,
    `sin_cpv`): los agujeros de esta base son muy distintos según la columna —el 20% de
    las adjudicaciones repite el presupuesto en lugar del precio— y un porcentaje sin
    denominador es peor que no dar el dato.

    `desde` y `hasta` son 'AAAA-MM' y acotan por el mes de PRIMERA publicación del
    expediente. Los bloques de renovaciones y de cartera los ignoran a propósito, porque
    hablan de hoy, y lo dicen con `siempre_a_fecha_de_hoy`.
    """
    clave = (_firma_de_la_base(con), perfil, desde, hasta)
    if clave in _MEMO:
        return _MEMO[clave]

    entrada, p_entrada = _entrada_expedientes(perfil)
    filtro, p_rango = _solo_en_rango(perfil, desde, hasta)
    anuncios = con.execute(
        f"SELECT COUNT(*){entrada}{filtro}", p_entrada + p_rango).fetchone()[0]
    base, p_base = _expedientes(perfil, desde, hasta)
    expedientes = con.execute(f"SELECT COUNT(*) FROM ({base})", p_base).fetchone()[0]

    datos = {
        "generado_para": {
            "perfil": perfil, "desde": desde, "hasta": hasta,
            "expedientes": expedientes, "anuncios": anuncios,
        },
        "calendario": _bloque_calendario(con, perfil, desde, hasta),
        "importes": _bloque_importes(con, perfil, desde, hasta),
        "baja": _bloque_baja(con, perfil, desde, hasta),
        "renovaciones": _bloque_renovaciones(con, perfil),
        "ciclo": _bloque_ciclo(con, perfil, desde, hasta),
        "cpv": _bloque_cpv(con, perfil, desde, hasta),
        "comunidades": _bloque_comunidades(con, perfil, desde, hasta),
        "plazo": _bloque_plazo(con, perfil, desde, hasta),
        "procedimiento": _bloque_procedimiento(con, perfil, desde, hasta),
        "organos": _bloque_organos(con, perfil, desde, hasta),
        "cartera": _bloque_cartera(con, perfil),
    }

    if len(_MEMO) >= _MEMO_MAXIMO:
        _MEMO.clear()
    _MEMO[clave] = datos
    return datos
