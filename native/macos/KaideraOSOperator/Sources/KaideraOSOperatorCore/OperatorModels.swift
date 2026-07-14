import Foundation

public enum OperatorStatus: String, Equatable, Sendable {
    case running
    case degraded
    case stopped
    case unknown

    public var menuLabel: String {
        switch self {
        case .running:
            return "OK"
        case .degraded:
            return "DEGRADED"
        case .stopped:
            return "STOPPED"
        case .unknown:
            return "UNKNOWN"
        }
    }
}

public struct ServiceSnapshot: Equatable, Sendable {
    public var status: OperatorStatus
    public var version: String?
    public var consoleURL: String
    public var repoRoot: String?
    public var repoRootFound: Bool

    public init(
        status: OperatorStatus = .stopped,
        version: String? = nil,
        consoleURL: String = "http://127.0.0.1:8765",
        repoRoot: String? = nil,
        repoRootFound: Bool = false
    ) {
        self.status = status
        self.version = version
        self.consoleURL = consoleURL
        self.repoRoot = repoRoot
        self.repoRootFound = repoRootFound
    }

    public init(json: [String: Any]) {
        let statusValue = json["status"] as? String ?? "stopped"
        self.status = OperatorStatus(rawValue: statusValue) ?? .unknown
        self.version = json["version"] as? String
        self.consoleURL = json["console_url"] as? String ?? "http://127.0.0.1:8765"
        self.repoRoot = json["repo_root"] as? String
        self.repoRootFound = json["repo_root_found"] as? Bool ?? false
    }

    public var menuTitle: String {
        let suffix = version.map { " v\($0)" } ?? ""
        return "Kaidera OS \(status.menuLabel)\(suffix)"
    }
}

public enum OperatorAction: String, CaseIterable, Sendable {
    case open
    case start
    case stop
    case restart
    case runInstaller = "run-installer"
    case preflight
    case idleCheck = "idle-check"
    case checkUpdate = "check-update"
    case applyUpdate = "apply-update"
    case installLoginItem = "install-login-item"
    case uninstallLoginItem = "uninstall-login-item"

    public var title: String {
        switch self {
        case .open:
            return "Open Console"
        case .start:
            return "Start"
        case .stop:
            return "Stop"
        case .restart:
            return "Restart"
        case .runInstaller:
            return "Run Install / Repair"
        case .preflight:
            return "Preflight"
        case .idleCheck:
            return "Idle Check"
        case .checkUpdate:
            return "Check for Updates"
        case .applyUpdate:
            return "Apply Update"
        case .installLoginItem:
            return "Install Login Item"
        case .uninstallLoginItem:
            return "Uninstall Login Item"
        }
    }

    public var alwaysShowsResult: Bool {
        switch self {
        case .runInstaller, .preflight, .idleCheck, .checkUpdate, .applyUpdate, .installLoginItem, .uninstallLoginItem:
            return true
        case .open, .start, .stop, .restart:
            return false
        }
    }
}

public struct OperatorResult: Equatable, @unchecked Sendable {
    public var ok: Bool
    public var json: [String: Any]
    public var stdout: String
    public var stderr: String

    public init(ok: Bool, json: [String: Any] = [:], stdout: String = "", stderr: String = "") {
        self.ok = ok
        self.json = json
        self.stdout = stdout
        self.stderr = stderr
    }

    public static func == (lhs: OperatorResult, rhs: OperatorResult) -> Bool {
        lhs.ok == rhs.ok && lhs.stdout == rhs.stdout && lhs.stderr == rhs.stderr
    }

    public func shouldShowResult(for action: OperatorAction) -> Bool {
        action.alwaysShowsResult || !ok
    }

    public func title(for action: OperatorAction) -> String {
        "\(action.title): \(ok ? "OK" : "FAIL")"
    }

    public func detail(for action: OperatorAction) -> String {
        switch action {
        case .preflight:
            return preflightDetail()
        case .idleCheck:
            return idleCheckDetail()
        case .runInstaller:
            if ok {
                return """
                Canonical install.sh started in the background.
                PID: \(json.stringValue("pid", fallback: "unknown"))
                Log: \(json.stringValue("log_path", fallback: "unknown"))
                """
            }
            return """
            Canonical install.sh did not start.
            Error: \(json.stringValue("error", fallback: "unknown"))
            Log: \(json.stringValue("log_path", fallback: "not created"))
            """
        case .checkUpdate:
            let payload = json.dictionaryValue("payload")
            return """
            Current: \(payload.stringValue("current_version", fallback: "unknown"))
            Latest: \(payload.stringValue("latest_version", fallback: payload.stringValue("latest_tag", fallback: "unknown")))
            Update available: \(payload.stringValue("update_available", fallback: "unknown"))
            Source: \(payload.stringValue("source", fallback: "unknown"))
            Error: \(json.stringValue("error", fallback: payload.stringValue("error", fallback: "none")))
            """
        case .applyUpdate:
            let payload = json.dictionaryValue("payload")
            let job = payload.dictionaryValue("job")
            return """
            Accepted: \(payload.stringValue("accepted", fallback: "unknown"))
            Already running: \(payload.stringValue("already_running", fallback: "unknown"))
            Job: \(job.stringValue("job_id", fallback: "unknown"))
            Status: \(job.stringValue("status", fallback: "unknown"))
            Log: \(job.stringValue("log_path", fallback: "unknown"))
            Error: \(json.stringValue("error", fallback: job.stringValue("error", fallback: "none")))
            """
        case .start, .stop, .restart:
            let commandResult = json.dictionaryValue("result")
            return """
            System: \(json.stringValue("system", fallback: "unknown"))
            Action: \(json.stringValue("action", fallback: action.rawValue))
            Return code: \(commandResult.stringValue("return_code", fallback: "unknown"))
            Error: \(json.stringValue("error", fallback: commandResult.stringValue("stderr", fallback: "none")))
            """
        case .open:
            return "URL: \(json.stringValue("url", fallback: "unknown"))"
        case .installLoginItem, .uninstallLoginItem:
            return json.prettyPrinted()
        }
    }

    private func preflightDetail() -> String {
        var lines = ["Install root: \(json.stringValue("repo_root", fallback: "unknown"))", ""]
        let checks = json["checks"] as? [[String: Any]] ?? []
        for item in checks {
            let required = item.boolValue("required", fallback: true) ? "required" : "optional"
            let status = item.boolValue("ok", fallback: false) ? "OK" : "FAIL"
            lines.append("\(status) \(item.stringValue("name", fallback: "check")) (\(required)): \(item.stringValue("detail", fallback: ""))")
        }
        let guidance = json["guidance"] as? [String] ?? []
        if !guidance.isEmpty {
            lines.append("")
            lines.append("Guidance:")
            lines.append(contentsOf: guidance.map { "- \($0)" })
        }
        lines.append("")
        lines.append("Next: \(json.stringValue("next", fallback: "none"))")
        return lines.joined(separator: "\n")
    }

    private func idleCheckDetail() -> String {
        var lines = [
            "Idle: \(json.stringValue("idle", fallback: "unknown"))",
            "Checked: \(json.stringValue("checked", fallback: "unknown"))",
            "Reason: \(json.stringValue("reason", fallback: "unknown"))",
            "Projects checked: \(json.stringValue("projects_checked", fallback: "unknown"))",
            "Active runs: \(json.stringValue("active_count", fallback: "unknown"))",
        ]
        let activeRuns = json["active_runs"] as? [[String: Any]] ?? []
        for run in activeRuns.prefix(8) {
            lines.append(
                "- \(run.stringValue("project", fallback: "?")) / " +
                "\(run.stringValue("agent", fallback: "?")) / " +
                "\(run.stringValue("status", fallback: "?")) / " +
                "\(run.stringValue("run_id", fallback: "unknown run"))"
            )
        }
        if activeRuns.count > 8 {
            lines.append("- ... \(activeRuns.count - 8) more")
        }
        if let error = json["error"] as? String, !error.isEmpty {
            lines.append("Error: \(error)")
        }
        return lines.joined(separator: "\n")
    }
}

extension Dictionary where Key == String, Value == Any {
    func stringValue(_ key: String, fallback: String) -> String {
        guard let value = self[key] else {
            return fallback
        }
        if let string = value as? String {
            return string
        }
        if let bool = value as? Bool {
            return bool ? "true" : "false"
        }
        if let number = value as? NSNumber {
            return number.stringValue
        }
        return fallback
    }

    func boolValue(_ key: String, fallback: Bool) -> Bool {
        guard let value = self[key] else {
            return fallback
        }
        if let bool = value as? Bool {
            return bool
        }
        if let number = value as? NSNumber {
            return number.boolValue
        }
        return fallback
    }

    func dictionaryValue(_ key: String) -> [String: Any] {
        self[key] as? [String: Any] ?? [:]
    }

    func prettyPrinted() -> String {
        guard JSONSerialization.isValidJSONObject(self),
              let data = try? JSONSerialization.data(withJSONObject: self, options: [.prettyPrinted, .sortedKeys]),
              let text = String(data: data, encoding: .utf8)
        else {
            return "\(self)"
        }
        return text
    }
}
