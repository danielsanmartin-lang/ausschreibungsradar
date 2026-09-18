"""Motor de reglas: decide qué licitaciones interesan y deja constancia de por qué.

El problema real de esta herramienta no es traer datos, es el ruido. Medido sobre
datos alemanes reales, las licitaciones de concienciación aparecen con CPV 72200000,
72000000, 80500000, 79416000 y algunas SIN CPV: filtrar solo por CPV pierde la mayoría.
Y al revés, buscar "Schulung" a secas trae formación sanitaria, escolarización e higiene
del agua —toda compra de equipos incluye una Einweisung—, y "Sicherheit" trae prevención
de riesgos laborales y protección contra incendios.

De ahí el modelo de tres niveles:

- `starke_begriffe`: se bastan solos ("phishing", "DMARC", "ingeniería social").
- `schwache_begriffe`: ambiguos ("concienciación", "formación"). Solo valen si
  además aparece algo de `erforderlicher_kontext` EN EL TEXTO.
- `cpv_praefixe`: suman puntuación, pero nunca aceptan por sí solos ni sirven como
  contexto. Se probó dejarles hacer de contexto y colaba "servicio de formación
  informática en hojas de cálculo" con el CPV 80533100: el CPV dice que es
  formación en informática, no que tenga nada que ver con seguridad.

`ausschluss` manda sobre todo lo demás.

Dos lecciones de los datos reales que están grabadas en la configuración:
los términos deben ser RAÍCES ("conscienci" cubre conscienciar, conscienciació y
conscienciación), porque los pliegos usan el verbo tanto como el sustantivo; y hay
que incluir las erratas frecuentes ("phising" con una sola s aparece tal cual en
pliegos publicados).

Cada match guarda su motivo. Sin la traza no se puede afinar el ruido, y el ruido
es lo que hace que la gente deje de abrir la herramienta a las dos semanas.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import progreso, rutas
from .db import ahora, escribir_preferencia, leer_preferencia
from .model import normalizar

log = logging.getLogger(__name__)

RAIZ = rutas.CODIGO
# Los perfiles se editan, así que van con los datos y no con el código: dentro de un
# .app no se puede escribir, y la actualización se lleva el bundle entero por delante.
PERFILES_POR_DEFECTO = rutas.PERFILES
# El de ejemplo sí se versiona; el de verdad no. Los términos con los que cada uno busca
# son su trabajo —las raíces, las erratas de los pliegos, las lenguas cooficiales— y no
# tienen por qué acabar en un repositorio público ni viajar en una actualización.
PERFILES_EJEMPLO = rutas.PERFILES_PLANTILLA


def patron(termino: str) -> re.Pattern | None:
    """Compila un término en una expresión que solo casa a PRINCIPIO de palabra.

    Antes se buscaba con `termino in texto`, y eso casaba dentro de otras palabras:
    "formacion" aparecía en "sistemas de in-formación" y metía en la bandeja 612 de
    los 943 matches de una base real, todos con el motivo "término ambiguo:
    formacion" sobre pliegos donde no había ninguna formación. El panel «Por qué ha
    entrado» —que existe justo para poder afinar— estaba mintiendo.

    El ancla va SOLO al principio, y esto es lo importante: el fichero de perfiles
    está escrito con RAÍCES a propósito ("conscienci" cubre conscienciar,
    conscienciació y concienciación), así que cerrar también el final con `\\b`
    rompería la lección nº1 del README y perdería negocio real. Con el ancla solo
    delante, la raíz sigue creciendo hacia la derecha y el falso positivo desaparece:

        "formacion"  vs "informacion"     -> no casa (la 'n' de 'in' es letra)
        "formacio"   vs "de formació"     -> casa
        "conscienci" vs "conscienciació"  -> casa

    Y si el término se escribió con un espacio final se exige además fin de palabra,
    que es lo que ya documentaba el README para las siglas cortas: "ens " busca la
    sigla suelta y no debe casar dentro de "ensayo" ni "enseñanza".
    """
    norm = normalizar(termino)
    nucleo = norm.strip()
    if not nucleo:
        return None
    expresion = r"(?<!\w)" + re.escape(nucleo)
    if norm != norm.rstrip():
        expresion += r"(?!\w)"
    return re.compile(expresion)


def _termino(bruto: str) -> tuple[str, str, re.Pattern] | None:
    """El término listo para comparar: (original, literal, patrón anclado).

    El literal no es redundante. Buscar la regex de los ~100 términos de todos los
    perfiles sobre cada texto costaba 4,2 s más que el `in` de antes sobre esta base
    de 133.000 registros, y casi todo ese tiempo se iba en términos que no aparecen.
    Como el literal es un superconjunto del patrón —`(?<!\\w)formacion` solo puede
    casar donde también casa `formacion`—, se usa de guarda barata: el `in` en C
    descarta, y la regex solo se ejecuta cuando hay algo que afinar.
    """
    pat = patron(bruto)
    if pat is None:
        return None
    return (bruto, normalizar(bruto).strip(), pat)


def _preparar_terminos(brutos: list[str]) -> list[tuple[str, str, re.Pattern]]:
    return [t for t in map(_termino, brutos) if t]


def prefijo_cpv(codigo: str) -> str:
    """Quita los ceros de relleno de un CPV para poder compararlo como prefijo.

    El CPV es jerárquico y los ceros finales marcan el nivel: 72000000 es la división
    «servicios TI», 72500000 el grupo «servicios informáticos», 72514300 una
    subcategoría concreta. El campo se llama `cpv_praefixe` y se comparaba con
    `codigo.startswith(prefijo)`, pero al escribirlos con los ceros ningún prefijo
    llegaba a funcionar como tal: "72514300".startswith("72500000") es falso, así que
    "72500000" solo casaba consigo mismo. Medido sobre una base de 133.000 registros,
    los diez prefijos declarados solo tocaban su código exacto, y la oficina de
    ciberseguridad del Ministerio de Cultura (CPV 72514300) no recibía ni un punto
    del grupo 725 al que pertenece.

    Se dejan al menos dos dígitos: un CPV de una sola cifra no acota nada.
    """
    limpio = "".join(c for c in codigo if c.isdigit())
    if len(limpio) <= 2:
        return limpio
    recortado = limpio.rstrip("0")
    return recortado if len(recortado) >= 2 else limpio[:2]


def _casan(terminos, texto: str):
    """Los originales de los términos que aparecen de verdad en el texto.

    Devuelve un generador: `evaluar` necesita la lista completa para el motivo, pero
    la comprobación de `ausschluss` corta en el primero como hacía antes.
    """
    return (o for o, literal, pat in terminos if literal in texto and pat.search(texto))



@dataclass
class Perfil:
    name: str
    aktiv: bool = True
    cpv_praefixe: list[str] = field(default_factory=list)
    starke_begriffe: list[str] = field(default_factory=list)
    schwache_begriffe: list[str] = field(default_factory=list)
    erforderlicher_kontext: list[str] = field(default_factory=list)
    ausschluss: list[str] = field(default_factory=list)
    mindestbetrag: float | None = None
    bundesland: list[str] = field(default_factory=list)
    # Prefijos de fuente a los que se limita el perfil (vacío = todas). Permite,
    # por ejemplo, un perfil deliberadamente amplio que solo se aplique a las
    # consultas preliminares de mercado, donde el volumen es pequeño y merece la
    # pena verlo todo.
    quellen: list[str] = field(default_factory=list)
    # Términos que se envían a las fuentes que filtran en servidor (hoy solo TED: el
    # Bekanntmachungsservice no filtra, se descarga todo y se criba en local).
    # Si está vacío se usan los `starke_begriffe`. Existe porque el contexto
    # incluye palabras genéricas ("seguridad", "protección") que como filtro remoto
    # traen decenas de miles de avisos irrelevantes.
    abfrage_begriffe: list[str] = field(default_factory=list)

    @classmethod
    def desde_dict(cls, d: dict) -> "Perfil":
        conocidos = {f for f in cls.__dataclass_fields__}
        desconocidos = set(d) - conocidos
        if desconocidos:
            raise ValueError(
                f"Profil '{d.get('name', '?')}': unbekannte Schlüssel {sorted(desconocidos)}. "
                f"Gültig: {sorted(conocidos)}"
            )
        if not d.get("name"):
            raise ValueError("Jedes Profil braucht 'name'")
        return cls(**d)

    # Versiones normalizadas, calculadas una vez.
    def preparar(self) -> "Perfil":
        self._fuertes = _preparar_terminos(self.starke_begriffe)
        self._debiles = _preparar_terminos(self.schwache_begriffe)
        self._contexto = _preparar_terminos(self.erforderlicher_kontext)
        self._excluir = _preparar_terminos(self.ausschluss)

        # Solo para comparar en local. Lo que se manda a TED y Cataluña sigue siendo
        # el código declarado tal cual: TED consulta con `classification-cpv IN (...)`,
        # que exige códigos exactos de 8 dígitos, y mandarle "725" no devolvería nada.
        self._cpv_prefijos = [prefijo_cpv(c) for c in self.cpv_praefixe]

        # Criba: una sola expresión regular con todos los términos que pueden hacer
        # que una licitación entre. La gran mayoría de los 127.000 registros no
        # contiene ninguno, y descartarlos con una pasada en C en lugar de con
        # sesenta comprobaciones en Python baja la evaluación de 3 segundos a unas
        # décimas. `evaluar` exige un término fuerte o uno ambiguo para aceptar, así
        # que si no aparece ninguno no hay nada que mirar.
        #
        # Va con los términos DESNUDOS, sin el ancla de principio de palabra que sí
        # aplica `patron()`. Es a propósito: aquí solo hace falta un superconjunto
        # —quien no pase la criba tampoco pasaría el patrón anclado— y poner el
        # lookbehind en cada rama le quita a `re` la optimización de alternancia de
        # literales, medido sobre esta base: 4,1 s con literales, 32,1 s con el ancla.
        # Los pocos miles de textos que la criba deja pasar de más los descarta
        # después la comprobación término a término, que ya es exacta.
        candidatos = [
            normalizar(t).strip()
            for t in self.starke_begriffe + self.schwache_begriffe
            if normalizar(t).strip()
        ]
        self._criba = (
            re.compile("|".join(re.escape(c) for c in candidatos)) if candidatos else None
        )
        return self


@dataclass
class Resultado:
    casa: bool
    puntuacion: float = 0.0
    motivo: str = ""


def evaluar(perfil: Perfil, texto: str, cpvs: list[str], importe: float | None,
            bundesland: str | None, fuente: str | None = None,
            ya_normalizado: bool = False) -> Resultado:
    """Aplica un perfil a una licitación.

    `ya_normalizado` evita repetir la normalización del texto una vez por perfil.
    La base la guarda hecha (columna `texto_reglas_norm`), y eso convertía una evaluación
    de 8,7 segundos sobre 127.000 registros en algo instantáneo.
    """
    if not hasattr(perfil, "_fuertes"):
        perfil.preparar()

    if perfil.quellen and not any((fuente or "").startswith(f) for f in perfil.quellen):
        return Resultado(False, 0.0, f"Profil auf {perfil.quellen} beschränkt")

    t = texto if ya_normalizado else normalizar(texto)

    # Criba rápida: si no aparece ningún término que pueda hacerla entrar, fuera.
    if perfil._criba is not None and not perfil._criba.search(t):
        return Resultado(False, 0.0, "kein Treffer")

    excluido = next(_casan(perfil._excluir, t), None)
    if excluido:
        return Resultado(False, 0.0, f'ausgeschlossen durch "{excluido}"')

    if perfil.bundesland and bundesland and bundesland not in perfil.bundesland:
        return Resultado(False, 0.0, f"außerhalb des Gebiets ({bundesland})")

    # El importe solo descarta cuando se conoce. Muchas licitaciones publican el
    # presupuesto más tarde, y descartarlas por un dato ausente sería peor que
    # dejarlas pasar.
    if perfil.mindestbetrag and importe is not None and importe < perfil.mindestbetrag:
        return Resultado(False, 0.0, f"Wert {importe:.0f} < Mindestwert {perfil.mindestbetrag:.0f}")

    cpv_tocados = [
        c for c in cpvs if any(c.startswith(p) for p in perfil._cpv_prefijos)
    ]
    fuertes = list(_casan(perfil._fuertes, t))
    debiles = list(_casan(perfil._debiles, t))
    contexto = list(_casan(perfil._contexto, t))

    partes: list[str] = []
    puntuacion = 0.0

    if fuertes:
        puntuacion += 3.0 + 0.5 * (len(fuertes) - 1)
        partes.append("eindeutiger Begriff: " + ", ".join(f'"{f}"' for f in fuertes[:3]))

    # El contexto tiene que estar en el TEXTO. Un CPV de formación informática no
    # convierte un curso de ofimática en una licitación de ciberseguridad.
    hay_contexto = bool(contexto)
    if debiles and hay_contexto:
        puntuacion += 2.0
        partes.append("mehrdeutiger Begriff mit Kontext: " + ", ".join(f'"{d}"' for d in debiles[:3]))
        partes.append("Kontext: " + ", ".join(f'"{c}"' for c in contexto[:3]))
    elif debiles and not fuertes:
        return Resultado(
            False, 0.0,
            f'nur mehrdeutige Begriffe ({", ".join(debiles[:2])}) ohne Sicherheitskontext',
        )

    if cpv_tocados:
        puntuacion += 1.0
        partes.append("CPV " + ", ".join(cpv_tocados[:3]))

    # El CPV nunca acepta por sí solo: 80533100 abarca toda la formación
    # informática del país y ahogaría la bandeja.
    if not fuertes and not (debiles and hay_contexto):
        if cpv_tocados:
            return Resultado(False, 0.0, f"nur CPV ({cpv_tocados[0]}), kein Signal im Text")
        return Resultado(False, 0.0, "kein Treffer")

    if importe is not None and importe >= 100_000:
        puntuacion += 0.5
        partes.append("relevanter Auftragswert")

    return Resultado(True, round(puntuacion, 2), " · ".join(partes))


# --- Carga de perfiles -----------------------------------------------------


def preparar_perfiles(ruta: Path | str | None = None) -> Path:
    """Se asegura de que exista el fichero de perfiles, creándolo del ejemplo.

    En una instalación recién descargada solo está `suchprofile.beispiel.json`, porque el
    de verdad no se versiona. Copiarlo aquí evita que la primera vez que alguien abre
    start.command lo reciba un error en la cara; a partir de ese momento el fichero es
    suyo y nadie lo vuelve a tocar.
    """
    ruta = Path(ruta) if ruta else PERFILES_POR_DEFECTO
    if not ruta.exists() and ruta == PERFILES_POR_DEFECTO and PERFILES_EJEMPLO.exists():
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(PERFILES_EJEMPLO.read_text(encoding="utf-8"), encoding="utf-8")
        log.info("%s aus der Vorlage erstellt. Dort kannst du deine Suchbegriffe anpassen.",
                 ruta.name)
    return ruta


def solo_activos(perfiles: list[Perfil]) -> list[Perfil]:
    """Los perfiles que de verdad se aplican. Uno desactivado es como si no estuviera.

    Está en una función y no repetido en cada sitio porque durante un tiempo no lo
    estuvo, y los dos caminos que aplican los perfiles decían cosas distintas:
    `cargar_perfiles` —lo que lee `radar.py match`— filtraba los inactivos, pero
    `validar_perfiles` —por donde pasa lo que se guarda desde la pestaña «Términos de
    búsqueda»— devuelve la lista entera, porque esa lista también es la que se escribe
    tal cual en el fichero. Resultado: quitar la marca «activo» en la pantalla no
    quitaba nada de la bandeja, y hacerlo a mano en `suchprofile.json` sí.
    """
    return [p for p in perfiles if p.aktiv]


def cargar_perfiles(ruta: Path | str | None = None) -> list[Perfil]:
    ruta = preparar_perfiles(ruta)
    if not ruta.exists():
        raise FileNotFoundError(
            f"{ruta} gibt es nicht. Kopiere config/suchprofile.beispiel.json nach config/suchprofile.json."
        )
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    if isinstance(datos, dict):
        datos = datos.get("profile", [])
    perfiles = [Perfil.desde_dict(d).preparar() for d in datos]
    activos = solo_activos(perfiles)
    if not activos:
        raise ValueError(f"Kein aktives Profil in {ruta}")
    return activos


def leer_fichero_perfiles(ruta: Path | str | None = None) -> dict:
    """Devuelve el JSON tal cual, incluida la ayuda, para poder editarlo y reescribirlo."""
    # También aquí: la pestaña de términos es lo primero que abre alguien que acaba de
    # descargar el programa, y en ese momento el fichero puede no existir todavía.
    ruta = preparar_perfiles(ruta)
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    if isinstance(datos, list):
        datos = {"profile": datos}
    return datos


def validar_perfiles(perfiles_json: list[dict]) -> list[Perfil]:
    """Valida antes de escribir nada. Lanza ValueError con el motivo concreto."""
    if not isinstance(perfiles_json, list) or not perfiles_json:
        raise ValueError("Es braucht mindestens ein Profil.")

    nombres = set()
    validados = []
    for bruto in perfiles_json:
        if not isinstance(bruto, dict):
            raise ValueError("Jedes Profil muss ein Objekt mit seinen Feldern sein.")
        perfil = Perfil.desde_dict(bruto)  # ya rechaza claves raras y falta de nombre
        if perfil.name in nombres:
            raise ValueError(f"Zwei Profile heißen gleich: „{perfil.name}“.")
        nombres.add(perfil.name)
        if perfil.aktiv and not (perfil.starke_begriffe or perfil.schwache_begriffe):
            raise ValueError(
                f"Das Profil „{perfil.name}“ ist aktiv, hat aber keinen einzigen Begriff: "
                "es würde alles annehmen oder alles verwerfen."
            )
        if perfil.aktiv and perfil.schwache_begriffe and not perfil.erforderlicher_kontext:
            raise ValueError(
                f"Das Profil „{perfil.name}“ hat mehrdeutige Begriffe "
                f"({', '.join(perfil.schwache_begriffe[:3])}) ohne erforderlichen Kontext. "
                "So käme alles herein, was irgendwie von Schulung oder Sensibilisierung spricht."
            )
        validados.append(perfil.preparar())

    if not any(p.aktiv for p in validados):
        raise ValueError("Kein Profil ist aktiv: der Posteingang bliebe leer.")
    return validados


def avisos_perfiles(perfiles: list[Perfil]) -> list[str]:
    """Cosas que probablemente no quieres, pero que decides tú.

    No son errores y no bloquean el guardado: hay términos cortos perfectamente
    válidos («spf», «dkim»). Lo que sí conviene decir es que un término corto casa
    DENTRO de otras palabras: «ens» aparece en «defensa», «bienes» y «ensayo», y
    escribirlo con un espacio final («ens ») lo limita a la palabra suelta. La
    respuesta empírica la da la previsualización, que dice cuántas entran y salen.
    """
    avisos = []
    for perfil in solo_activos(perfiles):
        for lista, etiqueta in (
            ("starke_begriffe", "eindeutige"),
            ("schwache_begriffe", "mehrdeutige"),
            ("erforderlicher_kontext", "Kontext-"),
        ):
            for termino in getattr(perfil, lista):
                limpio = termino.strip()
                if limpio and len(limpio) <= 3 and termino == limpio:
                    avisos.append(
                        f"„{perfil.name}“: der {etiqueta} Begriff „{termino}“ hat "
                        f"{len(limpio)} Buchstaben und trifft auch innerhalb anderer Wörter. "
                        f"Wenn du das Wort allein suchst, schreib „{limpio} “ mit einem "
                        "Leerzeichen am Ende."
                    )
    return avisos


def guardar_perfiles(perfiles_json: list[dict], ruta: Path | str | None = None) -> list[Perfil]:
    """Valida y escribe `suchprofile.json`, guardando antes una copia del anterior.

    Se conserva el bloque `_ayuda` del fichero: son las tres lecciones que costaron
    encontrar (usar raíces, dejar las erratas tipo «phising», incluir el catalán) y
    perderlas al editar desde la pantalla sería un retroceso.
    """
    validados = validar_perfiles(perfiles_json)
    ruta = Path(ruta) if ruta else PERFILES_POR_DEFECTO

    documento = {}
    if ruta.exists():
        try:
            anterior = leer_fichero_perfiles(ruta)
            if "_hilfe" in anterior:
                documento["_hilfe"] = anterior["_hilfe"]
            # Copia de seguridad antes de sobrescribir.
            (ruta.parent / "suchprofile.vorherige.json").write_text(
                ruta.read_text(encoding="utf-8"), encoding="utf-8"
            )
        except (ValueError, OSError):
            pass

    documento["profile"] = perfiles_json
    # Se escribe a un temporal y se mueve: si falla a medias, el fichero bueno sigue.
    temporal = ruta.with_suffix(".json.tmp")
    temporal.write_text(
        json.dumps(documento, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporal.replace(ruta)
    return validados


def previsualizar(con, perfiles: list[Perfil]) -> dict:
    """Qué pasaría con estos perfiles, sin tocar la base.

    Cambiar un término a ciegas sobre más de cien mil registros es la forma más
    rápida de ensuciar la bandeja, así que primero se dice cuántas coincidencias
    habría y qué entra o sale.
    """
    # Lo que se enseña aquí tiene que ser lo que hará `reevaluar` al guardar. Contar
    # un perfil desactivado prometería coincidencias que el guardado retira acto
    # seguido, que es justo la duda que viene a resolver «Ver qué cambiaría».
    perfiles = solo_activos(perfiles)
    actuales = {
        f["licitacion_id"] for f in con.execute("SELECT DISTINCT licitacion_id FROM matches")
    }
    nuevas: set[int] = set()
    for fila in con.execute(
        # `texto_reglas_norm`, el mismo que usa `reevaluar`. Antes se leía `texto_norm`
        # —el texto de búsqueda, que lleva pegados el órgano y el expediente—, así que
        # «Ver qué cambiaría» evaluaba un texto DISTINTO del que se evaluaría al
        # guardar y prometía coincidencias que luego no aparecían.
        """SELECT id, COALESCE(texto_reglas_norm, '') AS texto, cpv, importe_referencia,
                  bundesland, fuente FROM licitaciones"""
    ):
        cpvs = (fila["cpv"] or "").split()
        for perfil in perfiles:
            if evaluar(perfil, fila["texto"], cpvs, fila["importe_referencia"],
                       fila["bundesland"], fila["fuente"], ya_normalizado=True).casa:
                nuevas.add(fila["id"])
                break

    entran, salen = nuevas - actuales, actuales - nuevas

    def muestra(ids):
        if not ids:
            return []
        marcas = ", ".join("?" * min(len(ids), 5))
        return [
            dict(f) for f in con.execute(
                f"SELECT id, objeto, organo FROM licitaciones WHERE id IN ({marcas})",
                list(ids)[:5],
            )
        ]

    return {
        "antes": len(actuales),
        "despues": len(nuevas),
        "entran": len(entran),
        "salen": len(salen),
        "muestra_entran": muestra(entran),
        "muestra_salen": muestra(salen),
        "avisos": avisos_perfiles(perfiles),
    }


def terminos_para_consultas(perfiles: list[Perfil]) -> tuple[list[str], list[str]]:
    """CPV y términos que se mandan a las fuentes que filtran en servidor.

    Lo que se pregunta al servidor NO es lo mismo que lo que se filtra en local, y
    el criterio se eligió midiendo contra TED:

    - mandar los términos débiles ("formacion", "email") -> 5.957 avisos
    - mandar además el contexto ("seguridad", "proteccion") -> 12.948 avisos
    - mandar solo términos discriminantes + CPV -> unos cientos

    Todos para quedarse en ~200 coincidencias reales. Por eso cada perfil declara
    `abfrage_begriffe`; si no lo hace, se usan sus `starke_begriffe`. Los CPV van
    siempre, y son la red que recoge lo que el texto no delata.
    """
    cpv: list[str] = []
    terminos: list[str] = []
    # Un perfil desactivado tampoco se pregunta: lo que trajera se descartaría luego.
    for p in solo_activos(perfiles):
        for c in p.cpv_praefixe:
            if c not in cpv:
                cpv.append(c)
        for t in (p.abfrage_begriffe or p.starke_begriffe):
            t = t.strip()
            if len(t) >= 4 and t not in terminos:
                terminos.append(t)
    return cpv, terminos


# --- Aplicación sobre la base ---------------------------------------------


# Se sube A MANO cuando cambia la LÓGICA de evaluación: `evaluar`, `patron`,
# `prefijo_cpv`, `_casan`, o `model.normalizar()`, que decide sobre qué texto corren.
# Sin esto la pasada incremental es una trampa: si `patron()` deja de casar algo, hay
# matches que retirar sin que haya cambiado ni la huella de los perfiles ni la de ninguna
# ficha, y una reevaluación incremental dejaría la bandeja mintiendo para siempre.
#
# El "1" es la línea base alemana: ancla a principio de palabra —con el guion contando
# como límite, que es lo que hace que «sicherheit» encuentre «IT-Sicherheit» y no
# «Ausfallsicherheit»—, `casefold()` para plegar la ß, y `ausschluss` deliberadamente
# casi vacío.
VERSION_MATCHING = "1"

# Donde se anota con qué perfiles se evaluó la base la última vez. Mismo idioma de
# autocorrección que las VERSION_* de `db.py`.
CLAVE_HUELLA_PERFILES = "huella_perfiles"

# Los campos del perfil que deciden A QUIÉN casa. `abfrage_begriffe` queda fuera a
# propósito: solo cambia lo que se PREGUNTA a las fuentes que filtran en servidor, no la evaluación local
# (ver `terminos_para_consultas`), y meterlo obligaría a una pasada completa por tocar
# algo que no mueve ni un match.
#
# `activo` se queda aunque el retrato solo se haga con los perfiles activos —o sea,
# valga siempre True—: quitarlo cambiaría la huella de todas las instalaciones y les
# costaría una pasada completa de 39 s a cambio de nada.
CAMPOS_HUELLA_PERFIL = (
    "name", "aktiv", "cpv_praefixe", "starke_begriffe", "schwache_begriffe",
    "erforderlicher_kontext", "ausschluss", "mindestbetrag", "bundesland", "quellen",
)

# Filas por `executemany`. No es un límite de SQLite: es para que un perfil demasiado
# amplio sobre cientos de miles de fichas no acumule millones de tuplas en memoria
# antes de escribir nada.
LOTE = 5_000

_SELECT_EVAL = """SELECT id, COALESCE(texto_reglas_norm, '') AS texto, cpv,
                         importe_referencia, bundesland, fuente FROM licitaciones"""

# Escrito con `IS NOT` y no con `IS NULL OR !=` porque es el WHERE del índice parcial
# `idx_lic_pendientes` (db.py) y SQLite solo usa un índice parcial cuando la condición
# de la consulta coincide con la suya. Hay un test que lo fija con EXPLAIN QUERY PLAN.
_PENDIENTES = " WHERE huella_evaluada IS NOT huella"


def huella_perfiles(perfiles: list[Perfil]) -> str:
    """Retrato de los perfiles: si cambia, lo evaluado antes ya no vale.

    El orden de los términos SÍ cuenta, y no es un descuido: el motivo que se guarda en
    cada match cita los tres primeros, así que reordenarlos cambia lo que la bandeja
    explica en «Por qué ha entrado».
    """
    retrato = [
        {campo: getattr(perfil, campo) for campo in CAMPOS_HUELLA_PERFIL}
        for perfil in perfiles
    ]
    crudo = json.dumps(retrato, ensure_ascii=False, sort_keys=True) + f"|{VERSION_MATCHING}"
    return hashlib.sha256(crudo.encode("utf-8")).hexdigest()[:32]


def _volcar(con: sqlite3.Connection, altas: list[tuple], bajas: list[tuple]) -> None:
    """Escribe un lote de matches y retira otro. En bloque, no fila a fila.

    Antes se emitía un DELETE por cada ficha y cada perfil que no casaba: sobre la base
    real son 673.755 × 4 = 2,7 millones de sentencias contra una tabla de 3.705 filas,
    casi todas para borrar algo que no existía.
    """
    if altas:
        con.executemany(
            """INSERT INTO matches (licitacion_id, perfil, puntuacion, motivo, creado_en)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(licitacion_id, perfil) DO UPDATE SET
                 puntuacion = excluded.puntuacion,
                 motivo = excluded.motivo""",
            altas,
        )
        altas.clear()
    if bajas:
        # Se retira el match, pero el triaje humano de esa licitación se conserva.
        con.executemany(
            "DELETE FROM matches WHERE licitacion_id = ? AND perfil = ?", bajas
        )
        bajas.clear()


def _limpiar_huerfanos(con: sqlite3.Connection, nombres: set[str]) -> int:
    """Borra de una vez los matches de los perfiles que ya no se aplican.

    Los que se han borrado del fichero, los que se han renombrado y los que están
    desactivados: para la bandeja los tres son lo mismo, un perfil que ya no está.
    """
    existentes = {r[0] for r in con.execute("SELECT DISTINCT perfil FROM matches")}
    huerfanos = sorted(existentes - nombres)
    if not huerfanos:
        return 0
    marcas = ", ".join("?" * len(huerfanos))
    cur = con.execute(f"DELETE FROM matches WHERE perfil IN ({marcas})", huerfanos)
    return cur.rowcount


def _marcar_evaluadas(con: sqlite3.Connection) -> int:
    """Alinea `huella_evaluada` con `huella` en todo lo que se acaba de evaluar.

    Por tramos y con commit en cada uno, no con un UPDATE global: la primera pasada
    después de actualizar marca las 673.755 filas y, como SQLite reescribe la fila
    entera (~1 kB de media), eso serían unos 700 MB de WAL en una sola transacción.
    """
    total = 0
    while True:
        cur = con.execute(
            "UPDATE licitaciones SET huella_evaluada = huella WHERE id IN ("
            f"  SELECT id FROM licitaciones{_PENDIENTES} LIMIT ?)",
            (LOTE,),
        )
        con.commit()
        if not cur.rowcount:
            return total
        total += cur.rowcount


def reevaluar(con: sqlite3.Connection, perfiles: list[Perfil], *,
              incremental: bool = False) -> dict:
    """Aplica los perfiles a la base y actualiza la tabla `matches`.

    Se ejecuta en Python en lugar de con FTS5 MATCH porque las reglas son
    condicionales (fuerte / débil+contexto / exclusiones / umbral de importe) y
    eso no se expresa en una consulta FTS sin volverla ilegible. FTS5 se sigue
    usando para la caja de búsqueda libre de la bandeja, que sí es un MATCH.

    `incremental` evalúa solo lo nuevo o lo que ha cambiado de verdad, que es lo que
    necesita la ingesta de cada mañana: sobre la base real, evaluarlo todo son 39 s de
    Python para acabar reescribiendo los mismos matches. La petición se ignora —y se
    hace la pasada completa igualmente— cuando los perfiles no son los mismos con los
    que se evaluó la última vez; de eso se encarga la huella, y sin ella «he cambiado
    un término y la bandeja no se ha movido» sería el comportamiento normal.

    Los perfiles desactivados no se evalúan, y sus coincidencias se retiran igual que
    las de un perfil borrado.
    """
    # Aquí se filtra, y no en quien llama, porque `reevaluar` es lo único por lo que
    # pasan los dos caminos: `radar.py match`, que carga con `cargar_perfiles`, y el
    # botón «Guardar» de la pestaña de términos, que valida con `validar_perfiles` y
    # necesita conservar los inactivos para volver a escribirlos en el fichero. Un
    # perfil desactivado se va por el camino de los huérfanos: al no estar en
    # `nombres`, sus matches los retira `_limpiar_huerfanos`.
    #
    # La huella se calcula sobre los activos, que son los que de verdad se han
    # aplicado. Así, borrar del fichero un perfil que ya estaba desactivado no obliga
    # a otra pasada completa que no cambiaría ni un match.
    perfiles = solo_activos(perfiles)
    huella = huella_perfiles(perfiles)
    guardada = leer_preferencia(con, CLAVE_HUELLA_PERFILES)
    motivo = None
    if incremental and guardada != huella:
        motivo = ("die Profile haben sich geändert" if guardada
                  else "es war nicht vermerkt, mit welchen Profilen zuletzt bewertet wurde")
        incremental = False

    momento = ahora()
    nombres = {p.name for p in perfiles}
    # Los pares que ya están en `matches`: son unos miles y caben de sobra en memoria.
    # Tenerlos aquí es lo que permite borrar solo lo que de verdad hay que retirar.
    existentes = {
        (f["licitacion_id"], f["perfil"])
        for f in con.execute("SELECT licitacion_id, perfil FROM matches")
    }

    stats = {"evaluadas": 0, "completa": not incremental, "motivo": motivo,
             "creados": 0, "actualizados": 0, "retirados": 0, "huerfanos": 0}
    altas: list[tuple] = []
    bajas: list[tuple] = []

    progreso.fuente("Profile werden ausgewertet")
    progreso.fase("aplicando reglas")
    for fila in con.execute(_SELECT_EVAL + ("" if stats["completa"] else _PENDIENTES)):
        stats["evaluadas"] += 1
        progreso.fichas(stats["evaluadas"])
        cpvs = (fila["cpv"] or "").split()
        for perfil in perfiles:
            res = evaluar(
                perfil, fila["texto"], cpvs,
                fila["importe_referencia"], fila["bundesland"], fila["fuente"],
                ya_normalizado=True,
            )
            par = (fila["id"], perfil.name)
            if res.casa:
                altas.append((*par, res.puntuacion, res.motivo, momento))
                if par in existentes:
                    stats["actualizados"] += 1
                else:
                    stats["creados"] += 1
            elif par in existentes:
                bajas.append(par)
                stats["retirados"] += 1
        if len(altas) >= LOTE or len(bajas) >= LOTE:
            _volcar(con, altas, bajas)
    _volcar(con, altas, bajas)

    stats["huerfanos"] = _limpiar_huerfanos(con, nombres)
    con.commit()
    _marcar_evaluadas(con)
    escribir_preferencia(con, CLAVE_HUELLA_PERFILES, huella)
    con.commit()

    # El total de la tabla, no lo escrito en esta pasada: con pasadas parciales son
    # cifras distintas, y la que se enseña —en la terminal y en «Guardado · N
    # coincidencias»— tiene que ser cuántas coincidencias hay, no cuántas se han tocado.
    stats["matches"] = con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    stats["por_perfil"] = {
        f["perfil"]: f["total"]
        for f in con.execute(
            "SELECT perfil, COUNT(*) AS total FROM matches GROUP BY perfil"
        )
    }
    return stats
