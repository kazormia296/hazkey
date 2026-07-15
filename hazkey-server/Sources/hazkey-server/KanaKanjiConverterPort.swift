import Foundation

struct ConversionOptions: Equatable, Sendable {
    /// Matches the settings UI's supported range. Keeping the converter-side
    /// bound here prevents a malformed config or recovery policy from turning
    /// `N_best` into an unbounded allocation request.
    static let supportedSuggestionListLimits = 1...10

    let allowLearning: Bool
    let zenzaiEnabled: Bool
    /// Privacy boundary for out-of-process converters. This is separate from
    /// learning/Zenzai because a normal learning-off composition may still be
    /// sent to a converter while secure input must never cross that boundary.
    let secureInput: Bool
    let leftContext: String
    let rightContext: String
    let suggestionListMode: ImeSuggestionListMode
    let suggestionListLimit: Int

    init(
        allowLearning: Bool,
        zenzaiEnabled: Bool,
        secureInput: Bool = false,
        leftContext: String,
        rightContext: String,
        suggestionListMode: ImeSuggestionListMode,
        suggestionListLimit: Int = 9
    ) {
        self.allowLearning = allowLearning
        self.zenzaiEnabled = zenzaiEnabled
        self.secureInput = secureInput
        self.leftContext = leftContext
        self.rightContext = rightContext
        self.suggestionListMode = suggestionListMode
        self.suggestionListLimit = Self.clampSuggestionListLimit(
            suggestionListLimit
        )
    }

    static func clampSuggestionListLimit(_ value: Int) -> Int {
        min(
            max(value, supportedSuggestionListLimits.lowerBound),
            supportedSuggestionListLimits.upperBound
        )
    }

    static let `default` = ConversionOptions(
        allowLearning: true,
        zenzaiEnabled: true,
        secureInput: false,
        leftContext: "",
        rightContext: "",
        suggestionListMode: .predictive,
        suggestionListLimit: 9
    )
}

struct ConverterCandidate: Equatable, Hashable, Codable, Sendable {
    let text: String
    let annotation: String?
    let consumingCount: Int
    let sourceID: String?
    let provenance: CandidateProvenance
    let isLearnable: Bool

    init(
        text: String,
        annotation: String? = nil,
        consumingCount: Int,
        sourceID: String? = nil,
        provenance: CandidateProvenance = .unknown,
        isLearnable: Bool = true
    ) {
        self.text = text
        self.annotation = annotation
        self.consumingCount = max(1, consumingCount)
        self.sourceID = sourceID
        self.provenance = provenance
        self.isLearnable = isLearnable
    }
}

enum CandidateProvenance: String, Codable, Hashable, Sendable {
    case standard
    case personalDictionary
    case projectDictionary
    case temporaryDictionary
    case zenzai
    case builtInGuard
    case unknown
}

struct ConverterLearningToken: Hashable, Codable, Sendable {
    let rawValue: String
}

/// Backend-wide persistence support. Composition policy can further disable
/// learning, but it cannot enable persistence on a conversion-only backend.
enum ConverterLearningCapability: String, Codable, Sendable, Equatable {
    case persistent
    case conversionOnly

    var persistentLearningAvailable: Bool { self == .persistent }
}

enum LearningOrigin: String, Codable, Sendable, Equatable {
    case liveConversion
    case directCommit
    case explicitConversion
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

/// Internal revision for speculative conversion work. Unlike the protocol
/// snapshot revision, this advances only when editable composition semantics
/// change, so live-presentation refreshes and candidate navigation do not make
/// an otherwise valid background result stale.
struct CompositionRevision: Equatable, Hashable, Sendable {
    let rawValue: UInt64
}

struct SpeculativeConversionContext: Equatable, Sendable {
    let revision: CompositionRevision
    let input: CompositionInput
    let options: ConversionOptions
    let projectRevision: UInt64
    /// Snapshot of backend-wide persisted learning. Reducers leave this at
    /// zero; a composite converter that owns the revision store replaces it
    /// before scheduling work.
    let learningRevision: UInt64

    init(
        revision: CompositionRevision,
        input: CompositionInput,
        options: ConversionOptions,
        projectRevision: UInt64,
        learningRevision: UInt64 = 0
    ) {
        self.revision = revision
        self.input = input
        self.options = options
        self.projectRevision = projectRevision
        self.learningRevision = learningRevision
    }
}

enum SpeculationInvalidationReason: String, Equatable, Hashable, Sendable {
    case edit
    case cursorMove
    case segmentResize
    case commit
    case cancel
    case dictionaryChange
    case restore
    case lifecycle
    case secureTransition
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

enum ImeAuxTextMode: String, Codable, Sendable, Equatable {
    case disabled
    case always
    case whenCursorNotAtEnd
}

struct DirectCommitTargetSet: OptionSet, Codable, Sendable, Equatable {
    let rawValue: UInt32

    static let comma = Self(rawValue: 1 << 0)
    static let period = Self(rawValue: 1 << 1)
    static let question = Self(rawValue: 1 << 2)
    static let exclamation = Self(rawValue: 1 << 3)
    static let initial: Self = []

    init(rawValue: UInt32) {
        self.rawValue = rawValue
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        self.init(rawValue: try container.decode(UInt32.self))
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }

    func contains(renderedSuffix: String) -> Bool {
        guard let scalar = renderedSuffix.unicodeScalars.last,
              renderedSuffix.unicodeScalars.count == 1 else {
            return false
        }
        switch scalar.value {
        case 0x3001: return contains(.comma) // 、
        case 0x3002: return contains(.period) // 。
        case 0xFF1F, 0x003F: return contains(.question) // ？ or ?
        case 0xFF01, 0x0021: return contains(.exclamation) // ！ or !
        default: return false
        }
    }
}

struct CompositionDisplay: Equatable, Sendable {
    let text: String
    let caretUtf8ByteOffset: UInt32
}

protocol KanaKanjiConverting: AnyObject {
    /// Whether this converter can expose stable first-clause candidates for a
    /// fully segmented, still-uncommitted conversion plan.
    var supportsSegmentEditing: Bool { get }

    func display(for composition: CompositionInput) -> CompositionDisplay

    /// Returns an input-element cursor positioned at the requested display
    /// boundary. Converters whose input and rendered surfaces differ can skip
    /// input indices that do not correspond to a stable visible caret.
    func inputCursorPosition(
        for composition: CompositionInput,
        movingBy offset: Int
    ) -> Int

    func candidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput

    /// Returns candidates for the first natural clause of the composition.
    /// The reducer uses this to retain a complete, editable list of clauses
    /// instead of treating conversion as a single prefix plus raw suffix.
    func segmentCandidates(
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
    func stageLearning(
        candidate: ConverterCandidate,
        reading: String
    ) -> ConverterLearningToken?
    func commitStagedLearning(_ token: ConverterLearningToken)
    func discardStagedLearning(_ token: ConverterLearningToken)
    func forget(_ candidate: ConverterCandidate)
    func stopComposition()
    /// Stops conversion and removes every retained converter candidate.
    /// Secure-input and other privacy boundaries use this instead of the
    /// rollback-preserving `stopComposition()` path.
    func purgeSensitiveState()

    /// Starts optional background work for the current editable composition.
    /// Implementations must return immediately and must not publish candidates
    /// from the completion callback.
    func prepareSpeculativeConversion(_ context: SpeculativeConversionContext)

    /// Invalidates every result prepared for the previous composition revision.
    func invalidateSpeculativeConversion(reason: SpeculationInvalidationReason)

    /// Freezes the candidate order for formal conversion. An implementation may
    /// snapshot an already-completed immutable prefix, but unfinished work must
    /// never be inserted after this call returns.
    func lockCandidateOrder(for revision: CompositionRevision)

    /// Signals that every candidate previously published to the reducer has
    /// become unreachable. Implementations may release foreground reservations
    /// that were retained for candidate-origin learning.
    func retireCandidateWindow()
}

extension KanaKanjiConverting {
    var supportsSegmentEditing: Bool { false }

    func inputCursorPosition(
        for composition: CompositionInput,
        movingBy offset: Int
    ) -> Int {
        let cursor = min(max(composition.cursor, 0), composition.elements.count)
        let (moved, overflow) = cursor.addingReportingOverflow(offset)
        if overflow {
            return offset < 0 ? 0 : composition.elements.count
        }
        return min(max(moved, 0), composition.elements.count)
    }

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

    func segmentCandidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        try candidates(for: composition, options: options)
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

    func stageLearning(
        candidate: ConverterCandidate,
        reading: String
    ) -> ConverterLearningToken? {
        return nil
    }

    func commitStagedLearning(_ token: ConverterLearningToken) {}

    func discardStagedLearning(_ token: ConverterLearningToken) {}

    func purgeSensitiveState() {
        stopComposition()
    }

    func prepareSpeculativeConversion(_ context: SpeculativeConversionContext) {}

    func invalidateSpeculativeConversion(reason: SpeculationInvalidationReason) {}

    func lockCandidateOrder(for revision: CompositionRevision) {}

    func retireCandidateWindow() {}
}

final class NoopKanaKanjiConverter: KanaKanjiConverting {
    var supportsSegmentEditing: Bool { true }

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
    var liveConversionDelayMilliseconds: UInt32
    var suggestionListMode: ImeSuggestionListMode
    var suggestionListLimit: Int
    /// `false` only while decoding a checkpoint written before the suggestion
    /// limit became composition-pinned. The decoded semantic fallback remains
    /// 9 for standalone Codable compatibility; restore can distinguish that
    /// fallback from an explicitly pinned value and rebind it to the current
    /// session configuration.
    var suggestionListLimitWasPresentInEncodedPolicy = true
    var auxTextMode: ImeAuxTextMode
    var directCommitTargets: DirectCommitTargetSet
    var projectRevision: UInt64
    var inputTableName: String? = nil
    var keymap: [String: PinnedKeymapRule] = [:]

    static let `default` = PinnedCompositionPolicy(
        allowsLearning: true,
        secureInput: false,
        zenzaiEnabled: true,
        projectRevision: 0,
        autoConvertMode: .disabled,
        liveConversionDelayMilliseconds: 228,
        suggestionListMode: .predictive,
        suggestionListLimit: 9,
        auxTextMode: .whenCursorNotAtEnd,
        directCommitTargets: [],
        inputTableName: nil,
        keymap: [:]
    )

    private enum CodingKeys: String, CodingKey {
        case allowsLearning
        case secureInput
        case zenzaiEnabled
        case autoConvertMode
        case liveConversionDelayMilliseconds
        case suggestionListMode
        case suggestionListLimit
        case auxTextMode
        case directCommitTargets
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
        liveConversionDelayMilliseconds: UInt32 = 228,
        suggestionListMode: ImeSuggestionListMode = .predictive,
        suggestionListLimit: Int = 9,
        auxTextMode: ImeAuxTextMode = .whenCursorNotAtEnd,
        directCommitTargets: DirectCommitTargetSet = [],
        inputTableName: String? = nil,
        keymap: [String: PinnedKeymapRule] = [:]
    ) {
        self.allowsLearning = allowsLearning
        self.secureInput = secureInput
        self.zenzaiEnabled = zenzaiEnabled
        self.autoConvertMode = autoConvertMode
        self.liveConversionDelayMilliseconds = liveConversionDelayMilliseconds
        self.suggestionListMode = suggestionListMode
        self.suggestionListLimit = ConversionOptions.clampSuggestionListLimit(
            suggestionListLimit
        )
        suggestionListLimitWasPresentInEncodedPolicy = true
        self.auxTextMode = auxTextMode
        self.directCommitTargets = directCommitTargets
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
        liveConversionDelayMilliseconds = try container.decodeIfPresent(
            UInt32.self,
            forKey: .liveConversionDelayMilliseconds
        ) ?? 228
        suggestionListMode = try container.decodeIfPresent(
            ImeSuggestionListMode.self,
            forKey: .suggestionListMode
        ) ?? .predictive
        let decodedSuggestionListLimit: Int
        if container.contains(.suggestionListLimit) {
            suggestionListLimitWasPresentInEncodedPolicy = true
            decodedSuggestionListLimit = try container.decode(
                Int.self,
                forKey: .suggestionListLimit
            )
        } else {
            suggestionListLimitWasPresentInEncodedPolicy = false
            decodedSuggestionListLimit = 9
        }
        guard ConversionOptions.supportedSuggestionListLimits.contains(
            decodedSuggestionListLimit
        ) else {
            throw DecodingError.dataCorruptedError(
                forKey: .suggestionListLimit,
                in: container,
                debugDescription: "suggestionListLimit must be between 1 and 10"
            )
        }
        suggestionListLimit = decodedSuggestionListLimit
        auxTextMode = try container.decodeIfPresent(
            ImeAuxTextMode.self,
            forKey: .auxTextMode
        ) ?? .whenCursorNotAtEnd
        directCommitTargets = try container.decodeIfPresent(
            DirectCommitTargetSet.self,
            forKey: .directCommitTargets
        ) ?? []
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

    mutating func rebindLegacySuggestionListLimit(to currentValue: Int) {
        guard !suggestionListLimitWasPresentInEncodedPolicy else { return }
        suggestionListLimit = ConversionOptions.clampSuggestionListLimit(currentValue)
        suggestionListLimitWasPresentInEncodedPolicy = true
    }
}
