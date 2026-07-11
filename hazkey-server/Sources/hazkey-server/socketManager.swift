import Foundation

protocol SocketManagerDelegate: AnyObject {
    func socketManager(_ manager: SocketManager, didReceiveData data: Data, from clientFd: Int32)
        -> Data
    func socketManager(_ manager: SocketManager, clientDidConnect clientFd: Int32)
    func socketManager(_ manager: SocketManager, clientDidDisconnect clientFd: Int32)
}

class SocketManager {
    private struct ClientState {
        var input = Data()
        var output = Data()
    }

    private static let maximumMessageSize = 1024 * 1024
    private static let readChunkSize = 64 * 1024
    private static let maximumReadPerPoll = 4 * readChunkSize
    private static let maximumPendingOutputSize = 2 * maximumMessageSize

    weak var delegate: SocketManagerDelegate?

    private var signalSources: [DispatchSourceSignal] = []
    private let servingLock = NSLock()
    private var continueServing = true

    private var serverFd: Int32 = -1
    private var clients: [Int32: ClientState] = [:]
    private let socketPath: String
    private var pipeFds: [Int32] = [-1, -1]

    private func stopServing(reason: String) {
        servingLock.lock()
        defer { servingLock.unlock() }
        guard continueServing else { return }
        NSLog(reason)
        continueServing = false
        if pipeFds[1] != -1 {
            close(pipeFds[1])
            pipeFds[1] = -1
        }
    }

    private var isServing: Bool {
        servingLock.lock()
        defer { servingLock.unlock() }
        return continueServing
    }

    func stop() {
        stopServing(reason: "Stop requested, shutting down...")
    }

    init(socketPath: String) {
        self.socketPath = socketPath
    }

    deinit {
        closeSocket()
    }

    func setupSocket() throws {
        unlink(socketPath)

        serverFd = socket(AF_UNIX, Int32(SOCK_STREAM.rawValue), 0)
        guard serverFd != -1 else {
            throw SocketError.readFailed("Failed to create socket", errno)
        }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        strncpy(&addr.sun_path.0, socketPath, MemoryLayout.size(ofValue: addr.sun_path))

        let addrSize = socklen_t(MemoryLayout.size(ofValue: addr))
        let bindResult = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(serverFd, $0, addrSize)
            }
        }

        guard bindResult != -1 else {
            throw SocketError.readFailed("Failed to bind socket", errno)
        }

        guard chmod(socketPath, 0o600) != -1 else {
            throw SocketError.readFailed("Failed to set socket permissions", errno)
        }

        guard listen(serverFd, 10) != -1 else {
            throw SocketError.readFailed("Failed to listen", errno)
        }

        // Set non-blocking
        let flags = fcntl(serverFd, F_GETFL, 0)
        let fcntlRes = fcntl(serverFd, F_SETFL, flags | O_NONBLOCK)
        if fcntlRes != 0 {
            NSLog("fcntl() failed")
        }

        var fds: [Int32] = [0, 0]
        guard pipe(&fds) != -1 else {
            throw SocketError.readFailed("Failed to bind pipe socket", errno)
        }
        pipeFds = fds
    }

    private func setupSignalHandlers() {
        signal(SIGPIPE, SIG_IGN)

        let signalQueue = DispatchQueue(
            label: "com.miyakey.grimodex.ime.fcitx5.server.socketmanager.signals"
        )
        let signals = [SIGINT, SIGTERM, SIGHUP]

        for sig in signals {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: signalQueue)
            source.setEventHandler { [weak self] in
                self?.stopServing(reason: "Signal \(sig) received, shutting down...")
            }
            source.resume()
            self.signalSources.append(source)
        }
    }

    func startListening() {
        setupSignalHandlers()
        while isServing {
            var pollFds: [pollfd] = []

            // Always poll the server socket for new connections
            pollFds.append(pollfd(fd: serverFd, events: Int16(POLLIN), revents: 0))

            // poll stopper
            pollFds.append(pollfd(fd: pipeFds[0], events: Int16(POLLIN), revents: 0))

            // Poll every connected client. The fd in each poll entry is the
            // stable identity for this iteration even if another client is
            // accepted or disconnected while handling the snapshot.
            for clientFd in clients.keys.sorted() {
                let wantsWrite = !(clients[clientFd]?.output.isEmpty ?? true)
                let events = Int16(POLLIN) | (wantsWrite ? Int16(POLLOUT) : 0)
                pollFds.append(pollfd(fd: clientFd, events: events, revents: 0))
            }

            let pollRes = poll(&pollFds, nfds_t(pollFds.count), 1000)

            if pollRes < 0 {
                if errno == EINTR {
                    // signal received
                    if !isServing {
                        break
                    }
                    continue
                }
                NSLog("Poll failed: \(errno)")
                break
            }

            if pollRes == 0 {
                // Timeout
                continue
            }

            // pipe closed by signalhandler
            if pollFds[1].revents & Int16(POLLIN|POLLHUP) != 0 {
                break
            }

            // Check if server socket has a new connection
            if pollFds[0].revents & Int16(POLLIN) != 0 {
                handleNewConnection()
            }

            // Check every client that was present when this poll began.
            for clientPoll in pollFds.dropFirst(2) {
                let clientFd = clientPoll.fd
                let clientEvents = Int32(clientPoll.revents)

                if clientEvents & POLLHUP != 0 || clientEvents & POLLERR != 0
                    || clientEvents & POLLNVAL != 0
                {
                    NSLog("Client disconnected or error: \(clientFd)")
                    closeClient(clientFd)
                    continue
                }

                if clientEvents & POLLIN != 0 {
                    handleClientData(clientFd)
                }
                if clients[clientFd] != nil && clientEvents & POLLOUT != 0 {
                    handleClientWrite(clientFd)
                }
            }
        }
    }

    private func handleNewConnection() {
        var clientAddr = sockaddr()
        var clientLen: socklen_t = socklen_t(MemoryLayout<sockaddr>.size)
        let newClientFd = accept(serverFd, &clientAddr, &clientLen)

        if newClientFd != -1 {
            // Set up the new client
            NSLog("Client connected: \(newClientFd)")

            // Make client non-blocking
            let clientFlags = fcntl(newClientFd, F_GETFL, 0)
            let fcntlRes = fcntl(newClientFd, F_SETFL, clientFlags | O_NONBLOCK)
            if fcntlRes != 0 {
                NSLog("fcntl() failed for client")
                close(newClientFd)
            } else {
                clients[newClientFd] = ClientState()
                delegate?.socketManager(self, clientDidConnect: newClientFd)
            }
        }
    }

    private func handleClientData(_ clientFd: Int32) {
        do {
            try readAvailableData(from: clientFd)
            try processAvailableFrames(for: clientFd)
            try flushAvailableOutput(to: clientFd)

        } catch let error as SocketError {
            handleSocketError(error, clientFd: clientFd)
        } catch {
            NSLog("An unexpected error occurred: \(error)")
            closeClient(clientFd)
        }
    }

    private func handleClientWrite(_ clientFd: Int32) {
        do {
            try flushAvailableOutput(to: clientFd)
        } catch let error as SocketError {
            handleSocketError(error, clientFd: clientFd)
        } catch {
            NSLog("An unexpected write error occurred: \(error)")
            closeClient(clientFd)
        }
    }

    private func readAvailableData(from clientFd: Int32) throws {
        var chunk = [UInt8](repeating: 0, count: Self.readChunkSize)
        var totalRead = 0
        while totalRead < Self.maximumReadPerPoll {
            let count = chunk.withUnsafeMutableBytes { bytes in
                read(clientFd, bytes.baseAddress, bytes.count)
            }
            if count > 0 {
                guard clients[clientFd] != nil else { return }
                clients[clientFd]?.input.append(contentsOf: chunk.prefix(count))
                totalRead += count
                continue
            }
            if count == 0 {
                throw SocketError.clientDisconnected("Client disconnected while reading")
            }
            if errno == EAGAIN || errno == EWOULDBLOCK {
                return
            }
            throw SocketError.readFailed("Read failed", errno)
        }
    }

    private func processAvailableFrames(for clientFd: Int32) throws {
        while let state = clients[clientFd], state.input.count >= 4 {
            let input = state.input
            let length =
                (UInt32(input[input.startIndex]) << 24)
                | (UInt32(input[input.startIndex + 1]) << 16)
                | (UInt32(input[input.startIndex + 2]) << 8)
                | UInt32(input[input.startIndex + 3])
            guard length <= UInt32(Self.maximumMessageSize) else {
                throw SocketError.messageTooLarge(length)
            }
            let frameSize = 4 + Int(length)
            guard input.count >= frameSize else { return }

            let query = Data(input.dropFirst(4).prefix(Int(length)))
            clients[clientFd]?.input.removeFirst(frameSize)
            let response =
                delegate?.socketManager(self, didReceiveData: query, from: clientFd) ?? Data()
            guard response.count <= Self.maximumMessageSize else {
                throw SocketError.messageTooLarge(UInt32(response.count))
            }
            let pendingOutputSize = (clients[clientFd]?.output.count ?? 0) + 4 + response.count
            guard pendingOutputSize <= Self.maximumPendingOutputSize else {
                throw SocketError.messageTooLarge(UInt32(pendingOutputSize))
            }
            var responseLength = UInt32(response.count).bigEndian
            clients[clientFd]?.output.append(
                withUnsafeBytes(of: &responseLength) { Data($0) })
            clients[clientFd]?.output.append(response)
        }
    }

    private func flushAvailableOutput(to clientFd: Int32) throws {
        while let output = clients[clientFd]?.output, !output.isEmpty {
            let count = output.withUnsafeBytes { bytes in
                write(clientFd, bytes.baseAddress, bytes.count)
            }
            if count > 0 {
                clients[clientFd]?.output.removeFirst(count)
                continue
            }
            if count == 0 {
                throw SocketError.clientDisconnected("Client disconnected while writing")
            }
            if errno == EAGAIN || errno == EWOULDBLOCK {
                return
            }
            throw SocketError.writeFailed("Write failed", errno)
        }
    }

    private func handleSocketError(_ error: SocketError, clientFd: Int32) {
        switch error {
        case .clientDisconnected(let msg):
            NSLog(msg)
        case .readFailed(let msg, let err):
            NSLog("Read failed: \(msg), errno: \(err)")
        case .incompleteRead(let msg), .incompleteWrite(let msg):
            NSLog(msg)
        case .messageTooLarge(let len):
            NSLog("Message too large: \(len)")
        case .writeFailed(let msg, let err):
            NSLog("Write failed: \(msg), errno: \(err)")
        default:
            NSLog("Socket error: \(error)")
        }
        closeClient(clientFd)
    }

    private func closeClient(_ clientFd: Int32) {
        guard clients.removeValue(forKey: clientFd) != nil else { return }
        NSLog("Closing client connection: \(clientFd)")
        close(clientFd)
        delegate?.socketManager(self, clientDidDisconnect: clientFd)
    }

    func closeSocket() {
        for clientFd in clients.keys {
            close(clientFd)
        }
        clients.removeAll()

        if serverFd != -1 {
            close(serverFd)
            serverFd = -1
        }

        for index in pipeFds.indices where pipeFds[index] != -1 {
            close(pipeFds[index])
            pipeFds[index] = -1
        }

        unlink(socketPath)
    }
}
