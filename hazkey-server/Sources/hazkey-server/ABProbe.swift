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

enum ABProbeBoundaryMode: String, Equatable, Sendable {
    case isolatedDictionary = "isolated_dictionary"
    case nativeZenzaiFirstClause = "native_zenzai_first_clause"
    case mozcFixed = "mozc_fixed"
    case fullComposition = "full_composition"
}

enum ABProbeResultSchema: String, Equatable, Sendable {
    case v3
    case v4
    case v5
    case v6
    case v7

    var conversionPath: ABProbeConversionPath {
        switch self {
        case .v3:
            .candidates
        case .v4, .v5, .v6, .v7:
            .segmentCandidates
        }
    }
}

enum ABProbeConversionPath: String, Equatable, Sendable {
    case candidates
    case segmentCandidates = "segment_candidates"
    case nativeSegmentCandidates = "native_segment_candidates"
    case mozcFixedSegmentCandidates = "mozc_fixed_segment_candidates"
    case fullCompositionCandidates = "full_composition_candidates"
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
    let resultSchema: ABProbeResultSchema
    let zenzaiModelPath: String?
    let zenzaiInferenceLimit: Int?
    let zenzaiDevice: String?
    let leftContextsPath: String?
    let mozcFixedBoundariesPath: String?
    let boundaryMode: ABProbeBoundaryMode

    init(
        corpusPath: String,
        dictionaryPath: String?,
        sourceRef: String,
        warmups: Int,
        iterations: Int,
        topK: Int,
        backendName: String,
        converterBackend: ABProbeConverterBackend,
        mozcBundlePath: String?,
        resultSchema: ABProbeResultSchema = .v3,
        zenzaiModelPath: String? = nil,
        zenzaiInferenceLimit: Int? = nil,
        zenzaiDevice: String? = nil,
        leftContextsPath: String? = nil,
        mozcFixedBoundariesPath: String? = nil,
        boundaryMode: ABProbeBoundaryMode = .isolatedDictionary
    ) {
        self.corpusPath = corpusPath
        self.dictionaryPath = dictionaryPath
        self.sourceRef = sourceRef
        self.warmups = warmups
        self.iterations = iterations
        self.topK = topK
        self.backendName = backendName
        self.converterBackend = converterBackend
        self.mozcBundlePath = mozcBundlePath
        self.resultSchema = resultSchema
        self.zenzaiModelPath = zenzaiModelPath
        self.zenzaiInferenceLimit = zenzaiInferenceLimit
        self.zenzaiDevice = zenzaiDevice
        self.leftContextsPath = leftContextsPath
        self.mozcFixedBoundariesPath = mozcFixedBoundariesPath
        self.boundaryMode = boundaryMode
    }

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
        var resultSchema = ABProbeResultSchema.v3
        var zenzaiModelPath: String?
        var zenzaiInferenceLimit: Int?
        var zenzaiDevice: String?
        var leftContextsPath: String?
        var mozcFixedBoundariesPath: String?
        var boundaryMode = ABProbeBoundaryMode.isolatedDictionary
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
            case "--result-schema":
                let value = try value(after: option)
                guard let parsed = ABProbeResultSchema(rawValue: value) else {
                    throw ABProbeError.invalidArguments(
                        "--result-schema must be v3, v4, v5, v6, or v7"
                    )
                }
                resultSchema = parsed
            case "--zenzai-model":
                let value = try value(after: option)
                guard !value.isEmpty else {
                    throw ABProbeError.invalidArguments(
                        "--zenzai-model must not be empty"
                    )
                }
                zenzaiModelPath = value
            case "--zenzai-inference-limit":
                let raw = try value(after: option)
                guard let parsed = Int(raw),
                      parsed > 0,
                      parsed <= Int(Int32.max)
                else {
                    throw ABProbeError.invalidArguments(
                        "--zenzai-inference-limit must be between 1 and \(Int32.max)"
                    )
                }
                zenzaiInferenceLimit = parsed
            case "--zenzai-device":
                let value = try value(after: option)
                guard !value.isEmpty else {
                    throw ABProbeError.invalidArguments(
                        "--zenzai-device must not be empty"
                    )
                }
                zenzaiDevice = value
            case "--left-contexts":
                let value = try value(after: option)
                guard !value.isEmpty else {
                    throw ABProbeError.invalidArguments(
                        "--left-contexts must not be empty"
                    )
                }
                leftContextsPath = value
            case "--mozc-fixed-boundaries":
                let value = try value(after: option)
                guard !value.isEmpty else {
                    throw ABProbeError.invalidArguments(
                        "--mozc-fixed-boundaries must not be empty"
                    )
                }
                mozcFixedBoundariesPath = value
            case "--boundary-mode":
                let value = try value(after: option)
                guard let parsed = ABProbeBoundaryMode(rawValue: value) else {
                    throw ABProbeError.invalidArguments(
                        "--boundary-mode must be isolated_dictionary, native_zenzai_first_clause, mozc_fixed, or full_composition"
                    )
                }
                boundaryMode = parsed
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
        if zenzaiModelPath == nil {
            guard zenzaiInferenceLimit == nil else {
                throw ABProbeError.invalidArguments(
                    "--zenzai-inference-limit requires --zenzai-model"
                )
            }
            guard zenzaiDevice == nil else {
                throw ABProbeError.invalidArguments(
                    "--zenzai-device requires --zenzai-model"
                )
            }
        } else {
            guard resultSchema == .v6 || resultSchema == .v7 else {
                throw ABProbeError.invalidArguments(
                    "--zenzai-model requires --result-schema v6 or v7"
                )
            }
            guard converterBackend == .hazkey else {
                throw ABProbeError.invalidArguments(
                    "--zenzai-model requires --converter-backend hazkey"
                )
            }
            guard iterations == 1 else {
                throw ABProbeError.invalidArguments(
                    "--zenzai-model requires --iterations 1"
                )
            }
            zenzaiInferenceLimit = zenzaiInferenceLimit ?? 10
        }
        if let leftContextsPath {
            guard resultSchema == .v7 else {
                throw ABProbeError.invalidArguments(
                    "--left-contexts requires --result-schema v7"
                )
            }
            guard zenzaiModelPath != nil else {
                throw ABProbeError.invalidArguments(
                    "--left-contexts requires --zenzai-model"
                )
            }
            guard converterBackend == .hazkey else {
                throw ABProbeError.invalidArguments(
                    "--left-contexts requires --converter-backend hazkey"
                )
            }
            guard !leftContextsPath.isEmpty else {
                throw ABProbeError.invalidArguments(
                    "--left-contexts must not be empty"
                )
            }
        } else if resultSchema == .v7 {
            throw ABProbeError.invalidArguments(
                "--result-schema v7 requires --left-contexts"
            )
        }
        if boundaryMode == .nativeZenzaiFirstClause
            || boundaryMode == .mozcFixed
            || boundaryMode == .fullComposition
        {
            guard resultSchema == .v7 else {
                throw ABProbeError.invalidArguments(
                    "\(boundaryMode.rawValue) requires --result-schema v7"
                )
            }
            guard converterBackend == .hazkey, zenzaiModelPath != nil else {
                throw ABProbeError.invalidArguments(
                    "\(boundaryMode.rawValue) requires Hazkey with --zenzai-model"
                )
            }
        }
        if boundaryMode == .mozcFixed {
            guard mozcFixedBoundariesPath != nil else {
                throw ABProbeError.invalidArguments(
                    "mozc_fixed requires --mozc-fixed-boundaries"
                )
            }
        } else if mozcFixedBoundariesPath != nil {
            throw ABProbeError.invalidArguments(
                "--mozc-fixed-boundaries requires --boundary-mode mozc_fixed"
            )
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
            mozcBundlePath: mozcBundlePath,
            resultSchema: resultSchema,
            zenzaiModelPath: zenzaiModelPath,
            zenzaiInferenceLimit: zenzaiInferenceLimit,
            zenzaiDevice: zenzaiDevice,
            leftContextsPath: leftContextsPath,
            mozcFixedBoundariesPath: mozcFixedBoundariesPath,
            boundaryMode: boundaryMode
        )
    }

    var conversionPath: ABProbeConversionPath {
        switch boundaryMode {
        case .isolatedDictionary:
            resultSchema.conversionPath
        case .nativeZenzaiFirstClause:
            .nativeSegmentCandidates
        case .mozcFixed:
            .mozcFixedSegmentCandidates
        case .fullComposition:
            .fullCompositionCandidates
        }
    }
}

struct ABProbeCorpusCase: Equatable {
    let id: String
    let reading: String
    let category: String
    let elements: [CompositionElement]

    init(id: String, reading: String, category: String) {
        self.id = id
        self.reading = reading
        self.category = category
        self.elements = reading.map {
            CompositionElement(text: String($0), inputStyle: .direct)
        }
    }

    init(id: String, category: String, elements: [CompositionElement]) {
        self.id = id
        self.reading = elements.map(\.text).joined()
        self.category = category
        self.elements = elements
    }

    var composition: CompositionInput {
        composition(leftContext: "")
    }

    func composition(
        leftContext: String,
        targetCount: Int? = nil
    ) -> CompositionInput {
        CompositionInput(
            elements: elements,
            cursor: elements.count,
            leftContext: leftContext,
            targetCount: targetCount
        )
    }
}

struct ABProbeCorpusProvenance: Encodable, Equatable {
    let sha256: String
    let cases: Int
}

struct ABProbeCorpusSnapshot: Equatable {
    let cases: [ABProbeCorpusCase]
    let provenance: ABProbeCorpusProvenance
}

struct ABProbeLeftContextSource: Encodable, Equatable {
    let schema: String
    let sha256: String
    let cases: Int
}

struct ABProbeLeftContextEvidence: Encodable, Equatable {
    let mode: String
    let leftContextSHA256: String
    let leftContextCodePointCount: Int
    let leftContextUTF8ByteCount: Int
    let sourceContentSHA256: String
    let source: ABProbeLeftContextSource

    private enum CodingKeys: String, CodingKey {
        case mode
        case leftContextSHA256 = "left_context_sha256"
        case leftContextCodePointCount = "left_context_code_point_count"
        case leftContextUTF8ByteCount = "left_context_utf8_byte_count"
        case sourceContentSHA256 = "source_content_sha256"
        case source
    }
}

struct ABProbeLeftContextEntry: Equatable {
    let id: String
    let sourceContentSHA256: String
    let leftContext: String
    let leftContextSHA256: String

    func evidence(source: ABProbeLeftContextSource) -> ABProbeLeftContextEvidence {
        ABProbeLeftContextEvidence(
            mode: leftContext.isEmpty ? "empty" : "natural_left",
            leftContextSHA256: leftContextSHA256,
            leftContextCodePointCount: leftContext.unicodeScalars.count,
            leftContextUTF8ByteCount: leftContext.utf8.count,
            sourceContentSHA256: sourceContentSHA256,
            source: source
        )
    }
}

struct ABProbeLeftContextSnapshot: Equatable {
    let entriesByID: [String: ABProbeLeftContextEntry]
    let source: ABProbeLeftContextSource
    let fileIdentity: ABProbeFileIdentity
}

enum ABProbeLeftContexts {
    static let schema = "hazkey.blind-silver-left-context.v1"
    private static let fields: Set<String> = [
        "schema", "id", "source_content_sha256", "left_context",
        "left_context_sha256",
    ]

    static func load(
        path: String,
        cases: [ABProbeCorpusCase]
    ) throws -> ABProbeLeftContextSnapshot {
        let fileIdentity = try ABProbeFileIdentity.capture(
            path: path,
            label: "left-context sidecar"
        )
        let data: Data
        do {
            data = try Data(contentsOf: URL(fileURLWithPath: path))
        } catch {
            throw ABProbeError.invalidCorpus(
                "unable to read left-context sidecar \(path): \(error)"
            )
        }
        guard data.count == fileIdentity.sizeBytes,
              sha256(data) == fileIdentity.sha256
        else {
            throw ABProbeError.invalidCorpus(
                "left-context sidecar changed while it was read: \(path)"
            )
        }
        try fileIdentity.revalidate(label: "left-context sidecar")
        guard !data.isEmpty,
              !data.starts(with: [0xEF, 0xBB, 0xBF]),
              !data.contains(0x0D),
              data.last == 0x0A,
              let contents = String(data: data, encoding: .utf8)
        else {
            throw ABProbeError.invalidCorpus(
                "\(path): left-context sidecar must be BOM-free UTF-8 JSONL with LF endings"
            )
        }
        var lines = contents.split(separator: "\n", omittingEmptySubsequences: false)
        lines.removeLast()
        guard !lines.isEmpty else {
            throw ABProbeError.invalidCorpus(
                "\(path): left-context sidecar has no cases"
            )
        }

        var entriesByID: [String: ABProbeLeftContextEntry] = [:]
        for (offset, rawLine) in lines.enumerated() {
            let lineNumber = offset + 1
            guard !rawLine.isEmpty else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): left-context sidecar contains an empty line"
                )
            }
            let record: [String: Any]
            do {
                let lineData = Data(rawLine.utf8)
                try ABProbeJSONDuplicateKeyValidator.validate(lineData)
                guard let object = try JSONSerialization.jsonObject(with: lineData)
                    as? [String: Any]
                else {
                    throw ABProbeJSONKeyValidationError.invalidJSON
                }
                record = object
            } catch ABProbeJSONKeyValidationError.duplicateKey {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): duplicate JSON object key"
                )
            } catch {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): invalid JSON object"
                )
            }
            guard Set(record.keys) == fields,
                  record["schema"] as? String == schema,
                  let id = record["id"] as? String,
                  let sourceContentSHA256 = record["source_content_sha256"] as? String,
                  let leftContext = record["left_context"] as? String,
                  let leftContextSHA256 = record["left_context_sha256"] as? String
            else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): left-context record does not match the exact schema"
                )
            }
            try validateText(
                id,
                field: "id",
                path: path,
                line: lineNumber,
                allowEmpty: false
            )
            try validateText(
                leftContext,
                field: "left_context",
                path: path,
                line: lineNumber,
                allowEmpty: true
            )
            guard leftContext.unicodeScalars.count <= 4_096 else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): left_context exceeds 4096 Unicode code points"
                )
            }
            guard isSHA256(sourceContentSHA256) else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): source_content_sha256 must be a canonical SHA-256 URI"
                )
            }
            let observedContextSHA256 = sha256(Data(leftContext.utf8))
            guard leftContextSHA256 == observedContextSHA256 else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): left_context_sha256 does not match left_context"
                )
            }
            let entry = ABProbeLeftContextEntry(
                id: id,
                sourceContentSHA256: sourceContentSHA256,
                leftContext: leftContext,
                leftContextSHA256: leftContextSHA256
            )
            guard entriesByID.updateValue(entry, forKey: id) == nil else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): duplicate id \(id)"
                )
            }
        }

        let expectedIDs = Set(cases.map(\.id))
        let observedIDs = Set(entriesByID.keys)
        guard observedIDs == expectedIDs else {
            let missing = expectedIDs.subtracting(observedIDs).sorted()
            let unexpected = observedIDs.subtracting(expectedIDs).sorted()
            throw ABProbeError.invalidCorpus(
                "\(path): left-context IDs do not match the corpus; missing=\(missing), unexpected=\(unexpected)"
            )
        }
        return ABProbeLeftContextSnapshot(
            entriesByID: entriesByID,
            source: ABProbeLeftContextSource(
                schema: schema,
                sha256: sha256(data),
                cases: entriesByID.count
            ),
            fileIdentity: fileIdentity
        )
    }

    private static func validateText(
        _ value: String,
        field: String,
        path: String,
        line: Int,
        allowEmpty: Bool
    ) throws {
        guard allowEmpty || !value.isEmpty else {
            throw ABProbeError.invalidCorpus(
                "\(path):\(line): \(field) must not be empty"
            )
        }
        guard value.utf8.elementsEqual(
            value.precomposedStringWithCanonicalMapping.utf8
        ) else {
            throw ABProbeError.invalidCorpus(
                "\(path):\(line): \(field) must be NFC"
            )
        }
        guard value.unicodeScalars.allSatisfy({ scalar in
            !CharacterSet.controlCharacters.contains(scalar)
                && scalar.value != 0xFEFF
                && !isNoncharacter(scalar.value)
        }) else {
            throw ABProbeError.invalidCorpus(
                "\(path):\(line): \(field) contains an invalid Unicode scalar"
            )
        }
    }

    private static func isNoncharacter(_ value: UInt32) -> Bool {
        (0xFDD0...0xFDEF).contains(value)
            || value & 0xFFFF == 0xFFFE
            || value & 0xFFFF == 0xFFFF
    }

    private static func isSHA256(_ value: String) -> Bool {
        guard value.count == 71, value.hasPrefix("sha256:") else { return false }
        return value.dropFirst(7).allSatisfy { character in
            ("0"..."9").contains(character) || ("a"..."f").contains(character)
        }
    }

    private static func sha256(_ data: Data) -> String {
        var hasher = ABProbeSHA256()
        hasher.update(data)
        return "sha256:" + hasher.finalize().map {
            String(format: "%02x", $0)
        }.joined()
    }
}

struct ABProbeFixedBoundarySource: Encodable, Equatable {
    let schema: String
    let sha256: String
    let cases: Int
}

struct ABProbeMozcFixedBoundaryOrigin: Equatable {
    let schema: String
    let sha256: String
    let cases: Int
    let converterBackend: String
    let conversionPath: String
}

struct ABProbeFixedBoundaryEvidence: Encodable, Equatable {
    let readingSHA256: String
    let consumingCount: Int
    let source: ABProbeFixedBoundarySource

    private enum CodingKeys: String, CodingKey {
        case readingSHA256 = "reading_sha256"
        case consumingCount = "consuming_count"
        case source
    }
}

struct ABProbeFixedBoundaryEntry: Equatable {
    let id: String
    let reading: String
    let readingSHA256: String
    let consumingCount: Int
    let origin: ABProbeMozcFixedBoundaryOrigin

    func evidence(source: ABProbeFixedBoundarySource) -> ABProbeFixedBoundaryEvidence {
        ABProbeFixedBoundaryEvidence(
            readingSHA256: readingSHA256,
            consumingCount: consumingCount,
            source: source
        )
    }
}

struct ABProbeFixedBoundarySnapshot: Equatable {
    let entriesByID: [String: ABProbeFixedBoundaryEntry]
    let source: ABProbeFixedBoundarySource
    let origin: ABProbeMozcFixedBoundaryOrigin
    let fileIdentity: ABProbeFileIdentity
}

enum ABProbeMozcFixedBoundaries {
    static let schema = "hazkey.mozc-fixed-boundary.v1"
    static let mozcResultSchema = "hazkey.ab-probe-result.v6"
    private static let fields: Set<String> = [
        "schema", "id", "reading", "reading_sha256", "consuming_count",
        "origin",
    ]
    private static let originFields: Set<String> = [
        "schema", "sha256", "cases", "converter_backend", "conversion_path",
    ]

    static func load(
        path: String,
        cases: [ABProbeCorpusCase]
    ) throws -> ABProbeFixedBoundarySnapshot {
        let fileIdentity = try ABProbeFileIdentity.capture(
            path: path,
            label: "Mozc fixed-boundary sidecar"
        )
        let data: Data
        do {
            data = try Data(contentsOf: URL(fileURLWithPath: path))
        } catch {
            throw ABProbeError.invalidCorpus(
                "unable to read Mozc fixed-boundary sidecar \(path): \(error)"
            )
        }
        guard data.count == fileIdentity.sizeBytes,
              sha256(data) == fileIdentity.sha256
        else {
            throw ABProbeError.invalidCorpus(
                "Mozc fixed-boundary sidecar changed while it was read: \(path)"
            )
        }
        try fileIdentity.revalidate(label: "Mozc fixed-boundary sidecar")
        guard !data.isEmpty,
              !data.starts(with: [0xEF, 0xBB, 0xBF]),
              !data.contains(0x0D),
              data.last == 0x0A,
              let contents = String(data: data, encoding: .utf8)
        else {
            throw ABProbeError.invalidCorpus(
                "\(path): Mozc fixed-boundary sidecar must be BOM-free UTF-8 JSONL with LF endings"
            )
        }
        var lines = contents.split(separator: "\n", omittingEmptySubsequences: false)
        lines.removeLast()
        guard !lines.isEmpty else {
            throw ABProbeError.invalidCorpus(
                "\(path): Mozc fixed-boundary sidecar has no cases"
            )
        }

        var entriesByID: [String: ABProbeFixedBoundaryEntry] = [:]
        var observedIDs: [String] = []
        var commonOrigin: ABProbeMozcFixedBoundaryOrigin?
        for (offset, rawLine) in lines.enumerated() {
            let lineNumber = offset + 1
            guard !rawLine.isEmpty else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): Mozc fixed-boundary sidecar contains an empty line"
                )
            }
            let record: [String: Any]
            do {
                let lineData = Data(rawLine.utf8)
                try ABProbeJSONDuplicateKeyValidator.validate(lineData)
                guard let object = try JSONSerialization.jsonObject(with: lineData)
                    as? [String: Any]
                else {
                    throw ABProbeJSONKeyValidationError.invalidJSON
                }
                record = object
            } catch ABProbeJSONKeyValidationError.duplicateKey {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): duplicate JSON object key"
                )
            } catch {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): invalid JSON object"
                )
            }
            guard Set(record.keys) == fields,
                  record["schema"] as? String == schema,
                  let id = record["id"] as? String,
                  let reading = record["reading"] as? String,
                  let readingSHA256 = record["reading_sha256"] as? String,
                  let consumingCount = exactInteger(record["consuming_count"]),
                  let rawOrigin = record["origin"] as? [String: Any],
                  Set(rawOrigin.keys) == originFields,
                  let originSchema = rawOrigin["schema"] as? String,
                  let originSHA256 = rawOrigin["sha256"] as? String,
                  let originCases = exactInteger(rawOrigin["cases"]),
                  let converterBackend = rawOrigin["converter_backend"] as? String,
                  let conversionPath = rawOrigin["conversion_path"] as? String
            else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): Mozc fixed-boundary record does not match the exact schema"
                )
            }
            try validateText(id, field: "id", path: path, line: lineNumber)
            try validateText(
                reading,
                field: "reading",
                path: path,
                line: lineNumber
            )
            guard isSHA256(readingSHA256),
                  readingSHA256 == sha256(Data(reading.utf8))
            else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): reading_sha256 does not match reading"
                )
            }
            guard originSchema == mozcResultSchema,
                  isSHA256(originSHA256),
                  originCases > 0,
                  converterBackend == ABProbeConverterBackend.mozc.rawValue,
                  conversionPath == ABProbeConversionPath.segmentCandidates.rawValue
            else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): Mozc origin is invalid"
                )
            }
            let origin = ABProbeMozcFixedBoundaryOrigin(
                schema: originSchema,
                sha256: originSHA256,
                cases: originCases,
                converterBackend: converterBackend,
                conversionPath: conversionPath
            )
            if let commonOrigin, commonOrigin != origin {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): Mozc origin differs within the sidecar"
                )
            }
            commonOrigin = origin
            let entry = ABProbeFixedBoundaryEntry(
                id: id,
                reading: reading,
                readingSHA256: readingSHA256,
                consumingCount: consumingCount,
                origin: origin
            )
            guard entriesByID.updateValue(entry, forKey: id) == nil else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): duplicate id \(id)"
                )
            }
            observedIDs.append(id)
        }

        guard let commonOrigin,
              commonOrigin.cases == lines.count
        else {
            throw ABProbeError.invalidCorpus(
                "\(path): Mozc origin.cases does not match sidecar record count"
            )
        }
        let expectedIDs = cases.map(\.id)
        guard observedIDs == expectedIDs else {
            throw ABProbeError.invalidCorpus(
                "\(path): Mozc fixed-boundary IDs/order do not match the corpus"
            )
        }
        for testCase in cases {
            guard let entry = entriesByID[testCase.id],
                  entry.reading == testCase.reading
            else {
                throw ABProbeError.invalidCorpus(
                    "\(path): case \(testCase.id) reading does not match the corpus"
                )
            }
            guard (1...testCase.elements.count).contains(entry.consumingCount) else {
                throw ABProbeError.invalidCorpus(
                    "\(path): case \(testCase.id) consuming_count is outside the composition"
                )
            }
        }
        return ABProbeFixedBoundarySnapshot(
            entriesByID: entriesByID,
            source: ABProbeFixedBoundarySource(
                schema: schema,
                sha256: sha256(data),
                cases: entriesByID.count
            ),
            origin: commonOrigin,
            fileIdentity: fileIdentity
        )
    }

    private static func validateText(
        _ value: String,
        field: String,
        path: String,
        line: Int
    ) throws {
        guard !value.isEmpty else {
            throw ABProbeError.invalidCorpus(
                "\(path):\(line): \(field) must not be empty"
            )
        }
        guard value.utf8.elementsEqual(
            value.precomposedStringWithCanonicalMapping.utf8
        ) else {
            throw ABProbeError.invalidCorpus(
                "\(path):\(line): \(field) must be NFC"
            )
        }
        guard value.unicodeScalars.allSatisfy({ scalar in
            !CharacterSet.controlCharacters.contains(scalar)
                && scalar.value != 0xFEFF
                && !isNoncharacter(scalar.value)
        }) else {
            throw ABProbeError.invalidCorpus(
                "\(path):\(line): \(field) contains an invalid Unicode scalar"
            )
        }
    }

    private static func isNoncharacter(_ value: UInt32) -> Bool {
        (0xFDD0...0xFDEF).contains(value)
            || value & 0xFFFF == 0xFFFE
            || value & 0xFFFF == 0xFFFF
    }

    private static func isSHA256(_ value: String) -> Bool {
        guard value.count == 71, value.hasPrefix("sha256:") else { return false }
        return value.dropFirst(7).allSatisfy { character in
            ("0"..."9").contains(character) || ("a"..."f").contains(character)
        }
    }

    private static func exactInteger(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber,
              !["c", "B"].contains(String(cString: number.objCType)),
              let integer = value as? Int
        else {
            return nil
        }
        return integer
    }

    private static func sha256(_ data: Data) -> String {
        var hasher = ABProbeSHA256()
        hasher.update(data)
        return "sha256:" + hasher.finalize().map {
            String(format: "%02x", $0)
        }.joined()
    }
}

enum ABProbeJSONKeyValidationError: Error, Equatable {
    case invalidJSON
    case duplicateKey
}

enum ABProbeJSONDuplicateKeyValidator {
    static func validate(_ data: Data) throws {
        var parser = Parser(bytes: Array(data))
        try parser.parseDocument()
    }

    private struct Parser {
        private let bytes: [UInt8]
        private var index = 0

        init(bytes: [UInt8]) {
            self.bytes = bytes
        }

        mutating func parseDocument() throws {
            skipWhitespace()
            try parseValue(depth: 0)
            skipWhitespace()
            guard index == bytes.count else {
                throw ABProbeJSONKeyValidationError.invalidJSON
            }
        }

        private mutating func parseValue(depth: Int) throws {
            skipWhitespace()
            guard index < bytes.count else {
                throw ABProbeJSONKeyValidationError.invalidJSON
            }
            switch bytes[index] {
            case 0x7B: // {
                guard depth < 512 else {
                    throw ABProbeJSONKeyValidationError.invalidJSON
                }
                try parseObject(depth: depth + 1)
            case 0x5B: // [
                guard depth < 512 else {
                    throw ABProbeJSONKeyValidationError.invalidJSON
                }
                try parseArray(depth: depth + 1)
            case 0x22: // "
                _ = try parseString()
            case 0x74: // true
                try parseLiteral([0x74, 0x72, 0x75, 0x65])
            case 0x66: // false
                try parseLiteral([0x66, 0x61, 0x6C, 0x73, 0x65])
            case 0x6E: // null
                try parseLiteral([0x6E, 0x75, 0x6C, 0x6C])
            case 0x2D, 0x30...0x39: // - or digit
                try parseNumber()
            default:
                throw ABProbeJSONKeyValidationError.invalidJSON
            }
        }

        private mutating func parseObject(depth: Int) throws {
            guard consume(0x7B) else {
                throw ABProbeJSONKeyValidationError.invalidJSON
            }
            skipWhitespace()
            if consume(0x7D) {
                return
            }

            var keys = Set<Data>()
            while true {
                skipWhitespace()
                guard index < bytes.count, bytes[index] == 0x22 else {
                    throw ABProbeJSONKeyValidationError.invalidJSON
                }
                let key = try parseString()
                guard keys.insert(Data(key.utf8)).inserted else {
                    throw ABProbeJSONKeyValidationError.duplicateKey
                }
                skipWhitespace()
                guard consume(0x3A) else {
                    throw ABProbeJSONKeyValidationError.invalidJSON
                }
                try parseValue(depth: depth)
                skipWhitespace()
                if consume(0x7D) {
                    return
                }
                guard consume(0x2C) else {
                    throw ABProbeJSONKeyValidationError.invalidJSON
                }
            }
        }

        private mutating func parseArray(depth: Int) throws {
            guard consume(0x5B) else {
                throw ABProbeJSONKeyValidationError.invalidJSON
            }
            skipWhitespace()
            if consume(0x5D) {
                return
            }

            while true {
                try parseValue(depth: depth)
                skipWhitespace()
                if consume(0x5D) {
                    return
                }
                guard consume(0x2C) else {
                    throw ABProbeJSONKeyValidationError.invalidJSON
                }
            }
        }

        private mutating func parseString() throws -> String {
            guard consume(0x22) else {
                throw ABProbeJSONKeyValidationError.invalidJSON
            }
            let start = index - 1
            while index < bytes.count {
                let byte = bytes[index]
                if byte == 0x22 {
                    index += 1
                    return try decodeStringToken(bytes[start..<index])
                }
                if byte == 0x5C {
                    index += 1
                    guard index < bytes.count else {
                        throw ABProbeJSONKeyValidationError.invalidJSON
                    }
                    let escaped = bytes[index]
                    index += 1
                    if escaped == 0x75 {
                        guard index + 4 <= bytes.count,
                              bytes[index..<(index + 4)].allSatisfy(isHexDigit)
                        else {
                            throw ABProbeJSONKeyValidationError.invalidJSON
                        }
                        index += 4
                    } else if ![
                        0x22, 0x5C, 0x2F, 0x62, 0x66, 0x6E, 0x72, 0x74,
                    ].contains(escaped) {
                        throw ABProbeJSONKeyValidationError.invalidJSON
                    }
                } else {
                    guard byte >= 0x20 else {
                        throw ABProbeJSONKeyValidationError.invalidJSON
                    }
                    index += 1
                }
            }
            throw ABProbeJSONKeyValidationError.invalidJSON
        }

        private func decodeStringToken(_ token: ArraySlice<UInt8>) throws -> String {
            do {
                return try JSONDecoder().decode(String.self, from: Data(token))
            } catch {
                throw ABProbeJSONKeyValidationError.invalidJSON
            }
        }

        private mutating func parseLiteral(_ literal: [UInt8]) throws {
            guard index + literal.count <= bytes.count,
                  bytes[index..<(index + literal.count)].elementsEqual(literal)
            else {
                throw ABProbeJSONKeyValidationError.invalidJSON
            }
            index += literal.count
        }

        private mutating func parseNumber() throws {
            _ = consume(0x2D)
            guard index < bytes.count else {
                throw ABProbeJSONKeyValidationError.invalidJSON
            }
            if consume(0x30) {
                if index < bytes.count, isDigit(bytes[index]) {
                    throw ABProbeJSONKeyValidationError.invalidJSON
                }
            } else {
                guard index < bytes.count, (0x31...0x39).contains(bytes[index]) else {
                    throw ABProbeJSONKeyValidationError.invalidJSON
                }
                index += 1
                while index < bytes.count, isDigit(bytes[index]) {
                    index += 1
                }
            }

            if consume(0x2E) {
                guard index < bytes.count, isDigit(bytes[index]) else {
                    throw ABProbeJSONKeyValidationError.invalidJSON
                }
                while index < bytes.count, isDigit(bytes[index]) {
                    index += 1
                }
            }

            if consume(0x65) || consume(0x45) {
                if !consume(0x2B) {
                    _ = consume(0x2D)
                }
                guard index < bytes.count, isDigit(bytes[index]) else {
                    throw ABProbeJSONKeyValidationError.invalidJSON
                }
                while index < bytes.count, isDigit(bytes[index]) {
                    index += 1
                }
            }
        }

        private mutating func skipWhitespace() {
            while index < bytes.count,
                  [0x20, 0x09, 0x0A, 0x0D].contains(bytes[index])
            {
                index += 1
            }
        }

        private mutating func consume(_ byte: UInt8) -> Bool {
            guard index < bytes.count, bytes[index] == byte else {
                return false
            }
            index += 1
            return true
        }

        private func isDigit(_ byte: UInt8) -> Bool {
            (0x30...0x39).contains(byte)
        }

        private func isHexDigit(_ byte: UInt8) -> Bool {
            (0x30...0x39).contains(byte)
                || (0x41...0x46).contains(byte)
                || (0x61...0x66).contains(byte)
        }
    }
}

enum ABProbeCorpus {
    private static let segmentProbeInputSchema =
        "hazkey.mozc-hybrid-segment-probe-input.v1"
    private static let segmentProbeRootFields: Set<String> = [
        "schema", "id", "category", "elements",
    ]
    private static let segmentProbeElementFields: Set<String> = [
        "text", "input_style",
    ]

    static func load(path: String) throws -> [ABProbeCorpusCase] {
        try loadSnapshot(path: path).cases
    }

    static func loadSnapshot(path: String) throws -> ABProbeCorpusSnapshot {
        let data: Data
        do {
            data = try Data(contentsOf: URL(fileURLWithPath: path))
        } catch {
            throw ABProbeError.invalidCorpus("unable to read corpus \(path): \(error)")
        }
        guard let contents = String(data: data, encoding: .utf8) else {
            throw ABProbeError.invalidCorpus("\(path): corpus is not valid UTF-8")
        }
        let cases: [ABProbeCorpusCase]
        if hasTSVHeader(contents) {
            cases = try loadTSV(contents: contents, path: path)
        } else if looksLikeJSONLines(contents) {
            cases = try loadSegmentProbeJSONLines(
                data: data,
                contents: contents,
                path: path
            )
        } else {
            cases = try loadTSV(contents: contents, path: path)
        }
        var hasher = ABProbeSHA256()
        hasher.update(data)
        let digest = hasher.finalize().map {
            String(format: "%02x", $0)
        }.joined()
        return ABProbeCorpusSnapshot(
            cases: cases,
            provenance: ABProbeCorpusProvenance(
                sha256: "sha256:" + digest,
                cases: cases.count
            )
        )
    }

    private static func looksLikeJSONLines(_ contents: String) -> Bool {
        guard let first = contents.first(where: {
            !$0.isWhitespace && $0 != "\u{FEFF}"
        }) else {
            return false
        }
        return first == "{" || first == "["
    }

    private static func hasTSVHeader(_ contents: String) -> Bool {
        guard let rawHeader = contents.split(
            omittingEmptySubsequences: false,
            whereSeparator: \.isNewline
        ).first else {
            return false
        }
        let fields = Set(
            rawHeader.split(separator: "\t", omittingEmptySubsequences: false)
                .map(String.init)
        )
        return fields.isSuperset(of: ["id", "reading", "category"])
    }

    private static func loadTSV(
        contents: String,
        path: String
    ) throws -> [ABProbeCorpusCase] {
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

    private static func loadSegmentProbeJSONLines(
        data: Data,
        contents: String,
        path: String
    ) throws -> [ABProbeCorpusCase] {
        if data.range(of: Data([0xEF, 0xBB, 0xBF])) != nil {
            throw ABProbeError.invalidCorpus("\(path): JSONL corpus must not contain a BOM")
        }
        if data.contains(0x0D) {
            throw ABProbeError.invalidCorpus("\(path): JSONL corpus must use LF line endings")
        }

        var lines = contents.split(separator: "\n", omittingEmptySubsequences: false)
        if contents.hasSuffix("\n") {
            lines.removeLast()
        }
        guard !lines.isEmpty else {
            throw ABProbeError.invalidCorpus("\(path): corpus is empty")
        }

        var seen = Set<String>()
        var result: [ABProbeCorpusCase] = []
        result.reserveCapacity(lines.count)
        for (offset, rawLine) in lines.enumerated() {
            let lineNumber = offset + 1
            guard !rawLine.isEmpty else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): JSONL corpus contains an empty line"
                )
            }
            let object: Any
            do {
                let lineData = Data(rawLine.utf8)
                try ABProbeJSONDuplicateKeyValidator.validate(lineData)
                object = try JSONSerialization.jsonObject(with: lineData)
            } catch ABProbeJSONKeyValidationError.duplicateKey {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): duplicate JSON object key"
                )
            } catch {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): invalid JSON object"
                )
            }
            guard let record = object as? [String: Any],
                  Set(record.keys) == segmentProbeRootFields
            else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): JSONL record fields do not match the exact schema"
                )
            }
            guard record["schema"] as? String == segmentProbeInputSchema,
                  let id = record["id"] as? String,
                  let category = record["category"] as? String,
                  let rawElements = record["elements"] as? [Any]
            else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): JSONL record has invalid field types or schema"
                )
            }
            try validateSegmentProbeText(id, field: "id", path: path, line: lineNumber)
            try validateSegmentProbeText(
                category,
                field: "category",
                path: path,
                line: lineNumber
            )
            guard !rawElements.isEmpty else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): elements must not be empty"
                )
            }

            var elements: [CompositionElement] = []
            elements.reserveCapacity(rawElements.count)
            for (elementOffset, rawElement) in rawElements.enumerated() {
                let elementNumber = elementOffset + 1
                guard let element = rawElement as? [String: Any],
                      Set(element.keys) == segmentProbeElementFields,
                      let text = element["text"] as? String,
                      let inputStyle = element["input_style"] as? String
                else {
                    throw ABProbeError.invalidCorpus(
                        "\(path):\(lineNumber): element \(elementNumber) does not match the exact schema"
                    )
                }
                try validateSegmentProbeText(
                    text,
                    field: "elements[\(elementOffset)].text",
                    path: path,
                    line: lineNumber
                )
                guard inputStyle == "direct" else {
                    throw ABProbeError.invalidCorpus(
                        "\(path):\(lineNumber): element \(elementNumber) input_style must be direct"
                    )
                }
                elements.append(CompositionElement(text: text, inputStyle: .direct))
            }
            guard seen.insert(id).inserted else {
                throw ABProbeError.invalidCorpus(
                    "\(path):\(lineNumber): duplicate id \(id)"
                )
            }
            result.append(
                ABProbeCorpusCase(id: id, category: category, elements: elements)
            )
        }
        return result
    }

    private static func validateSegmentProbeText(
        _ value: String,
        field: String,
        path: String,
        line: Int
    ) throws {
        guard !value.isEmpty else {
            throw ABProbeError.invalidCorpus(
                "\(path):\(line): \(field) must not be empty"
            )
        }
        let normalized = value.precomposedStringWithCanonicalMapping
        guard value.utf8.elementsEqual(normalized.utf8) else {
            throw ABProbeError.invalidCorpus(
                "\(path):\(line): \(field) must be NFC"
            )
        }
        guard !value.unicodeScalars.contains(where: {
            CharacterSet.controlCharacters.contains($0) || $0.value == 0xFEFF
        }) else {
            throw ABProbeError.invalidCorpus(
                "\(path):\(line): \(field) must not contain control characters"
            )
        }
    }
}

struct ABProbeResourceProvenance: Encodable, Equatable {
    let kind: String
    let path: String
    let fingerprint: String
}

struct ABProbeFileIdentity: Encodable, Equatable {
    let path: String
    let sizeBytes: Int
    let sha256: String

    private enum CodingKeys: String, CodingKey {
        case path
        case sizeBytes = "size_bytes"
        case sha256
    }

    static func capture(path: String, label: String) throws -> Self {
        let url = URL(fileURLWithPath: path, isDirectory: false)
            .standardizedFileURL
            .resolvingSymlinksInPath()
        let fileManager = FileManager.default
        let before: [FileAttributeKey: Any]
        do {
            before = try fileManager.attributesOfItem(atPath: url.path)
        } catch {
            throw ABProbeError.backendInstability(
                "unable to inspect \(label) at \(url.path): \(error)"
            )
        }
        guard before[.type] as? FileAttributeType == .typeRegular,
              let sizeNumber = before[.size] as? NSNumber,
              sizeNumber.uint64Value <= UInt64(Int.max)
        else {
            throw ABProbeError.backendInstability(
                "\(label) must be a regular file: \(url.path)"
            )
        }

        var hasher = ABProbeSHA256()
        let handle: FileHandle
        do {
            handle = try FileHandle(forReadingFrom: url)
        } catch {
            throw ABProbeError.backendInstability(
                "unable to read \(label) at \(url.path): \(error)"
            )
        }
        defer { try? handle.close() }
        do {
            while let data = try handle.read(upToCount: 1_048_576), !data.isEmpty {
                hasher.update(data)
            }
        } catch {
            throw ABProbeError.backendInstability(
                "unable to hash \(label) at \(url.path): \(error)"
            )
        }

        let after: [FileAttributeKey: Any]
        do {
            after = try fileManager.attributesOfItem(atPath: url.path)
        } catch {
            throw ABProbeError.backendInstability(
                "unable to re-inspect \(label) at \(url.path): \(error)"
            )
        }
        guard stableFileAttributes(before) == stableFileAttributes(after) else {
            throw ABProbeError.backendInstability(
                "\(label) changed while its identity was acquired: \(url.path)"
            )
        }
        let digest = hasher.finalize().map {
            String(format: "%02x", $0)
        }.joined()
        return Self(
            path: url.path,
            sizeBytes: sizeNumber.intValue,
            sha256: "sha256:" + digest
        )
    }

    static func currentProducer() throws -> Self {
        let executableURL = Bundle.main.executableURL
            ?? URL(fileURLWithPath: CommandLine.arguments[0], isDirectory: false)
        return try capture(path: executableURL.path, label: "ABProbe producer")
    }

    func revalidate(label: String) throws {
        let observed = try Self.capture(path: path, label: label)
        guard observed == self else {
            throw ABProbeError.backendInstability(
                "\(label) identity changed during the ABProbe run: \(path)"
            )
        }
    }

    private static func stableFileAttributes(
        _ attributes: [FileAttributeKey: Any]
    ) -> [String] {
        [
            String(describing: attributes[.type]),
            String(describing: attributes[.size]),
            String(describing: attributes[.systemNumber]),
            String(describing: attributes[.systemFileNumber]),
            String(describing: attributes[.modificationDate]),
        ]
    }
}

struct ABProbeZenzaiQualityPolicy: Encodable, Equatable {
    let enabled: Bool
    let modelPath: String?
    let modelSizeBytes: Int?
    let modelSHA256: String?
    let inferenceLimit: Int?
    let resolvedDevice: String?

    private enum CodingKeys: String, CodingKey {
        case enabled
        case modelPath = "model_path"
        case modelSizeBytes = "model_size_bytes"
        case modelSHA256 = "model_sha256"
        case inferenceLimit = "inference_limit"
        case resolvedDevice = "resolved_device"
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(enabled, forKey: .enabled)
        try container.encode(modelPath, forKey: .modelPath)
        try container.encode(modelSizeBytes, forKey: .modelSizeBytes)
        try container.encode(modelSHA256, forKey: .modelSHA256)
        try container.encode(inferenceLimit, forKey: .inferenceLimit)
        try container.encode(resolvedDevice, forKey: .resolvedDevice)
    }
}

struct ABProbeQualityPolicy: Encodable, Equatable {
    let learning = false
    let context: String
    let zenzai: ABProbeZenzaiQualityPolicy

    init(
        context: String = "empty",
        zenzai: ABProbeZenzaiQualityPolicy
    ) {
        self.context = context
        self.zenzai = zenzai
    }
}

struct ABProbeBoundaryPolicy: Encodable, Equatable {
    let mode: String
    let boundaryZenzaiEnabled: Bool
    let surfaceZenzaiEnabled: Bool
    let source: String

    init(mode: ABProbeBoundaryMode) {
        switch mode {
        case .isolatedDictionary:
            self.mode = mode.rawValue
            boundaryZenzaiEnabled = false
            surfaceZenzaiEnabled = true
            source = "separate_converter"
        case .nativeZenzaiFirstClause:
            self.mode = mode.rawValue
            boundaryZenzaiEnabled = true
            surfaceZenzaiEnabled = true
            source = "primary_converter_first_clause_results"
        case .mozcFixed:
            self.mode = mode.rawValue
            boundaryZenzaiEnabled = false
            surfaceZenzaiEnabled = true
            source = "mozc_top1_fixed_boundary_sidecar"
        case .fullComposition:
            self.mode = mode.rawValue
            boundaryZenzaiEnabled = false
            surfaceZenzaiEnabled = true
            source = "entire_composition"
        }
    }

    private enum CodingKeys: String, CodingKey {
        case mode
        case boundaryZenzaiEnabled = "boundary_zenzai_enabled"
        case surfaceZenzaiEnabled = "surface_zenzai_enabled"
        case source
    }
}

struct ABProbeMozcArtifactIdentity: Equatable {
    let size: Int
    let sha256: String
}

struct ABProbeMozcTrustedArtifacts: Equatable {
    let helper: ABProbeMozcArtifactIdentity
    let data: ABProbeMozcArtifactIdentity

    static let fixedB0 = ABProbeMozcTrustedArtifacts(
        helper: ABProbeMozcArtifactIdentity(
            size: 5_695_048,
            sha256: "8676275bb47aefe963c8b82047cc66fb7a5140caec72d1ebbfa17556b281577d"
        ),
        data: ABProbeMozcArtifactIdentity(
            size: 18_887_468,
            sha256: "b9884362e37772f772a0d28d1e12622455c14353497b3435deed60aa7e592c5e"
        )
    )

    static let fixedB1 = ABProbeMozcTrustedArtifacts(
        helper: ABProbeMozcArtifactIdentity(
            size: 5_746_568,
            sha256: "728d9a79c0f540a832d3f404a2603f49080e1f9e7ee1d24df1a0a69f5a4a75e8"
        ),
        data: fixedB0.data
    )

    // Preserve the existing single-profile injection surface for tests.
    static let fixed = fixedB0
    static let fixedProfiles = [fixedB0, fixedB1]
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

    static func prepare(sourceURL: URL) throws -> ABProbeMozcRuntimeSnapshot {
        try prepare(
            sourceURL: sourceURL,
            trustedArtifactProfiles: ABProbeMozcTrustedArtifacts.fixedProfiles
        )
    }

    static func prepare(
        sourceURL: URL,
        trustedArtifacts: ABProbeMozcTrustedArtifacts
    ) throws -> ABProbeMozcRuntimeSnapshot {
        try prepare(
            sourceURL: sourceURL,
            trustedArtifactProfiles: [trustedArtifacts]
        )
    }

    static func prepare(
        sourceURL: URL,
        trustedArtifactProfiles: [ABProbeMozcTrustedArtifacts]
    ) throws -> ABProbeMozcRuntimeSnapshot {
        guard !trustedArtifactProfiles.isEmpty else {
            throw ABProbeError.mozcBundleInvalid(
                "no trusted Mozc artifact profile is configured"
            )
        }
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
            maximumSize: trustedArtifactProfiles.map(\.helper.size).max() ?? 0
        )
        let data = try readPinnedFile(
            directoryFD: directoryFD,
            name: dataName,
            expectedMode: 0o444,
            maximumSize: trustedArtifactProfiles.map(\.data.size).max() ?? 0
        )
        let manifest = try readPinnedFile(
            directoryFD: directoryFD,
            name: manifestName,
            expectedMode: 0o444,
            maximumSize: maximumManifestSize
        )

        guard let trustedArtifacts = trustedArtifactProfiles.first(where: { profile in
            artifactMatches(helper, trusted: profile.helper)
                && artifactMatches(data, trusted: profile.data)
        }) else {
            throw ABProbeError.mozcBundleInvalid(
                "Mozc helper and dataset do not match one trusted artifact profile"
            )
        }
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

    private static func artifactMatches(
        _ data: Data,
        trusted: ABProbeMozcArtifactIdentity
    ) -> Bool {
        data.count == trusted.size && digest(data) == trusted.sha256
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
        trustedMozcArtifacts: ABProbeMozcTrustedArtifacts? = nil
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
            let sourceURL = URL(fileURLWithPath: bundlePath, isDirectory: true)
            let runtime: ABProbeMozcRuntimeSnapshot
            if let trustedMozcArtifacts {
                runtime = try ABProbeMozcRuntimeSnapshot.prepare(
                    sourceURL: sourceURL,
                    trustedArtifacts: trustedMozcArtifacts
                )
            } else {
                runtime = try ABProbeMozcRuntimeSnapshot.prepare(sourceURL: sourceURL)
            }
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
    let schema = "hazkey.ab-probe-result.v3"
    let id: String
    let reading: String
    let category: String
    let backend: String
    let backendVersion: String
    let converterBackend: String
    let sourceRef: String
    let resource: ABProbeResourceProvenance
    let topK: Int
    let corpus: ABProbeCorpusProvenance
    let candidates: [String]
    let measurement: ABProbeMeasurement

    private enum CodingKeys: String, CodingKey {
        case schema
        case id
        case reading
        case category
        case backend
        case backendVersion = "backend_version"
        case converterBackend = "converter_backend"
        case sourceRef = "source_ref"
        case resource
        case topK = "top_k"
        case corpus
        case candidates
        case measurement
    }
}

struct ABProbeV4Candidate: Encodable, Equatable {
    let text: String
    let rank: Int
    let consumingCount: Int

    private enum CodingKeys: String, CodingKey {
        case text
        case rank
        case consumingCount = "consuming_count"
    }
}

struct ABProbeV6Candidate: Encodable, Equatable {
    let text: String
    let rank: Int
    let consumingCount: Int
    let provenance: String
    let rankingInfluence: String
    let zenzaiScore: Float?
    let zenzaiScoreTokenCount: Int?
    let zenzaiScoreScope: String?

    private enum CodingKeys: String, CodingKey {
        case text
        case rank
        case consumingCount = "consuming_count"
        case provenance
        case rankingInfluence = "ranking_influence"
        case zenzaiScore = "zenzai_score"
        case zenzaiScoreTokenCount = "zenzai_score_token_count"
        case zenzaiScoreScope = "zenzai_score_scope"
    }

    func encode(to encoder: Encoder) throws {
        try validateZenzaiEvidence()
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(text, forKey: .text)
        try container.encode(rank, forKey: .rank)
        try container.encode(consumingCount, forKey: .consumingCount)
        try container.encode(provenance, forKey: .provenance)
        try container.encode(rankingInfluence, forKey: .rankingInfluence)
        try container.encode(zenzaiScore, forKey: .zenzaiScore)
        try container.encode(zenzaiScoreTokenCount, forKey: .zenzaiScoreTokenCount)
        try container.encode(zenzaiScoreScope, forKey: .zenzaiScoreScope)
    }

    fileprivate func validateZenzaiEvidence() throws {
        switch (zenzaiScore, zenzaiScoreTokenCount, zenzaiScoreScope) {
        case (nil, nil, nil):
            return
        case let (.some(score), .some(tokenCount), .some(scope)):
            guard score.isFinite else {
                throw ABProbeError.backendInstability(
                    "candidate rank \(rank) has a non-finite Zenzai score"
                )
            }
            guard tokenCount > 0 else {
                throw ABProbeError.backendInstability(
                    "candidate rank \(rank) has a non-positive Zenzai score token count"
                )
            }
            guard Self.validZenzaiScoreScopes.contains(scope) else {
                throw ABProbeError.backendInstability(
                    "candidate rank \(rank) has an invalid Zenzai score scope: \(scope)"
                )
            }
        default:
            throw ABProbeError.backendInstability(
                "candidate rank \(rank) must emit Zenzai score, token count, and scope together"
            )
        }
    }

    private static let validZenzaiScoreScopes: Set<String> = [
        "full_candidate",
        "constraint_suffix",
    ]

    fileprivate var stabilityEvidence: StabilityEvidence {
        StabilityEvidence(
            text: text,
            rank: rank,
            consumingCount: consumingCount,
            provenance: provenance,
            rankingInfluence: rankingInfluence,
            hasZenzaiScore: zenzaiScore != nil,
            zenzaiScoreTokenCount: zenzaiScoreTokenCount,
            zenzaiScoreScope: zenzaiScoreScope
        )
    }

    fileprivate struct StabilityEvidence: Equatable {
        let text: String
        let rank: Int
        let consumingCount: Int
        let provenance: String
        let rankingInfluence: String
        let hasZenzaiScore: Bool
        let zenzaiScoreTokenCount: Int?
        let zenzaiScoreScope: String?
    }
}

enum ABProbeCandidateObservation {
    static func constrain(
        _ output: ConversionOutput,
        toFixedBoundary targetCount: Int
    ) -> ConversionOutput {
        let candidates = output.candidates.filter {
            $0.consumingCount == targetCount
        }
        return ConversionOutput(
            candidates: candidates,
            pageSize: min(output.pageSize, candidates.count),
            zenzaiExecutionEvidence: output.zenzaiExecutionEvidence
        )
    }

    static func capture(
        _ candidates: [ConverterCandidate],
        topK: Int
    ) -> [ABProbeV4Candidate] {
        candidates.prefix(topK).enumerated().map { offset, candidate in
            ABProbeV4Candidate(
                text: candidate.text,
                rank: offset + 1,
                // Preserve the converter's exact composition-element count;
                // downstream validation rejects invalid non-positive evidence.
                consumingCount: candidate.consumingCount
            )
        }
    }

    static func validateStable(
        reference: [ABProbeV4Candidate],
        observed: [ABProbeV4Candidate],
        resultSchema: ABProbeResultSchema = .v4,
        caseID: String
    ) throws {
        let isStable = switch resultSchema {
        case .v3:
            reference.map(\.text) == observed.map(\.text)
        case .v4, .v5, .v6, .v7:
            reference == observed
        }
        guard isStable else {
            throw ABProbeError.candidateDrift(
                "candidate output drifted during case \(caseID)"
            )
        }
    }

    static func captureV6(
        _ candidates: [ConverterCandidate],
        topK: Int
    ) throws -> [ABProbeV6Candidate] {
        try candidates.prefix(topK).enumerated().map { offset, candidate in
            let observed = ABProbeV6Candidate(
                text: candidate.text,
                rank: offset + 1,
                consumingCount: candidate.consumingCount,
                provenance: candidate.provenance.rawValue,
                rankingInfluence: candidate.rankingInfluence.rawValue,
                zenzaiScore: candidate.zenzaiScore,
                zenzaiScoreTokenCount: candidate.zenzaiScoredTokenCount,
                zenzaiScoreScope: candidate.zenzaiScoreScope?.rawValue
            )
            try observed.validateZenzaiEvidence()
            return observed
        }
    }

    static func validateStable(
        reference: [ABProbeV6Candidate],
        observed: [ABProbeV6Candidate],
        caseID: String
    ) throws {
        try reference.forEach { try $0.validateZenzaiEvidence() }
        try observed.forEach { try $0.validateZenzaiEvidence() }
        guard reference.map(\.stabilityEvidence)
            == observed.map(\.stabilityEvidence)
        else {
            throw ABProbeError.candidateDrift(
                "candidate output drifted during case \(caseID)"
            )
        }
    }
}

struct ABProbeResultV4: Encodable {
    let schema = "hazkey.ab-probe-result.v4"
    let conversionPath = ABProbeConversionPath.segmentCandidates.rawValue
    let id: String
    let reading: String
    let category: String
    let backend: String
    let backendVersion: String
    let converterBackend: String
    let sourceRef: String
    let resource: ABProbeResourceProvenance
    let topK: Int
    let corpus: ABProbeCorpusProvenance
    let candidates: [ABProbeV4Candidate]
    let measurement: ABProbeMeasurement

    init(v3: ABProbeResult, candidates: [ABProbeV4Candidate]) {
        id = v3.id
        reading = v3.reading
        category = v3.category
        backend = v3.backend
        backendVersion = v3.backendVersion
        converterBackend = v3.converterBackend
        sourceRef = v3.sourceRef
        resource = v3.resource
        topK = v3.topK
        corpus = v3.corpus
        self.candidates = candidates
        measurement = v3.measurement
    }

    private enum CodingKeys: String, CodingKey {
        case schema
        case conversionPath = "conversion_path"
        case id
        case reading
        case category
        case backend
        case backendVersion = "backend_version"
        case converterBackend = "converter_backend"
        case sourceRef = "source_ref"
        case resource
        case topK = "top_k"
        case corpus
        case candidates
        case measurement
    }
}

struct ABProbeCompositionSpan: Encodable, Equatable {
    let start: Int
    let count: Int
    let unit: String

    static func entireComposition(_ composition: CompositionInput) -> Self {
        Self(
            start: 0,
            count: composition.elements.count,
            unit: "composition_element"
        )
    }
}

struct ABProbeResultV5: Encodable {
    let schema = "hazkey.ab-probe-result.v5"
    let conversionPath = ABProbeConversionPath.segmentCandidates.rawValue
    let id: String
    let reading: String
    let category: String
    let backend: String
    let backendVersion: String
    let converterBackend: String
    let sourceRef: String
    let resource: ABProbeResourceProvenance
    let topK: Int
    let corpus: ABProbeCorpusProvenance
    let candidates: [ABProbeV4Candidate]
    let compositionSpan: ABProbeCompositionSpan
    let measurement: ABProbeMeasurement

    init(
        v3: ABProbeResult,
        candidates: [ABProbeV4Candidate],
        compositionSpan: ABProbeCompositionSpan
    ) {
        id = v3.id
        reading = v3.reading
        category = v3.category
        backend = v3.backend
        backendVersion = v3.backendVersion
        converterBackend = v3.converterBackend
        sourceRef = v3.sourceRef
        resource = v3.resource
        topK = v3.topK
        corpus = v3.corpus
        self.candidates = candidates
        self.compositionSpan = compositionSpan
        measurement = v3.measurement
    }

    private enum CodingKeys: String, CodingKey {
        case schema
        case conversionPath = "conversion_path"
        case id
        case reading
        case category
        case backend
        case backendVersion = "backend_version"
        case converterBackend = "converter_backend"
        case sourceRef = "source_ref"
        case resource
        case topK = "top_k"
        case corpus
        case candidates
        case compositionSpan = "composition_span"
        case measurement
    }
}

struct ABProbeResultV6: Encodable {
    let schema = "hazkey.ab-probe-result.v6"
    let conversionPath = ABProbeConversionPath.segmentCandidates.rawValue
    let id: String
    let reading: String
    let category: String
    let backend: String
    let backendVersion: String
    let converterBackend: String
    let sourceRef: String
    let resource: ABProbeResourceProvenance
    let topK: Int
    let corpus: ABProbeCorpusProvenance
    let candidates: [ABProbeV6Candidate]
    let compositionSpan: ABProbeCompositionSpan
    let producer: ABProbeFileIdentity
    let qualityPolicy: ABProbeQualityPolicy
    let measurement: ABProbeMeasurement

    init(
        v3: ABProbeResult,
        candidates: [ABProbeV6Candidate],
        compositionSpan: ABProbeCompositionSpan,
        producer: ABProbeFileIdentity,
        qualityPolicy: ABProbeQualityPolicy
    ) {
        id = v3.id
        reading = v3.reading
        category = v3.category
        backend = v3.backend
        backendVersion = v3.backendVersion
        converterBackend = v3.converterBackend
        sourceRef = v3.sourceRef
        resource = v3.resource
        topK = v3.topK
        corpus = v3.corpus
        self.candidates = candidates
        self.compositionSpan = compositionSpan
        self.producer = producer
        self.qualityPolicy = qualityPolicy
        measurement = v3.measurement
    }

    private enum CodingKeys: String, CodingKey {
        case schema
        case conversionPath = "conversion_path"
        case id
        case reading
        case category
        case backend
        case backendVersion = "backend_version"
        case converterBackend = "converter_backend"
        case sourceRef = "source_ref"
        case resource
        case topK = "top_k"
        case corpus
        case candidates
        case compositionSpan = "composition_span"
        case producer
        case qualityPolicy = "quality_policy"
        case measurement
    }
}

struct ABProbeNullableFixedBoundaryEvidence: Encodable, Equatable {
    let value: ABProbeFixedBoundaryEvidence?

    init(_ value: ABProbeFixedBoundaryEvidence?) {
        self.value = value
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        if let value {
            try container.encode(value)
        } else {
            try container.encodeNil()
        }
    }
}

struct ABProbeResultV7: Encodable {
    let schema = "hazkey.ab-probe-result.v7"
    let conversionPath: String
    let id: String
    let reading: String
    let category: String
    let backend: String
    let backendVersion: String
    let converterBackend: String
    let sourceRef: String
    let resource: ABProbeResourceProvenance
    let topK: Int
    let corpus: ABProbeCorpusProvenance
    let candidates: [ABProbeV6Candidate]
    let compositionSpan: ABProbeCompositionSpan
    let producer: ABProbeFileIdentity
    let qualityPolicy: ABProbeQualityPolicy
    let boundaryPolicy: ABProbeBoundaryPolicy
    let context: ABProbeLeftContextEvidence
    let fixedBoundary: ABProbeNullableFixedBoundaryEvidence
    let zenzaiExecution: ZenzaiExecutionEvidence
    let measurement: ABProbeMeasurement

    init(
        v3: ABProbeResult,
        candidates: [ABProbeV6Candidate],
        compositionSpan: ABProbeCompositionSpan,
        producer: ABProbeFileIdentity,
        qualityPolicy: ABProbeQualityPolicy,
        boundaryPolicy: ABProbeBoundaryPolicy,
        conversionPath: ABProbeConversionPath,
        context: ABProbeLeftContextEvidence,
        zenzaiExecution: ZenzaiExecutionEvidence,
        fixedBoundary: ABProbeFixedBoundaryEvidence? = nil
    ) {
        id = v3.id
        reading = v3.reading
        category = v3.category
        backend = v3.backend
        backendVersion = v3.backendVersion
        converterBackend = v3.converterBackend
        sourceRef = v3.sourceRef
        resource = v3.resource
        topK = v3.topK
        corpus = v3.corpus
        self.candidates = candidates
        self.compositionSpan = compositionSpan
        self.producer = producer
        self.qualityPolicy = qualityPolicy
        self.boundaryPolicy = boundaryPolicy
        self.conversionPath = conversionPath.rawValue
        self.context = context
        self.fixedBoundary = ABProbeNullableFixedBoundaryEvidence(fixedBoundary)
        self.zenzaiExecution = zenzaiExecution
        measurement = v3.measurement
    }

    private enum CodingKeys: String, CodingKey {
        case schema
        case conversionPath = "conversion_path"
        case id
        case reading
        case category
        case backend
        case backendVersion = "backend_version"
        case converterBackend = "converter_backend"
        case sourceRef = "source_ref"
        case resource
        case topK = "top_k"
        case corpus
        case candidates
        case compositionSpan = "composition_span"
        case producer
        case qualityPolicy = "quality_policy"
        case boundaryPolicy = "boundary_policy"
        case context
        case fixedBoundary = "fixed_boundary"
        case zenzaiExecution = "zenzai_execution"
        case measurement
    }
}

enum ABProbeZenzaiEvidenceValidation {
    static func validateExecutionEvidence(
        _ evidence: ZenzaiExecutionEvidence,
        caseID: String,
        boundaryMode: ABProbeBoundaryMode? = nil,
        inferenceLimit: Int? = nil
    ) throws {
        let expectedRequestCount = boundaryMode.map { mode in
            switch mode {
            case .isolatedDictionary:
                2
            case .nativeZenzaiFirstClause, .mozcFixed, .fullComposition:
                1
            }
        }
        let attemptsAreWithinLimit = inferenceLimit.map { limit in
            limit > 0
                && evidence.evaluationAttemptCount
                    <= limit * evidence.requestCount
        } ?? true
        let requestCountMatchesMode = expectedRequestCount.map {
            evidence.requestCount == $0
        } ?? true
        let counts = [
            evidence.requestCount,
            evidence.evaluationAttemptCount,
            evidence.attemptOutcomes.pass,
            evidence.attemptOutcomes.fixRequired,
            evidence.attemptOutcomes.wholeResult,
            evidence.attemptOutcomes.error,
            evidence.terminalOutcomes.pass,
            evidence.terminalOutcomes.fixRequired,
            evidence.terminalOutcomes.wholeResult,
            evidence.terminalOutcomes.error,
            evidence.terminalOutcomes.inferenceLimit,
            evidence.terminalOutcomes.noCandidate,
        ]
        guard counts.allSatisfy({ $0 >= 0 }),
              evidence.requestCount > 0,
              requestCountMatchesMode,
              attemptsAreWithinLimit,
              evidence.attemptOutcomes.total
                == evidence.evaluationAttemptCount,
              evidence.terminalOutcomes.total == evidence.requestCount,
              evidence.terminalOutcomes.pass
                <= evidence.attemptOutcomes.pass,
              evidence.terminalOutcomes.fixRequired
                <= evidence.attemptOutcomes.fixRequired,
              evidence.terminalOutcomes.wholeResult
                <= evidence.attemptOutcomes.wholeResult,
              evidence.terminalOutcomes.error
                <= evidence.attemptOutcomes.error
        else {
            throw ABProbeError.backendInstability(
                "invalid Zenzai execution evidence for case \(caseID)"
            )
        }
    }

    static func validate(
        requested: Bool,
        requiresObservedCandidateScore: Bool = true,
        requiresExecutionEvidence: Bool = false,
        observedScoreCount: Int,
        executionEvidence: [ZenzaiExecutionEvidence] = [],
        diagnostics: ZenzaiRuntimeDiagnosticsSnapshot?
    ) throws {
        guard requested else { return }
        guard let diagnostics,
              diagnostics.status == .modelLoadVerified,
              diagnostics.modelLoadVerified,
              diagnostics.zenzaiEnabledRequestCount > 0,
              diagnostics.modelLoadFailureCount == 0
        else {
            throw ABProbeError.backendInstability(
                "Zenzai model loading was not verified for the ABProbe run"
            )
        }
        guard !requiresObservedCandidateScore || observedScoreCount > 0 else {
            throw ABProbeError.backendInstability(
                "Zenzai was requested but no candidate evaluation score was observed"
            )
        }
        guard !requiresExecutionEvidence
                || (!executionEvidence.isEmpty
                    && executionEvidence.reduce(0) {
                        $0 + $1.evaluationAttemptCount
                    } > 0)
        else {
            throw ABProbeError.backendInstability(
                "Zenzai was requested but no model evaluation attempt was observed"
            )
        }
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
        let producerIdentity = options.resultSchema == .v6
            || options.resultSchema == .v7
            ? try ABProbeFileIdentity.currentProducer()
            : nil
        let zenzaiModelIdentity = try options.zenzaiModelPath.map {
            try ABProbeFileIdentity.capture(path: $0, label: "Zenzai model")
        }
        let provenance = try ABProbeProvenance.resolve(options: options)
        var didRemoveMozcRuntime = false
        defer {
            if let runtimePath = provenance.mozcRuntimePath,
               !didRemoveMozcRuntime {
                try? ABProbeMozcRuntimeSnapshot.remove(runtimePath: runtimePath)
            }
        }
        let corpus = try ABProbeCorpus.loadSnapshot(path: options.corpusPath)
        let leftContextSnapshot = try options.leftContextsPath.map {
            try ABProbeLeftContexts.load(path: $0, cases: corpus.cases)
        }
        let fixedBoundarySnapshot = try options.mozcFixedBoundariesPath.map {
            try ABProbeMozcFixedBoundaries.load(path: $0, cases: corpus.cases)
        }
        let adapter: any KanaKanjiConverting
        let diagnosticsProvider: () -> MozcSidecarDiagnostics?
        let zenzaiDiagnosticsStore: ZenzaiRuntimeDiagnosticsStore?
        let qualityPolicy: ABProbeQualityPolicy
        switch options.converterBackend {
        case .hazkey:
            let backendDevices = zenzaiModelIdentity == nil ? [] : getZenzaiDevices()
            if zenzaiModelIdentity != nil, backendDevices.isEmpty {
                throw ABProbeError.backendInstability(
                    "Zenzai was requested but no GGML backend device is available"
                )
            }
            if let requestedDevice = options.zenzaiDevice,
               !backendDevices.contains(where: { $0.name == requestedDevice }) {
                throw ABProbeError.backendInstability(
                    "requested Zenzai device is unavailable: \(requestedDevice)"
                )
            }
            let resolvedDevice = zenzaiModelIdentity.map { _ in
                resolveZenzaiBackendDeviceName(
                    configuredName: options.zenzaiDevice ?? "",
                    availableDevices: backendDevices.map(
                        ZenzaiBackendDeviceCandidate.init
                    )
                )
            }
            let modelURL = zenzaiModelIdentity.map {
                URL(fileURLWithPath: $0.path, isDirectory: false)
            }
            let config = HazkeyServerConfig(
                zenzaiBackendDevicesProvider: { backendDevices },
                zenzaiModelPathProvider: { modelURL },
                zenzaiBackendAvailableOverride: zenzaiModelIdentity == nil
                    ? false : nil
            )
            if let inferenceLimit = options.zenzaiInferenceLimit,
               let resolvedDevice {
                config.currentProfile.zenzaiEnable = true
                config.currentProfile.zenzaiInferLimit = Int32(inferenceLimit)
                config.currentProfile.zenzaiBackendDeviceName = resolvedDevice
                config.currentProfile.zenzaiContextualMode = leftContextSnapshot != nil
                config.currentProfile.zenzaiProfile = ""
                config.currentProfile.zenzaiTopic = ""
                config.currentProfile.zenzaiStyle = ""
                config.currentProfile.zenzaiPreference = ""
                config.currentProfile.useRichCandidates = false
                guard case .enabled = config.zenzaiRuntimeDecision(
                    zenzaiAllowed: true
                ) else {
                    throw ABProbeError.backendInstability(
                        "Zenzai backend or model is unavailable"
                    )
                }
            }
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
            let baseRequestOptions = requestOptions
            let validationZenzaiMode = zenzaiModelIdentity == nil
                ? ConvertRequestOptions.ZenzaiMode.off
                : config.genZenzaiMode(
                    leftContext: leftContextSnapshot?.entriesByID.values
                        .first(where: { !$0.leftContext.isEmpty })?.leftContext ?? "",
                    rightContext: "",
                    zenzaiAllowed: true,
                    contextualModeOverride: leftContextSnapshot == nil
                        ? nil
                        : true
                )
            guard zenzaiModelIdentity == nil || validationZenzaiMode.enabled else {
                throw ABProbeError.backendInstability(
                    "Zenzai mode could not be enabled for the ABProbe run"
                )
            }
            let runtimeDiagnosticsStore = zenzaiModelIdentity.map { _ in
                ZenzaiRuntimeDiagnosticsStore()
            }
            adapter = HazkeyKanaKanjiConverterAdapter(
                converter: converter,
                boundaryConverter: boundaryConverter,
                optionsProvider: { conversionOptions in
                    var perRequestOptions = baseRequestOptions
                    perRequestOptions.zenzaiMode = zenzaiModelIdentity == nil
                        ? .off
                        : config.genZenzaiMode(
                            leftContext: conversionOptions.leftContext,
                            rightContext: conversionOptions.rightContext,
                            zenzaiAllowed: conversionOptions.zenzaiEnabled,
                            contextualModeOverride: leftContextSnapshot == nil
                                ? nil
                                : !conversionOptions.leftContext.isEmpty
                        )
                    return perRequestOptions
                },
                projectDictionaryIndexProvider: { .empty },
                zenzaiDiagnosticsReporter: { conversionOptions, status in
                    runtimeDiagnosticsStore?.record(
                        decision: config.zenzaiRuntimeDecision(
                            zenzaiAllowed: conversionOptions.zenzaiEnabled
                        ),
                        converterStatus: status
                    )
                }
            )
            diagnosticsProvider = { nil }
            zenzaiDiagnosticsStore = runtimeDiagnosticsStore
            qualityPolicy = ABProbeQualityPolicy(
                context: leftContextSnapshot == nil
                    ? "empty"
                    : "left_context_sidecar",
                zenzai: ABProbeZenzaiQualityPolicy(
                    enabled: zenzaiModelIdentity != nil,
                    modelPath: zenzaiModelIdentity?.path,
                    modelSizeBytes: zenzaiModelIdentity?.sizeBytes,
                    modelSHA256: zenzaiModelIdentity?.sha256,
                    inferenceLimit: options.zenzaiInferenceLimit,
                    resolvedDevice: resolvedDevice
                )
            )
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
            zenzaiDiagnosticsStore = nil
            qualityPolicy = ABProbeQualityPolicy(
                zenzai: ABProbeZenzaiQualityPolicy(
                    enabled: false,
                    modelPath: nil,
                    modelSizeBytes: nil,
                    modelSHA256: nil,
                    inferenceLimit: nil,
                    resolvedDevice: nil
                )
            )
        }
        var didPurge = false
        defer {
            if !didPurge {
                adapter.purgeSensitiveState()
            }
        }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        var bufferedJSONLines: [Data] = []
        bufferedJSONLines.reserveCapacity(corpus.cases.count)
        var observedZenzaiScoreCount = 0
        var observedZenzaiExecutionEvidence: [ZenzaiExecutionEvidence] = []
        observedZenzaiExecutionEvidence.reserveCapacity(corpus.cases.count)

        for testCase in corpus.cases {
            let contextEntry = leftContextSnapshot?.entriesByID[testCase.id]
            let leftContext = contextEntry?.leftContext ?? ""
            let fixedBoundaryEntry = fixedBoundarySnapshot?.entriesByID[testCase.id]
            if options.boundaryMode == .mozcFixed,
               fixedBoundaryEntry == nil {
                throw ABProbeError.backendInstability(
                    "Mozc fixed-boundary evidence is missing for case \(testCase.id)"
                )
            }
            let composition = testCase.composition(
                leftContext: leftContext,
                targetCount: fixedBoundaryEntry?.consumingCount
            )
            let conversionOptions = ConversionOptions(
                allowLearning: false,
                zenzaiEnabled: zenzaiModelIdentity != nil,
                leftContext: leftContext,
                rightContext: "",
                suggestionListMode: .normal,
                suggestionListLimit: options.topK
            )

            for _ in 0..<options.warmups {
                adapter.stopComposition()
                _ = try requestCandidates(
                    from: adapter,
                    for: composition,
                    options: conversionOptions,
                    path: options.conversionPath
                )
            }
            let diagnosticsBefore = diagnosticsProvider()
            let processBefore = processMemoryKilobytes(processIdentifier: getpid())
            let backendBefore = diagnosticsBefore?.processIdentifier.flatMap {
                processMemoryKilobytes(processIdentifier: $0)
            }

            var samples: [Double] = []
            var finalCandidates: [ABProbeV4Candidate]?
            var finalV6Candidates: [ABProbeV6Candidate]?
            var finalZenzaiExecutionEvidence: ZenzaiExecutionEvidence?
            samples.reserveCapacity(options.iterations)
            for _ in 0..<options.iterations {
                adapter.stopComposition()
                let started = DispatchTime.now().uptimeNanoseconds
                let output = try requestCandidates(
                    from: adapter,
                    for: composition,
                    options: conversionOptions,
                    path: options.conversionPath
                )
                let finished = DispatchTime.now().uptimeNanoseconds
                samples.append(Double(finished - started) / 1_000_000)
                if options.resultSchema == .v7 {
                    guard let executionEvidence = output.zenzaiExecutionEvidence else {
                        throw ABProbeError.backendInstability(
                            "Zenzai execution evidence is missing for case \(testCase.id)"
                        )
                    }
                    try ABProbeZenzaiEvidenceValidation.validateExecutionEvidence(
                        executionEvidence,
                        caseID: testCase.id,
                        boundaryMode: options.boundaryMode,
                        inferenceLimit: options.zenzaiInferenceLimit
                    )
                    if let finalZenzaiExecutionEvidence,
                       finalZenzaiExecutionEvidence != executionEvidence {
                        throw ABProbeError.candidateDrift(
                            "Zenzai execution evidence drifted during case \(testCase.id)"
                        )
                    }
                    finalZenzaiExecutionEvidence = executionEvidence
                }
                let observed = ABProbeCandidateObservation.capture(
                    output.candidates,
                    topK: options.topK
                )
                if let finalCandidates {
                    try ABProbeCandidateObservation.validateStable(
                        reference: finalCandidates,
                        observed: observed,
                        resultSchema: options.resultSchema,
                        caseID: testCase.id
                    )
                }
                finalCandidates = observed
                if options.resultSchema == .v6 || options.resultSchema == .v7 {
                    let observedV6 = try ABProbeCandidateObservation.captureV6(
                        output.candidates,
                        topK: options.topK
                    )
                    if let fixedBoundaryEntry,
                       !observedV6.allSatisfy({
                           $0.consumingCount == fixedBoundaryEntry.consumingCount
                       }) {
                        throw ABProbeError.backendInstability(
                            "fixed-boundary candidates escaped the Mozc span for case \(testCase.id)"
                        )
                    }
                    if options.boundaryMode == .fullComposition,
                       !observedV6.allSatisfy({
                           $0.consumingCount == composition.elements.count
                       }) {
                        throw ABProbeError.backendInstability(
                            "full-composition candidates escaped the entire composition for case \(testCase.id)"
                        )
                    }
                    guard observedV6.allSatisfy({ candidate in
                        candidate.zenzaiScore == nil
                            || candidate.rankingInfluence
                                == CandidateRankingInfluence.zenzai.rawValue
                    }) else {
                        throw ABProbeError.backendInstability(
                            "a Zenzai score was emitted without Zenzai ranking influence"
                        )
                    }
                    if let finalV6Candidates {
                        try ABProbeCandidateObservation.validateStable(
                            reference: finalV6Candidates,
                            observed: observedV6,
                            caseID: testCase.id
                        )
                    }
                    observedZenzaiScoreCount += observedV6.reduce(into: 0) {
                        if $1.zenzaiScore != nil { $0 += 1 }
                    }
                    finalV6Candidates = observedV6
                }
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
            let capturedCandidates = finalCandidates ?? []
            let capturedV6Candidates = finalV6Candidates ?? []
            let capturedZenzaiExecutionEvidence = finalZenzaiExecutionEvidence
            let v3Result = ABProbeResult(
                id: testCase.id,
                reading: testCase.reading,
                category: testCase.category,
                backend: options.backendName,
                backendVersion: hazkeyVersion,
                converterBackend: options.converterBackend.rawValue,
                sourceRef: provenance.sourceRef,
                resource: provenance.resource,
                topK: options.topK,
                corpus: corpus.provenance,
                candidates: capturedCandidates.map(\.text),
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
            var encoded: Data
            switch options.resultSchema {
            case .v3:
                encoded = try encoder.encode(v3Result)
            case .v4:
                encoded = try encoder.encode(
                    ABProbeResultV4(v3: v3Result, candidates: capturedCandidates)
                )
            case .v5:
                encoded = try encoder.encode(
                    ABProbeResultV5(
                        v3: v3Result,
                        candidates: capturedCandidates,
                        compositionSpan: .entireComposition(composition)
                    )
                )
            case .v6:
                guard let producerIdentity else {
                    throw ABProbeError.backendInstability(
                        "ABProbe producer identity was not acquired"
                    )
                }
                encoded = try encoder.encode(
                    ABProbeResultV6(
                        v3: v3Result,
                        candidates: capturedV6Candidates,
                        compositionSpan: .entireComposition(composition),
                        producer: producerIdentity,
                        qualityPolicy: qualityPolicy
                    )
                )
            case .v7:
                guard let producerIdentity,
                      let leftContextSnapshot,
                      let contextEntry,
                      let capturedZenzaiExecutionEvidence
                else {
                    throw ABProbeError.backendInstability(
                        "ABProbe contextual evidence was not acquired"
                    )
                }
                encoded = try encoder.encode(
                    ABProbeResultV7(
                        v3: v3Result,
                        candidates: capturedV6Candidates,
                        compositionSpan: .entireComposition(composition),
                        producer: producerIdentity,
                        qualityPolicy: qualityPolicy,
                        boundaryPolicy: ABProbeBoundaryPolicy(
                            mode: options.boundaryMode
                        ),
                        conversionPath: options.conversionPath,
                        context: contextEntry.evidence(
                            source: leftContextSnapshot.source
                        ),
                        zenzaiExecution: capturedZenzaiExecutionEvidence,
                        fixedBoundary: fixedBoundaryEntry.flatMap { entry in
                            fixedBoundarySnapshot.map {
                                entry.evidence(source: $0.source)
                            }
                        }
                    )
                )
                observedZenzaiExecutionEvidence.append(
                    capturedZenzaiExecutionEvidence
                )
            }
            encoded.append(0x0A)
            bufferedJSONLines.append(encoded)
        }
        try ABProbeJSONOutput.publishBuffered(
            bufferedJSONLines,
            to: jsonOutput
        ) {
            adapter.purgeSensitiveState()
            didPurge = true
            try producerIdentity?.revalidate(label: "ABProbe producer")
            try zenzaiModelIdentity?.revalidate(label: "Zenzai model")
            try leftContextSnapshot?.fileIdentity.revalidate(
                label: "left-context sidecar"
            )
            try fixedBoundarySnapshot?.fileIdentity.revalidate(
                label: "Mozc fixed-boundary sidecar"
            )
            try ABProbeZenzaiEvidenceValidation.validate(
                requested: zenzaiModelIdentity != nil,
                requiresObservedCandidateScore: options.resultSchema == .v6,
                requiresExecutionEvidence: options.resultSchema == .v7,
                observedScoreCount: observedZenzaiScoreCount,
                executionEvidence: observedZenzaiExecutionEvidence,
                diagnostics: zenzaiDiagnosticsStore?.snapshot()
            )
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

    static func requestCandidates(
        from adapter: any KanaKanjiConverting,
        for composition: CompositionInput,
        options: ConversionOptions,
        path: ABProbeConversionPath
    ) throws -> ConversionOutput {
        switch path {
        case .candidates:
            return try adapter.candidates(for: composition, options: options)
        case .segmentCandidates:
            return try adapter.segmentCandidates(
                for: composition,
                options: options
            )
        case .nativeSegmentCandidates:
            guard let hazkeyAdapter = adapter as? HazkeyKanaKanjiConverterAdapter else {
                throw ABProbeError.backendInstability(
                    "native Zenzai boundary probing requires the Hazkey adapter"
                )
            }
            return hazkeyAdapter.nativeZenzaiSegmentCandidatesForProbe(
                for: composition,
                options: options
            )
        case .mozcFixedSegmentCandidates:
            guard let targetCount = composition.targetCount,
                  let hazkeyAdapter = adapter as? HazkeyKanaKanjiConverterAdapter
            else {
                throw ABProbeError.backendInstability(
                    "Mozc fixed-boundary probing requires a target span and the Hazkey adapter"
                )
            }
            let output = try hazkeyAdapter.candidates(
                for: composition,
                options: options
            )
            return ABProbeCandidateObservation.constrain(
                output,
                toFixedBoundary: targetCount
            )
        case .fullCompositionCandidates:
            let output = try adapter.candidates(
                for: composition,
                options: options
            )
            return ABProbeCandidateObservation.constrain(
                output,
                toFixedBoundary: composition.elements.count
            )
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
