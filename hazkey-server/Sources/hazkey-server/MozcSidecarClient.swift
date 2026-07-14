import Foundation
import Glibc
import SwiftProtobuf

struct MozcCoreCandidate: Equatable, Sendable {
    let value: String
    let description: String?
    let consumedKeySize: Int
}

struct MozcCoreConversion: Equatable, Sendable {
    let candidates: [MozcCoreCandidate]
    let segmentKeySize: Int
}

protocol MozcCoreConverting: AnyObject {
    func convert(
        reading: String,
        targetKeySize: Int?,
        maxCandidates: Int
    ) throws -> MozcCoreConversion

    /// Crosses a privacy boundary without serializing another request. The
    /// process implementation closes its pipes and terminates the helper.
    func purgeSensitiveState()
}

enum MozcSidecarError: Error, LocalizedError, Equatable {
    case invalidRequest
    case launchFailed
    case writeFailed
    case timeout
    case disconnected
    case oversizedFrame
    case malformedResponse
    case responseMismatch
    case datasetMismatch
    case helperRejected

    var errorDescription: String? {
        switch self {
        case .invalidRequest: "invalid Mozc sidecar request"
        case .launchFailed: "Mozc sidecar could not be launched"
        case .writeFailed: "Mozc sidecar request could not be written"
        case .timeout: "Mozc sidecar timed out"
        case .disconnected: "Mozc sidecar disconnected"
        case .oversizedFrame: "Mozc sidecar frame exceeded the limit"
        case .malformedResponse: "Mozc sidecar returned malformed data"
        case .responseMismatch: "Mozc sidecar response did not match the request"
        case .datasetMismatch: "Mozc sidecar dataset identity did not match"
        case .helperRejected: "Mozc sidecar rejected the request"
        }
    }
}

/// Synchronous, process-wide supervisor for the optional converter helper.
/// Calls are serialized because Mozc's ConverterInterface and the stdio frame
/// stream are both single-owner resources. A failed request is never replayed;
/// the next independent request lazily starts a fresh helper.
final class MozcSidecarClient: MozcCoreConverting {
    static let protocolVersion: UInt32 = 1
    static let maximumFrameSize = 4 * 1024 * 1024
    static let maximumReadingScalars = 255
    static let maximumReadingBytes = 1_023
    static let fixedB0DatasetSHA256 =
        "b9884362e37772f772a0d28d1e12622455c14353497b3435deed60aa7e592c5e"

    private let helperPath: String
    private let dataPath: String
    private let expectedDatasetSHA256: String
    private let timeoutMilliseconds: Int32
    private let lock = NSLock()
    private var process: Process?
    private var inputHandle: FileHandle?
    private var outputHandle: FileHandle?
    private var temporaryDirectoryPath: String?
    private var nextRequestID: UInt64 = 1
    private var temporaryDirectoryCleanupFailures: UInt64 = 0

    /// Exposes cleanup failures to diagnostics/tests without publishing the
    /// private directory name or any composition-derived payload.
    var temporaryDirectoryCleanupFailureCount: UInt64 {
        lock.lock()
        defer { lock.unlock() }
        return temporaryDirectoryCleanupFailures
    }

    init(
        helperPath: String,
        dataPath: String,
        expectedDatasetSHA256: String = MozcSidecarClient.fixedB0DatasetSHA256,
        timeoutMilliseconds: Int = 1_500
    ) {
        self.helperPath = helperPath
        self.dataPath = dataPath
        self.expectedDatasetSHA256 = expectedDatasetSHA256
        self.timeoutMilliseconds = Int32(
            min(max(timeoutMilliseconds, 1), Int(Int32.max))
        )
        // The daemon already ignores SIGPIPE for client sockets. Do the same
        // here so a crashed helper becomes a typed request error in direct
        // adapter tests as well as in the running server.
        signal(SIGPIPE, SIG_IGN)
    }

    deinit {
        lock.lock()
        terminateProcessLocked()
        lock.unlock()
    }

    func convert(
        reading: String,
        targetKeySize: Int?,
        maxCandidates: Int
    ) throws -> MozcCoreConversion {
        guard !reading.isEmpty,
              reading.utf8.count <= Self.maximumReadingBytes,
              reading.unicodeScalars.count <= Self.maximumReadingScalars,
              maxCandidates > 0,
              maxCandidates <= 100,
              targetKeySize.map({ $0 > 0 }) ?? true,
              (targetKeySize ?? 0) <= reading.unicodeScalars.count,
              (targetKeySize ?? 0) <= Self.maximumReadingScalars else {
            throw MozcSidecarError.invalidRequest
        }

        lock.lock()
        defer { lock.unlock() }
        do {
            try ensureProcessLocked()
            let requestID = allocateRequestIDLocked()
            var request = Hazkey_MozcSidecar_Request()
            request.protocolVersion = Self.protocolVersion
            request.requestID = requestID
            request.operation = .convert
            request.reading = reading
            request.targetKeySize = UInt32(targetKeySize ?? 0)
            request.maxCandidates = UInt32(maxCandidates)
            let response = try transactLocked(request, requestID: requestID)
            guard response.ok else {
                // Do not propagate helper-supplied text: even a buggy helper
                // must not be able to reflect the reading into server logs.
                throw MozcSidecarError.helperRejected
            }
            let segmentKeySize = Int(response.segmentKeySize)
            guard !response.candidates.isEmpty,
                  response.candidates.count <= maxCandidates,
                  segmentKeySize > 0,
                  segmentKeySize <= reading.unicodeScalars.count,
                  targetKeySize == nil || segmentKeySize == targetKeySize else {
                throw MozcSidecarError.malformedResponse
            }
            let candidates = try response.candidates.map { candidate in
                guard !candidate.value.isEmpty,
                      Int(candidate.consumedKeySize) == segmentKeySize else {
                    throw MozcSidecarError.malformedResponse
                }
                return MozcCoreCandidate(
                    value: candidate.value,
                    description: candidate.description_p.isEmpty
                        ? nil
                        : candidate.description_p,
                    consumedKeySize: Int(candidate.consumedKeySize)
                )
            }
            return MozcCoreConversion(
                candidates: candidates,
                segmentKeySize: segmentKeySize
            )
        } catch {
            terminateProcessLocked()
            if let typed = error as? MozcSidecarError {
                throw typed
            }
            throw MozcSidecarError.malformedResponse
        }
    }

    func purgeSensitiveState() {
        lock.lock()
        terminateProcessLocked()
        lock.unlock()
    }

    private func allocateRequestIDLocked() -> UInt64 {
        let result = nextRequestID
        nextRequestID = nextRequestID == UInt64.max ? 1 : nextRequestID + 1
        return result
    }

    private func ensureProcessLocked() throws {
        if let process, process.isRunning,
           inputHandle != nil, outputHandle != nil {
            return
        }
        terminateProcessLocked()

        let child = Process()
        let inputPipe = Pipe()
        let outputPipe = Pipe()
        let privateTemporaryDirectory = try createPrivateTemporaryDirectoryLocked()
        temporaryDirectoryPath = privateTemporaryDirectory
        child.executableURL = URL(fileURLWithPath: helperPath)
        child.arguments = [
            "--data_file=\(dataPath)",
            "--dataset_sha256=\(expectedDatasetSHA256)",
        ]
        child.environment = [
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            // Mozc creates process-local profile databases below TMPDIR. The
            // supervisor owns this private root so SIGKILL/error paths can
            // remove those files even though C++ destructors do not run.
            "TMPDIR": privateTemporaryDirectory,
        ]
        child.standardInput = inputPipe
        child.standardOutput = outputPipe
        child.standardError = FileHandle.nullDevice
        do {
            try child.run()
        } catch {
            temporaryDirectoryPath = nil
            if !removePrivateTemporaryDirectory(privateTemporaryDirectory) {
                recordPrivateTemporaryDirectoryCleanupFailure(
                    "directory removal failed after launch"
                )
            }
            throw MozcSidecarError.launchFailed
        }

        // Close the parent's unused copies so EOF from the child is observable.
        inputPipe.fileHandleForReading.closeFile()
        outputPipe.fileHandleForWriting.closeFile()
        process = child
        inputHandle = inputPipe.fileHandleForWriting
        outputHandle = outputPipe.fileHandleForReading
        try performHandshakeLocked()
    }

    /// Authenticates the helper contract without exposing composition text.
    /// Every freshly launched process must prove its protocol and fixed B0
    /// dataset identity before the first CONVERT frame is written.
    private func performHandshakeLocked() throws {
        let requestID = allocateRequestIDLocked()
        var request = Hazkey_MozcSidecar_Request()
        request.protocolVersion = Self.protocolVersion
        request.requestID = requestID
        request.operation = .ping
        let response = try transactLocked(request, requestID: requestID)
        guard response.ok else {
            throw MozcSidecarError.helperRejected
        }
        guard response.candidates.isEmpty,
              response.segmentKeySize == 0 else {
            throw MozcSidecarError.malformedResponse
        }
    }

    private func transactLocked(
        _ request: Hazkey_MozcSidecar_Request,
        requestID: UInt64
    ) throws -> Hazkey_MozcSidecar_Response {
        let payload = try request.serializedData()
        try writeFrameLocked(payload)
        let responsePayload = try readFrameLocked()
        let response: Hazkey_MozcSidecar_Response
        do {
            response = try Hazkey_MozcSidecar_Response(
                serializedBytes: responsePayload
            )
        } catch {
            throw MozcSidecarError.malformedResponse
        }
        guard response.protocolVersion == Self.protocolVersion,
              response.requestID == requestID else {
            throw MozcSidecarError.responseMismatch
        }
        guard response.datasetSha256 == expectedDatasetSHA256 else {
            throw MozcSidecarError.datasetMismatch
        }
        return response
    }

    private func terminateProcessLocked() {
        let child = process
        let privateTemporaryDirectory = temporaryDirectoryPath
        inputHandle?.closeFile()
        outputHandle?.closeFile()
        inputHandle = nil
        outputHandle = nil
        process = nil
        temporaryDirectoryPath = nil
        var childStoppedWriting = true
        if let child, child.isRunning {
            let processIdentifier = child.processIdentifier
            if Glibc.kill(processIdentifier, SIGKILL) == 0 {
                childStoppedWriting = waitForProcessExitWithoutReaping(
                    processIdentifier
                )
            } else if errno == ESRCH {
                childStoppedWriting = true
            } else {
                childStoppedWriting = false
            }
        }
        if let privateTemporaryDirectory {
            if childStoppedWriting {
                if !removePrivateTemporaryDirectory(privateTemporaryDirectory) {
                    recordPrivateTemporaryDirectoryCleanupFailure(
                        "directory removal failed"
                    )
                }
            } else {
                // Removing a live helper's profile can race a background writer
                // which recreates entries after FileManager's enumeration. Keep
                // the private root intact rather than claim a successful purge.
                recordPrivateTemporaryDirectoryCleanupFailure(
                    "helper termination was not observed"
                )
            }
        }
        // Foundation remains the sole reaper. waitid(WNOWAIT) only observes the
        // stopped child, avoiding both zombie ownership races and an unbounded
        // Process.waitUntilExit() on the request thread.
    }

    private func waitForProcessExitWithoutReaping(
        _ processIdentifier: pid_t
    ) -> Bool {
        let deadline = DispatchTime.now().uptimeNanoseconds &+ 250_000_000
        while true {
            var information = siginfo_t()
            errno = 0
            let result = Glibc.waitid(
                P_PID,
                id_t(processIdentifier),
                &information,
                WEXITED | WNOHANG | WNOWAIT
            )
            if result == 0, information.si_signo == SIGCHLD {
                return true
            }
            if result < 0, errno == ECHILD {
                // Foundation won the reap race, so the child cannot write.
                return true
            }
            if result < 0, errno != EINTR {
                return false
            }
            guard DispatchTime.now().uptimeNanoseconds < deadline else {
                return false
            }
            sleepForProcessCleanupRetry()
        }
    }

    private func createPrivateTemporaryDirectoryLocked() throws -> String {
        var template = Array(
            "/tmp/fcitx5-grimodex-mozc-XXXXXX".utf8CString
        )
        let path = template.withUnsafeMutableBufferPointer { buffer -> String? in
            guard let baseAddress = buffer.baseAddress,
                  let created = Glibc.mkdtemp(baseAddress) else {
                return nil
            }
            return String(cString: created)
        }
        guard let path else { throw MozcSidecarError.launchFailed }
        return path
    }

    private func removePrivateTemporaryDirectory(_ path: String) -> Bool {
        let url = URL(fileURLWithPath: path, isDirectory: true)
        for attempt in 0..<8 {
            if !FileManager.default.fileExists(atPath: path) {
                return true
            }
            do {
                try FileManager.default.removeItem(at: url)
            } catch {
                // A just-terminated helper may leave Foundation observing a
                // transient directory state. Retry without logging paths.
            }
            if !FileManager.default.fileExists(atPath: path) {
                return true
            }
            if attempt < 7 {
                sleepForProcessCleanupRetry()
            }
        }
        return false
    }

    private func sleepForProcessCleanupRetry() {
        var requested = timespec(tv_sec: 0, tv_nsec: 2_000_000)
        var remaining = timespec()
        while Glibc.nanosleep(&requested, &remaining) < 0, errno == EINTR {
            requested = remaining
        }
    }

    private func recordPrivateTemporaryDirectoryCleanupFailure(_ reason: String) {
        if temporaryDirectoryCleanupFailures < UInt64.max {
            temporaryDirectoryCleanupFailures += 1
        }
        NSLog("Mozc sidecar private temporary directory cleanup failed: %@", reason)
    }

    private func writeFrameLocked(_ payload: Data) throws {
        guard payload.count <= Self.maximumFrameSize,
              let inputHandle else {
            throw MozcSidecarError.oversizedFrame
        }
        var length = UInt32(payload.count).bigEndian
        var frame = Data(bytes: &length, count: MemoryLayout<UInt32>.size)
        frame.append(payload)
        try writeAll(frame, to: inputHandle.fileDescriptor)
    }

    private func writeAll(_ data: Data, to descriptor: Int32) throws {
        try data.withUnsafeBytes { bytes in
            guard let baseAddress = bytes.baseAddress else { return }
            var offset = 0
            while offset < bytes.count {
                let count = Glibc.write(
                    descriptor,
                    baseAddress.advanced(by: offset),
                    bytes.count - offset
                )
                if count > 0 {
                    offset += count
                } else if count < 0 && errno == EINTR {
                    continue
                } else {
                    throw MozcSidecarError.writeFailed
                }
            }
        }
    }

    private func readFrameLocked() throws -> Data {
        guard let outputHandle else {
            throw MozcSidecarError.disconnected
        }
        let deadline = DispatchTime.now().uptimeNanoseconds
            &+ UInt64(timeoutMilliseconds) * 1_000_000
        let header = try readExactly(
            MemoryLayout<UInt32>.size,
            from: outputHandle.fileDescriptor,
            deadline: deadline
        )
        let length = header.withUnsafeBytes { bytes -> UInt32 in
            bytes.loadUnaligned(as: UInt32.self).bigEndian
        }
        guard length > 0, length <= UInt32(Self.maximumFrameSize) else {
            throw MozcSidecarError.oversizedFrame
        }
        return try readExactly(
            Int(length),
            from: outputHandle.fileDescriptor,
            deadline: deadline
        )
    }

    private func readExactly(
        _ count: Int,
        from descriptor: Int32,
        deadline: UInt64
    ) throws -> Data {
        var result = [UInt8](repeating: 0, count: count)
        var offset = 0
        try result.withUnsafeMutableBytes { bytes in
            while offset < count {
                try waitForReadable(descriptor, deadline: deadline)
                let readCount = Glibc.read(
                    descriptor,
                    bytes.baseAddress?.advanced(by: offset),
                    count - offset
                )
                if readCount > 0 {
                    offset += readCount
                } else if readCount == 0 {
                    throw MozcSidecarError.disconnected
                } else if errno == EINTR {
                    continue
                } else {
                    throw MozcSidecarError.disconnected
                }
            }
        }
        return Data(result)
    }

    private func waitForReadable(_ descriptor: Int32, deadline: UInt64) throws {
        while true {
            let now = DispatchTime.now().uptimeNanoseconds
            guard now < deadline else { throw MozcSidecarError.timeout }
            let remaining = deadline - now
            let milliseconds = min(
                max((remaining + 999_999) / 1_000_000, 1),
                UInt64(Int32.max)
            )
            var pollDescriptor = pollfd(
                fd: descriptor,
                events: Int16(POLLIN),
                revents: 0
            )
            let status = Glibc.poll(
                &pollDescriptor,
                1,
                Int32(milliseconds)
            )
            if status > 0 {
                if pollDescriptor.revents & Int16(POLLIN) != 0 { return }
                let failures = Int16(POLLERR) | Int16(POLLHUP) | Int16(POLLNVAL)
                if pollDescriptor.revents & failures != 0 {
                    throw MozcSidecarError.disconnected
                }
            } else if status == 0 {
                throw MozcSidecarError.timeout
            } else if errno != EINTR {
                throw MozcSidecarError.disconnected
            }
        }
    }
}
