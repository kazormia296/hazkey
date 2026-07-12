import Dispatch
import Foundation
import Glibc

enum GrimodexConsumerRegistrarError: Error, Equatable, Sendable {
    case invalidVersion
    case oversizedPayload
    case unsafeDirectory(path: String)
    case wrongOwner(path: String)
    case systemCall(operation: String, errno: Int32)
}

private struct GrimodexConsumerCapabilities: Encodable, Sendable {
    let profile = true
    let dynamicDictionary = true
    let zenzaiV3Conditions = true
    let applicationScoping = true

    enum CodingKeys: String, CodingKey {
        case profile
        case dynamicDictionary = "dynamic_dictionary"
        case zenzaiV3Conditions = "zenzai_v3_conditions"
        case applicationScoping = "application_scoping"
    }
}

private struct GrimodexConsumerHandshake: Encodable, Sendable {
    let formatVersion = 1
    let consumerID: String
    let name: String
    let version: String
    let platform = "linux"
    let capabilities = GrimodexConsumerCapabilities()
    let lastSeen: String

    enum CodingKeys: String, CodingKey {
        case formatVersion = "format_version"
        case consumerID = "consumer_id"
        case name
        case version
        case platform
        case capabilities
        case lastSeen = "last_seen"
    }
}

private struct GrimodexConsumerAtomicWriter: Sendable {
    func replace(_ data: Data, at destination: URL) throws {
        let directory = destination.deletingLastPathComponent()
        let directoryFD = directory.path.withCString {
            Glibc.open($0, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW)
        }
        guard directoryFD >= 0 else {
            throw systemCall("open consumer directory")
        }
        defer { _ = Glibc.close(directoryFD) }

        let temporaryName = ".\(destination.lastPathComponent).\(UUID().uuidString).tmp"
        let flags = O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW
        let temporaryFD = temporaryName.withCString {
            Glibc.openat(directoryFD, $0, flags, mode_t(0o600))
        }
        guard temporaryFD >= 0 else {
            throw systemCall("create consumer temporary file")
        }

        var renamed = false
        defer {
            _ = Glibc.close(temporaryFD)
            if !renamed {
                _ = temporaryName.withCString {
                    Glibc.unlinkat(directoryFD, $0, 0)
                }
            }
        }

        try writeAll(data, to: temporaryFD)
        guard Glibc.fchmod(temporaryFD, mode_t(0o600)) == 0 else {
            throw systemCall("chmod consumer temporary file")
        }
        guard Glibc.fsync(temporaryFD) == 0 else {
            throw systemCall("fsync consumer temporary file")
        }

        let renameResult = temporaryName.withCString { temporaryPointer in
            destination.lastPathComponent.withCString { destinationPointer in
                Glibc.renameat(
                    directoryFD,
                    temporaryPointer,
                    directoryFD,
                    destinationPointer
                )
            }
        }
        guard renameResult == 0 else {
            throw systemCall("rename consumer handshake")
        }
        renamed = true
        guard Glibc.fsync(directoryFD) == 0 else {
            throw systemCall("fsync consumer directory")
        }
    }

    func remove(_ destination: URL) throws {
        let directory = destination.deletingLastPathComponent()
        let directoryFD = directory.path.withCString {
            Glibc.open($0, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW)
        }
        if directoryFD < 0, errno == ENOENT { return }
        guard directoryFD >= 0 else {
            throw systemCall("open consumer directory")
        }
        defer { _ = Glibc.close(directoryFD) }

        let result = destination.lastPathComponent.withCString {
            Glibc.unlinkat(directoryFD, $0, 0)
        }
        if result < 0, errno != ENOENT {
            throw systemCall("remove consumer handshake")
        }
        guard Glibc.fsync(directoryFD) == 0 else {
            throw systemCall("fsync consumer directory")
        }
    }

    private func writeAll(_ data: Data, to descriptor: Int32) throws {
        try data.withUnsafeBytes { rawBuffer in
            guard let baseAddress = rawBuffer.baseAddress else { return }
            var offset = 0
            while offset < rawBuffer.count {
                let count = Glibc.write(
                    descriptor,
                    baseAddress.advanced(by: offset),
                    rawBuffer.count - offset
                )
                if count < 0, errno == EINTR { continue }
                guard count > 0 else {
                    throw systemCall("write consumer handshake")
                }
                offset += count
            }
        }
    }

    private func systemCall(_ operation: String) -> GrimodexConsumerRegistrarError {
        GrimodexConsumerRegistrarError.systemCall(operation: operation, errno: errno)
    }
}

final class GrimodexConsumerRegistrar: @unchecked Sendable {
    static let consumerID = "fcitx5-grimodex"
    static let heartbeatInterval: TimeInterval = 15 * 60

    private let rootURL: URL
    private let version: String
    private let now: @Sendable () -> Date
    private let heartbeatInterval: TimeInterval
    private let writer = GrimodexConsumerAtomicWriter()
    private let registrationLock = NSLock()
    private let heartbeatQueue = DispatchQueue(
        label: "com.miyakey.grimodex.ime.consumer-heartbeat"
    )
    private var heartbeatTimer: DispatchSourceTimer?
    private var registered = false

    var isRegistered: Bool {
        registrationLock.lock()
        defer { registrationLock.unlock() }
        return registered
    }

    init(
        rootURL: URL = GrimodexPathResolver.resolve(),
        version: String,
        now: @escaping @Sendable () -> Date = { Date() },
        heartbeatInterval: TimeInterval = GrimodexConsumerRegistrar.heartbeatInterval
    ) {
        self.rootURL = rootURL.standardizedFileURL
        self.version = version
        self.now = now
        self.heartbeatInterval = max(0.01, heartbeatInterval)
    }

    @discardableResult
    func registerNow() throws -> URL {
        registrationLock.lock()
        defer { registrationLock.unlock() }

        do {
            guard validVersion(version) else {
                throw GrimodexConsumerRegistrarError.invalidVersion
            }
            try ensurePrivateDirectory(rootURL)
            let consumersURL = rootURL.appendingPathComponent("consumers", isDirectory: true)
            try ensurePrivateDirectory(consumersURL)
            let destination = consumersURL.appendingPathComponent("\(Self.consumerID).json")

            let handshake = GrimodexConsumerHandshake(
                consumerID: Self.consumerID,
                name: "Grimodex IME for Linux",
                version: version,
                lastSeen: timestamp(now())
            )
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            var data = try encoder.encode(handshake)
            data.append(0x0A)
            guard data.count <= GrimodexProtocolLimits.consumerBytes else {
                throw GrimodexConsumerRegistrarError.oversizedPayload
            }
            try writer.replace(data, at: destination)
            registered = true
            return destination
        } catch {
            registered = false
            throw error
        }
    }

    func start() throws {
        let result: Result<Void, Error> = heartbeatQueue.sync {
            Result {
                guard heartbeatTimer == nil else { return }
                _ = try registerNow()
                let timer = DispatchSource.makeTimerSource(queue: heartbeatQueue)
                timer.schedule(
                    deadline: .now() + heartbeatInterval,
                    repeating: heartbeatInterval
                )
                timer.setEventHandler { [weak self] in
                    guard let self else { return }
                    do {
                        _ = try self.registerNow()
                    } catch {
                        NSLog("Failed to refresh Grimodex consumer handshake: \(error)")
                    }
                }
                heartbeatTimer = timer
                timer.resume()
            }
        }
        try result.get()
    }

    func stop() {
        heartbeatQueue.sync {
            heartbeatTimer?.cancel()
            heartbeatTimer = nil
        }
        registrationLock.lock()
        registered = false
        registrationLock.unlock()
    }

    func unregister() throws {
        let result: Result<Void, Error> = heartbeatQueue.sync {
            Result {
                heartbeatTimer?.cancel()
                heartbeatTimer = nil
                registrationLock.lock()
                defer {
                    registered = false
                    registrationLock.unlock()
                }
                let destination = rootURL
                    .appendingPathComponent("consumers", isDirectory: true)
                    .appendingPathComponent("\(Self.consumerID).json")
                try writer.remove(destination)
            }
        }
        try result.get()
    }

    private func ensurePrivateDirectory(_ url: URL) throws {
        let fileManager = FileManager.default
        if !fileManager.fileExists(atPath: url.path) {
            try fileManager.createDirectory(
                at: url,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
        }

        let descriptor = url.path.withCString {
            Glibc.open($0, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW)
        }
        guard descriptor >= 0 else {
            throw GrimodexConsumerRegistrarError.systemCall(
                operation: "open private directory",
                errno: errno
            )
        }
        defer { _ = Glibc.close(descriptor) }

        var information = stat()
        guard Glibc.fstat(descriptor, &information) == 0 else {
            throw GrimodexConsumerRegistrarError.systemCall(
                operation: "inspect private directory",
                errno: errno
            )
        }
        guard information.st_mode & mode_t(S_IFMT) == mode_t(S_IFDIR) else {
            throw GrimodexConsumerRegistrarError.unsafeDirectory(path: url.path)
        }
        guard information.st_uid == Glibc.getuid() else {
            throw GrimodexConsumerRegistrarError.wrongOwner(path: url.path)
        }
        guard Glibc.fchmod(descriptor, mode_t(0o700)) == 0 else {
            throw GrimodexConsumerRegistrarError.systemCall(
                operation: "chmod private directory",
                errno: errno
            )
        }
    }

    private func validVersion(_ value: String) -> Bool {
        let scalars = value.unicodeScalars
        guard
            !scalars.isEmpty,
            scalars.count <= 64,
            !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else {
            return false
        }
        return scalars.allSatisfy { scalar in
            let number = scalar.value
            return !((0...0x1F).contains(number)
                || (0x7F...0x9F).contains(number))
        }
    }

    private func timestamp(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSS'Z'"
        return formatter.string(from: date)
    }
}
