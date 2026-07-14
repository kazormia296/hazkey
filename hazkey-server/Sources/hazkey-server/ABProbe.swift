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
    case outputIsolationFailed(Int32)

    var errorDescription: String? {
        switch self {
        case .invalidArguments(let message), .invalidCorpus(let message):
            return message
        case .dictionaryMissing(let path):
            return "dictionary does not exist or is not a directory: \(path)"
        case .dictionaryUnreadable(let message):
            return message
        case .outputIsolationFailed(let errorNumber):
            return "unable to isolate AB probe stdout (errno \(errorNumber))"
        }
    }
}

struct ABProbeOptions: Equatable {
    let corpusPath: String
    let dictionaryPath: String
    let sourceRef: String
    let warmups: Int
    let iterations: Int
    let topK: Int
    let backendName: String

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
        var backendName = "hazkey"
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
                backendName = try value(after: option)
                guard !backendName.isEmpty else {
                    throw ABProbeError.invalidArguments(
                        "--backend-name must not be empty"
                    )
                }
            default:
                throw ABProbeError.invalidArguments("unknown AB probe option: \(option)")
            }
            index = arguments.index(after: index)
        }

        guard let corpusPath, !corpusPath.isEmpty else {
            throw ABProbeError.invalidArguments("--corpus is required")
        }
        guard let dictionaryPath, !dictionaryPath.isEmpty else {
            throw ABProbeError.invalidArguments("--dictionary is required")
        }
        guard let sourceRef, !sourceRef.isEmpty else {
            throw ABProbeError.invalidArguments("--source-ref is required")
        }
        return ABProbeOptions(
            corpusPath: corpusPath,
            dictionaryPath: dictionaryPath,
            sourceRef: sourceRef,
            warmups: warmups,
            iterations: iterations,
            topK: topK,
            backendName: backendName
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

struct ABProbeProvenance: Equatable {
    let sourceRef: String
    let dictionaryPath: String
    let dictionaryFingerprint: String

    static func resolve(options: ABProbeOptions) throws -> ABProbeProvenance {
        let dictionaryURL = URL(fileURLWithPath: options.dictionaryPath)
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
            dictionaryPath: dictionaryURL.path,
            dictionaryFingerprint: try ABProbeDictionaryFingerprint.sha256(
                directoryURL: dictionaryURL
            )
        )
    }
}

enum ABProbeDictionaryFingerprint {
    private struct Entry {
        let pathBytes: [UInt8]
        let url: URL
    }

    static func sha256(directoryURL: URL) throws -> String {
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
        directoryHasher.update(Data("hazkey.dictionary-fingerprint.v1\0".utf8))
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

    private enum CodingKeys: String, CodingKey {
        case beforeKiB = "before_kib"
        case afterKiB = "after_kib"
    }
}

struct ABProbeMeasurement: Encodable {
    let warmups: Int
    let iterations: Int
    let latencyMilliseconds: ABProbeLatency
    let residentMemory: ABProbeMemory

    private enum CodingKeys: String, CodingKey {
        case warmups
        case iterations
        case latencyMilliseconds = "latency_ms"
        case residentMemory = "rss"
    }
}

struct ABProbeResult: Encodable {
    let schema = "hazkey.ab-probe-result.v1"
    let id: String
    let category: String
    let backend: String
    let backendVersion: String
    let sourceRef: String
    let dictionaryPath: String
    let dictionaryFingerprint: String
    let candidates: [String]
    let measurement: ABProbeMeasurement

    private enum CodingKeys: String, CodingKey {
        case schema
        case id
        case category
        case backend
        case backendVersion = "backend_version"
        case sourceRef = "source_ref"
        case dictionaryPath = "dictionary_path"
        case dictionaryFingerprint = "dictionary_fingerprint"
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
        let cases = try ABProbeCorpus.load(path: options.corpusPath)
        let config = HazkeyServerConfig(
            zenzaiBackendDevicesProvider: { [] },
            zenzaiModelPathProvider: { nil },
            zenzaiBackendAvailableOverride: false
        )
        let dictionaryURL = URL(
            fileURLWithPath: provenance.dictionaryPath,
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

        let adapter = HazkeyKanaKanjiConverterAdapter(
            converter: converter,
            boundaryConverter: boundaryConverter,
            optionsProvider: { _ in requestOptions },
            projectDictionaryIndexProvider: { .empty }
        )
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

        for testCase in cases {
            let elements = testCase.reading.map {
                CompositionElement(text: String($0), inputStyle: .direct)
            }
            let composition = CompositionInput(
                elements: elements,
                cursor: elements.count,
                leftContext: ""
            )
            let rssBefore = residentMemoryKilobytes()

            for _ in 0..<options.warmups {
                adapter.stopComposition()
                _ = try adapter.candidates(
                    for: composition,
                    options: conversionOptions
                )
            }

            var samples: [Double] = []
            var finalCandidates: [String] = []
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
                finalCandidates = Array(output.candidates.prefix(options.topK).map(\.text))
            }
            adapter.stopComposition()
            let result = ABProbeResult(
                id: testCase.id,
                category: testCase.category,
                backend: options.backendName,
                backendVersion: hazkeyVersion,
                sourceRef: provenance.sourceRef,
                dictionaryPath: provenance.dictionaryPath,
                dictionaryFingerprint: provenance.dictionaryFingerprint,
                candidates: finalCandidates,
                measurement: ABProbeMeasurement(
                    warmups: options.warmups,
                    iterations: options.iterations,
                    latencyMilliseconds: ABProbeLatency.summarize(samples),
                    residentMemory: ABProbeMemory(
                        beforeKiB: rssBefore,
                        afterKiB: residentMemoryKilobytes()
                    )
                )
            )
            var encoded = try encoder.encode(result)
            encoded.append(0x0A)
            jsonOutput.write(encoded)
        }
    }

    private static func residentMemoryKilobytes() -> Int? {
        guard let status = try? String(
            contentsOfFile: "/proc/self/status",
            encoding: .utf8
        ),
            let line = status.split(separator: "\n").first(where: {
                $0.hasPrefix("VmRSS:")
            })
        else {
            return nil
        }
        return line.split(whereSeparator: \.isWhitespace).dropFirst().first.flatMap {
            Int($0)
        }
    }
}
