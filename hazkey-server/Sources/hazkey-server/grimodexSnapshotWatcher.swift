import Dispatch
import Foundation
import Glibc

enum GrimodexSnapshotWatcherError: Error, Equatable, Sendable {
    case initializeFailed(errno: Int32)
    case addWatchFailed(path: String, errno: Int32)
    case noExistingAncestor(path: String)
    case readFailed(errno: Int32)
    case malformedEvent
}

final class GrimodexSnapshotWatcher: @unchecked Sendable {
    private enum Role: Hashable {
        case ancestor
        case root
        case projects
    }

    private struct Registration {
        let descriptor: Int32
        let path: String
        let expectedChild: String?
    }

    private static let watchMask = UInt32(
        IN_CREATE
            | IN_MOVED_TO
            | IN_MOVED_FROM
            | IN_DELETE
            | IN_CLOSE_WRITE
            | IN_ATTRIB
            | IN_DELETE_SELF
            | IN_MOVE_SELF
            | IN_ONLYDIR
    )
    private static let topologyMask = UInt32(
        IN_CREATE | IN_MOVED_TO | IN_MOVED_FROM | IN_DELETE | IN_ATTRIB
    )
    private static let reloadMask = UInt32(
        IN_CREATE | IN_MOVED_TO | IN_MOVED_FROM | IN_DELETE | IN_CLOSE_WRITE | IN_ATTRIB
    )
    private static let selfInvalidationMask = UInt32(
        IN_DELETE_SELF | IN_MOVE_SELF | IN_IGNORED | IN_UNMOUNT
    )

    private let rootURL: URL
    private let projectsURL: URL
    private let debounceInterval: TimeInterval
    private let retryInterval: TimeInterval
    private let maxRearmAttempts: Int
    private let beforeReconcile: @Sendable () throws -> Void
    private let reload: @Sendable () -> Bool
    private let queue = DispatchQueue(label: "com.miyakey.grimodex.ime.snapshot-watcher")
    private let healthLock = NSLock()

    private var fileDescriptor: Int32 = -1
    private var source: DispatchSourceRead?
    private var registrations: [Role: Registration] = [:]
    private var rolesByDescriptor: [Int32: Role] = [:]
    private var pendingReload: DispatchWorkItem?
    private var pendingRetry: DispatchWorkItem?
    private var pendingRearm: DispatchWorkItem?
    private var started = false
    private var active = false

    var isActive: Bool {
        healthLock.lock()
        defer { healthLock.unlock() }
        return active
    }

    init(
        rootURL: URL,
        debounceInterval: TimeInterval = 0.1,
        retryInterval: TimeInterval = 0.1,
        maxRearmAttempts: Int = 5,
        beforeReconcile: @escaping @Sendable () throws -> Void = {},
        reload: @escaping @Sendable () -> Bool
    ) {
        self.rootURL = rootURL.standardizedFileURL
        self.projectsURL = rootURL.standardizedFileURL
            .appendingPathComponent("projects", isDirectory: true)
        self.debounceInterval = max(0, debounceInterval)
        self.retryInterval = max(0, retryInterval)
        self.maxRearmAttempts = max(0, maxRearmAttempts)
        self.beforeReconcile = beforeReconcile
        self.reload = reload
    }

    func start() throws {
        let result: Result<Void, Error> = queue.sync {
            Result { try startIsolated() }
        }
        try result.get()
    }

    func stop() {
        queue.sync {
            stopIsolated()
        }
    }

    private func startIsolated() throws {
        guard !started else { return }
        let descriptor = inotify_init1(Int32(IN_NONBLOCK | IN_CLOEXEC))
        guard descriptor >= 0 else {
            throw GrimodexSnapshotWatcherError.initializeFailed(errno: errno)
        }

        fileDescriptor = descriptor
        let readSource = DispatchSource.makeReadSource(fileDescriptor: descriptor, queue: queue)
        readSource.setEventHandler { [weak self] in
            self?.drainEvents()
        }
        readSource.setCancelHandler {
            _ = Glibc.close(descriptor)
        }
        source = readSource
        readSource.resume()

        do {
            try reconcileWatches()
            started = true
            setActive(true)
            performReload(allowRetry: true)
        } catch {
            setActive(false)
            source = nil
            fileDescriptor = -1
            readSource.cancel()
            throw error
        }
    }

    private func stopIsolated() {
        setActive(false)
        guard started || source != nil else { return }
        started = false
        pendingReload?.cancel()
        pendingReload = nil
        pendingRetry?.cancel()
        pendingRetry = nil
        pendingRearm?.cancel()
        pendingRearm = nil
        registrations.removeAll()
        rolesByDescriptor.removeAll()
        fileDescriptor = -1
        source?.cancel()
        source = nil
    }

    private func reconcileWatches() throws {
        try beforeReconcile()
        var transientError: GrimodexSnapshotWatcherError?
        for _ in 0..<2 {
            do {
                try reconcileOnce()
                return
            } catch let error as GrimodexSnapshotWatcherError {
                switch error {
                case .addWatchFailed(_, let number) where number == ENOENT || number == ENOTDIR:
                    transientError = error
                default:
                    throw error
                }
            }
        }
        throw transientError
            ?? GrimodexSnapshotWatcherError.noExistingAncestor(path: rootURL.path)
    }

    private func reconcileOnce() throws {
        if isDirectory(rootURL) {
            removeWatch(for: .ancestor)
            try ensureWatch(for: .root, url: rootURL, expectedChild: nil)
            if isDirectory(projectsURL) {
                try ensureWatch(for: .projects, url: projectsURL, expectedChild: nil)
            } else {
                removeWatch(for: .projects)
            }
            return
        }

        removeWatch(for: .root)
        removeWatch(for: .projects)
        guard let (ancestor, expectedChild) = nearestExistingAncestor() else {
            throw GrimodexSnapshotWatcherError.noExistingAncestor(path: rootURL.path)
        }
        try ensureWatch(for: .ancestor, url: ancestor, expectedChild: expectedChild)
    }

    private func ensureWatch(for role: Role, url: URL, expectedChild: String?) throws {
        let path = url.path
        if let registration = registrations[role],
            registration.path == path,
            registration.expectedChild == expectedChild
        {
            return
        }
        removeWatch(for: role)
        let descriptor = path.withCString {
            inotify_add_watch(fileDescriptor, $0, Self.watchMask)
        }
        guard descriptor >= 0 else {
            throw GrimodexSnapshotWatcherError.addWatchFailed(path: path, errno: errno)
        }
        registrations[role] = Registration(
            descriptor: descriptor,
            path: path,
            expectedChild: expectedChild
        )
        rolesByDescriptor[descriptor] = role
    }

    private func removeWatch(for role: Role) {
        guard let registration = registrations.removeValue(forKey: role) else { return }
        rolesByDescriptor.removeValue(forKey: registration.descriptor)
        if fileDescriptor >= 0 {
            _ = inotify_rm_watch(fileDescriptor, registration.descriptor)
        }
    }

    private func discardInvalidatedWatch(for role: Role, descriptor: Int32) {
        guard registrations[role]?.descriptor == descriptor else { return }
        registrations.removeValue(forKey: role)
        rolesByDescriptor.removeValue(forKey: descriptor)
    }

    private func nearestExistingAncestor() -> (URL, String)? {
        var current = rootURL
        var missingComponents: [String] = []
        while true {
            if isDirectory(current) {
                guard let expectedChild = missingComponents.first else { return nil }
                return (current, expectedChild)
            }
            let component = current.lastPathComponent
            guard !component.isEmpty else { return nil }
            missingComponents.insert(component, at: 0)
            let parent = current.deletingLastPathComponent()
            guard parent.path != current.path else { return nil }
            current = parent
        }
    }

    private func isDirectory(_ url: URL) -> Bool {
        var isDirectory = ObjCBool(false)
        return FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory)
            && isDirectory.boolValue
    }

    private func drainEvents() {
        do {
            var buffer = [UInt8](repeating: 0, count: 64 * 1024)
            while true {
                let count: Int = buffer.withUnsafeMutableBytes {
                    Glibc.read(fileDescriptor, $0.baseAddress, $0.count)
                }
                if count < 0 {
                    if errno == EINTR { continue }
                    if errno == EAGAIN || errno == EWOULDBLOCK { return }
                    throw GrimodexSnapshotWatcherError.readFailed(errno: errno)
                }
                if count == 0 { return }
                try parseEvents(buffer, count: count)
            }
        } catch {
            NSLog("Grimodex snapshot watcher error: \(error)")
            reconcileAfterEvent()
            scheduleReload()
        }
    }

    private func parseEvents(_ buffer: [UInt8], count: Int) throws {
        let headerSize = MemoryLayout<inotify_event>.size
        var offset = 0
        while offset + headerSize <= count {
            let event: inotify_event = buffer.withUnsafeBytes {
                $0.loadUnaligned(fromByteOffset: offset, as: inotify_event.self)
            }
            let recordSize = headerSize + Int(event.len)
            guard recordSize >= headerSize, offset + recordSize <= count else {
                throw GrimodexSnapshotWatcherError.malformedEvent
            }
            let nameStart = offset + headerSize
            let nameBytes = buffer[nameStart..<(nameStart + Int(event.len))]
                .prefix { $0 != 0 }
            let name = nameBytes.isEmpty ? nil : String(bytes: nameBytes, encoding: .utf8)
            handleEvent(descriptor: event.wd, mask: event.mask, name: name)
            offset += recordSize
        }
        guard offset == count else {
            throw GrimodexSnapshotWatcherError.malformedEvent
        }
    }

    private func handleEvent(descriptor: Int32, mask: UInt32, name: String?) {
        if mask & UInt32(IN_Q_OVERFLOW) != 0 {
            reconcileAfterEvent()
            scheduleReload()
            return
        }
        guard let role = rolesByDescriptor[descriptor] else { return }

        if mask & Self.selfInvalidationMask != 0 {
            discardInvalidatedWatch(for: role, descriptor: descriptor)
            reconcileAfterEvent()
            scheduleReload()
            return
        }

        switch role {
        case .ancestor:
            guard mask & Self.topologyMask != 0 else { return }
            let expectedChild = registrations[role]?.expectedChild
            guard expectedChild == nil || expectedChild == name else { return }
            reconcileAfterEvent()
            scheduleReload()
        case .root:
            if name == "projects", mask & Self.topologyMask != 0 {
                reconcileAfterEvent()
                scheduleReload()
            } else if name == "state.json", mask & Self.reloadMask != 0 {
                scheduleReload()
            }
        case .projects:
            if let name, name.hasSuffix(".json"), mask & Self.reloadMask != 0 {
                scheduleReload()
            }
        }
    }

    private func reconcileAfterEvent() {
        pendingRearm?.cancel()
        pendingRearm = nil
        do {
            try reconcileWatches()
            setActive(true)
        } catch {
            setActive(false)
            NSLog("Failed to rearm Grimodex snapshot watches: \(error)")
            scheduleRearm(attempt: 1)
        }
    }

    private func scheduleRearm(attempt: Int) {
        guard started, attempt <= maxRearmAttempts else { return }
        pendingRearm?.cancel()
        let item = DispatchWorkItem { [weak self] in
            guard let self, self.started else { return }
            self.pendingRearm = nil
            do {
                try self.reconcileWatches()
                self.setActive(true)
                self.scheduleReload()
            } catch {
                self.setActive(false)
                NSLog("Failed to rearm Grimodex snapshot watches: \(error)")
                self.scheduleRearm(attempt: attempt + 1)
            }
        }
        pendingRearm = item
        let multiplier = Double(1 << (attempt - 1))
        queue.asyncAfter(
            deadline: .now() + max(0.05, retryInterval * multiplier),
            execute: item
        )
    }

    private func scheduleReload() {
        pendingRetry?.cancel()
        pendingRetry = nil
        pendingReload?.cancel()
        let item = DispatchWorkItem { [weak self] in
            guard let self, self.started else { return }
            self.pendingReload = nil
            self.performReload(allowRetry: true)
        }
        pendingReload = item
        queue.asyncAfter(deadline: .now() + debounceInterval, execute: item)
    }

    private func performReload(allowRetry: Bool) {
        let shouldRetry = reload()
        guard shouldRetry, allowRetry, started else { return }
        pendingRetry?.cancel()
        let item = DispatchWorkItem { [weak self] in
            guard let self, self.started else { return }
            self.pendingRetry = nil
            self.performReload(allowRetry: false)
        }
        pendingRetry = item
        queue.asyncAfter(deadline: .now() + retryInterval, execute: item)
    }

    private func setActive(_ value: Bool) {
        healthLock.lock()
        active = value
        healthLock.unlock()
    }
}
