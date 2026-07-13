import Foundation

struct GrimodexProductPaths {
    static let packageName = "fcitx5-grimodex"
    static let serverExecutableName = "fcitx5-grimodex-server"
    static let settingsExecutableName = "fcitx5-grimodex-settings"
    private static let maximumUnixSocketPathBytes = 108

    let runtimeDirectory: URL
    let socketURL: URL
    let lockURL: URL
    let configDirectory: URL
    let dataDirectory: URL
    let stateDirectory: URL
    let cacheDirectory: URL

    init(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser,
        uid: uid_t = getuid()
    ) {
        let fallbackRuntimeDirectory = URL(
            fileURLWithPath: "/tmp/\(Self.packageName)-\(uid)",
            isDirectory: true
        )
        var runtimeDirectory: URL
        if let base = Self.nonEmpty(environment["XDG_RUNTIME_DIR"]) {
            runtimeDirectory = URL(fileURLWithPath: base, isDirectory: true)
                .appendingPathComponent(Self.packageName, isDirectory: true)
        } else {
            runtimeDirectory = fallbackRuntimeDirectory
        }
        let candidateSocketURL = runtimeDirectory.appendingPathComponent(
            "server.sock",
            isDirectory: false
        )
        if candidateSocketURL.path.utf8.count >= Self.maximumUnixSocketPathBytes {
            runtimeDirectory = fallbackRuntimeDirectory
        }
        self.runtimeDirectory = runtimeDirectory
        socketURL = runtimeDirectory.appendingPathComponent("server.sock", isDirectory: false)
        lockURL = runtimeDirectory.appendingPathComponent("server.lock", isDirectory: false)

        configDirectory = Self.xdgDirectory(
            environment: environment,
            variable: "XDG_CONFIG_HOME",
            fallback: homeDirectory.appendingPathComponent(".config", isDirectory: true)
        )
        dataDirectory = Self.xdgDirectory(
            environment: environment,
            variable: "XDG_DATA_HOME",
            fallback: homeDirectory
                .appendingPathComponent(".local", isDirectory: true)
                .appendingPathComponent("share", isDirectory: true)
        )
        stateDirectory = Self.xdgDirectory(
            environment: environment,
            variable: "XDG_STATE_HOME",
            fallback: homeDirectory
                .appendingPathComponent(".local", isDirectory: true)
                .appendingPathComponent("state", isDirectory: true)
        )
        cacheDirectory = Self.xdgDirectory(
            environment: environment,
            variable: "XDG_CACHE_HOME",
            fallback: homeDirectory.appendingPathComponent(".cache", isDirectory: true)
        )
    }

    private static func xdgDirectory(
        environment: [String: String],
        variable: String,
        fallback: URL
    ) -> URL {
        let base = nonEmpty(environment[variable]).map {
            URL(fileURLWithPath: $0, isDirectory: true)
        } ?? fallback
        return base.appendingPathComponent(packageName, isDirectory: true)
    }

    private static func nonEmpty(_ value: String?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        return value
    }
}
