import Foundation

#if canImport(Glibc)
import Glibc
#elseif canImport(Musl)
import Musl
#endif

enum GrimodexRuntimeDirectoryError: Error {
    case createFailed(path: String, errno: Int32)
    case unsafeType(path: String)
    case wrongOwner(path: String, expected: uid_t, actual: uid_t)
    case unsafePermissions(path: String, mode: mode_t)
}

enum GrimodexRuntimeDirectory {
    static func prepare(at url: URL, uid: uid_t = getuid()) throws {
        let parent = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(
            at: parent,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )

        if mkdir(url.path, 0o700) != 0 && errno != EEXIST {
            throw GrimodexRuntimeDirectoryError.createFailed(path: url.path, errno: errno)
        }

        var info = stat()
        guard lstat(url.path, &info) == 0 else {
            throw GrimodexRuntimeDirectoryError.createFailed(path: url.path, errno: errno)
        }
        guard (info.st_mode & S_IFMT) == S_IFDIR else {
            throw GrimodexRuntimeDirectoryError.unsafeType(path: url.path)
        }
        guard info.st_uid == uid else {
            throw GrimodexRuntimeDirectoryError.wrongOwner(
                path: url.path,
                expected: uid,
                actual: info.st_uid
            )
        }
        let permissions = info.st_mode & 0o777
        guard permissions == 0o700 else {
            throw GrimodexRuntimeDirectoryError.unsafePermissions(
                path: url.path,
                mode: permissions
            )
        }
    }
}
