import Foundation

class HazkeyServer: SocketManagerDelegate {
    private let processManager: ProcessManager
    private var socketManager: SocketManager
    private var protocolHandler: ProtocolHandler?
    private var sessionRegistry: HazkeySessionRegistry?
    private var grimodexRuntime: GrimodexLinuxRuntime?

    private let runtimeDir: URL
    private let socketPath: String
    private let lockFilePath: String

    init() {
        let paths = GrimodexProductPaths()
        self.runtimeDir = paths.runtimeDirectory
        self.socketPath = paths.socketURL.path
        self.lockFilePath = paths.lockURL.path

        self.processManager = ProcessManager(lockFilePath: lockFilePath)
        self.socketManager = SocketManager(socketPath: socketPath)
        socketManager.delegate = self
    }

    func parseCommandLineArguments() -> Bool {
        let arguments = CommandLine.arguments
        for arg in arguments {
            if arg == "-r" || arg == "--replace" {
                return true
            }
        }
        return false
    }

    func start() throws {
        let forceRestart = parseCommandLineArguments()
        try GrimodexRuntimeDirectory.prepare(at: runtimeDir)
        do {
            try processManager.tryLock(force: forceRestart)
        } catch ProcessManagerError.anotherInstanceRunning {
            // NSLogged by tryLock()
            // expected exit
            return
        } catch {
            NSLog("Failed to start \(GrimodexProductPaths.serverExecutableName): \(error)")
            exit(1)
        }
        let serverConfig = HazkeyServerConfig()
        let grimodexRuntime = GrimodexLinuxRuntime(
            version: hazkeyVersion,
            initialScopeMode: serverConfig.grimodexScopeMode
        )
        grimodexRuntime.start()
        self.grimodexRuntime = grimodexRuntime
        let sessionRegistry = HazkeySessionRegistry(
            serverConfig: serverConfig,
            revisionProviderFactory: { clientContext in
                grimodexRuntime.revisionProvider(clientContext: clientContext)
            }
        )
        self.sessionRegistry = sessionRegistry
        self.protocolHandler = ProtocolHandler(
            sessionRegistry: sessionRegistry,
            onConfigurationChanged: { config in
                grimodexRuntime.updateScopeMode(config.grimodexScopeMode)
            },
            diagnosticsProvider: {
                GrimodexDiagnosticsSnapshot(
                    runtime: grimodexRuntime.diagnostics(),
                    sessions: sessionRegistry.diagnostics(
                        scopeMode: serverConfig.grimodexScopeMode
                    )
                )
            }
        )
        do {
            try socketManager.setupSocket()
        } catch {
            grimodexRuntime.stop()
            throw error
        }
        // start main loop
        NSLog("start listening...")
        socketManager.startListening()
        // finish process
        sessionRegistry.saveAll()
        grimodexRuntime.stop()
    }

    func socketManager(_ manager: SocketManager, didReceiveData data: Data, from clientFd: Int32)
        -> Data
    {
        guard let handler = protocolHandler else {
            NSLog("protocolHandler is nil! exiting...")
            exit(1)
        }
        return handler.processProto(data: data, clientFd: clientFd)
    }

    func socketManager(_ manager: SocketManager, clientDidConnect clientFd: Int32) {}

    func socketManager(_ manager: SocketManager, clientDidDisconnect clientFd: Int32) {
        sessionRegistry?.closeAll(ownerFd: clientFd)
    }
}
