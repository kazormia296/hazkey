import Foundation
import KanaKanjiConverterModule
#if os(Linux)
import Glibc
#else
import Darwin
#endif

enum ABProbeError: LocalizedError, Equatable {
    case invalidArguments(String)
    case invalidCorpus(String)
    case dictionaryMissing(String)
    case dictionaryUnreadable(String)
    case mozcBundleInvalid(String)
    case candidateDrift(String)
    case backendInstability(String)
    case outputIsolationFailed(Int32)

    var errorDescription: String? {
        switch self {
        case .invalidArguments(let message), .invalidCorpus(let message):
            return message
        case .dictionaryMissing(let path):
            return "dictionary does not exist or is not a directory: \(path)"
        case .dictionaryUnreadable(let message):
            return message
        case .mozcBundleInvalid(let message),
             .candidateDrift(let message),
             .backendInstability(let message):
            return message
        case .outputIsolationFailed(let errorNumber):
            return "unable to isolate AB probe stdout (errno \(errorNumber))"
        }
    }
}

enum ABProbeConverterBackend: String, Equatable, Sendable {
    case hazkey
    case mozc
}

struct ABProbeOptions: Equatable {
    let corpusPath: String
    let dictionaryPath: String?
    let sourceRef: String
    let warmups: Int
    let iterations: Int
    let topK: Int
    let backendName: String
    let converterBackend: ABProbeConverterBackend
    let mozcBundlePath: String?

    static func parse(arguments: [String]) throws -> ABProbeOptions {
        guard let probeIndex = arguments.firstIndex(of: "--ab-probe") else {
            throw ABProbeError.invalidArguments("--ab-probe is required")
        }

        var corpusPath: String?
        var dictionaryPath: String?
        var sourceRef: String?
        var warmups = 2
        var iterations = 10
        var topK = 10
        var backendName: String?
        var converterBackend = ABProbeConverterBackend.hazkey
        var mozcBundlePath: String?
        var index = arguments.index(after: probeIndex)

        func value(after option: String) throws -> String {
            let valueIndex = arguments.index(after: index)
            guard valueIndex < arguments.endIndex else {
                throw ABProbeError.invalidArguments("\(option) requires a value")
            }
            index = valueIndex
            return arguments[valueIndex]
        }

        while index < arguments.endIndex {
            let option = arguments[index]
            switch option {
            case "--corpus":
                corpusPath = try value(after: option)
            case "--dictionary":
                dictionaryPath = try value(after: option)
            case "--source-ref":
                sourceRef = try value(after: option)
            case "--warmups":
                let raw = try value(after: option)
                guard let parsed = Int(raw), parsed >= 0 else {
                    throw ABProbeError.invalidArguments(
                        "--warmups must be a non-negative integer"
                    )
                }
                warmups = parsed
            case "--iterations":
                let raw = try value(after: option)
                guard let parsed = Int(raw), parsed > 0 else {
                    throw ABProbeError.invalidArguments(
                        "--iterations must be a positive integer"
                    )
                }
                iterations = parsed
            case "--top-k":
                let raw = try value(after: option)
                guard let parsed = Int(raw),
                      ConversionOptions.supportedSuggestionListLimits.contains(parsed)
                else {
                    throw ABProbeError.invalidArguments("--top-k must be between 1 and 10")
                }
                topK = parsed
            case "--backend-name":
                let value = try value(after: option)
                guard !value.isEmpty else {
                    throw ABProbeError.invalidArguments(
                        "--backend-name must not be empty"
                    )
                }
                backendName = value
            case "--converter-backend":
                let value = try value(after: option)
                guard let parsed = ABProbeConverterBackend(rawValue: value) else {
                    throw ABProbeError.invalidArguments(
                        "--converter-backend must be hazkey or mozc"
                    )
                }
                converterBackend = parsed
            case "--mozc-bundle":
                let value = try value(after: option)
                guard !value.isEmpty else {
                    throw ABProbeError.invalidArguments(
                        "--mozc-bundle must not be empty"
                    )
                }
                mozcBundlePath = value
            default:
                throw ABProbeError.invalidArguments("unknown AB probe option: \(option)")
            }
            index = arguments.index(after: index)
        }

        guard let corpusPath, !corpusPath.isEmpty else {
            throw ABProbeError.invalidArguments("--corpus is required")
        }
        guard let sourceRef, !sourceRef.isEmpty else {
            throw ABProbeError.invalidArguments("--source-ref is required")
        }
        switch converterBackend {
        case .hazkey:
            guard let dictionaryPath, !dictionaryPath.isEmpty else {
                throw ABProbeError.invalidArguments(
                    "--dictionary is required for the Hazkey backend"
                )
            }
            guard mozcBundlePath == nil else {
                throw ABProbeError.invalidArguments(
                    "--mozc-bundle requires --converter-backend mozc"
                )
            }
        case .mozc:
            guard let mozcBundlePath, !mozcBundlePath.isEmpty else {
                throw ABProbeError.invalidArguments(
                    "--mozc-bundle is required for the Mozc backend"
                )
            }
            guard dictionaryPath == nil else {
                throw ABProbeError.invalidArguments(
                    "--dictionary is not used by the Mozc backend"
                )
            }
        }
        return ABProbeOptions(
            corpusPath: corpusPath,
            dictionaryPath: dictionaryPath,
            sourceRef: sourceRef,
            warmups: warmups,
            iterations: iterations,
            topK: topK,
            backendName: backendName ?? converterBackend.rawValue,
            converterBackend: converterBackend,
            mozcBundlePath: mozcBundlePath
        )
    }
}

struct ABProbeCorpusCase: Equatable {
    let id: String
    let reading: String
    let category: String
}

enum ABProbeCorpus {
    static func load(path: String) throws -> [ABProbeCorpusCase] {
        let contents: String
        do {
            contents = try String(contentsOfFile: path, encoding: .utf8)
        } catch {
            throw ABProbeError.invalidCorpus("unable to read corpus \(path): \(error)")
        }
        let lines = contents.split(
            omittingEmptySubsequences: false,
            whereSeparator: \.isNewline
        )
        guard let rawHeader = lines.first else {
            throw ABProbeError.invalidCorpus("\(path): corpus is empty")
        }
        let header = rawHeader.split(separator: "\t", omittingEmptySubsequences: false)
            .map(String.init)
        guard let idIndex = header.firstIndex(of: "id"),
              let readingIndex = header.firstIndex(of: "reading"),
              let categoryIndex = header.firstIndex(of: "category")
        else {
            throw ABProbeError.invalidCorpus(
                "\(path): required columns are id, reading, and category"
            )
        }
        let maximumIndex = max(idIndex, readingIndex, categoryIndex)
        var seen = Set<String>()
        var result: [ABProbeCorpusCase] = []

        for (offset, rawLine) in lines.dropFirst().enumerated() {
            if rawLine.isEmpty { continue }
            let fields = rawLine.split(separator: "\t", omittingEmptySubsequences: false)
                .map(String.init)
            let lineNumber = offset + 2
            guard fields.count > maximumIndex else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): row has fewer columns than the header"
                )
            }
            let id = fields[idIndex]
            let reading = fields[readingIndex]
            let category = fields[categoryIndex]
            guard !id.isEmpty, !reading.isEmpty, !category.isEmpty else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): id, reading, and category must not be empty"
                )
            }
            guard seen.insert(id).inserted else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): duplicate id \(id)"
                )
            }
            result.append(ABProbeCorpusCase(id: id, reading: reading, category: category))
        }

        guard !result.isEmpty else {
            throw ABProbeError.invalidCorpus("\(path): corpus has no cases")
        }
        return result
    }
}

struct ABProbeResourceProvenance: Encodable, Equatable {
    let kind: String
    let path: String
    let fingerprint: String
}

struct ABProbeMozcArtifactIdentity: Equatable {
    let size: Int
    let sha256: String
}

struct ABProbeMozcTrustedArtifacts: Equatable {
    let helper: ABProbeMozcArtifactIdentity
    let data: ABProbeMozcArtifactIdentity

    static let fixed = ABProbeMozcTrustedArtifacts(
        helper: ABProbeMozcArtifactIdentity(
            size: 5_695_048,
            sha256: "8676275bb47aefe963c8b82047cc66fb7a5140caec72d1ebbfa17556b281577d"
        ),
        data: ABProbeMozcArtifactIdentity(
            size: 18_887_468,
            sha256: "b9884362e37772f772a0d28d1e12622455c14353497b3435deed60aa7e592c5e"
        )
    )
}

struct ABProbeMozcRuntimeSnapshot: Equatable {
    private static let helperName = "fcitx5-grimodex-mozc-helper"
    private static let dataName = "mozc.data"
    private static let manifestName = "manifest.json"
    private static let manifestSchema = "grimodex.mozc-artifact-bundle.v1"
    private static let maximumManifestSize = 64 * 1024

    let sourcePath: String
    let runtimePath: String
    let fingerprint: String

    static func prepare(
        sourceURL: URL,
        trustedArtifacts: ABProbeMozcTrustedArtifacts = .fixed
    ) throws -> ABProbeMozcRuntimeSnapshot {
        let standardizedSourceURL = sourceURL.standardizedFileURL
        let generationName = standardizedSourceURL.lastPathComponent
        let lowercaseHexCharacters = Set("0123456789abcdef")
        guard generationName.hasPrefix("sha256-"),
              generationName.count == 71,
              generationName.dropFirst(7).allSatisfy({
                  lowercaseHexCharacters.contains($0)
              }) else {
            throw ABProbeError.mozcBundleInvalid(
                "expected a content-addressed generation directory named sha256-<64 lowercase hex>"
            )
        }

        let directoryFD = open(
            standardizedSourceURL.path,
            O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW
        )
        guard directoryFD >= 0 else {
            throw ABProbeError.mozcBundleInvalid(
                "could not open Mozc generation without following symlinks: \(standardizedSourceURL.path)"
            )
        }
        defer { close(directoryFD) }

        var directoryMetadata = stat()
        guard fstat(directoryFD, &directoryMetadata) == 0,
              (directoryMetadata.st_mode & mode_t(S_IFMT)) == mode_t(S_IFDIR),
              Int(directoryMetadata.st_mode & 0o7777) == 0o755 else {
            throw ABProbeError.mozcBundleInvalid(
                "Mozc generation must be a real directory with mode 0755: \(standardizedSourceURL.path)"
            )
        }

        let helper = try readPinnedFile(
            directoryFD: directoryFD,
            name: helperName,
            expectedMode: 0o555,
            maximumSize: trustedArtifacts.helper.size
        )
        let data = try readPinnedFile(
            directoryFD: directoryFD,
            name: dataName,
            expectedMode: 0o444,
            maximumSize: trustedArtifacts.data.size
        )
        let manifest = try readPinnedFile(
            directoryFD: directoryFD,
            name: manifestName,
            expectedMode: 0o444,
            maximumSize: maximumManifestSize
        )

        try validateArtifact(helper, named: helperName, trusted: trustedArtifacts.helper)
        try validateArtifact(data, named: dataName, trusted: trustedArtifacts.data)
        try validateManifest(manifest, trustedArtifacts: trustedArtifacts)

        let runtimeURL = FileManager.default.temporaryDirectory.appendingPathComponent(
            "hazkey-ab-probe-mozc-\(UUID().uuidString)",
            isDirectory: true
        )
        var shouldRemoveRuntime = true
        do {
            try FileManager.default.createDirectory(
                at: runtimeURL,
                withIntermediateDirectories: false,
                attributes: [.posixPermissions: 0o700]
            )
            defer {
                if shouldRemoveRuntime {
                    try? remove(runtimePath: runtimeURL.path)
                }
            }

            try writePinnedFile(
                helper,
                to: runtimeURL.appendingPathComponent(helperName),
                mode: 0o555
            )
            try writePinnedFile(
                data,
                to: runtimeURL.appendingPathComponent(dataName),
                mode: 0o444
            )
            try writePinnedFile(
                manifest,
                to: runtimeURL.appendingPathComponent(manifestName),
                mode: 0o444
            )
            guard chmod(runtimeURL.path, 0o555) == 0 else {
                throw ABProbeError.mozcBundleInvalid(
                    "could not make Mozc runtime snapshot read-only: \(runtimeURL.path)"
                )
            }

            let fingerprint = try ABProbeDictionaryFingerprint.sha256(
                directoryURL: runtimeURL,
                domain: "hazkey.mozc-runtime-fingerprint.v1"
            )
            shouldRemoveRuntime = false
            return ABProbeMozcRuntimeSnapshot(
                sourcePath: standardizedSourceURL.resolvingSymlinksInPath().path,
                runtimePath: runtimeURL.path,
                fingerprint: fingerprint
            )
        } catch let error as ABProbeError {
            throw error
        } catch {
            throw ABProbeError.mozcBundleInvalid(
                "could not create pinned Mozc runtime snapshot: \(error.localizedDescription)"
            )
        }
    }

    static func remove(runtimePath: String) throws {
        guard chmod(runtimePath, 0o700) == 0 else {
            throw ABProbeError.backendInstability(
                "could not make Mozc runtime snapshot removable: \(runtimePath)"
            )
        }
        do {
            try FileManager.default.removeItem(atPath: runtimePath)
        } catch {
            throw ABProbeError.backendInstability(
                "could not remove Mozc runtime snapshot \(runtimePath): \(error.localizedDescription)"
            )
        }
    }

    private static func readPinnedFile(
        directoryFD: Int32,
        name: String,
        expectedMode: Int,
        maximumSize: Int
    ) throws -> Data {
        let fileFD = openat(directoryFD, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW)
        guard fileFD >= 0 else {
            throw ABProbeError.mozcBundleInvalid(
                "could not open required Mozc artifact without following symlinks: \(name)"
            )
        }
        defer { close(fileFD) }

        var metadata = stat()
        guard fstat(fileFD, &metadata) == 0,
              (metadata.st_mode & mode_t(S_IFMT)) == mode_t(S_IFREG),
              metadata.st_nlink == 1,
              Int(metadata.st_mode & 0o7777) == expectedMode,
              metadata.st_size >= 0,
              Int64(metadata.st_size) <= Int64(maximumSize) else {
            throw ABProbeError.mozcBundleInvalid(
                "Mozc artifact must be a non-hardlinked regular file with mode \(String(expectedMode, radix: 8)): \(name)"
            )
        }

        var result = Data()
        result.reserveCapacity(Int(metadata.st_size))
        var buffer = [UInt8](repeating: 0, count: 1024 * 1024)
        while true {
            let count = buffer.withUnsafeMutableBytes { bytes in
                read(fileFD, bytes.baseAddress, bytes.count)
            }
            if count == 0 {
                break
            }
            guard count > 0 else {
                throw ABProbeError.mozcBundleInvalid(
                    "could not read required Mozc artifact: \(name)"
                )
            }
            result.append(contentsOf: buffer.prefix(Int(count)))
            guard result.count <= maximumSize else {
                throw ABProbeError.mozcBundleInvalid(
                    "Mozc artifact is larger than expected: \(name)"
                )
            }
        }
        guard result.count == Int(metadata.st_size) else {
            throw ABProbeError.mozcBundleInvalid(
                "Mozc artifact changed while it was being read: \(name)"
            )
        }
        return result
    }

    private static func validateArtifact(
        _ data: Data,
        named name: String,
        trusted: ABProbeMozcArtifactIdentity
    ) throws {
        guard data.count == trusted.size, digest(data) == trusted.sha256 else {
            throw ABProbeError.mozcBundleInvalid(
                "Mozc artifact identity mismatch for \(name)"
            )
        }
    }

    private static func validateManifest(
        _ data: Data,
        trustedArtifacts: ABProbeMozcTrustedArtifacts
    ) throws {
        let object: Any
        do {
            object = try JSONSerialization.jsonObject(with: data)
        } catch {
            throw ABProbeError.mozcBundleInvalid("Mozc artifact manifest is not valid JSON")
        }
        guard let manifest = object as? [String: Any],
              manifest["schema"] as? String == manifestSchema,
              let artifacts = manifest["artifacts"] as? [String: Any],
              Set(artifacts.keys) == Set([helperName, dataName]) else {
            throw ABProbeError.mozcBundleInvalid(
                "Mozc artifact manifest schema or artifact set is invalid"
            )
        }
        try validateManifestArtifact(
            artifacts[helperName],
            named: helperName,
            trusted: trustedArtifacts.helper
        )
        try validateManifestArtifact(
            artifacts[dataName],
            named: dataName,
            trusted: trustedArtifacts.data
        )
    }

    private static func validateManifestArtifact(
        _ value: Any?,
        named name: String,
        trusted: ABProbeMozcArtifactIdentity
    ) throws {
        guard let artifact = value as? [String: Any],
              artifact["size"] as? Int == trusted.size,
              artifact["sha256"] as? String == trusted.sha256 else {
            throw ABProbeError.mozcBundleInvalid(
                "Mozc artifact manifest identity mismatch for \(name)"
            )
        }
    }

    private static func digest(_ data: Data) -> String {
        var hasher = ABProbeSHA256()
        hasher.update(data)
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    private static func writePinnedFile(_ data: Data, to url: URL, mode: mode_t) throws {
        try data.write(to: url, options: .atomic)
        guard chmod(url.path, mode) == 0 else {
            throw ABProbeError.mozcBundleInvalid(
                "could not set Mozc runtime mode for \(url.lastPathComponent)"
            )
        }
    }
}

struct ABProbeProvenance: Equatable {
    let sourceRef: String
    let resource: ABProbeResourceProvenance
    let mozcRuntimePath: String?

    static func resolve(
        options: ABProbeOptions,
        trustedMozcArtifacts: ABProbeMozcTrustedArtifacts = .fixed
    ) throws -> ABProbeProvenance {
        switch options.converterBackend {
        case .hazkey:
            guard let dictionaryPath = options.dictionaryPath else {
                throw ABProbeError.invalidArguments(
                    "--dictionary is required for the Hazkey backend"
                )
            }
            let dictionaryURL = URL(fileURLWithPath: dictionaryPath)
                .standardizedFileURL
                .resolvingSymlinksInPath()
            var isDirectory = ObjCBool(false)
            guard FileManager.default.fileExists(
                atPath: dictionaryURL.path,
                isDirectory: &isDirectory
            ), isDirectory.boolValue else {
                throw ABProbeError.dictionaryMissing(dictionaryURL.path)
            }
            return ABProbeProvenance(
                sourceRef: options.sourceRef,
                resource: ABProbeResourceProvenance(
                    kind: "hazkey_dictionary",
                    path: dictionaryURL.path,
                    fingerprint: try ABProbeDictionaryFingerprint.sha256(
                        directoryURL: dictionaryURL
                    )
                ),
                mozcRuntimePath: nil
            )
        case .mozc:
            guard let bundlePath = options.mozcBundlePath else {
                throw ABProbeError.invalidArguments(
                    "--mozc-bundle is required for the Mozc backend"
                )
            }
            let runtime = try ABProbeMozcRuntimeSnapshot.prepare(
                sourceURL: URL(fileURLWithPath: bundlePath, isDirectory: true),
                trustedArtifacts: trustedMozcArtifacts
            )
            return ABProbeProvenance(
                sourceRef: options.sourceRef,
                resource: ABProbeResourceProvenance(
                    kind: "mozc_runtime_inputs",
                    path: runtime.sourcePath,
                    fingerprint: runtime.fingerprint
                ),
                mozcRuntimePath: runtime.runtimePath
            )
        }
    }
}

enum ABProbeDictionaryFingerprint {
    private struct Entry {
        let pathBytes: [UInt8]
        let url: URL
    }

    static func sha256(
        directoryURL: URL,
        domain: String = "hazkey.dictionary-fingerprint.v1"
    ) throws -> String {
        let fileManager = FileManager.default
        var enumerationError: Error?
        guard let enumerator = fileManager.enumerator(
            at: directoryURL,
            includingPropertiesForKeys: [.isRegularFileKey],
            options: [],
            errorHandler: { _, error in
                enumerationError = error
                return false
            }
        ) else {
            throw ABProbeError.dictionaryUnreadable(
                "unable to enumerate dictionary: \(directoryURL.path)"
            )
        }

        let rootPrefix = directoryURL.path.hasSuffix("/")
            ? directoryURL.path
            : directoryURL.path + "/"
        var entries: [Entry] = []
        while let url = enumerator.nextObject() as? URL {
            let values: URLResourceValues
            do {
                values = try url.resourceValues(forKeys: [.isRegularFileKey])
            } catch {
                throw ABProbeError.dictionaryUnreadable(
                    "unable to inspect dictionary entry \(url.path): \(error)"
                )
            }
            guard values.isRegularFile == true else { continue }
            guard url.path.hasPrefix(rootPrefix) else {
                throw ABProbeError.dictionaryUnreadable(
                    "dictionary entry escaped its root: \(url.path)"
                )
            }
            let relativePath = String(url.path.dropFirst(rootPrefix.count))
            entries.append(
                Entry(
                    pathBytes: Array(relativePath.utf8),
                    url: url
                )
            )
        }
        if let enumerationError {
            throw ABProbeError.dictionaryUnreadable(
                "unable to enumerate dictionary \(directoryURL.path): \(enumerationError)"
            )
        }

        entries.sort { lhs, rhs in
            lhs.pathBytes.lexicographicallyPrecedes(rhs.pathBytes)
        }
        var directoryHasher = ABProbeSHA256()
        directoryHasher.update(Data("\(domain)\0".utf8))
        for entry in entries {
            var fileHasher = ABProbeSHA256()
            let handle: FileHandle
            do {
                handle = try FileHandle(forReadingFrom: entry.url)
            } catch {
                throw ABProbeError.dictionaryUnreadable(
                    "unable to read dictionary entry \(entry.url.path): \(error)"
                )
            }
            defer { try? handle.close() }
            do {
                while let data = try handle.read(upToCount: 1_048_576), !data.isEmpty {
                    fileHasher.update(data)
                }
            } catch {
                throw ABProbeError.dictionaryUnreadable(
                    "unable to read dictionary entry \(entry.url.path): \(error)"
                )
            }

            directoryHasher.update(Data([0x01]))
            directoryHasher.update(encodedUInt64(UInt64(entry.pathBytes.count)))
            directoryHasher.update(Data(entry.pathBytes))
            directoryHasher.update(Data(fileHasher.finalize()))
        }
        return "sha256:" + directoryHasher.finalize().map {
            String(format: "%02x", $0)
        }.joined()
    }

    private static func encodedUInt64(_ value: UInt64) -> Data {
        Data((0..<8).map { shift in
            UInt8(truncatingIfNeeded: value >> UInt64((7 - shift) * 8))
        })
    }
}

struct ABProbeSHA256 {
    private static let roundConstants: [UInt32] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
        0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
        0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
        0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
        0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
        0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
        0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
        0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    ]

    private var state: [UInt32] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
        0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    ]
    private var buffered: [UInt8] = []
    private var byteCount: UInt64 = 0

    mutating func update(_ data: Data) {
        guard !data.isEmpty else { return }
        byteCount &+= UInt64(data.count)
        var bytes = buffered
        bytes.append(contentsOf: data)
        var offset = 0
        while offset + 64 <= bytes.count {
            processBlock(bytes, offset: offset)
            offset += 64
        }
        buffered = Array(bytes[offset...])
    }

    mutating func finalize() -> [UInt8] {
        let bitCount = byteCount &* 8
        var finalBytes = buffered
        finalBytes.append(0x80)
        while finalBytes.count % 64 != 56 {
            finalBytes.append(0)
        }
        finalBytes.append(contentsOf: (0..<8).map { shift in
            UInt8(truncatingIfNeeded: bitCount >> UInt64((7 - shift) * 8))
        })
        var offset = 0
        while offset < finalBytes.count {
            processBlock(finalBytes, offset: offset)
            offset += 64
        }
        return state.flatMap { word in
            [
                UInt8(truncatingIfNeeded: word >> 24),
                UInt8(truncatingIfNeeded: word >> 16),
                UInt8(truncatingIfNeeded: word >> 8),
                UInt8(truncatingIfNeeded: word),
            ]
        }
    }

    private mutating func processBlock(_ block: [UInt8], offset: Int) {
        var words = [UInt32](repeating: 0, count: 64)
        for index in 0..<16 {
            let base = offset + index * 4
            words[index] = UInt32(block[base]) << 24
                | UInt32(block[base + 1]) << 16
                | UInt32(block[base + 2]) << 8
                | UInt32(block[base + 3])
        }
        for index in 16..<64 {
            let s0 = rotateRight(words[index - 15], by: 7)
                ^ rotateRight(words[index - 15], by: 18)
                ^ (words[index - 15] >> 3)
            let s1 = rotateRight(words[index - 2], by: 17)
                ^ rotateRight(words[index - 2], by: 19)
                ^ (words[index - 2] >> 10)
            words[index] = words[index - 16] &+ s0 &+ words[index - 7] &+ s1
        }

        var a = state[0]
        var b = state[1]
        var c = state[2]
        var d = state[3]
        var e = state[4]
        var f = state[5]
        var g = state[6]
        var h = state[7]
        for index in 0..<64 {
            let sum1 = rotateRight(e, by: 6) ^ rotateRight(e, by: 11)
                ^ rotateRight(e, by: 25)
            let choice = (e & f) ^ ((~e) & g)
            let temporary1 = h &+ sum1 &+ choice
                &+ Self.roundConstants[index] &+ words[index]
            let sum0 = rotateRight(a, by: 2) ^ rotateRight(a, by: 13)
                ^ rotateRight(a, by: 22)
            let majority = (a & b) ^ (a & c) ^ (b & c)
            let temporary2 = sum0 &+ majority
            h = g
            g = f
            f = e
            e = d &+ temporary1
            d = c
            c = b
            b = a
            a = temporary1 &+ temporary2
        }
        state[0] &+= a
        state[1] &+= b
        state[2] &+= c
        state[3] &+= d
        state[4] &+= e
        state[5] &+= f
        state[6] &+= g
        state[7] &+= h
    }

    private func rotateRight(_ value: UInt32, by amount: UInt32) -> UInt32 {
        (value >> amount) | (value << (32 - amount))
    }
}

enum ABProbeJSONOutput {
    static func publishBuffered(
        _ lines: [Data],
        to output: FileHandle,
        afterSuccessfulCleanup cleanup: () throws -> Void
    ) throws {
        try cleanup()
        for line in lines {
            output.write(line)
        }
    }

    static func withIsolatedStandardOutput<T>(
        _ body: (FileHandle) throws -> T
    ) throws -> T {
        _ = fflush(nil)
        let savedStandardOutput = dup(STDOUT_FILENO)
        guard savedStandardOutput >= 0 else {
            throw ABProbeError.outputIsolationFailed(errno)
        }
        guard dup2(STDERR_FILENO, STDOUT_FILENO) >= 0 else {
            let errorNumber = errno
            _ = close(savedStandardOutput)
            throw ABProbeError.outputIsolationFailed(errorNumber)
        }

        let jsonOutput = FileHandle(
            fileDescriptor: savedStandardOutput,
            closeOnDealloc: false
        )
        let result: Result<T, Error> = Result {
            try body(jsonOutput)
        }
        _ = fflush(nil)
        let restoreResult = dup2(savedStandardOutput, STDOUT_FILENO)
        let restoreError = errno
        _ = close(savedStandardOutput)
        guard restoreResult >= 0 else {
            throw ABProbeError.outputIsolationFailed(restoreError)
        }
        return try result.get()
    }
}

struct ABProbeLatency: Encodable, Equatable {
    let median: Double
    let p95: Double
    let minimum: Double
    let maximum: Double
    let samples: [Double]

    static func summarize(_ samples: [Double]) -> ABProbeLatency {
        precondition(!samples.isEmpty)
        let sorted = samples.sorted()
        let middle = sorted.count / 2
        let median = sorted.count.isMultiple(of: 2)
            ? (sorted[middle - 1] + sorted[middle]) / 2
            : sorted[middle]
        let p95Index = min(
            sorted.count - 1,
            max(0, Int(ceil(Double(sorted.count) * 0.95)) - 1)
        )
        return ABProbeLatency(
            median: median,
            p95: sorted[p95Index],
            minimum: sorted[0],
            maximum: sorted[sorted.count - 1],
            samples: samples
        )
    }
}

struct ABProbeMemory: Encodable {
    let beforeKiB: Int?
    let afterKiB: Int?
    let beforePssKiB: Int?
    let afterPssKiB: Int?
    let backendBeforeKiB: Int?
    let backendAfterKiB: Int?
    let backendBeforePssKiB: Int?
    let backendAfterPssKiB: Int?

    init(
        beforeKiB: Int?,
        afterKiB: Int?,
        beforePssKiB: Int? = nil,
        afterPssKiB: Int? = nil,
        backendBeforeKiB: Int? = nil,
        backendAfterKiB: Int? = nil,
        backendBeforePssKiB: Int? = nil,
        backendAfterPssKiB: Int? = nil
    ) {
        self.beforeKiB = beforeKiB
        self.afterKiB = afterKiB
        self.beforePssKiB = beforePssKiB
        self.afterPssKiB = afterPssKiB
        self.backendBeforeKiB = backendBeforeKiB
        self.backendAfterKiB = backendAfterKiB
        self.backendBeforePssKiB = backendBeforePssKiB
        self.backendAfterPssKiB = backendAfterPssKiB
    }

    private enum CodingKeys: String, CodingKey {
        case beforeKiB = "before_kib"
        case afterKiB = "after_kib"
        case beforePssKiB = "before_pss_kib"
        case afterPssKiB = "after_pss_kib"
        case backendBeforeKiB = "backend_before_kib"
        case backendAfterKiB = "backend_after_kib"
        case backendBeforePssKiB = "backend_before_pss_kib"
        case backendAfterPssKiB = "backend_after_pss_kib"
    }
}

struct ABProbeBackendDiagnosticsResult: Encodable {
    let processLaunchCount: UInt64?
    let cleanupFailureCount: UInt64?

    private enum CodingKeys: String, CodingKey {
        case processLaunchCount = "process_launch_count"
        case cleanupFailureCount = "cleanup_failure_count"
    }
}

struct ABProbeMeasurement: Encodable {
    let warmups: Int
    let iterations: Int
    let latencyMilliseconds: ABProbeLatency
    let residentMemory: ABProbeMemory
    let backendDiagnostics: ABProbeBackendDiagnosticsResult

    private enum CodingKeys: String, CodingKey {
        case warmups
        case iterations
        case latencyMilliseconds = "latency_ms"
        case residentMemory = "rss"
        case backendDiagnostics = "backend_diagnostics"
    }
}

struct ABProbeResult: Encodable {
    let schema = "hazkey.ab-probe-result.v2"
    let id: String
    let category: String
    let backend: String
    let backendVersion: String
    let converterBackend: String
    let sourceRef: String
    let resource: ABProbeResourceProvenance
    let candidates: [String]
    let measurement: ABProbeMeasurement

    private enum CodingKeys: String, CodingKey {
        case schema
        case id
        case category
        case backend
        case backendVersion = "backend_version"
        case converterBackend = "converter_backend"
        case sourceRef = "source_ref"
        case resource
        case candidates
        case measurement
    }
}

enum ABProbeCommand {
    static var isRequested: Bool {
        CommandLine.arguments.contains("--ab-probe")
    }

    static func run(arguments: [String] = CommandLine.arguments) throws {
        let options = try ABProbeOptions.parse(arguments: arguments)
        try ABProbeJSONOutput.withIsolatedStandardOutput { jsonOutput in
            try run(options: options, jsonOutput: jsonOutput)
        }
    }

    private static func run(
        options: ABProbeOptions,
        jsonOutput: FileHandle
    ) throws {
        let provenance = try ABProbeProvenance.resolve(options: options)
        var didRemoveMozcRuntime = false
        defer {
            if let runtimePath = provenance.mozcRuntimePath,
               !didRemoveMozcRuntime {
                try? ABProbeMozcRuntimeSnapshot.remove(runtimePath: runtimePath)
            }
        }
        let cases = try ABProbeCorpus.load(path: options.corpusPath)
        let adapter: any KanaKanjiConverting
        let diagnosticsProvider: () -> MozcSidecarDiagnostics?
        switch options.converterBackend {
        case .hazkey:
            let config = HazkeyServerConfig(
                zenzaiBackendDevicesProvider: { [] },
                zenzaiModelPathProvider: { nil },
                zenzaiBackendAvailableOverride: false
            )
            let dictionaryURL = URL(
                fileURLWithPath: provenance.resource.path,
                isDirectory: true
            )
            let store = DicdataStore(dictionaryURL: dictionaryURL)
            let converter = KanaKanjiConverter(dicdataStore: store)
            let boundaryConverter = KanaKanjiConverter(dicdataStore: store)
            var requestOptions = config.genBaseConvertRequestOptions()
            requestOptions.N_best = options.topK
            requestOptions.needTypoCorrection = false
            requestOptions.requireJapanesePrediction = .disabled
            requestOptions.requireEnglishPrediction = .disabled
            requestOptions.englishCandidateInRoman2KanaInput = false
            requestOptions.fullWidthRomanCandidate = false
            requestOptions.halfWidthKanaCandidate = false
            requestOptions.learningType = .nothing
            requestOptions.shouldResetMemory = false
            requestOptions.specialCandidateProviders = []
            requestOptions.zenzaiMode = .off
            adapter = HazkeyKanaKanjiConverterAdapter(
                converter: converter,
                boundaryConverter: boundaryConverter,
                optionsProvider: { _ in requestOptions },
                projectDictionaryIndexProvider: { .empty }
            )
            diagnosticsProvider = { nil }
        case .mozc:
            guard let runtimePath = provenance.mozcRuntimePath else {
                throw ABProbeError.backendInstability(
                    "Mozc runtime snapshot was not prepared"
                )
            }
            let bundleURL = URL(
                fileURLWithPath: runtimePath,
                isDirectory: true
            )
            let core = MozcSidecarClient(
                helperPath: bundleURL.appendingPathComponent(
                    "fcitx5-grimodex-mozc-helper",
                    isDirectory: false
                ).path,
                dataPath: bundleURL.appendingPathComponent(
                    "mozc.data",
                    isDirectory: false
                ).path,
                timeoutMilliseconds: 10_000
            )
            adapter = MozcKanaKanjiConverterAdapter(core: core)
            diagnosticsProvider = { core.diagnostics() }
        }
        var didPurge = false
        defer {
            if !didPurge {
                adapter.purgeSensitiveState()
            }
        }
        let conversionOptions = ConversionOptions(
            allowLearning: false,
            zenzaiEnabled: false,
            leftContext: "",
            rightContext: "",
            suggestionListMode: .normal,
            suggestionListLimit: options.topK
        )
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        var bufferedJSONLines: [Data] = []
        bufferedJSONLines.reserveCapacity(cases.count)

        for testCase in cases {
            let elements = testCase.reading.map {
                CompositionElement(text: String($0), inputStyle: .direct)
            }
            let composition = CompositionInput(
                elements: elements,
                cursor: elements.count,
                leftContext: ""
            )

            for _ in 0..<options.warmups {
                adapter.stopComposition()
                _ = try adapter.candidates(
                    for: composition,
                    options: conversionOptions
                )
            }
            let diagnosticsBefore = diagnosticsProvider()
            let processBefore = processMemoryKilobytes(processIdentifier: getpid())
            let backendBefore = diagnosticsBefore?.processIdentifier.flatMap {
                processMemoryKilobytes(processIdentifier: $0)
            }

            var samples: [Double] = []
            var finalCandidates: [String]?
            samples.reserveCapacity(options.iterations)
            for _ in 0..<options.iterations {
                adapter.stopComposition()
                let started = DispatchTime.now().uptimeNanoseconds
                let output = try adapter.candidates(
                    for: composition,
                    options: conversionOptions
                )
                let finished = DispatchTime.now().uptimeNanoseconds
                samples.append(Double(finished - started) / 1_000_000)
                let observed = Array(
                    output.candidates.prefix(options.topK).map(\.text)
                )
                if let finalCandidates, finalCandidates != observed {
                    throw ABProbeError.candidateDrift(
                        "candidate output drifted during case \(testCase.id)"
                    )
                }
                finalCandidates = observed
            }
            let diagnosticsAfter = diagnosticsProvider()
            if options.converterBackend == .mozc {
                guard diagnosticsAfter?.processIdentifier != nil,
                      diagnosticsAfter?.processLaunchCount == 1,
                      diagnosticsAfter?.temporaryDirectoryCleanupFailureCount == 0
                else {
                    throw ABProbeError.backendInstability(
                        "Mozc helper did not remain a single healthy process"
                    )
                }
            }
            let processAfter = processMemoryKilobytes(processIdentifier: getpid())
            let backendAfter = diagnosticsAfter?.processIdentifier.flatMap {
                processMemoryKilobytes(processIdentifier: $0)
            }
            adapter.stopComposition()
            let result = ABProbeResult(
                id: testCase.id,
                category: testCase.category,
                backend: options.backendName,
                backendVersion: hazkeyVersion,
                converterBackend: options.converterBackend.rawValue,
                sourceRef: provenance.sourceRef,
                resource: provenance.resource,
                candidates: finalCandidates ?? [],
                measurement: ABProbeMeasurement(
                    warmups: options.warmups,
                    iterations: options.iterations,
                    latencyMilliseconds: ABProbeLatency.summarize(samples),
                    residentMemory: ABProbeMemory(
                        beforeKiB: processBefore.rssKiB,
                        afterKiB: processAfter.rssKiB,
                        beforePssKiB: processBefore.pssKiB,
                        afterPssKiB: processAfter.pssKiB,
                        backendBeforeKiB: backendBefore?.rssKiB,
                        backendAfterKiB: backendAfter?.rssKiB,
                        backendBeforePssKiB: backendBefore?.pssKiB,
                        backendAfterPssKiB: backendAfter?.pssKiB
                    ),
                    backendDiagnostics: ABProbeBackendDiagnosticsResult(
                        processLaunchCount: diagnosticsAfter?.processLaunchCount,
                        cleanupFailureCount: diagnosticsAfter?
                            .temporaryDirectoryCleanupFailureCount
                    )
                )
            )
            var encoded = try encoder.encode(result)
            encoded.append(0x0A)
            bufferedJSONLines.append(encoded)
        }
        try ABProbeJSONOutput.publishBuffered(
            bufferedJSONLines,
            to: jsonOutput
        ) {
            adapter.purgeSensitiveState()
            didPurge = true
            if let diagnostics = diagnosticsProvider(),
               diagnostics.processIdentifier != nil
                || diagnostics.temporaryDirectoryCleanupFailureCount != 0 {
                throw ABProbeError.backendInstability(
                    "Mozc helper cleanup did not complete cleanly"
                )
            }
            if let runtimePath = provenance.mozcRuntimePath {
                try ABProbeMozcRuntimeSnapshot.remove(runtimePath: runtimePath)
                didRemoveMozcRuntime = true
            }
        }
    }

    private struct ProcessMemory {
        let rssKiB: Int?
        let pssKiB: Int?
    }

    private static func processMemoryKilobytes(
        processIdentifier: Int32
    ) -> ProcessMemory {
        ProcessMemory(
            rssKiB: memoryKilobytes(
                path: "/proc/\(processIdentifier)/status",
                field: "VmRSS:"
            ),
            pssKiB: memoryKilobytes(
                path: "/proc/\(processIdentifier)/smaps_rollup",
                field: "Pss:"
            )
        )
    }

    private static func memoryKilobytes(path: String, field: String) -> Int? {
        guard let contents = try? String(contentsOfFile: path, encoding: .utf8),
              let line = contents.split(separator: "\n").first(where: {
                  $0.hasPrefix(field)
              }) else {
            return nil
        }
        return line.split(whereSeparator: \.isWhitespace).dropFirst().first.flatMap {
            Int($0)
        }
    }
}
