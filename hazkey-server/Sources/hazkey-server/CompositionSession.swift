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

    func snapshot() -> CandidateWindowSnapshot {
        CandidateWindowSnapshot(
            generation: generation,
            items: items,
            selectedIndex: selectedIndex,
            pageSize: pageSize
        )
    }
}

struct ReconversionReplacement: Equatable, Codable, Sendable {
    let before: Int
    let after: Int
}

struct CompositionSession: Equatable, Sendable {
    var phase: ImePhase = .idle
    var composingText = CompositionBuffer()
    var activeBoundary: Int?
    var candidates: CandidateSet?
    var revision: UInt64 = 0
    var nextCandidateGeneration: UInt64 = 0
    var nextEffectID: UInt64 = 1
    var context: SessionContext
    var policy: PinnedCompositionPolicy
    var recoveryCheckpoint: RecoveryCheckpoint?
    var reconversionReplacement: ReconversionReplacement?
    var unicodeInputBuffer = ""
    var phaseBeforeUnicodeInput: ImePhase?

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
