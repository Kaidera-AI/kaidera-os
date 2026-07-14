import Foundation
import Testing
@testable import KaideraOSOperatorCore

@Test func serviceSnapshotParsesStatusAndTitle() {
    let snapshot = ServiceSnapshot(json: [
        "status": "running",
        "version": "0.1.198",
        "console_url": "http://127.0.0.1:8765",
        "repo_root_found": true
    ])

    #expect(snapshot.status == .running)
    #expect(snapshot.menuTitle == "Kaidera OS OK v0.1.198")
    #expect(snapshot.consoleURL == "http://127.0.0.1:8765")
    #expect(snapshot.repoRootFound == true)
}

@Test func preflightDetailMatchesOperatorCliShape() {
    let result = OperatorResult(ok: false, json: [
        "repo_root": "/tmp/kaidera-os",
        "checks": [
            ["name": "repo_root", "ok": true, "required": true, "detail": "/tmp/kaidera-os"],
            ["name": "docker_daemon", "ok": false, "required": true, "detail": "not running"],
            ["name": "runner", "ok": false, "required": false, "detail": "missing"]
        ],
        "guidance": [
            "Start Docker Desktop or OrbStack and wait until `docker info` succeeds."
        ],
        "next": "run-installer"
    ])

    let detail = result.detail(for: .preflight)

    #expect(detail.contains("Install root: /tmp/kaidera-os"))
    #expect(detail.contains("OK repo_root (required): /tmp/kaidera-os"))
    #expect(detail.contains("FAIL docker_daemon (required): not running"))
    #expect(detail.contains("FAIL runner (optional): missing"))
    #expect(detail.contains("Guidance:"))
    #expect(detail.contains("Next: run-installer"))
}

@Test func idleCheckDetailListsActiveRuns() {
    let result = OperatorResult(ok: true, json: [
        "idle": false,
        "checked": true,
        "reason": "active_workers",
        "projects_checked": 1,
        "active_count": 1,
        "active_runs": [
            [
                "project": "marketing",
                "agent": "marlow",
                "status": "running",
                "run_id": "run-1"
            ]
        ]
    ])

    let detail = result.detail(for: .idleCheck)

    #expect(detail.contains("Idle: false"))
    #expect(detail.contains("Reason: active_workers"))
    #expect(detail.contains("Active runs: 1"))
    #expect(detail.contains("marketing / marlow / running / run-1"))
}

@Test func operatorHomeResolverPrefersConfiguredRoot() throws {
    let temp = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("kaidera-os-swift-test-\(UUID().uuidString)", isDirectory: true)
    let root = temp.appendingPathComponent("repo", isDirectory: true)
    try FileManager.default.createDirectory(
        at: root.appendingPathComponent("local-cortex/console/scripts", isDirectory: true),
        withIntermediateDirectories: true
    )
    FileManager.default.createFile(atPath: root.appendingPathComponent("install.sh").path, contents: Data())
    FileManager.default.createFile(
        atPath: root.appendingPathComponent("local-cortex/console/scripts/kaidera-os").path,
        contents: Data()
    )
    defer { try? FileManager.default.removeItem(at: temp) }

    let resolver = OperatorHomeResolver(
        environment: ["KAIDERA_OS_HOME": root.path],
        homeDirectory: temp
    )

    #expect(resolver.resolve().path == root.standardizedFileURL.path)
}

@Test func operatorClientBuildsCliCommand() {
    final class FakeRunner: CommandRunning {
        var args: [String] = []
        func run(_ args: [String], cwd: URL?, environment: [String: String]) throws -> CommandResult {
            self.args = args
            return CommandResult(
                exitCode: 0,
                stdout: #"{"status":"running","version":"0.1.198","console_url":"http://127.0.0.1:8765","repo_root_found":true}"#
            )
        }
    }

    let runner = FakeRunner()
    let root = URL(fileURLWithPath: "/tmp/kaidera-os", isDirectory: true)
    let client = OperatorClient(repoRoot: root, runner: runner, environment: [:])

    let snapshot = client.status()

    #expect(snapshot.status == .running)
    #expect(runner.args == [
        "/bin/bash",
        "/tmp/kaidera-os/local-cortex/console/scripts/kaidera-os",
        "operator",
        "status"
    ])
}
