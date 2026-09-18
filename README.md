# Ausschreibungsradar

Findet öffentliche Ausschreibungen aus Deutschland zu **Security-Awareness,
Phishing-Simulation, Schulungen und E-Mail-Sicherheit** — dazu Cybersicherheit im
weiteren Sinne (SOC, Informationssicherheit, ISMS, BSI IT-Grundschutz) — und legt sie in
einen Posteingang, in dem man sie abarbeiten kann.

Es hängt an keinem CRM und an keinen fremden Notizen. Alles lebt in diesem Ordner: man
kann ihn kopieren, per AirDrop weitergeben oder in ein Repository legen, und er
funktioniert auf jedem Mac gleich.

> **Zur Sprache.** Die Oberfläche, die Suchprofile und alles, was man am Bildschirm
> liest, sind auf Deutsch. Der Quelltext und seine Kommentare sind auf Spanisch
> geblieben, und das ist Absicht: dieses Projekt ist ein Fork des spanischen
> `licitaciones-radar`, und so lassen sich Korrekturen weiter zwischen beiden hin- und
> herschieben. Wer den Code liest, findet dort auch die Messungen, aus denen jede
> Entscheidung stammt.

---

## In 30 Sekunden loslegen

Doppelklick auf **`Ausschreibungsradar.app`**.

Sie öffnet sich wie jedes andere Mac-Programm: eigenes Fenster, eigenes Dock-Symbol,
eigenes Menü. Intern startet sie den Python-Server, wartet auf seine Antwort und zeigt
den Posteingang; das dauert zwei bis drei Sekunden. Es muss kein Terminal offen bleiben,
und mit ⌘Q wird der Server mit beendet.

**Die App trägt das Programm in sich** — `radar/`, `web/` und die Zertifikate liegen in
`Contents/Resources` — also ist sie **eine einzige Datei von 1,8 MB**, die man nach
Programme ziehen, zippen und verschicken kann. Auf dem anderen Mac braucht es nur
Python 3.9 oder neuer.

Falls sie fehlt, ist sie in einer Sekunde gebaut:

```bash
python3 herramientas/construir_app.py
```

**`start.command`** gibt es weiterhin und tut dasselbe ohne eigenes Fenster: es lädt die
Neuigkeiten und öffnet den Posteingang im Browser. Das ist der Weg, den man nimmt, wenn
etwas schiefläuft, denn er erzählt im Terminal mit, was er gerade tut.

> **Beim ersten Mal ist macOS misstrauisch.** Die App ist nicht mit einem
> Apple-Zertifikat signiert — das kostet 99 $ im Jahr und lohnt für ein internes Werkzeug
> nicht —, also verweigert das System bei einem Download aus GitHub den Doppelklick. Das
> löst man einmalig: **Rechtsklick auf die App → Öffnen**, dann bestätigen. Danach geht
> sie normal auf. Wer lieber das Terminal nimmt:
>
> ```bash
> xattr -dr com.apple.quarantine "Ausschreibungsradar.app"
> ```
>
> Wer sie sich mit dem Befehl oben selbst baut, hat das Problem gar nicht.

Wer lieber alles im Terminal macht:

```bash
python3 radar.py ingest && python3 radar.py serve
```

Voraussetzung: **Python 3.9 oder neuer**. Achtung, macOS bringt es nicht mehr mit: Apple
hat es mit Catalina aus dem System genommen, auf einem frisch ausgepackten Mac muss man
es also einmal installieren (python.org oder `brew install python3`).

### Wo deine Daten liegen

| Was | Wo |
|---|---|
| Datenbank | `data/ausschreibungen.db` (oder `~/Library/Application Support/Ausschreibungsradar`, wenn die App gepackt läuft) |
| Deine Suchbegriffe | `config/suchprofile.json` — **wird nie versioniert und nie von einem Update überschrieben** |
| Archiv-Cache | `data/cache/` — reine Downloads, kann man jederzeit löschen |

Ein Update tauscht nur den Programmcode. Datenbank, Bearbeitungsstand und Suchbegriffe
bleiben unangetastet.

---

## Das erste Mal

Eine frische Installation kann sich das Archiv nicht selbst holen: ohne Cursor lädt der
Konnektor nur ein kurzes Tagesfenster, und sobald dieser Cursor geschrieben ist, schaut
der tägliche Abruf — zu Recht — nur noch nach vorn. Deshalb gibt es die Erstbefüllung,
in **zwei Schritten**, vom billigsten zum teuersten:

| # | Schritt | Was er bringt | Kosten |
|---|---|---|---|
| 1 | **Was gerade läuft** | der laufende und der vorige Monat | ~260 MB, ~1 Minute |
| 2 | **Zwei Jahre Historie** | rund 500.000 Bekanntmachungen: füllt Vertragsenden, Auftragnehmer und Auswertung | ~3,3 GB, 15–25 Minuten |

**Das muss niemand anstoßen.** Beim Öffnen sieht die Anwendung nach, welche Schritte
fehlen, und lädt sie im Hintergrund nach. Schritt 1 reicht, um sofort zu arbeiten; die
anderen laufen weiter, während man schon im Posteingang ist. Oben zeigt eine Leiste, wo
sie gerade sind, was der Schritt kostet und einen Knopf zum Abbrechen — was schon
geladen ist, bleibt im Cache, ein Abbruch kostet also nichts.

Von Hand geht es natürlich auch:

```bash
python3 radar.py ingest --primera-carga              # beide
python3 radar.py ingest --primera-carga --etapas 2   # nur die Historie
```

### Drei Jahre statt zwei

Ab Werk reicht das Archiv **zwei Jahre** zurück. Das deckt den üblichen Vertragszyklus
ab, und es ist die Grenze, ab der sich Plattenplatz und Wartezeit nicht mehr von selbst
rechtfertigen.

Wer weiter zurück will, findet den Knopf in der Anwendung selbst, unter «Was dieser Radar
abdeckt – und was nicht»: **Auf 3 Jahre erweitern**. Er sagt vorher, was er kostet — rund
1,6 GB und 8 bis 12 Minuten pro zusätzlichem Jahr, und der Platz bleibt danach belegt —
und fragt nach. Nichts davon passiert von allein: ohne diesen Knopf bleibt es bei zwei
Jahren.

Danach lädt nur der Historien-Schritt neu, im Hintergrund, und auch der nur die Monate,
die noch fehlen: was schon im Cache liegt, wird nicht zweimal geholt. Der Posteingang
bleibt währenddessen benutzbar. Bricht der Abruf ab, gilt der Schritt als offen und das
nächste Öffnen nimmt ihn wieder auf.

Auf der Kommandozeile ist es dieselbe Einstellung — sie wird gespeichert, ein späterer
Abruf ohne `--meses` schrumpft das Archiv also nicht wieder:

```bash
python3 radar.py ingest --primera-carga --etapas 2 --meses 36
```

Mehr als 36 Monate nimmt weder der Knopf noch die Kommandozeile an. Nicht aus Prinzip:
so weit zurück ist die Abdeckung des Bekanntmachungsservice noch lückenhaft (die
Veröffentlichungspflicht nach VgV §10a Abs. 5 gilt erst seit dem 25.10.2023), und ein
Archiv, das behauptet vollständig zu sein und es nicht ist, ist schlechter als ein
kurzes.

### Und danach: sie hält sich selbst aktuell

Dieselbe Prüfung beim Öffnen deckt den Alltag ab. Sind alle Schritte erledigt und der
letzte erfolgreiche Abruf älter als **20 Stunden**, wird im Hintergrund abgerufen.

Warum 20 und nicht 24: wer die Anwendung jeden Morgen zur selben Zeit öffnet, öffnet sie
mal fünf Minuten früher — und mit 24 Stunden würde ein ganzer Tag übersprungen.

Damit ist `radar.py programar` (die launchd-Aufgabe) optional: sie lohnt nur, wenn die
Daten auch dann frisch sein sollen, wenn man die Anwendung tagelang nicht öffnet. Wer das
Nachladen beim Öffnen nicht will:

```bash
python3 radar.py serve --sin-autocarga
```

Beide Schritte laden **zwei Formate**: den OCDS-Export und das eForms-XML. Das ist der
Grund für die 3,3 GB, und es lohnt sich — warum, steht unten unter «Die Quelle».

**Gemessen**, nicht geschätzt: Schritt 1 brauchte 50 Sekunden für 35.286
Bekanntmachungen, die ganze Erstbefüllung rund 10 Minuten für 575.230. Danach liegt die
Datenbank bei rund 3,6 GB und der Cache bei rund 1,1 GB. Wie lange die Downloads dauern, hängt an deiner Leitung; die Verarbeitung
hängt nur an der Maschine — rund 700 Bekanntmachungen pro Sekunde.

Bricht ein Download ab, wird er mehrfach automatisch wiederholt, mit wachsenden
Wartezeiten. Und weil nur **abgeschlossene** Zeiträume zwischengespeichert werden —
ein beendeter Monat, ein beendeter Tag —, kann ein halb geladener Zeitraum nie
dauerhaft im Cache landen.

---

## Die Quelle

Alles kommt aus **einem** Dienst: dem **Bekanntmachungsservice** des Beschaffungsamts
des BMI, `oeffentlichevergabe.de`. Kein Schlüssel, keine Registrierung, keine Quote,
Lizenz **CC0**.

Das ist der entscheidende Unterschied zu Spanien, wo dasselbe Werkzeug zwei Konnektoren
und 5 GB Download braucht: seit dem 25.10.2023 ist die Veröffentlichung dort oberhalb
der EU-Schwellenwerte **gesetzlich vorgeschrieben** (VgV §10a Abs. 5), und unterhalb der
Schwelle laufen die großen Plattformen ohnehin mit auf — DTVP, evergabe-online,
evergabe.de, RIB, subreport, die Vergabemarktplätze der Länder, Autobahn GmbH, Deutsche
Bahn — dazu die Übernahme aus service.bund.de.

**Es werden zwei Formate geladen, und das kostet 2,3 GB extra.** Der OCDS-Export ist
bequem — normalisiert, ein JSON je Bekanntmachung —, aber er lässt drei Dinge weg, die
im eForms-XML stehen. Alles gemessen an einem ganzen Tag:

| | im OCDS | im eForms |
|---|---|---|
| **Angebotsfrist** | 0 von 857 | 420 von 857 (83 % der laufenden Verfahren) |
| **Zuschlagspreis** | 50 von 314 Zuschlägen (15 %) | 231 von 314 (**73 %**), als `PayableAmount` |
| **Geschäftszeichen** | nirgends | in allen nativen eForms-DE |

Der Zuschlagspreis ist der Grund, warum eForms auch für die ganze Historie geladen wird
und nicht nur für das jüngste Fenster: Zuschläge sammeln sich über die zwei Jahre an, und
ohne sie steht die Auftragnehmer-Liste fast durchgehend auf 0 € und dem Block
«Preisabschlag» fehlt die Vergleichszahl.

Das dritte Format, `ocds2`, wäre mit 0,8 GB billiger, deckt aber nur die Hälfte der
Bekanntmachungen ab — unterm Strich rund 41 % der Zuschlagspreise statt 73 %.

**TED** (das EU-Amtsblatt) ist als Konnektor vorhanden, aber **standardmäßig aus**: für
Deutschland bringt es keine neuen Datensätze und erzwingt nur, zwei fast deckungsgleiche
Quellen abzugleichen. Einschalten mit `--fuente ted`.

---

## Wie man es benutzt

### Die Zahlen oben

| Kennzahl | Was sie zählt |
|---|---|
| **laufend** | offene Verfahren, deren Frist noch nicht abgelaufen ist |
| **Frist ≤7 T** | was diese Woche schließt |
| **ungeprüft** | noch nicht angesehen |
| **beobachtet** | auf „Beobachten“ gesetzt |
| **Treffer** | alles, was zu einem Suchprofil passt |

Ein Klick auf eine Kennzahl filtert den Posteingang danach.

### Posteingang

Eine Karte je **Vorgang**, nicht je Bekanntmachung: Ausschreibung, Korrektur und
Zuschlag desselben Verfahrens werden zusammengefasst. Möglich wird das durch die `ocid`
des Dienstes, einen echten Vorgangsidentifikator — in Spanien muss dieselbe Gruppierung
über Heuristik auf Aktenzeichen und Titel laufen.

Jede Karte zeigt in zehn Sekunden, ob sich Hinsehen lohnt: Gegenstand, Vergabestelle,
Tage bis zum Fristende, Auftragswert, Bundesland, Verfahrensart und das Suchprofil, über
das sie hereinkam. Ein Klick öffnet die Detailansicht mit **„Warum dieser Treffer“** —
welcher Begriff genau gegriffen hat. Ohne diese Spur kann man das Rauschen nicht
schärfen, und das Rauschen ist das, was Leute nach zwei Wochen aufhören lässt, ein
Werkzeug zu öffnen.

**Bearbeitungsstatus** gilt für den ganzen Vorgang: „Verworfen“ auf einer Karte nimmt
auch die Korrektur und den Zuschlag mit heraus, und ein Anschlusshinweis, der morgen zum
selben Vorgang kommt, erbt die Entscheidung.

### Vertragsenden

Laufende Verträge, die bald auslaufen. Die Liste für den Anruf, **bevor** die neue
Ausschreibung herausgeht.

Das Enddatum ist **errechnet**, nicht veröffentlicht: aus Vertragsbeginn beziehungsweise
Zuschlagsdatum plus Laufzeit. Wo sich nichts errechnen lässt, bleibt es leer — lieber
kein Datum als ein erfundenes, auf dessen Grundlage man jemanden anruft.

### Auftragnehmer

Wer diese Aufträge bekommt. Schreibvarianten derselben Firma werden zusammengefasst
(`GmbH`, `GmbH & Co. KG`, `ARGE`, `UG haftungsbeschränkt` …). Das `&` mitzubehandeln ist
dabei kein Detail: es steckt in der Hälfte der deutschen Firmierungen, und ohne das
erscheint dieselbe Firma dreimal.

Gemessen an echten Daten: von 5.818 Zuschlägen nennen nur 14 % die Firma unter
`awards[].suppliers`, aber 92 % unter `parties` mit der Rolle `supplier`. Wer nur das
Erste liest, bekommt eine zu 3 % gefüllte Liste und hält Deutschland für intransparent.

### Auswertung

Die Muster dieses Marktes, nicht der Stand von heute: wann veröffentlicht wird, zu
welchen Preisen zugeschlagen wird, wie lange Entscheidungen dauern, wer wiederholt
einkauft, unter welchen CPV-Codes das eigene Produkt läuft.

Eine Einschränkung, die man kennen muss: **die meisten Bekanntmachungen nennen keinen
Auftragswert.** In Deutschland gibt es keine allgemeine Pflicht dazu, weder ober- noch
unterhalb der Schwellenwerte; in Spanien nennt ihn fast jede. Alles, was nach Geld
sortiert, arbeitet auf dem Teil, der ihn nennt — die Fußnoten unter den Blöcken sagen
jeweils, worauf sie gerechnet sind.

Gemessen über einen ganzen Tag: der OCDS-Export nennt ihn bei **9 %** aller
Bekanntmachungen, das eForms-XML bei **19 %** — es steht dort in Feldern, die der Export
gar nicht abbildet (`PayableAmount`, `TotalAmount`, `EstimatedOverallContractAmount`).
Deshalb wird eForms für den gesamten Zeitraum geladen.

Bei den **Zuschlägen** ist der Unterschied noch deutlicher, und dort zählt er auch am
meisten: 15 % im OCDS gegen **73 %** im eForms. Ohne das stünde die Auftragnehmer-Liste
fast durchgehend auf 0 €.

### Suchbegriffe

Hier stehen die Wörter, die deine Suche definieren. Beim Speichern wird alles bereits
Heruntergeladene neu bewertet, **ohne erneut zu laden**. „Vorschau“ sagt vorher, wie
viele Treffer dazukommen und wegfallen.

---

## Die Suche schärfen

Drei Ebenen, und die Reihenfolge ist der ganze Trick:

- **Eindeutige Begriffe** reichen allein: `phishing`, `dmarc`, `awareness-plattform`.
- **Mehrdeutige Begriffe** zählen nur zusammen mit **erforderlichem Kontext** im Text:
  `schulung` allein ist nichts, `schulung` + `it-sicherheit` ist ein Treffer.
- **CPV-Codes** geben Punkte, nehmen aber **nie allein auf**. 80533100 ist die gesamte
  IT-Schulung des Landes, Tabellenkalkulation inklusive.
- **Ausschlüsse** stechen alles andere.

### Drei Regeln, die im Deutschen anders sind

**1. Ein Begriff greift nur am Wortanfang — und das ist hier ein Geschenk.** Der
Bindestrich zählt als Wortgrenze, also findet `sicherheit` auch `IT-Sicherheit` und
`Cyber-Sicherheit`. Am Ende eines zusammengeschriebenen Kompositums greift er nicht, und
genau dort sitzen die falschen Freunde: `Ausfallsicherheit`, `Arbeitssicherheit`,
`Betriebssicherheitsverordnung`, `Hochwassersicherheit` fallen von selbst heraus.
Gemessen: 13 % der Vorkommen von `sicherheit` und 9 % von `schulung` stecken in einem
Kompositum — und es ist jedes Mal Rauschen.

**2. Mehrwortbegriffe immer doppelt eintragen**, mit Leerzeichen und mit Bindestrich:
`social engineering` findet `Social-Engineering` nicht, und umgekehrt.

**3. `ausschluss` bleibt fast leer** — anders als in der spanischen Fassung, und das ist
gemessen. Mit den naheliegenden Ausschlüssen (`arbeitssicherheit`, `ausfallsicherheit`,
`brandschutz`) wurden 10 von 13 Testfällen richtig eingeordnet, ohne sie **12 von 13**.
Zwei echte Treffer gingen verloren: Sammelausschreibungen für Schulungen bündeln
IT-Sicherheit, Brandschutz und Erste Hilfe in *einer* Bekanntmachung, und genau die will
man sehen. Ein Ausschluss greift außerdem auch nach einem Bindestrich und würde
`IT-Sicherheitsdienstleistungen` mitnehmen.

Umlaute und ß sind egal: `Maßnahmen` = `Massnahmen` = `MASSNAHMEN`, `Prüfung` =
`Prufung`. Tippfehler ruhig stehen lassen — `phising` mit einem s steht so in
veröffentlichten Unterlagen. Und Englisch bleibt englisch: `Awareness`, `Phishing` und
`Penetration Test` werden in deutschen Vergabeunterlagen nicht übersetzt.

### Was zu erwarten ist

Über einen echten Monat (August 2026, 22.892 Bekanntmachungen) liefern die mitgelieferten
Profile **45 Treffer, also 0,20 %** — rund zwei pro Arbeitstag. Ganz oben standen die
beiden, die man sehen will: die `Awareness-Plattform` des IT-Dienstleistungszentrums
Saarland und das Anti-Phishing-Projekt des BSI.

Darunter gibt es Rauschen, und das ist Absicht: die Punktzahl sortiert es nach unten,
statt es zu verstecken. Ein Filter, der nichts durchlässt, versteckt auch die Treffer.

---

## Was abgedeckt ist — und was nicht

**Abgedeckt:** alles oberhalb der EU-Schwellenwerte (gesetzlich verpflichtend seit dem
25.10.2023) und ein großer Teil darunter, über die Plattformen der Länder und die
Übernahme aus service.bund.de. Gemessen über einen Tag: 93 % der Bekanntmachungen
bringen ein Bundesland mit, 83 % einen CPV-Code, 87 % der laufenden Verfahren eine
Angebotsfrist, 62 % ein Geschäftszeichen.

**Nicht abgedeckt:**

- **Es wird nicht in den Vergabeunterlagen gesucht**, nur in Titel, Gegenstand und
  Losbeschreibung. Bei vielen Portalen liegen die Unterlagen ohnehin erst nach
  Registrierung vor. Eine Ausschreibung, die dein Produkt nur im Dokument nennt, taucht
  hier nicht auf.
- **Unterschwellige Direktaufträge** sind nicht vollständig erfasst: sie werden oft gar
  nicht veröffentlicht, und wo doch, selten mit genug Angaben.
- **Das Geschäftszeichen fehlt in der tiefen Historie.** Es steht nur im eForms-XML, das
  nur für das jüngste Fenster geladen wird — und auch dort nur in den nativen eForms-DE,
  nicht in den aus service.bund.de übernommenen (gemessen: 150/150 gegen 0/150). Die
  Gruppierung leidet nicht darunter, die läuft über die `ocid`.
- **Das Bundesland ist das der Vergabestelle**, nicht das des Leistungsorts. Zentrale
  Beschaffungen des Bundes werden in Bonn gezeichnet.

---

## Befehle

```bash
python3 radar.py ingest                  # täglicher Abruf
python3 radar.py ingest --primera-carga  # Erstbefüllung in zwei Schritten
python3 radar.py ingest --backfill 2025  # ein bestimmtes Jahr nachladen
python3 radar.py match                   # neu bewerten, ohne zu laden
python3 radar.py serve                   # Posteingang auf 127.0.0.1:8812
python3 radar.py export ziel.csv         # CSV exportieren
python3 radar.py vencimientos            # auslaufende Verträge im Terminal
python3 radar.py adjudicatarios          # Ranking der Auftragnehmer
python3 radar.py estado                  # Zustand der Datenbank und des Cache
python3 radar.py doctor                  # 13 Prüfungen, jede mit ihrer Abhilfe
python3 radar.py programar --hora 8 --minuto 30   # täglich per launchd
python3 radar.py actualizar              # neue Version holen, falls es eine gibt
```

`doctor` fasst nichts an: die Datenbank wird schreibgeschützt geöffnet, und jede Prüfung,
die nicht „ok“ ist, nennt die Abhilfe.

---

## Wenn etwas schiefgeht

| Symptom | Was dahintersteckt |
|---|---|
| Der Posteingang bleibt leer | Noch keine Erstbefüllung. `python3 radar.py ingest --primera-carga --etapas 1` |
| Alte Vergaben fehlen | Das Archiv reicht zwei Jahre zurück. In der Anwendung unter «Was dieser Radar abdeckt» auf 3 Jahre erweitern |
| „Ungültige Suche“ | Die Freitextsuche fasst jedes Wort als Phrase; Anführungszeichen im Suchfeld weglassen |
| Der Download bricht ständig ab | Wird automatisch wiederholt. Was schon geladen ist, bleibt liegen: `radar.py estado` zeigt den Cache |
| Die App startet nicht | Meist fehlt Python 3.9+. `start.command` sagt im Terminal, was los ist |
| Doppelte Karten | `python3 radar.py match` neu laufen lassen; die Gruppierung wird beim Speichern gesetzt |
| Nichts passt mehr nach dem Bearbeiten der Begriffe | „Vorschau“ vor dem Speichern sagt vorher, wie viel wegfällt. `config/suchprofile.vorherige.json` ist die letzte Kopie |

---

## Wie es gebaut ist

Reine Standardbibliothek. Keine Abhängigkeiten, kein Build, keine Konten. Was es gibt:

```
radar.py                     Kommandozeile
radar/
  sources/oeffentlichevergabe.py   Konnektor: OCDS + eForms, Cache nach Zeiträumen
  sources/ted.py                   EU-Amtsblatt (standardmäßig aus)
  sources/base.py                  Quellen-Protokoll + NUTS1 → Bundesland
  model.py                   Ausschreibung, normalisiert, quellenübergreifend
  matching.py                Regelwerk: drei Ebenen, mit Begründung je Treffer
  db.py                      SQLite + FTS5, Versionierung über Fingerabdruck
  consultas.py               Abfragen für Posteingang und Auswertung
  pipeline.py                Orchestrierung, Quelle für Quelle isoliert
  net.py                     HTTP mit Wiederholung und fortsetzbarem Download
  progreso.py                Fortschrittsanzeige, Terminal und App
web/                         Oberfläche: HTML, CSS, JavaScript, ohne Framework
config/suchprofile.json      Deine Suchbegriffe (nicht versioniert)
tests/                       466 Tests, unittest, ohne Netz
```

Getestet wird mit:

```bash
python3 -m unittest discover -s tests -t . -v
```

Die Fixtures unter `tests/fixtures/` sind **echte** Downloads des Dienstes (CC0), eine je
Fall, den der Konnektor lesen können muss: Ausschreibung mit Losen, Zuschlag mit mehreren
Auftragnehmern, Aufhebung, Vorinformation, aus service.bund.de übernommen, ohne CPV, in
fremder Währung.

---

## Das Projekt selbst ändern

Es gibt keinen Build und keine Abhängigkeiten: Python aus der Standardbibliothek, dazu
HTML, CSS und JavaScript ohne Framework. Eine Datei ändern und neu laden genügt.

Wer mit **Claude Code** arbeitet, öffnet einfach diesen Ordner — `claude` im
Projektverzeichnis starten, oder den Ordner im Desktop-Client auswählen. Zwei Dinge sind
dabei nützlich zu wissen:

- **Quelltext und Kommentare sind auf Spanisch.** Das ist Absicht (siehe ganz oben) und
  kein Versehen. In den Kommentaren stehen die Messungen, aus denen jede Entscheidung
  stammt — sie sind der interessanteste Teil des Projekts.
- **Die Tests sind das Sicherheitsnetz.** 483 Stück, ohne Netzzugang, in vier Sekunden:

  ```bash
  python3 -m unittest discover -s tests -t . -v
  ```

  Wer etwas ändert, lässt sie laufen. Mehrere davon sind eigens dafür da, Fehler zu
  fangen, die sonst *still* passieren — eine übersetzte Phasenkennung, die die
  Fortschrittsleiste tötet, ein Abfrageplan, der wieder die halbe Tabelle liest, ein
  Ladeschritt, der die Fristen des vorigen überschreibt.

Nach Änderungen am macOS-Fenster oder an `web/` die App neu bauen:

```bash
python3 herramientas/construir_app.py --forzar
```
