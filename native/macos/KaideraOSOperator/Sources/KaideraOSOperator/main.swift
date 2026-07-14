import AppKit
import KaideraOSOperatorCore
import Foundation

private let refreshInterval: TimeInterval = 10

@MainActor
final class OperatorAppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem?
    private var timer: Timer?
    private let queue = DispatchQueue(label: "ai.kaidera.kaidera-os", qos: .utility)
    private var snapshot = ServiceSnapshot()
    private var client: OperatorClient

    override init() {
        let root = OperatorHomeResolver().resolve()
        self.client = OperatorClient(repoRoot: root)
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        rebuildMenu()
        refreshStatus()
        timer = Timer.scheduledTimer(
            timeInterval: refreshInterval,
            target: self,
            selector: #selector(refreshStatusFromTimer),
            userInfo: nil,
            repeats: true
        )
    }

    private func refreshStatus() {
        let client = self.client
        queue.async {
            let next = client.status()
            DispatchQueue.main.async {
                self.snapshot = next
                self.rebuildMenu()
            }
        }
    }

    private func rebuildMenu() {
        guard let statusItem else { return }
        let button = statusItem.button
        button?.image = StatusIcon.image()
        button?.imagePosition = .imageOnly
        button?.toolTip = snapshot.menuTitle

        let menu = NSMenu()
        let title = NSMenuItem(title: snapshot.menuTitle, action: nil, keyEquivalent: "")
        title.isEnabled = false
        menu.addItem(title)
        menu.addItem(makeMenuItem("Open Console (\(snapshot.consoleURL))", action: #selector(openConsole), key: "o"))
        menu.addItem(.separator())
        menu.addItem(makeMenuItem("Start", action: #selector(startService), key: "s"))
        menu.addItem(makeMenuItem("Stop", action: #selector(stopService)))
        menu.addItem(makeMenuItem("Restart", action: #selector(restartService), key: "r"))
        menu.addItem(makeMenuItem("Run Install / Repair", action: #selector(runInstaller)))
        menu.addItem(.separator())
        menu.addItem(makeMenuItem("Preflight", action: #selector(preflight)))
        menu.addItem(makeMenuItem("Idle Check", action: #selector(idleCheck)))
        menu.addItem(makeMenuItem("Check for Updates", action: #selector(checkUpdates)))
        menu.addItem(makeMenuItem("Apply Update", action: #selector(applyUpdate)))
        menu.addItem(.separator())
        menu.addItem(makeMenuItem("Install Login Item", action: #selector(installLoginItem)))
        menu.addItem(makeMenuItem("Uninstall Login Item", action: #selector(uninstallLoginItem)))
        menu.addItem(.separator())
        menu.addItem(makeMenuItem("Quit Kaidera OS Operator", action: #selector(quit), key: "q"))
        statusItem.menu = menu
    }

    private func runAction(_ action: OperatorAction) {
        let client = self.client
        queue.async {
            let result = client.run(action)
            let updated = client.status()
            DispatchQueue.main.async {
                self.snapshot = updated
                self.rebuildMenu()
                if result.shouldShowResult(for: action) {
                    self.showResult(action: action, result: result)
                }
            }
        }
    }

    private func showResult(action: OperatorAction, result: OperatorResult) {
        let alert = NSAlert()
        alert.messageText = result.title(for: action)
        alert.informativeText = result.detail(for: action)
        alert.alertStyle = result.ok ? .informational : .warning
        alert.runModal()
    }

    private func makeMenuItem(_ title: String, action: Selector, key: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        return item
    }

    @objc private func refreshStatusFromTimer() { refreshStatus() }
    @objc private func openConsole() { runAction(.open) }
    @objc private func startService() { runAction(.start) }
    @objc private func stopService() { runAction(.stop) }
    @objc private func restartService() { runAction(.restart) }
    @objc private func runInstaller() { runAction(.runInstaller) }
    @objc private func preflight() { runAction(.preflight) }
    @objc private func idleCheck() { runAction(.idleCheck) }
    @objc private func checkUpdates() { runAction(.checkUpdate) }
    @objc private func applyUpdate() { runAction(.applyUpdate) }
    @objc private func installLoginItem() { runAction(.installLoginItem) }
    @objc private func uninstallLoginItem() { runAction(.uninstallLoginItem) }
    @objc private func quit() { NSApp.terminate(nil) }
}

private enum StatusIcon {
    static func image() -> NSImage {
        for bundle in resourceBundles() {
            if let url = bundle.url(forResource: "kaidera-icon-template", withExtension: "png"),
               let image = NSImage(contentsOf: url) {
                image.size = NSSize(width: 18, height: 18)
                image.isTemplate = true
                return image
            }
        }
        return fallbackImage()
    }

    private static func resourceBundles() -> [Bundle] {
        var bundles = [Bundle.module, Bundle.main]
        if let resourceURL = Bundle.main.resourceURL {
            let swiftPMBundleURL = resourceURL.appendingPathComponent("KaideraOSOperator_KaideraOSOperator.bundle")
            if let bundle = Bundle(url: swiftPMBundleURL) {
                bundles.append(bundle)
            }
        }
        return bundles
    }

    private static func fallbackImage() -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18))
        image.lockFocus()
        defer { image.unlockFocus() }
        NSColor.labelColor.setFill()
        NSBezierPath(ovalIn: NSRect(x: 3, y: 3, width: 12, height: 12)).fill()
        image.isTemplate = true
        return image
    }
}

let app = NSApplication.shared
let delegate = OperatorAppDelegate()
app.delegate = delegate
app.run()
