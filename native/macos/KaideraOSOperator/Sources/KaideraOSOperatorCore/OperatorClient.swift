import Foundation

public struct CommandResult: Equatable, Sendable {
    public var exitCode: Int32
    public var stdout: String
    public var stderr: String

    public init(exitCode: Int32, stdout: String = "", stderr: String = "") {
        self.exitCode = exitCode
        self.stdout = stdout
        self.stderr = stderr
    }
}

public protocol CommandRunning {
    func run(_ args: [String], cwd: URL?, environment: [String: String]) throws -> CommandResult
}

public struct ProcessRunner: CommandRunning {
    public init() {}

    public func run(_ args: [String], cwd: URL?, environment: [String: String]) throws -> CommandResult {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: args[0])
        process.arguments = Array(args.dropFirst())
        process.currentDirectoryURL = cwd
        process.environment = environment

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        try process.run()
        process.waitUntilExit()

        let stdout = String(data: stdoutPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        let stderr = String(data: stderrPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return CommandResult(exitCode: process.terminationStatus, stdout: stdout, stderr: stderr)
    }
}

public final class OperatorClient: @unchecked Sendable {
    public let repoRoot: URL
    private let runner: CommandRunning
    private let environment: [String: String]

    public init(
        repoRoot: URL,
        runner: CommandRunning = ProcessRunner(),
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.repoRoot = repoRoot
        self.runner = runner
        var env = environment
        env["KAIDERA_OS_HOME"] = repoRoot.path
        self.environment = env
    }

    public var cliURL: URL {
        repoRoot.appendingPathComponent("local-cortex/console/scripts/kaidera-os")
    }

    public func status() -> ServiceSnapshot {
        let result = runOperator(["status"])
        let json = Self.parseJSON(result.stdout)
        return ServiceSnapshot(json: json)
    }

    public func run(_ action: OperatorAction) -> OperatorResult {
        let result = runOperator([action.rawValue])
        let json = Self.parseJSON(result.stdout)
        let ok = result.exitCode == 0 && (json["ok"] as? Bool ?? true)
        return OperatorResult(ok: ok, json: json, stdout: result.stdout, stderr: result.stderr)
    }

    private func runOperator(_ args: [String]) -> CommandResult {
        do {
            return try runner.run(
                ["/bin/bash", cliURL.path, "operator"] + args,
                cwd: repoRoot,
                environment: environment
            )
        } catch {
            return CommandResult(exitCode: 1, stderr: error.localizedDescription)
        }
    }

    public static func parseJSON(_ text: String) -> [String: Any] {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data),
              let json = object as? [String: Any]
        else {
            return [:]
        }
        return json
    }
}
