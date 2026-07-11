import Foundation

protocol GrimodexDynamicDictionaryApplying: AnyObject {
    func stopComposition()
    func abortSessionComposition()
    func replaceDynamicDictionary(_ entries: [GrimodexMappedDictionaryEntry])
}

final class GrimodexCompositionIntegrationController {
    private let applier: any GrimodexDynamicDictionaryApplying
    private var generationPin = GrimodexCompositionGenerationPin()

    private(set) var activeConditions = GrimodexProjectConditions.empty
    private(set) var allowsLearning = true
    private(set) var secureInput = false

    var isComposing: Bool { generationPin.isComposing }
    var appliedRevision: GrimodexIntegrationRevision? { generationPin.applied }
    var pinnedRevision: GrimodexIntegrationRevision? { generationPin.pinned }

    init(applier: any GrimodexDynamicDictionaryApplying) {
        self.applier = applier
    }

    func prepareFirstInput(latest: GrimodexIntegrationRevision) {
        if latest.secureInput {
            if !secureInput {
                revokeImmediately(latest)
            }
            if let applied = generationPin.applied {
                _ = generationPin.beginComposition(latest: applied)
            }
            return
        }
        let revision = secureRevisionIfNeeded(latest)
        guard let revisionToApply = generationPin.beginComposition(latest: revision) else {
            return
        }
        applier.stopComposition()
        apply(revisionToApply)
    }

    func observe(_ revision: GrimodexIntegrationRevision) {
        if revision.secureInput {
            if !secureInput {
                revokeImmediately(revision)
            }
            return
        }
        guard let revisionToApply = generationPin.observe(revision) else { return }
        applier.stopComposition()
        apply(revisionToApply)
    }

    func endOrReset(latest: GrimodexIntegrationRevision) {
        if latest.secureInput, !secureInput {
            revokeImmediately(latest)
            return
        }
        applier.stopComposition()
        let revision = secureRevisionIfNeeded(latest)
        guard let revisionToApply = generationPin.endComposition(latest: revision) else {
            return
        }
        apply(revisionToApply)
    }

    func revokeImmediately(_ revision: GrimodexIntegrationRevision) {
        applier.abortSessionComposition()
        guard let revoked = generationPin.revokeImmediately(revision) else { return }
        apply(revoked)
    }

    private func apply(_ revision: GrimodexIntegrationRevision) {
        applier.replaceDynamicDictionary(revision.payload?.dictionaryEntries ?? [])
        activeConditions = revision.payload?.conditions ?? .empty
        allowsLearning = revision.allowsLearning
        secureInput = revision.secureInput
    }

    private func secureRevisionIfNeeded(
        _ revision: GrimodexIntegrationRevision
    ) -> GrimodexIntegrationRevision {
        guard revision.secureInput else { return revision }
        return GrimodexIntegrationRevision(
            generation: revision.generation,
            payload: nil,
            allowsLearning: false,
            secureInput: true
        )
    }
}
