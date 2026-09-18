#!/bin/bash
# Doble clic en este fichero: actualiza los datos y abre la bandeja.
cd "$(dirname "$0")" || exit 1

falta_python() {
  echo "Kein brauchbares Python 3 gefunden, und ohne das startet hier nichts."
  echo
  echo "macOS bringt es nicht immer mit: seit Catalina hat Apple Python aus dem System"
  echo "entfernt, auf einem neuen Mac muss man es also installieren. Nur einmal nötig:"
  echo
  echo "  1. Abre https://www.python.org/downloads/macos"
  echo "  2. Lade das Installationsprogramm der neuesten Python-3-Version und öffne es."
  echo "  3. Siguiente, siguiente, instalar — como cualquier programa."
  echo "  4. Schließ dieses Fenster und klick start.command erneut doppelt an."
  echo
  echo "Mit Homebrew reicht: brew install python3"
  echo
  echo "Zum Schließen Enter drücken."; read -r; exit 1
}

# No basta con que el comando exista. macOS trae en /usr/bin/python3 un lanzador que
# está ahí aunque Python no lo esté: al invocarlo abre el instalador de las
# herramientas de Xcode y falla. Así que se comprueba que además ARRANCA y que llega
# a la versión mínima, en vez de fiarnos de `command -v`.
if ! command -v python3 >/dev/null 2>&1; then
  falta_python
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
  version=$(python3 --version 2>&1)
  if [ -n "$version" ] && printf '%s' "$version" | grep -q '^Python 3'; then
    echo "Dein Python ist zu alt ($version); nötig ist 3.9 oder neuer."
    echo "Actualízalo desde https://www.python.org/downloads/macos"
    echo
    echo "Zum Schließen Enter drücken."; read -r; exit 1
  fi
  falta_python
fi

echo "== Ausschreibungsradar =="
echo

# Aquí ya no se decide nada. Antes este script miraba si la base estaba vacía y lanzaba
# él mismo la carga inicial, y esa lógica vivía duplicada: la aplicación no la tenía, así
# que quien abría el .app en vez de este script se quedaba sin histórico y sin saberlo.
#
# Ahora lo decide el propio servidor al arrancar (`radar.py serve`): mira qué etapas
# faltan, o si la última ingesta correcta es de hace más de veinte horas, y lanza por
# detrás lo que haga falta. La barra de la aplicación lo cuenta, con lo que cuesta y con
# un botón para pararlo. Los dos caminos —este script y el doble clic en la app— hacen
# ya exactamente lo mismo.
echo "Es wird geprüft, was noch fehlt; Fehlendes wird im Hintergrund geladen."
echo "Die Anwendung zeigt es oben mit einer Leiste an."
echo

python3 -u radar.py serve
