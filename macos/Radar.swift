// El envoltorio nativo: una ventana de macOS con la bandeja dentro.
//
// Lo que arregla es el arranque. Hasta ahora había que hacer doble clic en
// `start.command`, que deja una ventana de Terminal abierta —y quien la cierra por
// costumbre se lleva el servidor por delante— y la aplicación en una pestaña del
// navegador, entre las otras treinta. Esto es un icono en el Dock.
//
// Deliberadamente NO reimplementa nada. Toda la lógica sigue en Python y la interfaz
// sigue siendo `web/`: esto arranca `radar.py serve`, espera a que conteste y lo enseña
// en un WKWebView. El día que haya que cambiar la bandeja se cambia el HTML, como
// antes, sin recompilar nada.
//
// Se compila con las Command Line Tools, sin Xcode:
//
//     python3 herramientas/construir_app.py
//
// Dos cosas que parecen detalles y no lo son, explicadas donde ocurren: el menú
// «Edición» hay que construirlo a mano o no funcionan ⌘C y ⌘V dentro de la web, y al
// salir hay que matar el servidor pero NO la ingesta.

import AppKit
import WebKit

// MARK: - Registro

/// Traza a stderr. Invisible al hacer doble clic, legible al arrancar el binario a mano:
///
///     "Ausschreibungsradar.app/Contents/MacOS/radar"
///
/// Existe porque la primera versión se quedaba en «Arrancando el radar…» sin decir por
/// qué, y desde fuera no había manera de saber si el problema era Python, la carpeta o
/// el puerto.
func traza(_ mensaje: String) {
    FileHandle.standardError.write(Data("[radar] \(mensaje)\n".utf8))
}

// MARK: - Configuración

enum Ajustes {
    static let puerto = 8812
    static let host = "127.0.0.1"
    static var base: URL { URL(string: "http://\(host):\(puerto)/")! }

    /// Donde está el programa: la carpeta que tiene `radar.py`.
    ///
    /// Se mira en dos sitios, y el orden importa:
    ///
    /// 1. **`Contents/Resources` del propio bundle.** Es el caso normal: el `.app` se
    ///    lleva el código dentro, así que es un único fichero que se arrastra a
    ///    Aplicaciones y se manda por correo. Los datos no van ahí —dentro de un bundle
    ///    no se escribe— y de eso se encarga `radar/rutas.py`, que al verse dentro de
    ///    un `.app` manda la base y los perfiles a `~/Library/Application Support`.
    /// 2. **Al lado del bundle.** Es el caso de quien trabaja en el proyecto: se compila
    ///    la app en la carpeta del repositorio y se quiere que use ESE código y ESA
    ///    base, no una copia congelada dentro del bundle. Sin esto, cada cambio en el
    ///    Python obligaría a volver a montar la app para verlo.
    static func raizProyecto() -> URL? {
        let fm = FileManager.default
        let tiene = { (u: URL) in
            fm.fileExists(atPath: u.appendingPathComponent("radar.py").path)
        }

        // Una copia de trabajo manda sobre la copia empaquetada: si el .app está dentro
        // del repositorio, se desarrolla contra el repositorio.
        var candidata = Bundle.main.bundleURL.deletingLastPathComponent()
        for _ in 0..<5 {
            if tiene(candidata) { return candidata }
            candidata = candidata.deletingLastPathComponent()
        }

        if let recursos = Bundle.main.resourceURL, tiene(recursos) { return recursos }
        return nil
    }
}

/// La versión que dice el código Python, leyendo la línea de `radar/__init__.py`.
///
/// Es la misma técnica que usa `actualizacion._version_del_arbol()`, y por el mismo
/// motivo: para saber un número no hay que ejecutar nada.
func versionDelProyecto() -> String? {
    guard let raiz = Ajustes.raizProyecto(),
          let texto = try? String(contentsOf: raiz.appendingPathComponent("radar/__init__.py"),
                                  encoding: .utf8)
    else { return nil }
    for linea in texto.split(separator: "\n") where linea.hasPrefix("__version__") {
        return linea.split(separator: "=", maxSplits: 1).last?
            .trimmingCharacters(in: CharacterSet(charactersIn: " \"'\t\r"))
    }
    return nil
}

/// La versión de este bundle.
func versionDelBundle() -> String {
    Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
}

// MARK: - Localizar Python

enum Python {
    /// Dónde buscar, en orden. Homebrew primero porque en un Mac de desarrollo es el
    /// que está actualizado.
    static let candidatos = [
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ]

    /// El primer Python 3.9+ que de verdad arranque.
    ///
    /// No basta con que el fichero exista, y esto es exactamente lo que documenta
    /// `start.command`: macOS trae en `/usr/bin/python3` un lanzador que está ahí
    /// aunque Python no lo esté, y al invocarlo abre el instalador de las herramientas
    /// de Xcode y falla. Así que se comprueba ejecutándolo.
    static func localizar() -> String? {
        var aProbar = candidatos
        // Y lo que diga el PATH del usuario, por si tiene pyenv o un conda.
        if let delPath = conWhich() { aProbar.insert(delPath, at: 0) }

        for ruta in aProbar where FileManager.default.isExecutableFile(atPath: ruta) {
            if valeLaVersion(ruta) { return ruta }
        }
        return nil
    }

    private static func conWhich() -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/which")
        p.arguments = ["python3"]
        let salida = Pipe()
        p.standardOutput = salida
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return nil }

        // Se VACÍA la tubería antes de esperar, no después. Al revés es el bloqueo
        // clásico: si el hijo escribe más de lo que cabe en el búfer de la tubería
        // (unos 64 KB) se queda parado esperando que alguien lea, el padre se queda
        // parado en `waitUntilExit()` esperando que el hijo acabe, y la aplicación no
        // llega nunca a abrir la ventana. Con `which python3` no pasa —son cuarenta
        // bytes— pero es una trampa que no cuesta nada desarmar, y el arranque es
        // justo el sitio donde un bloqueo no se puede ni diagnosticar.
        let datos = salida.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()

        guard p.terminationStatus == 0 else { return nil }
        let texto = String(decoding: datos, as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return texto.isEmpty ? nil : texto
    }

    private static func valeLaVersion(_ ruta: String) -> Bool {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: ruta)
        p.arguments = ["-c", "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return false }
        p.waitUntilExit()
        return p.terminationStatus == 0
    }
}

// MARK: - El servidor

/// Arranca `radar.py serve` y lo para al salir.
final class Servidor {
    private var proceso: Process?
    /// Cierto cuando el servidor ya estaba en marcha antes de abrir la app: entonces no
    /// es nuestro y no se para al salir. Pasa al abrir la app teniendo ya un
    /// `start.command` corriendo, y matarle el servidor a otro proceso sería grosero.
    private(set) var ajeno = false

    /// ¿Contesta ya algo en el puerto?
    ///
    /// Se pregunta por `/api/resumen` y no solo por que el socket acepte conexiones:
    /// el `ThreadingHTTPServer` acepta desde el primer momento, pero la primera
    /// consulta contra una base de 3,3 GB tarda lo suyo, y cargar la página antes de
    /// que la API conteste enseña una bandeja vacía que luego se rellena de golpe.
    static func responde(timeout: TimeInterval = 1.5) -> Bool {
        var url = URLComponents(string: "http://\(Ajustes.host):\(Ajustes.puerto)/api/resumen")!
        url.queryItems = nil
        var peticion = URLRequest(url: url.url!)
        peticion.timeoutInterval = timeout
        peticion.cachePolicy = .reloadIgnoringLocalCacheData

        let espera = DispatchSemaphore(value: 0)
        var ok = false
        let tarea = URLSession.shared.dataTask(with: peticion) { _, respuesta, _ in
            ok = (respuesta as? HTTPURLResponse)?.statusCode == 200
            espera.signal()
        }
        tarea.resume()
        _ = espera.wait(timeout: .now() + timeout + 0.5)
        return ok
    }

    enum Fallo: Error, LocalizedError {
        case sinPython
        case sinProyecto
        case noArranca(String)

        var errorDescription: String? {
            switch self {
            case .sinPython:
                return """
                No encuentro un Python 3.9 o superior, y sin él esto no arranca.

                macOS ya no lo trae: Apple lo retiró del sistema en Catalina, así que en \
                un Mac recién estrenado hay que instalarlo una vez desde \
                python.org/downloads/macos. Si usas Homebrew: brew install python3
                """
            case .sinProyecto:
                return """
                Esta copia de la aplicación está incompleta: no lleva el programa dentro.

                Vuelve a montarla con «python3 herramientas/construir_app.py», o pide \
                una copia nueva a quien te la pasó.
                """
            case .noArranca(let motivo):
                return "El servidor no ha llegado a contestar.\n\n\(motivo)"
            }
        }
    }

    func arrancar() throws {
        traza("comprobando si ya hay un servidor en \(Ajustes.puerto)…")
        if Servidor.responde() {
            traza("ja: wird wiederverwendet")
            // Ya hay uno. Se reutiliza en lugar de fallar por el puerto ocupado: es lo
            // que hace que abrir la app dos veces no monte dos servidores.
            ajeno = true
            return
        }
        guard let raiz = Ajustes.raizProyecto() else {
            traza("radar.py ist weder im Bundle noch daneben zu finden "
                  + "(\(Bundle.main.bundleURL.path))")
            throw Fallo.sinProyecto
        }
        traza("proyecto en \(raiz.path)")
        guard let python = Python.localizar() else {
            traza("kein brauchbares Python 3.9+")
            throw Fallo.sinPython
        }
        traza("python en \(python)")

        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        // `-B`: que Python no escriba `__pycache__`. No es una optimización, es una
        // condición: el código vive dentro del .app, y escribir ahí invalida la firma
        // —medido: 17 ficheros añadidos y `codesign --verify` en rojo tras el primer
        // arranque— además de no poder hacerse si la app está en /Applications, que no
        // es del usuario.
        p.arguments = ["-B", "-u", "radar.py", "serve",
                       "--sin-navegador", "--puerto", String(Ajustes.puerto)]
        p.currentDirectoryURL = raiz

        // Y lo mismo por entorno, porque la ingesta la lanza `radar/busqueda.py` como
        // otro proceso con `sys.executable` y sin `-B`: la variable sí la hereda, el
        // argumento no. Se parte del entorno heredado en lugar de fijar uno nuevo para
        // no dejar al hijo sin PATH ni HOME.
        var entorno = ProcessInfo.processInfo.environment
        entorno["PYTHONDONTWRITEBYTECODE"] = "1"
        p.environment = entorno
        // La salida del servidor no se lee: lo que importa lo cuenta la propia bandeja,
        // y un Pipe que nadie vacía acaba bloqueando al hijo cuando llena el búfer.
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch {
            traza("no se pudo lanzar: \(error.localizedDescription)")
            throw Fallo.noArranca(error.localizedDescription)
        }
        proceso = p
        traza("servidor lanzado (pid \(p.processIdentifier))")
    }

    /// Espera a que conteste, sondeando. Devuelve en el hilo principal.
    func esperar(hasta segundos: Int = 90, listo: @escaping (Result<Void, Error>) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            for _ in 0..<segundos {
                if Servidor.responde() {
                    DispatchQueue.main.async { listo(.success(())) }
                    return
                }
                // Si el proceso se ha muerto, no tiene sentido seguir esperando 90 s.
                if let p = self?.proceso, !p.isRunning {
                    let codigo = p.terminationStatus
                    DispatchQueue.main.async {
                        listo(.failure(Fallo.noArranca(
                            "Der Python-Prozess endete mit Code \(codigo). "
                            + "Versuch, ihn von Hand mit „python3 radar.py serve“ zu starten, "
                            + "um zu sehen, was er sagt.")))
                    }
                    return
                }
                Thread.sleep(forTimeInterval: 1)
            }
            DispatchQueue.main.async {
                listo(.failure(Fallo.noArranca("Ha pasado \(segundos) s sin respuesta.")))
            }
        }
    }

    /// Para el servidor. NO toca la ingesta.
    ///
    /// `radar/busqueda.py` lanza la descarga como un proceso aparte y desligado, a
    /// propósito: dura horas y tiene que sobrevivir al cierre de la terminal. Aquí solo
    /// se termina el hijo que hemos arrancado nosotros —el servidor—, así que una carga
    /// en marcha sigue bajando datos después de cerrar la ventana, que es justo lo que
    /// promete el mensaje de `start.command`.
    func parar() {
        guard !ajeno, let p = proceso, p.isRunning else { return }
        p.terminate()
        // Un margen corto para que cierre el socket y suelte la base; si no se va,
        // se le deja, porque colgar el cierre de la app es peor.
        for _ in 0..<20 where p.isRunning { Thread.sleep(forTimeInterval: 0.05) }
    }
}

// MARK: - La aplicación

final class Delegado: NSObject, NSApplicationDelegate, WKNavigationDelegate,
                      WKUIDelegate, WKDownloadDelegate {
    private let servidor = Servidor()
    private var ventana: NSWindow!
    private var web: WKWebView!
    private var aviso: NSTextField!

    // MARK: Ciclo de vida

    func applicationDidFinishLaunching(_ notification: Notification) {
        construirMenu()
        construirVentana()

        do {
            try servidor.arrancar()
        } catch {
            mostrarFalloYSalir(error)
            return
        }

        aviso.stringValue = servidor.ajeno
            ? "Verbindung zum bereits laufenden Radar …"
            : "Radar wird gestartet …"

        servidor.esperar { [weak self] resultado in
            guard let self else { return }
            switch resultado {
            case .success:
                self.aviso.isHidden = true
                self.web.isHidden = false
                self.web.load(URLRequest(url: Ajustes.base))
                // Primero lo propio: si el actualizador ya trajo código nuevo, esta
                // ventana es la vieja y hay que rehacerla antes de ir a preguntar a
                // GitHub si hay otra versión más.
                if !self.avisarSiElBundleSeQuedoAtras() {
                    self.comprobarVersion()
                }
            case .failure(let error):
                self.mostrarFalloYSalir(error)
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        servidor.parar()
    }

    /// Es una app de una sola ventana: sin ella no hay nada que representar, y dejarla
    /// viva dejaría el servidor de Python corriendo sin que se vea por ningún sitio.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    // MARK: Ventana

    private func construirVentana() {
        ventana = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
            styleMask: [.titled, .closable, .resizable, .miniaturizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        ventana.title = "Ausschreibungsradar"
        ventana.titlebarAppearsTransparent = false
        // Por debajo de esto la bandeja empieza a apilar los filtros en cuatro líneas.
        ventana.minSize = NSSize(width: 820, height: 560)
        // Recuerda tamaño y posición sin que haya que guardar preferencias a mano.
        ventana.setFrameAutosaveName("ventana-radar")

        let configuracion = WKWebViewConfiguration()
        // El servidor manda `Cache-Control: no-store`, pero un almacén persistente
        // igualmente guardaría el localStorage de la bandeja —el orden elegido, los
        // avisos descartados— que es justo lo que se quiere conservar entre arranques.
        configuracion.websiteDataStore = .default()

        web = WKWebView(frame: ventana.contentView!.bounds, configuration: configuracion)
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = false
        // El inspector web, para poder mirar la consola cuando algo no pinta. Es una
        // herramienta interna; no hay motivo para esconderlo.
        if web.responds(to: Selector(("setInspectable:"))) {
            web.setValue(true, forKey: "inspectable")
        }
        web.isHidden = true

        aviso = NSTextField(labelWithString: "")
        aviso.alignment = .center
        aviso.font = .systemFont(ofSize: 13)
        aviso.textColor = .secondaryLabelColor
        aviso.translatesAutoresizingMaskIntoConstraints = false

        let contenedor = NSView(frame: ventana.contentView!.bounds)
        contenedor.addSubview(web)
        contenedor.addSubview(aviso)
        NSLayoutConstraint.activate([
            aviso.centerXAnchor.constraint(equalTo: contenedor.centerXAnchor),
            aviso.centerYAnchor.constraint(equalTo: contenedor.centerYAnchor),
        ])
        ventana.contentView = contenedor

        ventana.center()
        ventana.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func mostrarFalloYSalir(_ error: Error) {
        aviso.isHidden = false
        web.isHidden = true
        aviso.stringValue = error.localizedDescription

        let alerta = NSAlert()
        alerta.alertStyle = .critical
        alerta.messageText = "Der Radar konnte nicht geöffnet werden"
        alerta.informativeText = error.localizedDescription
        alerta.addButton(withTitle: "Schließen")
        alerta.beginSheetModal(for: ventana) { _ in NSApp.terminate(nil) }
    }

    // MARK: Menú
    //
    // Entero a mano, porque un binario de `swiftc` no tiene nib y `NSApp.mainMenu`
    // empieza vacío.
    //
    // El menú «Edición» NO es decorativo y es el que más fácil se olvida: los atajos de
    // cortar, copiar, pegar y seleccionar todo dentro de un WKWebView los sirve el
    // *first responder* a través de estos items. Sin el menú, ⌘C y ⌘V no hacen nada en
    // la caja de búsqueda ni en el área de notas de la ficha, y la app parece rota sin
    // que haya ningún error en ninguna parte.

    private func construirMenu() {
        let principal = NSMenu()

        let nombre = "Ausschreibungsradar"
        let mApp = NSMenuItem()
        let app = NSMenu()
        app.addItem(withTitle: "Über \(nombre)",
                    action: #selector(acercaDe), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(withTitle: "Jetzt nach Neuem suchen",
                    action: #selector(buscarAhora), keyEquivalent: "r")
            .keyEquivalentModifierMask = [.command, .shift]
        app.addItem(withTitle: "Nach neuer Version suchen …",
                    action: #selector(comprobarVersionManual), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(withTitle: "\(nombre) ausblenden",
                    action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        app.addItem(withTitle: "Andere ausblenden",
                    action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
            .keyEquivalentModifierMask = [.command, .option]
        app.addItem(.separator())
        app.addItem(withTitle: "\(nombre) beenden",
                    action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        mApp.submenu = app
        principal.addItem(mApp)

        let mEdicion = NSMenuItem()
        let edicion = NSMenu(title: "Bearbeiten")
        edicion.addItem(withTitle: "Widerrufen", action: Selector(("undo:")), keyEquivalent: "z")
        edicion.addItem(withTitle: "Wiederholen", action: Selector(("redo:")), keyEquivalent: "Z")
        edicion.addItem(.separator())
        edicion.addItem(withTitle: "Ausschneiden", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edicion.addItem(withTitle: "Kopieren", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edicion.addItem(withTitle: "Einsetzen", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edicion.addItem(withTitle: "Alles auswählen",
                        action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        mEdicion.submenu = edicion
        principal.addItem(mEdicion)

        let mVer = NSMenuItem()
        let ver = NSMenu(title: "Darstellung")
        ver.addItem(withTitle: "Neu laden", action: #selector(recargar), keyEquivalent: "r")
        ver.addItem(.separator())
        ver.addItem(withTitle: "Schrift vergrößern", action: #selector(masTexto), keyEquivalent: "+")
        ver.addItem(withTitle: "Schrift verkleinern", action: #selector(menosTexto), keyEquivalent: "-")
        ver.addItem(withTitle: "Originalgröße", action: #selector(textoNormal), keyEquivalent: "0")
        ver.addItem(.separator())
        ver.addItem(withTitle: "Vollbild",
                    action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
            .keyEquivalentModifierMask = [.command, .control]
        mVer.submenu = ver
        principal.addItem(mVer)

        let mVentana = NSMenuItem()
        let ventanaMenu = NSMenu(title: "Fenster")
        ventanaMenu.addItem(withTitle: "Im Dock ablegen",
                            action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        ventanaMenu.addItem(withTitle: "Zoom",
                            action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        ventanaMenu.addItem(.separator())
        ventanaMenu.addItem(withTitle: "Schließen",
                            action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        mVentana.submenu = ventanaMenu
        principal.addItem(mVentana)
        NSApp.windowsMenu = ventanaMenu

        let mAyuda = NSMenuItem()
        let ayuda = NSMenu(title: "Hilfe")
        ayuda.addItem(withTitle: "README im Browser öffnen",
                      action: #selector(abrirReadme), keyEquivalent: "")
        ayuda.addItem(withTitle: "Programmordner anzeigen",
                      action: #selector(mostrarCarpeta), keyEquivalent: "")
        mAyuda.submenu = ayuda
        principal.addItem(mAyuda)
        NSApp.helpMenu = ayuda

        NSApp.mainMenu = principal
    }

    // MARK: Acciones del menú

    @objc private func acercaDe() {
        let version = versionDelBundle()
        let alerta = NSAlert()
        alerta.messageText = "Ausschreibungsradar \(version)"
        alerta.informativeText = """
        Öffentliche Ausschreibungen zu Security-Awareness und E-Mail-Sicherheit \
        von Bund, Ländern und Kommunen.

        Daten und Oberfläche liefert das Python-Programm selbst; dieses Fenster \
        startet es nur und zeigt es an.
        """
        alerta.addButton(withTitle: "Schließen")
        alerta.beginSheetModal(for: ventana, completionHandler: nil)
    }

    @objc private func recargar() { web.reload() }
    @objc private func masTexto() { web.pageZoom = min(web.pageZoom + 0.1, 2.5) }
    @objc private func menosTexto() { web.pageZoom = max(web.pageZoom - 0.1, 0.6) }
    @objc private func textoNormal() { web.pageZoom = 1 }

    /// Pulsa el botón «Buscar ahora» de la bandeja en lugar de llamar a la API.
    ///
    /// Así hay un solo camino: el mismo que el del botón, con sus avisos, su barra de
    /// progreso y su comprobación de que no haya ya una carga en marcha. Duplicar la
    /// llamada aquí significaría mantener dos.
    @objc private func buscarAhora() {
        web.evaluateJavaScript("document.getElementById('buscar-ahora')?.click()")
    }

    @objc private func abrirReadme() {
        guard let raiz = Ajustes.raizProyecto() else { return }
        NSWorkspace.shared.open(raiz.appendingPathComponent("README.md"))
    }

    @objc private func mostrarCarpeta() {
        guard let raiz = Ajustes.raizProyecto() else { return }
        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: raiz.path)
    }

    // MARK: El bundle se queda atrás

    /// ¿El código Python es más nuevo que esta ventana?
    ///
    /// Es el último eslabón del mecanismo de actualización. El actualizador sustituye
    /// `radar/`, `web/` y `macos/` —incluido el binario prefabricado— pero NO el
    /// «.app», porque un bundle no puede reemplazarse a sí mismo mientras corre. Así
    /// que la app lo nota al abrirse y se ofrece a rehacerse.
    ///
    /// Sin esto, tras actualizar quedaría una ventana que dice 1.3.0 en «Acerca de»
    /// sobre un código 1.4.0. Funcionaría —el shell no depende de la versión de Python—
    /// pero la versión que enseña sería mentira, y el día que haya que diagnosticar algo
    /// es exactamente el dato del que se tira.
    ///
    /// Devuelve `true` si ha puesto un diálogo, para no apilarle encima el de GitHub.
    private func avisarSiElBundleSeQuedoAtras() -> Bool {
        let delBundle = versionDelBundle()
        guard let delProyecto = versionDelProyecto() else {
            traza("die Version aus radar/__init__.py ist nicht lesbar")
            return false
        }
        traza("Version: Bundle \(delBundle) · Projekt \(delProyecto)")
        guard delProyecto != delBundle else { return false }
        traza("das Bundle ist veraltet; Neubau wird angeboten")

        let alerta = NSAlert()
        alerta.messageText = "El programa se ha actualizado a la \(delProyecto)"
        alerta.informativeText = """
        Esta ventana es la de la versión \(delBundle). Todo funciona, pero \
        conviene rehacerla para que coincida.

        Tarda unos segundos y no toca ni la base de datos ni tus términos de búsqueda.
        """
        alerta.addButton(withTitle: "Rehacer ahora")
        alerta.addButton(withTitle: "Später")
        alerta.beginSheetModal(for: ventana) { [weak self] respuesta in
            guard respuesta == .alertFirstButtonReturn else { return }
            self?.reconstruirApp()
        }
        return true
    }

    private func reconstruirApp() {
        guard let raiz = Ajustes.raizProyecto(), let python = Python.localizar() else { return }
        // `herramientas/` no va dentro del bundle —no hace falta ahí— así que esto solo
        // tiene sentido en una copia de trabajo. En la empaquetada no se llega nunca,
        // porque la versión del bundle y la del código son la misma por construcción.
        guard FileManager.default.fileExists(
                atPath: raiz.appendingPathComponent("herramientas/construir_app.py").path) else {
            simpleAlerta("Diese Kopie kann sich nicht selbst neu bauen",
                         "Lade die neue Version der Anwendung und zieh sie über "
                         + "die alte.")
            return
        }
        aviso.isHidden = false
        aviso.stringValue = "Fenster wird neu gebaut …"

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let p = Process()
            p.executableURL = URL(fileURLWithPath: python)
            p.arguments = ["herramientas/construir_app.py", "--forzar"]
            p.currentDirectoryURL = raiz
            let salida = Pipe()
            p.standardOutput = salida
            p.standardError = salida
            var texto = ""
            var ok = false
            do {
                try p.run()
                // Se lee antes de esperar, por lo mismo que en `conWhich()`.
                let datos = salida.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                texto = String(decoding: datos, as: UTF8.self)
                ok = p.terminationStatus == 0
            } catch {
                texto = error.localizedDescription
            }

            DispatchQueue.main.async {
                guard let self else { return }
                self.aviso.isHidden = true
                let alerta = NSAlert()
                if ok {
                    alerta.messageText = "Fenster neu gebaut"
                    alerta.informativeText = "Schließe die Anwendung und öffne sie erneut, "
                        + "um die neue Version zu benutzen."
                    alerta.addButton(withTitle: "Jetzt beenden")
                    alerta.addButton(withTitle: "Später beenden")
                    alerta.beginSheetModal(for: self.ventana) { r in
                        if r == .alertFirstButtonReturn { NSApp.terminate(nil) }
                    }
                } else {
                    alerta.messageText = "Das Fenster konnte nicht neu gebaut werden"
                    alerta.informativeText = texto.isEmpty
                        ? "Prueba a mano: python3 herramientas/construir_app.py --forzar"
                        : String(texto.suffix(1200))
                    alerta.addButton(withTitle: "Schließen")
                    alerta.beginSheetModal(for: self.ventana, completionHandler: nil)
                }
            }
        }
    }

    // MARK: Versión nueva

    @objc private func comprobarVersionManual() { comprobarVersion(silencioso: false) }

    /// Pregunta a `/api/actualizacion`, que es el mismo sitio al que pregunta la web.
    ///
    /// El texto lo redacta Python y aquí no se reinterpreta: es quien sabe qué ha
    /// pasado de verdad y qué se ha tocado. La misma regla que ya sigue el aviso de la
    /// interfaz.
    private func comprobarVersion(silencioso: Bool = true) {
        var peticion = URLRequest(url: Ajustes.base.appendingPathComponent("api/actualizacion"))
        peticion.timeoutInterval = 20
        URLSession.shared.dataTask(with: peticion) { [weak self] datos, _, _ in
            guard let datos,
                  let json = try? JSONSerialization.jsonObject(with: datos) as? [String: Any]
            else {
                if !silencioso {
                    DispatchQueue.main.async {
                        self?.simpleAlerta("Konnte nicht nachfragen",
                                           "Keine Antwort bei der Suche nach einer neuen Version.")
                    }
                }
                return
            }
            let hayNueva = json["hay_nueva"] as? Bool ?? false
            let nueva = json["version_nueva"] as? String ?? "?"
            let actual = json["version_actual"] as? String ?? "?"
            let fallo = json["error"] as? String

            DispatchQueue.main.async {
                guard let self else { return }
                if hayNueva {
                    self.ofrecerActualizar(nueva: nueva, actual: actual)
                } else if !silencioso {
                    self.simpleAlerta("Es gibt nichts Neueres",
                                      fallo ?? "Du hast bereits die neueste Version (\(actual)).")
                }
            }
        }.resume()
    }

    private func ofrecerActualizar(nueva: String, actual: String) {
        let alerta = NSAlert()
        alerta.messageText = "Es gibt eine neue Version (\(nueva))"
        alerta.informativeText = """
        Tienes la \(actual). Se sustituye el programa y se deja lo anterior al lado \
        para poder volver atrás.

        Tu base de datos, tu triaje y tus términos de búsqueda no se tocan.
        """
        alerta.addButton(withTitle: "Actualizar ahora")
        alerta.addButton(withTitle: "Später")
        alerta.beginSheetModal(for: ventana) { [weak self] respuesta in
            guard respuesta == .alertFirstButtonReturn else { return }
            self?.aplicarActualizacion()
        }
    }

    private func aplicarActualizacion() {
        aviso.isHidden = false
        aviso.stringValue = "Wird geladen und ersetzt …"

        var peticion = URLRequest(url: Ajustes.base.appendingPathComponent("api/actualizacion"))
        peticion.httpMethod = "POST"
        peticion.setValue("application/json", forHTTPHeaderField: "Content-Type")
        peticion.httpBody = Data("{}".utf8)
        // Descargar el zip y sustituir puede tardar minutos con una línea lenta.
        peticion.timeoutInterval = 900

        URLSession.shared.dataTask(with: peticion) { [weak self] datos, _, _ in
            let json = datos.flatMap {
                try? JSONSerialization.jsonObject(with: $0) as? [String: Any]
            } ?? nil
            let mensaje = (json?["mensaje"] as? String)
                ?? "Konnte nicht abgeschlossen werden; das Programm bleibt unverändert."
            let ok = (json?["ok"] as? Bool ?? false) && !(json?["sin_cambios"] as? Bool ?? false)
            // La copia empaquetada no se sustituye a sí misma: lleva el programa dentro.
            // Python lo dice y da la URL; aquí solo se abre.
            let hayQueDescargar = json?["hay_que_descargar"] as? Bool ?? false
            let urlDescarga = (json?["url"] as? String).flatMap(URL.init(string:))

            DispatchQueue.main.async {
                guard let self else { return }
                self.aviso.isHidden = true

                if hayQueDescargar {
                    let a = NSAlert()
                    a.messageText = "Es gibt eine neue Version"
                    a.informativeText = mensaje
                    if urlDescarga != nil {
                        a.addButton(withTitle: "Descargar")
                        a.addButton(withTitle: "Später")
                    } else {
                        a.addButton(withTitle: "Schließen")
                    }
                    a.beginSheetModal(for: self.ventana) { r in
                        if r == .alertFirstButtonReturn, let u = urlDescarga {
                            NSWorkspace.shared.open(u)
                        }
                    }
                    return
                }

                let alerta = NSAlert()
                alerta.messageText = ok ? "Actualizado" : "Nicht aktualisiert"
                alerta.informativeText = mensaje
                if ok {
                    // Hay que reabrir: el proceso que está corriendo tiene en memoria
                    // la versión vieja. El .app se reconstruye solo en el arranque
                    // siguiente si su versión ya no cuadra con radar/__init__.py.
                    alerta.addButton(withTitle: "Jetzt beenden")
                    alerta.addButton(withTitle: "Später beenden")
                    alerta.beginSheetModal(for: self.ventana) { r in
                        if r == .alertFirstButtonReturn { NSApp.terminate(nil) }
                    }
                } else {
                    alerta.addButton(withTitle: "Schließen")
                    alerta.beginSheetModal(for: self.ventana, completionHandler: nil)
                }
            }
        }.resume()
    }

    private func simpleAlerta(_ titulo: String, _ texto: String) {
        let alerta = NSAlert()
        alerta.messageText = titulo
        alerta.informativeText = texto
        alerta.addButton(withTitle: "Schließen")
        alerta.beginSheetModal(for: ventana, completionHandler: nil)
    }

    // MARK: Navegación

    /// Todo lo que no sea el propio radar se abre en el navegador de verdad.
    ///
    /// Las fichas llevan enlaces a la plataforma oficial y a los pliegos. Sin esto se
    /// navegaría a contrataciondelestado.es DENTRO de la ventana, y como aquí no hay
    /// barra de direcciones ni botón de atrás, la única salida sería cerrar la app.
    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow); return
        }
        if url.host == Ajustes.host || url.scheme == "about" || url.scheme == "data" {
            decisionHandler(.allow)
        } else {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        traza("bandeja cargada (\(webView.url?.absoluteString ?? "?"))")
    }

    /// Si la página no carga hay que decirlo: un WKWebView que falla se queda en blanco
    /// y, sin barra de direcciones, no hay nada en pantalla que sugiera qué ha pasado.
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!,
                 withError error: Error) {
        traza("fallo al cargar: \(error.localizedDescription)")
        aviso.isHidden = false
        aviso.stringValue = "No se ha podido cargar la bandeja: \(error.localizedDescription)"
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        self.webView(webView, didFail: navigation, withError: error)
    }

    /// `target="_blank"`: los enlaces de la ficha lo llevan, y un WKWebView sin esto
    /// simplemente no hace nada al pulsarlos.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    /// El CSV. `/api/export.csv` se sirve con `Content-Disposition: attachment`, así que
    /// WebKit lo trata como descarga en lugar de como navegación; sin delegado, pulsar
    /// «Exportar CSV» no hace absolutamente nada y no hay error que lo explique.
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse,
                 didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        decisionHandler(navigationResponse.canShowMIMEType ? .allow : .download)
    }

    func download(_ download: WKDownload,
                  decideDestinationUsing response: URLResponse,
                  suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let descargas = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Downloads")
        var destino = descargas.appendingPathComponent(suggestedFilename)
        // Sin esto, exportar dos veces falla en silencio: WebKit cancela la descarga si
        // el fichero ya existe en lugar de sobrescribirlo.
        var n = 2
        let base = destino.deletingPathExtension().lastPathComponent
        let ext = destino.pathExtension
        while FileManager.default.fileExists(atPath: destino.path) {
            destino = descargas.appendingPathComponent(
                ext.isEmpty ? "\(base)-\(n)" : "\(base)-\(n).\(ext)")
            n += 1
        }
        completionHandler(destino)
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let url = download.progress.fileURL else { return }
        // Rebote en el Dock, que es lo que dice «ya está» en una app de Mac.
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    func download(_ download: WKDownload, didFailWithError error: Error,
                  resumeData: Data?) {
        simpleAlerta("Die CSV-Datei konnte nicht gespeichert werden", error.localizedDescription)
    }
}

// MARK: - Arranque

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegado = Delegado()
app.delegate = delegado
app.run()
