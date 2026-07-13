import Foundation

#if canImport(Glibc)
import Glibc
#elseif canImport(Musl)
import Musl
#else
#error("Unsupported platform")
#endif

enum ProcessManagerError: Error {
    case lockCreationFailed
    case anotherInstanceRunning
    case terminationFailed
}

struct ProcessIdentityVerifier {
    let procRoot: URL
    let uid: uid_t
    let expectedExecutablePath: String

    init(
        procRoot: URL = URL(fileURLWithPath: "/proc", isDirectory: true),
        uid: uid_t = getuid(),
        expectedExecutablePath: String
    ) {
        self.procRoot = procRoot
        self.uid = uid
        self.expectedExecutablePath = Self.normalizeExecutablePath(expectedExecutablePath)
    }

    func canTerminate(pid: pid_t) -> Bool {
        guard pid > 1 else { return false }
        let processDirectory = procRoot.appendingPathComponent(String(pid), isDirectory: true)
        let statusURL = processDirectory.appendingPathComponent("status", isDirectory: false)
        guard let status = try? String(contentsOf: statusURL, encoding: .utf8),
            let uidLine = status.split(separator: "\n").first(where: { $0.hasPrefix("Uid:") }),
            let realUID = uidLine.split(whereSeparator: { $0 == "\t" || $0 == " " })
                .dropFirst().first.flatMap({ uid_t($0) }),
            realUID == uid
        else {
            return false
        }

        let executableURL = processDirectory.appendingPathComponent("exe", isDirectory: false)
        guard let destination = try? FileManager.default.destinationOfSymbolicLink(
            atPath: executableURL.path
        ) else {
            return false
        }
        guard !expectedExecutablePath.isEmpty else { return false }
        return Self.normalizeExecutablePath(destination) == expectedExecutablePath
    }

    private static func normalizeExecutablePath(_ path: String) -> String {
        let deletedSuffix = " (deleted)"
        let withoutDeletedSuffix = path.hasSuffix(deletedSuffix)
            ? String(path.dropLast(deletedSuffix.count)) : path
        guard !withoutDeletedSuffix.isEmpty else { return "" }
        return URL(fileURLWithPath: withoutDeletedSuffix).standardizedFileURL.path
    }
}

class ProcessManager {
    private let lockFilePath: String
    private let processVerifier: ProcessIdentityVerifier
    private var lockFd: Int32 = -1

    init(lockFilePath: String) {
        self.lockFilePath = lockFilePath
        let currentExecutablePath = (try? FileManager.default.destinationOfSymbolicLink(
            atPath: "/proc/self/exe"
        )) ?? ""
        self.processVerifier = ProcessIdentityVerifier(
            expectedExecutablePath: currentExecutablePath
        )
    }

    deinit {
        if lockFd != -1 {
            close(lockFd)
        }
    }

    func tryLock(force: Bool) throws {
        // parent directory is created by HazkeyServer.start()

        // try lock
        self.lockFd = open(lockFilePath, O_CREAT | O_RDWR, 0o600)
        guard self.lockFd != -1 else {
            NSLog("Failed to get lock info.")
            throw ProcessManagerError.lockCreationFailed
        }

        if flock(lockFd, LOCK_EX | LOCK_NB) != 0 {
            // lock fail
            if let (oldPid, versionMatch) = readLockFile() {
                if !force, versionMatch {
                    NSLog("Another \(GrimodexProductPaths.serverExecutableName) is already running.")
                    NSLog("Use -r or --replace option to replace the existing server.")
                    throw ProcessManagerError.anotherInstanceRunning
                }

                if !versionMatch {
                    NSLog("Version mismatch detected. Terminating old server...")
                }

                // A lock file is user-writable and its PID may have been
                // recycled. Never signal anything until /proc confirms both
                // the current user and the exact Grimodex server executable.
                if kill(oldPid, 0) == 0 && processVerifier.canTerminate(pid: oldPid) {
                    try terminateAnotherServer(pid: oldPid)
                } else if kill(oldPid, 0) == 0 {
                    NSLog("Refusing to terminate an unverified lock owner with PID \(oldPid)")
                    throw ProcessManagerError.anotherInstanceRunning
                }
            } else {
                // Never scan or kill by process name. Besides PID reuse, Linux
                // comm names are truncated and would collide with upstream
                // Hazkey. A broken held lock must fail closed.
                NSLog("Failed to read existing lock info; refusing unsafe replacement.")
                throw ProcessManagerError.anotherInstanceRunning
            }

            // Retry briefly: the terminated process can disappear from /proc
            // just before the kernel releases its final flock reference.
            close(lockFd)
            self.lockFd = open(lockFilePath, O_CREAT | O_RDWR, 0o600)
            guard self.lockFd != -1 else {
                throw ProcessManagerError.lockCreationFailed
            }
            var acquired = false
            for _ in 0..<20 {
                if flock(self.lockFd, LOCK_EX | LOCK_NB) == 0 {
                    acquired = true
                    break
                }
                usleep(50_000)
            }
            if !acquired {
                NSLog("Failed to acquire lock after terminating existing process.")
                close(self.lockFd)
                self.lockFd = -1
                throw ProcessManagerError.anotherInstanceRunning
            }
        }

        // write current process info
        writeLockFile()
    }

    private func readLockFile() -> (Int32, Bool)? {
        let capacity = 256
        lseek(lockFd, 0, SEEK_SET)
        let buffer = UnsafeMutablePointer<Int8>.allocate(capacity: capacity)
        buffer.initialize(repeating: 0, count: capacity)
        defer { buffer.deallocate() }
        // capacity - 1 because last byte should be 0
        let bytesRead = read(lockFd, buffer, capacity - 1)
        guard bytesRead > 0 else { return nil }
        let fullContent = String(cString: buffer)
        let lines = fullContent.components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespaces) }
        guard lines.count >= 2 else { return nil }
        let versionMatch = lines[1] == hazkeyVersion
        guard let pid = Int32(lines[0]) else { return nil }
        return (pid, versionMatch)
    }

    private func writeLockFile() {
        guard ftruncate(lockFd, 0) == 0 else {
            NSLog("Failed to truncate lock file")
            return
        }
        lseek(lockFd, 0, SEEK_SET)
        let info = "\(getpid())\n\(hazkeyVersion)\n"
        let written = write(lockFd, info, info.utf8.count)
        if written != info.utf8.count {
            NSLog("Failed to write complete lock file data")
        }
        fsync(lockFd)
    }

    private func terminateAnotherServer(pid: pid_t) throws {
        guard processVerifier.canTerminate(pid: pid) else {
            throw ProcessManagerError.terminationFailed
        }
        NSLog("Terminating existing server with PID \(pid)...")

        // Send SIGTERM to gracefully terminate
        if kill(pid, SIGTERM) != 0 { return }

        for attempt in 1...30 {  // 30 try * 0.1 sec
            usleep(100_000)  // 0.1 sec

            // Check if process is still running
            if kill(pid, 0) != 0 {
                NSLog("Existing server terminated successfully")
                return
            }

            if attempt == 15 {  // try SIGKILL
                guard processVerifier.canTerminate(pid: pid) else {
                    throw ProcessManagerError.terminationFailed
                }
                NSLog("Server didn't respond to SIGTERM, sending SIGKILL...")
                kill(pid, SIGKILL)
            }
        }

        // Final check
        if kill(pid, 0) == 0 {
            NSLog("Failed to terminate existing server")
            throw ProcessManagerError.terminationFailed
        }

        NSLog("Existing server terminated")
    }
}
