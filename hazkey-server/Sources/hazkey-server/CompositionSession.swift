import Foundation

enum CandidateSetOrigin: Equatable, Sendable {
    case conversion
    case prediction
}

struct CandidateSet: Equatable, Sendable {
    let generation: UInt64
    var items: [CandidateSnapshot]
    var selectedIndex: Int?
    let pageSize: Int
    let origin: CandidateSetOrigin
    let liveCandidate: CandidateSnapshot?

    func snapshot() -> CandidateWindowSnapshot {
        CandidateWindowSnapshot(
            generation: generation,
            items: items,
            selectedIndex: selectedIndex,
            pageSize: pageSize
        )
    }

    var selectedCandidate: CandidateSnapshot? {
        guard let selectedIndex, items.indices.contains(selectedIndex) else {
            return nil
        }
        return items[selectedIndex]
    }
}

/// One uncommitted conversion segment. Candidate state stays with the segment
/// while focus moves, so revisiting a segment restores the user's selection
/// instead of regenerating or resetting it.
struct CompositionSegment: Equatable, Sendable {
    var inputCount: Int
    var candidates: CandidateSet

    var selectedCandidate: CandidateSnapshot? {
        candidates.selectedCandidate
    }
}

struct ReconversionReplacement: Equatable, Codable, Sendable {
    let before: Int
    let after: Int
}

struct MaterializedLivePrefix: Equatable, Sendable {
    let text: String
    let consumedElementCount: Int
    let sourceElements: [CompositionElement]
    let sourceReading: String
    let candidate: CandidateSnapshot?
}

struct LivePresentationState: Equatable, Sendable {
    var materializedPrefix: MaterializedLivePrefix?
    var pendingRevision: UInt64?

    static let empty = LivePresentationState(
        materializedPrefix: nil,
        pendingRevision: nil
    )
}

struct PendingLearningTransaction: Equatable, Sendable {
    let token: ConverterLearningToken
    let reading: String
    let surface: String
    let origin: LearningOrigin
    let createdRevision: UInt64
}

struct CompositionSession: Equatable, Sendable {
    var phase: ImePhase = .idle
    var composingText = CompositionBuffer()
    var activeBoundary: Int?
    var candidates: CandidateSet?
    var segments: [CompositionSegment] = []
    var activeSegmentIndex: Int?
    var revision: UInt64 = 0
    var nextCandidateGeneration: UInt64 = 0
    var nextEffectID: UInt64 = 1
    var context: SessionContext
    var policy: PinnedCompositionPolicy
    var recoveryCheckpoint: RecoveryCheckpoint?
    var reconversionReplacement: ReconversionReplacement?
    var unicodeInputBuffer = ""
    var phaseBeforeUnicodeInput: ImePhase?
    /// Non-persistent presentation cache. It may be discarded at any point
    /// without changing the semantic composition or recovery checkpoint.
    var livePresentation = LivePresentationState.empty
    /// Learning is held outside the checkpoint and resolved explicitly at the
    /// next stable boundary or cancellation action.
    var pendingLearningTransactions: [PendingLearningTransaction] = []

    init(
        sessionID: String = UUID().uuidString,
        context: SessionContext? = nil,
        policy: PinnedCompositionPolicy = .default
    ) {
        self.context = context ?? SessionContext(
            sessionID: sessionID,
            leftContext: "",
            projectRevision: policy.projectRevision
        )
        self.policy = policy
    }

    var isComposing: Bool { !composingText.isEmpty }

    mutating func allocateCandidateGeneration() -> UInt64 {
        nextCandidateGeneration &+= 1
        return nextCandidateGeneration
    }

    mutating func allocateEffectID() -> UInt64 {
        defer { nextEffectID &+= 1 }
        return nextEffectID
    }

    mutating func advanceRevision() {
        revision &+= 1
        guard !policy.secureInput else {
            // Secure text must not survive even in an in-memory checkpoint.
            // The transport already omits it, but retaining it here would let
            // a later secure -> non-secure transition publish stale secrets.
            recoveryCheckpoint = nil
            return
        }
        recoveryCheckpoint = RecoveryCheckpoint(
            revision: revision,
            phase: phase,
            composition: composingText,
            nextCandidateGeneration: nextCandidateGeneration,
            nextEffectID: nextEffectID,
            leftContext: context.leftContext,
            rightContext: context.rightContext,
            policy: policy,
            reconversionReplacement: reconversionReplacement,
            unicodeInputBuffer: unicodeInputBuffer,
            phaseBeforeUnicodeInput: phaseBeforeUnicodeInput
        )
    }
}
