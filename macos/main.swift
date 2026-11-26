// Deployer: native macOS shell.
// Starts the local server (app.py) and shows the UI in a WebKit window.
// Has a Dock / Cmd+Tab icon and a menu; closing the window stops the server.
import Cocoa
import WebKit

// ---------------- localization (menu and startup messages) ----------------
// The UI language is stored in ~/Library/Application Support/Deployer/projects.json ("lang").
func currentLang() -> String {
    let path = NSHomeDirectory() + "/Library/Application Support/Deployer/projects.json"
    guard let d = FileManager.default.contents(atPath: path),
          let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
          let l = j["lang"] as? String, ["uk", "ru"].contains(l) else { return "en" }
    return l
}
var LANG = currentLang()
let STRINGS: [String: [String: String]] = [
    "About Deployer": ["uk": "Про Deployer", "ru": "О Deployer"],
    "Hide Deployer": ["uk": "Сховати Deployer", "ru": "Скрыть Deployer"],
    "Hide Others": ["uk": "Сховати інші", "ru": "Скрыть остальные"],
    "Quit Deployer": ["uk": "Вийти з Deployer", "ru": "Выйти из Deployer"],
    "Edit": ["uk": "Редагування", "ru": "Правка"],
    "Undo": ["uk": "Скасувати дію", "ru": "Отменить"],
    "Redo": ["uk": "Повторити", "ru": "Повторить"],
    "Cut": ["uk": "Вирізати", "ru": "Вырезать"],
    "Copy": ["uk": "Копіювати", "ru": "Копировать"],
    "Paste": ["uk": "Вставити", "ru": "Вставить"],
    "Select All": ["uk": "Виділити все", "ru": "Выделить всё"],
    "View": ["uk": "Вигляд", "ru": "Вид"],
    "Reload": ["uk": "Оновити", "ru": "Обновить"],
    "Window": ["uk": "Вікно", "ru": "Окно"],
    "Minimize": ["uk": "Згорнути", "ru": "Свернуть"],
    "Close": ["uk": "Закрити", "ru": "Закрыть"],
    "Cancel": ["uk": "Скасувати", "ru": "Отмена"],
    "Starting…": ["uk": "Запуск…", "ru": "Запуск…"],
    "Could not start Python 3.\nInstall the Command Line Tools: xcode-select --install":
        ["uk": "Не вдалося запустити Python 3.\nВстановіть Command Line Tools: xcode-select --install",
         "ru": "Не удалось запустить Python 3.\nУстановите Command Line Tools: xcode-select --install"],
    "The Deployer server did not start.\nDetails: ~/Library/Logs/Deployer.log":
        ["uk": "Сервер Deployer не запустився.\nПодробиці: ~/Library/Logs/Deployer.log",
         "ru": "Сервер Deployer не запустился.\nПодробности: ~/Library/Logs/Deployer.log"],
]
func L(_ s: String) -> String { STRINGS[s]?[LANG] ?? s }

// ---------------- app.py server management ----------------
final class ServerManager: @unchecked Sendable {
    let res = Bundle.main.resourcePath!
    let dataDir = NSHomeDirectory() + "/Library/Application Support/Deployer"
    let logPath = NSHomeDirectory() + "/Library/Logs/Deployer.log"
    var portFile: String { dataDir + "/port" }
    private(set) var process: Process?
    private(set) var serverPid: Int32 = 0

    func readPort() -> String? {
        (try? String(contentsOfFile: portFile, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func ping(_ port: String) -> [String: Any]? {
        guard let url = URL(string: "http://127.0.0.1:\(port)/ping") else { return nil }
        var req = URLRequest(url: url)
        req.timeoutInterval = 1.5
        let sem = DispatchSemaphore(value: 0)
        var out: [String: Any]?
        URLSession.shared.dataTask(with: req) { d, _, _ in
            if let d = d, let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
               j["app"] as? String == "deployer" { out = j }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 2)
        return out
    }

    /// Returns the UI URL or an error.
    func start(onExit: @escaping @Sendable () -> Void) -> Result<URL, NSError> {
        // Stop a Deployer server left over from a previous launch or an older version
        if let p = readPort(), let j = ping(p), let pid = j["pid"] as? Int {
            kill(pid_t(pid), SIGTERM)
            usleep(600_000)
        }
        try? FileManager.default.removeItem(atPath: portFile)

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        proc.arguments = ["python3", res + "/app.py", "--no-browser"]
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        proc.environment = env
        if !FileManager.default.fileExists(atPath: logPath) {
            FileManager.default.createFile(atPath: logPath, contents: nil)
        }
        if let fh = FileHandle(forWritingAtPath: logPath) {
            fh.seekToEndOfFile()
            fh.write("--- \(Date()) start\n".data(using: .utf8)!)
            proc.standardOutput = fh
            proc.standardError = fh
        }
        proc.terminationHandler = { _ in onExit() }
        do { try proc.run() } catch {
            return .failure(err(L("Could not start Python 3.\nInstall the Command Line Tools: xcode-select --install")))
        }
        process = proc

        for _ in 0..<80 {
            if !proc.isRunning { break }
            if let p = readPort(), let j = ping(p), j["base"] as? String == res {
                serverPid = Int32((j["pid"] as? Int) ?? 0)
                return .success(URL(string: "http://127.0.0.1:\(p)/")!)
            }
            usleep(250_000)
        }
        return .failure(err(L("The Deployer server did not start.\nDetails: ~/Library/Logs/Deployer.log")))
    }

    func stop() {
        if serverPid > 0 { kill(serverPid, SIGTERM) }
        if let p = process, p.isRunning { p.terminate() }
    }

    private func err(_ m: String) -> NSError {
        NSError(domain: "Deployer", code: 1, userInfo: [NSLocalizedDescriptionKey: m])
    }
}

// ---------------- application ----------------
final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var web: WKWebView!
    let server = ServerManager()
    var quitting = false

    func applicationDidFinishLaunching(_ n: Notification) {
        buildMenu()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 860),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Deployer"
        window.minSize = NSSize(width: 760, height: 520)
        window.center()
        window.setFrameAutosaveName("DeployerMainWindow")
        web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        web.uiDelegate = self
        web.navigationDelegate = self
        window.contentView = web
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        showMessage(L("Starting…"), error: false)

        let srv = server
        DispatchQueue.global(qos: .userInitiated).async {
            let r = srv.start(onExit: {
                DispatchQueue.main.async {
                    // the server was stopped with ⏻ or crashed: quit the app
                    let d = NSApp.delegate as? AppDelegate
                    if d?.quitting == false { NSApp.terminate(nil) }
                }
            })
            DispatchQueue.main.async {
                switch r {
                case .success(let url): self.web.load(URLRequest(url: url))
                case .failure(let e): self.showMessage(e.localizedDescription, error: true)
                }
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    // The language may have been switched in the UI: rebuild the menu when the app becomes active
    func applicationDidBecomeActive(_ n: Notification) {
        let l = currentLang()
        if l != LANG { LANG = l; buildMenu() }
    }

    // Reopen (Dock, Finder, Spotlight): bring the existing window back
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        return true
    }

    func applicationWillTerminate(_ n: Notification) {
        quitting = true
        server.stop()
    }

    func showMessage(_ text: String, error: Bool) {
        let esc = text.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: "\n", with: "<br>")
        let html = """
        <html><head><meta charset="utf-8"><style>
        :root{color-scheme:light dark}
        body{font:15px -apple-system;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;
             background:Canvas;color:\(error ? "#dc2626" : "GrayText");text-align:center;line-height:1.5}
        </style></head><body><div>\(error ? "⚠️<br>" : "⚡<br>")\(esc)</div></body></html>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    // --- menu ---
    func buildMenu() {
        let main = NSMenu()
        func sub(_ title: String, _ items: [NSMenuItem]) {
            let it = NSMenuItem(); let m = NSMenu(title: title)
            items.forEach { m.addItem($0) }
            it.submenu = m; main.addItem(it)
        }
        func item(_ t: String, _ a: Selector?, _ k: String, _ target: AnyObject? = nil,
                  _ mods: NSEvent.ModifierFlags = [.command]) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: a, keyEquivalent: k)
            i.keyEquivalentModifierMask = mods
            i.target = target
            return i
        }
        sub("Deployer", [
            item(L("About Deployer"), #selector(NSApplication.orderFrontStandardAboutPanel(_:)), ""),
            .separator(),
            item(L("Hide Deployer"), #selector(NSApplication.hide(_:)), "h"),
            item(L("Hide Others"), #selector(NSApplication.hideOtherApplications(_:)), "h", nil, [.command, .option]),
            .separator(),
            item(L("Quit Deployer"), #selector(NSApplication.terminate(_:)), "q"),
        ])
        sub(L("Edit"), [
            item(L("Undo"), Selector(("undo:")), "z"),
            item(L("Redo"), Selector(("redo:")), "z", nil, [.command, .shift]),
            .separator(),
            item(L("Cut"), #selector(NSText.cut(_:)), "x"),
            item(L("Copy"), #selector(NSText.copy(_:)), "c"),
            item(L("Paste"), #selector(NSText.paste(_:)), "v"),
            item(L("Select All"), #selector(NSText.selectAll(_:)), "a"),
        ])
        sub(L("View"), [
            item(L("Reload"), #selector(reloadPage), "r", self),
        ])
        sub(L("Window"), [
            item(L("Minimize"), #selector(NSWindow.performMiniaturize(_:)), "m"),
            item(L("Close"), #selector(NSWindow.performClose(_:)), "w"),
        ])
        NSApp.mainMenu = main
    }

    @objc func reloadPage() { web.reload() }

    // --- alert / confirm / prompt from the web page → native dialogs ---
    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage m: String,
                 initiatedByFrame f: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = m; a.addButton(withTitle: "OK")
        a.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage m: String,
                 initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = m
        a.addButton(withTitle: "OK"); a.addButton(withTitle: L("Cancel"))
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn) }
    }

    func webView(_ w: WKWebView, runJavaScriptTextInputPanelWithPrompt p: String, defaultText d: String?,
                 initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert(); a.messageText = p
        let tf = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        tf.stringValue = d ?? ""
        a.accessoryView = tf
        a.addButton(withTitle: "OK"); a.addButton(withTitle: L("Cancel"))
        a.window.initialFirstResponder = tf
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn ? tf.stringValue : nil) }
    }

    // Open external links in the default browser
    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let u = a.request.url, a.navigationType == .linkActivated, u.host != "127.0.0.1" {
            NSWorkspace.shared.open(u); decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
