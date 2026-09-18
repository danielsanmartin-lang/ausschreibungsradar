#!/usr/bin/env python3
"""Monta «Ausschreibungsradar.app» desde macos/Radar.swift y macos/icono.svg.

    python3 herramientas/construir_app.py

No hace falta Xcode: basta con las Command Line Tools, que es lo que trae cualquier Mac
donde alguien haya escrito `git` una vez. Un `.app` no es un formato: es una carpeta con
un `Info.plist`, un binario y un icono dentro, y eso se monta con la biblioteca estándar.

    --prefabricar   además, copia el binario recién compilado a macos/prefabricado/,
                    que es de donde lo saca quien NO tiene las herramientas. Se hace al
                    publicar una versión, no en cada compilación.
    --forzar        recompila aunque el .app ya esté en la versión correcta.
    --zip           deja un «Ausschreibungsradar X.Y.Z.zip» para adjuntar a la
                    release, y dice su SHA-256 para poner en las notas.

Quien no tenga `swiftc` no necesita nada de esto: el script usa el binario prefabricado
del repositorio y monta el bundle igual.

Sobre la firma: se firma *ad hoc* (`codesign -s -`), que es lo máximo que se puede hacer
sin una cuenta de Apple Developer. Basta para el Mac donde se compila. A un compañero que
lo descargue de GitHub, macOS le pedirá confirmación la primera vez; el README lo cuenta.
"""

from __future__ import annotations

import argparse
import hashlib
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
FUENTE = RAIZ / "macos" / "Radar.swift"
ICONO_SVG = RAIZ / "macos" / "icono.svg"
PREFABRICADO = RAIZ / "macos" / "prefabricado"
DESTINO = RAIZ / "Ausschreibungsradar.app"

IDENTIFICADOR = "app.zepo.ausschreibungsradar"
EJECUTABLE = "ausschreibungsradar"
# 13.0 y no la del Mac donde se compila: el SDK de las Command Line Tools es el de la
# última versión de macOS, y sin fijar esto el bundle diría que necesita un sistema
# recién salido y no arrancaría en el Mac de un compañero.
MINIMO_MACOS = "13.0"

# Lo que se copia DENTRO del bundle, en Contents/Resources. Es lo que convierte la app
# en un solo fichero: un compañero recibe el .app y nada más, lo arrastra a Aplicaciones
# y funciona. Antes el código tenía que estar al lado, así que había que mandarle la
# carpeta entera y explicarle que no separara las cosas.
#
# Es una lista blanca y no «todo menos data/» por lo mismo que `REEMPLAZABLES` en el
# actualizador: con una lista de exclusiones, cualquier carpeta nueva del repositorio se
# colaría dentro del bundle sin que nadie lo hubiera decidido.
DENTRO_DEL_BUNDLE = (
    "radar",
    "radar.py",
    "web",
    "config/certs",
    "config/suchprofile.beispiel.json",
)

# Lo que NO se copia aunque cuelgue de las carpetas de arriba.
NO_COPIAR = ("__pycache__", ".DS_Store", ".pyc")


# Los diez que pide `iconutil`. Cada tamaño en normal y en @2x salvo los extremos.
TAMANOS_ICONO = [
    ("icon_16x16", 16), ("icon_16x16@2x", 32),
    ("icon_32x32", 32), ("icon_32x32@2x", 64),
    ("icon_128x128", 128), ("icon_128x128@2x", 256),
    ("icon_256x256", 256), ("icon_256x256@2x", 512),
    ("icon_512x512", 512), ("icon_512x512@2x", 1024),
]


def version_del_proyecto() -> str:
    """Lee `__version__` de radar/__init__.py sin importar el paquete.

    Se lee la línea en lugar de importar por el mismo motivo que lo hace
    `actualizacion._version_del_arbol()`: no hace falta ejecutar nada para saber un
    número, y así este script no depende de que el paquete importe limpiamente.

    Es la ÚNICA fuente de la versión. El `CFBundleShortVersionString` se genera de aquí
    en cada compilación justamente para que no puedan separarse: una app que dice 1.3.0
    en «Acerca de» sobre un código 1.4.0 es una pista falsa el día que algo falle.
    """
    init = RAIZ / "radar" / "__init__.py"
    for linea in init.read_text(encoding="utf-8").splitlines():
        if linea.startswith("__version__"):
            return linea.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit(f"No encuentro __version__ en {init}")


def hay(programa: str) -> bool:
    return shutil.which(programa) is not None


def hay_compilador() -> bool:
    """¿`swiftc` existe Y arranca?

    No basta con `which`, y es la misma trampa que documenta `start.command` para
    Python: macOS trae `/usr/bin/swiftc` en cualquier Mac, esté o no instalado el
    compilador. Si no lo está, ese fichero es un lanzador que abre el instalador de las
    herramientas de Apple y falla.

    Con solo `which` se intentaba compilar en el Mac de un compañero que no las tiene,
    la compilación fallaba y el script abortaba —sin llegar nunca a usar el binario
    prefabricado, que existe precisamente para ese caso—. Así que se comprueba
    ejecutándolo.
    """
    if not hay("swiftc"):
        return False
    r = subprocess.run(["swiftc", "--version"], capture_output=True, text=True)
    return r.returncode == 0


def _correr(orden: list[str], que: str) -> None:
    r = subprocess.run(orden, capture_output=True, text=True)
    if r.returncode != 0:
        detalle = (r.stderr or r.stdout or "").strip()
        raise SystemExit(f"Falló {que}:\n{detalle[:2000]}")


def construir_icono(destino: Path) -> None:
    """SVG -> .icns con solo `sips` e `iconutil`, que vienen con macOS.

    Así el icono se versiona como texto —se puede revisar en un diff— en lugar de como
    diez PNG binarios, y no hace falta ninguna herramienta de diseño para retocarlo.
    """
    if not (hay("sips") and hay("iconutil")):
        raise SystemExit("Hacen falta `sips` e `iconutil`; esto solo se monta en macOS.")

    conjunto = destino.parent / "Radar.iconset"
    if conjunto.exists():
        shutil.rmtree(conjunto)
    conjunto.mkdir(parents=True)
    for nombre, px in TAMANOS_ICONO:
        _correr(["sips", "-s", "format", "png",
                 "--resampleHeightWidth", str(px), str(px),
                 str(ICONO_SVG), "--out", str(conjunto / f"{nombre}.png")],
                f"rasterizar el icono a {px} px")
    _correr(["iconutil", "-c", "icns", str(conjunto), "-o", str(destino)],
            "empaquetar el .icns")
    shutil.rmtree(conjunto)


def compilar(destino: Path, universal: bool = True) -> str:
    """Compila el shell. Devuelve qué se ha hecho, para poder contarlo.

    Universal (Intel + Apple Silicon) para que sirva en el Mac de cualquier compañero:
    dos pasadas de `swiftc` y `lipo` para unirlas. Si la pasada de Intel falla —el SDK
    de las Command Line Tools no siempre trae todas las bibliotecas de compatibilidad
    para x86_64— se sigue con solo la arquitectura de este Mac y se dice, en lugar de
    romper la compilación por una arquitectura que quizá nadie necesite.
    """
    tmp = destino.parent / "_arq"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        hechas = []
        for arco in ("arm64", "x86_64"):
            if not universal and arco != "arm64":
                continue
            salida = tmp / arco
            r = subprocess.run(
                ["swiftc", "-O", "-target", f"{arco}-apple-macos{MINIMO_MACOS}",
                 str(FUENTE), "-o", str(salida)],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                hechas.append(salida)
            elif arco == "arm64":
                raise SystemExit(f"Falló la compilación:\n{(r.stderr or '')[:2000]}")

        if len(hechas) > 1:
            _correr(["lipo", "-create", *map(str, hechas), "-output", str(destino)],
                    "unir las dos arquitecturas")
            return "compilado universal (arm64 + x86_64)"
        shutil.copy2(hechas[0], destino)
        return "compilado solo para arm64 (la pasada de x86_64 no salió)"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def copiar_el_programa(recursos: Path) -> list[str]:
    """Mete el código en Contents/Resources. Devuelve qué ha entrado.

    Los datos NO van aquí y no pueden ir: dentro de un .app no se escribe —puede estar
    en /Applications, puede estar firmado, y la actualización lo sustituye entero—. De
    eso se encarga `radar/rutas.py`, que al ver que el código está dentro de un bundle
    manda la base y los perfiles a ~/Library/Application Support.
    """
    metidos = []
    for rel in DENTRO_DEL_BUNDLE:
        origen = RAIZ / rel
        if not origen.exists():
            raise SystemExit(f"Falta {rel}, que tiene que ir dentro de la app.")
        destino = recursos / rel
        destino.parent.mkdir(parents=True, exist_ok=True)
        if origen.is_dir():
            shutil.copytree(
                origen, destino,
                ignore=shutil.ignore_patterns(*NO_COPIAR),
            )
        else:
            shutil.copy2(origen, destino)
        metidos.append(rel)
    return metidos


def escribir_plist(contents: Path, version: str) -> None:
    plist = {
        "CFBundleName": "Ausschreibungsradar",
        "CFBundleDisplayName": "Ausschreibungsradar",
        "CFBundleIdentifier": IDENTIFICADOR,
        "CFBundleExecutable": EJECUTABLE,
        "CFBundleIconFile": "AppIcon",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": MINIMO_MACOS,
        "LSApplicationCategoryType": "public.app-category.business",
        "NSHighResolutionCapable": True,
        # Sin esto la ventana sale en blanco y no hay ningún error: App Transport
        # Security bloquea HTTP, y el servidor del proyecto es HTTP en 127.0.0.1 a
        # propósito —no es un servicio de red, es un proceso hablando consigo mismo—.
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        # Una sola ventana y sin documentos: que no salga en «Abrir con».
        "LSUIElement": False,
        "NSSupportsAutomaticTermination": False,
        "NSSupportsSuddenTermination": False,
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(plist))
    (contents / "PkgInfo").write_text("APPL????", encoding="utf-8")


def comprimir_para_release(version: str) -> Path:
    """Deja un .zip del .app listo para adjuntar a la release. Devuelve su ruta.

    Se usa `ditto` y no `zip` porque es la herramienta de macOS para esto: conserva los
    enlaces simbólicos y los metadatos del bundle. Un `.app` comprimido con `zip -r` a
    secas puede llegar al otro lado sin poder abrirse.

    Y se imprime el SHA-256, que aquí **sí** sirve de algo. El README explica por qué no
    servía antes: el zip que compara el actualizador lo genera GitHub al vuelo desde el
    tag y su suma puede cambiar sin que cambie el código. Este fichero lo subes tú, así
    que su hash es estable y ponerlo en las notas de la release es una comprobación de
    verdad y no una trampa.
    """
    destino = RAIZ / f"Ausschreibungsradar {version}.zip"
    destino.unlink(missing_ok=True)
    if not hay("ditto"):
        raise SystemExit("Hace falta `ditto`, que viene con macOS.")
    _correr(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
             str(DESTINO), str(destino)], "comprimir el .app")
    return destino


def version_instalada() -> str | None:
    plist = DESTINO / "Contents" / "Info.plist"
    if not plist.exists():
        return None
    try:
        return plistlib.loads(plist.read_bytes()).get("CFBundleShortVersionString")
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Monta la app de macOS.")
    p.add_argument("--prefabricar", action="store_true",
                   help="copia el binario compilado a macos/prefabricado/ (al publicar)")
    p.add_argument("--forzar", action="store_true",
                   help="recompila aunque el .app ya esté en la versión correcta")
    p.add_argument("--solo-arm64", action="store_true",
                   help="no intenta la pasada de Intel")
    p.add_argument("--zip", action="store_true",
                   help="deja un .zip del .app listo para adjuntar a la release")
    args = p.parse_args(argv)

    if sys.platform != "darwin":
        print("Esto solo se monta en macOS.", file=sys.stderr)
        return 1

    version = version_del_proyecto()
    if not args.forzar and version_instalada() == version:
        print(f"«{DESTINO.name}» ya está en la versión {version}. "
              "Usa --forzar para recompilar igualmente.")
        return 0

    if DESTINO.exists():
        shutil.rmtree(DESTINO)
    contents = DESTINO / "Contents"
    macos = contents / "MacOS"
    recursos = contents / "Resources"
    macos.mkdir(parents=True)
    recursos.mkdir(parents=True)

    notas = []

    # El binario: compilarlo si se puede, y si no, el del repositorio.
    binario = macos / EJECUTABLE
    compilado = False
    if hay_compilador():
        try:
            notas.append(compilar(binario, universal=not args.solo_arm64))
            compilado = True
        except SystemExit as exc:
            # Si la compilación falla habiendo prefabricado, se usa ese y se dice: es
            # mejor una app que funciona con el binario del repositorio que ninguna.
            if not (PREFABRICADO / EJECUTABLE).exists():
                raise
            notas.append(f"la compilación falló ({str(exc)[:120]}); "
                         "se usa el binario prefabricado")
        if compilado and args.prefabricar:
            PREFABRICADO.mkdir(parents=True, exist_ok=True)
            shutil.copy2(binario, PREFABRICADO / EJECUTABLE)
            notas.append("binario copiado a macos/prefabricado/")
    if not compilado:
        listo = PREFABRICADO / EJECUTABLE
        if not listo.exists():
            print("Este Mac no compila Swift y no hay binario prefabricado, así que no "
                  "puedo montar la app.\n\n"
                  "Opciones:\n"
                  "  · Instala las herramientas de línea de órdenes de Apple, que es "
                  "una sola vez:\n"
                  "        xcode-select --install\n"
                  "  · O usa start.command, que hace lo mismo sin la ventana propia.",
                  file=sys.stderr)
            return 1
        shutil.copy2(listo, binario)
        if "prefabricado" not in " ".join(notas):
            notas.append("usado el binario prefabricado (este Mac no compila Swift)")
    binario.chmod(0o755)

    construir_icono(recursos / "AppIcon.icns")
    metidos = copiar_el_programa(recursos)
    notas.append("dentro: " + ", ".join(metidos))
    escribir_plist(contents, version)

    # Firma ad hoc. Sin ella, macOS mata el proceso al abrirlo en algunos equipos y el
    # mensaje que sale no dice que sea por la firma.
    if hay("codesign"):
        _correr(["codesign", "--force", "--sign", "-", str(DESTINO)], "firmar el bundle")
        notas.append("firmado ad hoc")

    if args.zip:
        paquete = comprimir_para_release(version)
        sha = hashlib.sha256(paquete.read_bytes()).hexdigest()
        notas.append(f"comprimido: {paquete.name} "
                     f"({paquete.stat().st_size // 1024} KB)")
        notas.append(f"sha256: {sha}")

    print(f"OK · {DESTINO.name} · versión {version}")
    for n in notas:
        print(f"  · {n}")
    tam = sum(f.stat().st_size for f in DESTINO.rglob("*") if f.is_file()) // 1024
    print(f"\nSon {tam} KB y se lleva el programa dentro: se puede arrastrar a "
          "Aplicaciones,\ncomprimir y mandar. Lo único que hace falta en el otro Mac es "
          "Python 3.9+.\n\n"
          "Los datos de cada uno —la base, el triaje, los términos— van a\n"
          "~/Library/Application Support/Ausschreibungsradar, no dentro de la app.\n\n"
          "Ojo: esta copia lleva la PLANTILLA de términos de búsqueda, no los tuyos. Si\n"
          "quieres que un compañero arranque con los tuyos, mándale aparte tu\n"
          "config/suchprofile.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
