import Foundation

public struct OperatorHomeResolver {
    public var environment: [String: String]
    public var homeDirectory: URL
    public var fileManager: FileManager

    public init(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser,
        fileManager: FileManager = .default
    ) {
        self.environment = environment
        self.homeDirectory = homeDirectory
        self.fileManager = fileManager
    }

    public func resolve() -> URL {
        for candidate in candidates() {
            let standardized = candidate.standardizedFileURL
            if looksLikeRepoRoot(standardized) {
                return standardized
            }
        }
        return candidates().first?.standardizedFileURL ?? homeDirectory
    }

    public func looksLikeRepoRoot(_ url: URL) -> Bool {
        let install = url.appendingPathComponent("install.sh").path
        let canonicalCLI = url.appendingPathComponent("local-cortex/console/scripts/kaidera-os").path
        return fileManager.fileExists(atPath: install)
            && fileManager.fileExists(atPath: canonicalCLI)
    }

    private func candidates() -> [URL] {
        var out: [URL] = []
        if let raw = environment["KAIDERA_OS_HOME"]?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty {
            out.append(URL(fileURLWithPath: raw, isDirectory: true))
        }
        if let stored = readStoredOperatorHome() {
            out.append(stored)
        }
        out.append(
            homeDirectory
                .appendingPathComponent("Library/Application Support/Kaidera OS/kaidera-os", isDirectory: true)
        )
        return out
    }

    private func readStoredOperatorHome() -> URL? {
        for relativePath in [".kaidera-os/operator.json"] {
            let path = homeDirectory.appendingPathComponent(relativePath)
            guard let data = try? Data(contentsOf: path),
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let raw = json["repo_root"] as? String,
                  !raw.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            else {
                continue
            }
            return URL(fileURLWithPath: raw, isDirectory: true)
        }
        return nil
    }
}
