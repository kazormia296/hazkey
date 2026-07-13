import Foundation

struct ConversionOptions: Equatable, Sendable {
    let allowLearning: Bool
    let zenzaiEnabled: Bool
    let leftContext: String
    let rightContext: String

    static let `default` = ConversionOptions(
        allowLearning: true,
        zenzaiEnabled: true,
        leftContext: "",
        rightContext: ""
    )
}

struct ConverterCandidate: Equatable, Hashable, Codable, Sendable {
    let text: String
    let annotation: String?
    let consumingCount: Int
    let sourceID: String?

    init(
        text: String,
        annotation: String? = nil,
        consumingCount: Int,
        sourceID: String? = nil
    ) {
        self.text = text
        self.annotation = annotation
        self.consumingCount = max(1, consumingCount)
        self.sourceID = sourceID
    }
}

struct ConversionOutput: Equatable, Sendable {
    let candidates: [ConverterCandidate]
    let pageSize: Int
}

struct RealtimeConversionOutput: Equatable, Sendable {
    let liveCandidate: ConverterCandidate?
    let candidates: [ConverterCandidate]
    let pageSize: Int
}

enum ImeAutoConvertMode: String, Codable, Sendable, Equatable {
    case disabled
    case always
    case forMultipleChars
}

enum ImeSuggestionListMode: String, Codable, Sendable, Equatable {
    case disabled
    case normal
    case predictive
}

struct CompositionDisplay: Equatable, Sendable {
    let text: String
    let caretUtf8ByteOffset: UInt32
}

protocol KanaKanjiConverting: AnyObject {
    func display(for composition: CompositionInput) -> CompositionDisplay

    func candidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput

    func realtimeCandidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> RealtimeConversionOutput

    func predictions(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput

    func setCompletedData(_ candidate: ConverterCandidate)
    func updateLearningData(_ candidate: ConverterCandidate)
    func commitLearning()
    func forget(_ candidate: ConverterCandidate)
    func stopComposition()
}

extension KanaKanjiConverting {
    func display(for composition: CompositionInput) -> CompositionDisplay {
        let cursor = min(max(composition.cursor, 0), composition.elements.count)
        let text = composition.elements.map(\.text).joined()
        let caret = composition.elements.prefix(cursor).reduce(0) {
            $0 + $1.text.utf8.count
        }
        return CompositionDisplay(
            text: text,
            caretUtf8ByteOffset: UInt32(caret)
        )
    }

    func predictions(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        ConversionOutput(candidates: [], pageSize: 0)
    }

    func realtimeCandidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> RealtimeConversionOutput {
        let output = try candidates(for: composition, options: options)
        return RealtimeConversionOutput(
            liveCandidate: output.candidates.first,
            candidates: output.candidates,
            pageSize: output.pageSize
        )
    }
}

final class NoopKanaKanjiConverter: KanaKanjiConverting {
    func candidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        guard !composition.elements.isEmpty else {
            return ConversionOutput(candidates: [], pageSize: 0)
        }
        let targetCount = min(
            max(composition.targetCount ?? composition.elements.count, 1),
            composition.elements.count
        )
        return ConversionOutput(
            candidates: [
                ConverterCandidate(
                    text: composition.elements.prefix(targetCount).map(\.text).joined(),
                    consumingCount: targetCount
                )
            ],
            pageSize: 1
        )
    }

    func setCompletedData(_ candidate: ConverterCandidate) {}
    func updateLearningData(_ candidate: ConverterCandidate) {}
    func commitLearning() {}
    func forget(_ candidate: ConverterCandidate) {}
    func stopComposition() {}
}

struct RecoveryCheckpoint: Equatable, Codable, Sendable {
    let revision: UInt64
    let phase: ImePhase
    let composition: CompositionBuffer
    let nextCandidateGeneration: UInt64
    let nextEffectID: UInt64
    let leftContext: String
    let rightContext: String
    let policy: PinnedCompositionPolicy
    let reconversionReplacement: ReconversionReplacement?
    let unicodeInputBuffer: String?
    let phaseBeforeUnicodeInput: ImePhase?

    init(
        revision: UInt64,
        phase: ImePhase,
        composition: CompositionBuffer,
        nextCandidateGeneration: UInt64,
        nextEffectID: UInt64,
        leftContext: String,
        rightContext: String,
        policy: PinnedCompositionPolicy,
        reconversionReplacement: ReconversionReplacement? = nil,
        unicodeInputBuffer: String? = nil,
        phaseBeforeUnicodeInput: ImePhase? = nil
    ) {
        self.revision = revision
        self.phase = phase
        self.composition = composition
        self.nextCandidateGeneration = nextCandidateGeneration
        self.nextEffectID = nextEffectID
        self.leftContext = leftContext
        self.rightContext = rightContext
        self.policy = policy
        self.reconversionReplacement = reconversionReplacement
        self.unicodeInputBuffer = unicodeInputBuffer
        self.phaseBeforeUnicodeInput = phaseBeforeUnicodeInput
    }

    private enum CodingKeys: String, CodingKey {
        case revision
        case phase
        case composition
        case nextCandidateGeneration
        case nextEffectID
        case leftContext
        case rightContext
        case policy
        case reconversionReplacement
        case unicodeInputBuffer
        case phaseBeforeUnicodeInput
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        revision = try container.decode(UInt64.self, forKey: .revision)
        phase = try container.decode(ImePhase.self, forKey: .phase)
        composition = try container.decode(CompositionBuffer.self, forKey: .composition)
        nextCandidateGeneration = try container.decode(
            UInt64.self,
            forKey: .nextCandidateGeneration
        )
        nextEffectID = try container.decode(UInt64.self, forKey: .nextEffectID)
        leftContext = try container.decode(String.self, forKey: .leftContext)
        rightContext = try container.decode(String.self, forKey: .rightContext)
        policy = try container.decode(PinnedCompositionPolicy.self, forKey: .policy)
        reconversionReplacement = try container.decodeIfPresent(
            ReconversionReplacement.self,
            forKey: .reconversionReplacement
        )
        unicodeInputBuffer = try container.decodeIfPresent(
            String.self,
            forKey: .unicodeInputBuffer
        )
        phaseBeforeUnicodeInput = try container.decodeIfPresent(
            ImePhase.self,
            forKey: .phaseBeforeUnicodeInput
        )
    }

    /// Secure input checkpoints are intentionally never persisted by callers.
    func persistedData(isSecureInput: Bool) throws -> Data? {
        guard !isSecureInput else { return nil }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(self)
    }
}

struct SessionContext: Equatable, Codable, Sendable {
    let sessionID: String
    var leftContext: String
    var rightContext: String = ""
    var projectRevision: UInt64
}

struct PinnedKeymapRule: Equatable, Codable, Sendable {
    let intention: String
    let inputOverride: String?
}

struct PinnedCompositionPolicy: Equatable, Codable, Sendable {
    var allowsLearning: Bool
    var secureInput: Bool
    var zenzaiEnabled: Bool
    var autoConvertMode: ImeAutoConvertMode
    var projectRevision: UInt64
    var inputTableName: String? = nil
    var keymap: [String: PinnedKeymapRule] = [:]

    static let `default` = PinnedCompositionPolicy(
        allowsLearning: true,
        secureInput: false,
        zenzaiEnabled: true,
        projectRevision: 0,
        autoConvertMode: .disabled,
        inputTableName: nil,
        keymap: [:]
    )

    private enum CodingKeys: String, CodingKey {
        case allowsLearning
        case secureInput
        case zenzaiEnabled
        case autoConvertMode
        case projectRevision
        case inputTableName
        case keymap
    }

    init(
        allowsLearning: Bool,
        secureInput: Bool,
        zenzaiEnabled: Bool,
        projectRevision: UInt64,
        autoConvertMode: ImeAutoConvertMode = .disabled,
        inputTableName: String? = nil,
        keymap: [String: PinnedKeymapRule] = [:]
    ) {
        self.allowsLearning = allowsLearning
        self.secureInput = secureInput
        self.zenzaiEnabled = zenzaiEnabled
        self.autoConvertMode = autoConvertMode
        self.projectRevision = projectRevision
        self.inputTableName = inputTableName
        self.keymap = keymap
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        allowsLearning = try container.decode(Bool.self, forKey: .allowsLearning)
        secureInput = try container.decode(Bool.self, forKey: .secureInput)
        zenzaiEnabled = try container.decode(Bool.self, forKey: .zenzaiEnabled)
        autoConvertMode = try container.decodeIfPresent(
            ImeAutoConvertMode.self,
            forKey: .autoConvertMode
        ) ?? .disabled
        projectRevision = try container.decode(UInt64.self, forKey: .projectRevision)
        inputTableName = try container.decodeIfPresent(
            String.self,
            forKey: .inputTableName
        )
        keymap = try container.decodeIfPresent(
            [String: PinnedKeymapRule].self,
            forKey: .keymap
        ) ?? [:]
    }
}
