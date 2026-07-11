import Foundation

struct GrimodexClientContext: Equatable, Sendable {
    let program: String
    let frontend: String
    let secureInput: Bool
}

enum GrimodexScopeMode: String, CaseIterable, Sendable {
    case off
    case grimodexOnly
    case allApplications

    static let defaultValue = GrimodexScopeMode.grimodexOnly
}

enum GrimodexScopeReason: Equatable, Sendable {
    case allowedGrimodex
    case allowedAllApplications
    case disabled
    case secureInput
    case unknownProgram
    case otherProgram
}

struct GrimodexScopeDecision: Equatable, Sendable {
    let allowsGrimodexIntegration: Bool
    let allowsLearning: Bool
    let reason: GrimodexScopeReason
}

enum GrimodexScopePolicy {
    private static let grimodexPrograms: Set<String> = [
        "grimodex",
        "com.miyakey.grimodex",
    ]

    static func evaluate(
        mode: GrimodexScopeMode,
        context: GrimodexClientContext
    ) -> GrimodexScopeDecision {
        if context.secureInput {
            return GrimodexScopeDecision(
                allowsGrimodexIntegration: false,
                allowsLearning: false,
                reason: .secureInput
            )
        }

        switch mode {
        case .off:
            return GrimodexScopeDecision(
                allowsGrimodexIntegration: false,
                allowsLearning: true,
                reason: .disabled
            )
        case .allApplications:
            return GrimodexScopeDecision(
                allowsGrimodexIntegration: true,
                allowsLearning: true,
                reason: .allowedAllApplications
            )
        case .grimodexOnly:
            let program = context.program
                .trimmingCharacters(in: .whitespacesAndNewlines)
                .lowercased()
            if program.isEmpty {
                return GrimodexScopeDecision(
                    allowsGrimodexIntegration: false,
                    allowsLearning: true,
                    reason: .unknownProgram
                )
            }
            if grimodexPrograms.contains(program) {
                return GrimodexScopeDecision(
                    allowsGrimodexIntegration: true,
                    allowsLearning: true,
                    reason: .allowedGrimodex
                )
            }
            return GrimodexScopeDecision(
                allowsGrimodexIntegration: false,
                allowsLearning: true,
                reason: .otherProgram
            )
        }
    }
}

struct GrimodexIntegrationRevision: Equatable, Sendable {
    let generation: UInt64
    let payload: GrimodexIntegrationPayload?
    let allowsLearning: Bool
    let secureInput: Bool

    init(
        generation: UInt64,
        payload: GrimodexIntegrationPayload?,
        allowsLearning: Bool = true,
        secureInput: Bool = false
    ) {
        self.generation = generation
        self.payload = payload
        self.allowsLearning = allowsLearning
        self.secureInput = secureInput
    }

    init(
        snapshot: GrimodexPublishedSnapshot,
        decision: GrimodexScopeDecision
    ) {
        self.init(
            generation: snapshot.generation,
            payload: decision.allowsGrimodexIntegration ? snapshot.payload : nil,
            allowsLearning: decision.allowsLearning,
            secureInput: decision.reason == .secureInput
        )
    }
}

protocol GrimodexRevisionProviding: Sendable {
    func latest() -> GrimodexIntegrationRevision
}

struct GrimodexDisabledRevisionProvider: GrimodexRevisionProviding, Sendable {
    func latest() -> GrimodexIntegrationRevision {
        GrimodexIntegrationRevision(generation: 0, payload: nil)
    }
}

struct GrimodexCompositionGenerationPin: Equatable, Sendable {
    private(set) var applied: GrimodexIntegrationRevision?
    private(set) var pending: GrimodexIntegrationRevision?
    private(set) var pinned: GrimodexIntegrationRevision?

    var isComposing: Bool { pinned != nil }

    mutating func observe(
        _ revision: GrimodexIntegrationRevision
    ) -> GrimodexIntegrationRevision? {
        let baseline = pending ?? pinned ?? applied
        if let baseline {
            guard revision.generation >= baseline.generation else { return nil }
            guard revision != baseline else { return nil }
        }

        if isComposing {
            pending = revision
            return nil
        }
        applied = revision
        pending = nil
        return revision
    }

    mutating func beginComposition(
        latest: GrimodexIntegrationRevision
    ) -> GrimodexIntegrationRevision? {
        if isComposing {
            _ = observe(latest)
            return nil
        }
        let revisionToApply = observe(latest)
        pinned = applied
        return revisionToApply
    }

    mutating func endComposition(
        latest: GrimodexIntegrationRevision
    ) -> GrimodexIntegrationRevision? {
        guard isComposing else {
            return observe(latest)
        }
        _ = observe(latest)
        pinned = nil
        guard let next = pending else { return nil }
        pending = nil
        guard applied != next else { return nil }
        applied = next
        return next
    }

    mutating func revokeImmediately(
        _ revision: GrimodexIntegrationRevision
    ) -> GrimodexIntegrationRevision? {
        let newestGeneration = [
            revision.generation,
            applied?.generation,
            pending?.generation,
            pinned?.generation,
        ].compactMap { $0 }.max() ?? revision.generation
        let revoked = GrimodexIntegrationRevision(
            generation: newestGeneration,
            payload: nil,
            allowsLearning: false,
            secureInput: true
        )
        pinned = nil
        pending = nil
        guard applied != revoked else { return nil }
        applied = revoked
        return revoked
    }
}
