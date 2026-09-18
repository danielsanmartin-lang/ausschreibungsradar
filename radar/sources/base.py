"""Interfaz común de los conectores y utilidades compartidas.

Cada fuente es independiente a propósito: si TED cambia su esquema o el
Bekanntmachungsservice se cae, el resto de la ingesta sigue funcionando y la bandeja lo
refleja en la sección de salud de fuentes.
"""

from __future__ import annotations

from typing import Iterator, Protocol

from ..model import Licitacion


class Fuente(Protocol):
    """Un conector de datos."""

    nombre: str

    def incremental(self, cursor: str | None) -> Iterator[Licitacion]:
        """Devuelve lo publicado o modificado desde `cursor`."""

    def historico(self, anio: int) -> Iterator[Licitacion]:
        """Devuelve todo lo de un año concreto (backfill)."""

    def cursor_nuevo(self) -> str | None:
        """Cursor a guardar tras una ingesta correcta."""


# NUTS1 -> Bundesland. Permite filtrar por territorio sin depender de que cada fuente
# escriba el nombre del Land de la misma forma.
#
# El corte es a TRES caracteres, no a cuatro como en el radar español: allí el eje eran
# las comunidades autónomas, que son NUTS2 (ES51 = Cataluña); aquí son los Länder, que
# son NUTS1 (DE5 = Bremen). Lo que llega del Bekanntmachungsservice son códigos NUTS3 de
# cinco caracteres —`DEA5B`, `DE501`, `DED21`—, así que hay que recortar hasta el nivel
# del Land. En las tres ciudades-estado (Berlín, Bremen, Hamburgo) NUTS1 y NUTS2 son el
# mismo código, con lo que el corte a tres sigue siendo uniforme.
NUTS1_BUNDESLAND = {
    "DE1": "Baden-Württemberg",
    "DE2": "Bayern",
    "DE3": "Berlin",
    "DE4": "Brandenburg",
    "DE5": "Bremen",
    "DE6": "Hamburg",
    "DE7": "Hessen",
    "DE8": "Mecklenburg-Vorpommern",
    "DE9": "Niedersachsen",
    "DEA": "Nordrhein-Westfalen",
    "DEB": "Rheinland-Pfalz",
    "DEC": "Saarland",
    "DED": "Sachsen",
    "DEE": "Sachsen-Anhalt",
    "DEF": "Schleswig-Holstein",
    "DEG": "Thüringen",
}


def bundesland_desde_nuts(nuts: str | None) -> str | None:
    """`DEA5B` -> `Nordrhein-Westfalen`. Devuelve None si no es un NUTS alemán.

    Con esto caen solos los casos que no son un Land: `DEZ` (extra-regio, que es lo que
    se marca cuando la prestación no tiene sitio fijo), `DE` a secas, y cualquier NUTS de
    otro país —un lugar de ejecución en Austria— que llegue en un anuncio alemán.
    """
    if not nuts:
        return None
    return NUTS1_BUNDESLAND.get(nuts.strip().upper()[:3])
