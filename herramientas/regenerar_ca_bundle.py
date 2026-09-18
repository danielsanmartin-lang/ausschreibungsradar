#!/usr/bin/env python3
"""Regenera config/certs/ca-bundle.pem desde el almacén de CAs de Mozilla.

El bundle empaquetado envejece: las CAs se renuevan y se retiran. Ejecuta esto
una o dos veces al año, o cuando una fuente empiece a dar CERTIFICATE_VERIFY_FAILED.

    pip install --upgrade certifi
    python3 herramientas/regenerar_ca_bundle.py

Comprueba que las tres raíces de la administración española siguen presentes y
aborta si falta alguna, porque su ausencia rompe PLACSP de forma silenciosa.
"""

from __future__ import annotations

import datetime
import hashlib
import pathlib
import ssl
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "config" / "certs" / "ca-bundle.pem"

# Deben coincidir con radar/net.py: HUELLAS_ESPERADAS
RAICES_ES = {
    "554153b13d2cf9ddb753bfbe1a4e0ae08d0aa4187058fe60a2b862b2e4b87bcb": "AC RAIZ FNMT-RCM SERVIDORES SEGUROS",
    "ebc5570c29018c4d67b1aa127baf12f703b4611ebc17b7dab5573894179b93fa": "AC RAIZ FNMT-RCM",
    "2530cc8e98321502bad96f9b1fba1b099e2d299e0f4548bb914f363bc0d4531f": "Izenpe.com",
}


def huellas(texto: str) -> set[str]:
    ini_m, fin_m = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
    res, pos = set(), 0
    while (ini := texto.find(ini_m, pos)) != -1:
        fin = texto.find(fin_m, ini)
        if fin == -1:
            break
        fin += len(fin_m)
        res.add(hashlib.sha256(ssl.PEM_cert_to_DER_cert(texto[ini:fin] + "\n")).hexdigest())
        pos = fin
    return res


def main() -> int:
    try:
        import certifi
    except ImportError:
        print("Falta certifi. Instálalo con: pip install certifi", file=sys.stderr)
        return 1

    contenido = pathlib.Path(certifi.where()).read_text(encoding="utf-8")
    presentes = huellas(contenido)

    faltan = [n for fp, n in RAICES_ES.items() if fp not in presentes]
    if faltan:
        print(
            "ABORTADO: el almacén de origen no incluye estas raíces españolas:\n  - "
            + "\n  - ".join(faltan)
            + "\nSin ellas PLACSP fallaría el handshake TLS. No se sobrescribe el bundle.",
            file=sys.stderr,
        )
        return 1

    version = getattr(certifi, "__version__", "n/d")
    cabecera = (
        "# Bundle de CAs raíz para ausschreibungsradar.\n"
        "#\n"
        "# Origen: almacén de CAs de Mozilla (paquete certifi), que SÍ incluye las\n"
        "# raíces españolas que faltan en el almacén de macOS/OpenSSL:\n"
        "#   - AC RAIZ FNMT-RCM SERVIDORES SEGUROS  (necesaria para PLACSP)\n"
        "#   - AC RAIZ FNMT-RCM\n"
        "#   - Izenpe.com                            (necesaria para euskadi.eus)\n"
        "#\n"
        "# Se empaqueta completo a propósito: los intérpretes de Python instalados\n"
        "# desde python.org suelen venir SIN raíces cargadas, así que depender del\n"
        "# almacén del sistema haría que la herramienta funcionase en un Mac y no en\n"
        "# otro. Con este fichero el comportamiento es idéntico en cualquier equipo.\n"
        "#\n"
        f"# Generado: {datetime.date.today().isoformat()}  ·  certifi {version}\n"
        "# Regenerar: python3 herramientas/regenerar_ca_bundle.py\n\n"
    )
    DESTINO.write_text(cabecera + contenido)
    n = contenido.count("-----BEGIN CERTIFICATE-----")
    print(f"OK · {DESTINO.relative_to(RAIZ)} · {n} certificados · {DESTINO.stat().st_size // 1024} KB")
    print("Raíces españolas verificadas: " + ", ".join(RAICES_ES.values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
