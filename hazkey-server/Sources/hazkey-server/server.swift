import Foundation

class HazkeyServer: SocketManagerDelegate {
    private let processManager: ProcessManager
    private var socketManager: SocketManager
    private var protocolHandler: ProtocolHandler?
    private var sessionRegistry: HazkeySessionRegistry?

    private let runtimeDir: URL
    private let socketPath: String
    private let lockFilePath: String

    init() {
        let uid = getuid()
        self.runtimeDir = URL(
            fileURLWithPath:
                ProcessInfo.processInfo.environment["XDG_RUNTIME_DIR"]
                ?? "/tmp/hazkey-runtime-\(uid)", isDirectory: true)

        self.socketPath = "\(runtimeDir.path)/hazkey-server.\(uid).sock"
        self.lockFilePath = "\(runtimeDir.path)/hazkey-server.\(uid).lock"

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
        if !FileManager.default.fileExists(atPath: runtimeDir.path) {
            try FileManager.default.createDirectory(
                at: runtimeDir, withIntermediateDirectories: true,
                attributes: [FileAttributeKey.posixPermissions: 0o700])
        }
        do {
            try processManager.tryLock(force: forceRestart)
        } catch ProcessManagerError.anotherInstanceRunning {
            // NSLogged by tryLock()
            // expected exit
            return
        } catch {
            NSLog("Failed to start hazkey-server: \(error)")
            exit(1)
        }
        let sessionRegistry = HazkeySessionRegistry()
        self.sessionRegistry = sessionRegistry
        self.protocolHandler = ProtocolHandler(sessionRegistry: sessionRegistry)
        try socketManager.setupSocket()
        // start main loop
        NSLog("start listening...")
        socketManager.startListening()
        // finish process
        sessionRegistry.saveAll()
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
