import Foundation
import KanaKanjiConverterModule

protocol HazkeyCandidateLearning: AnyObject {
    func setCompletedData(_ candidate: Candidate)
    func updateLearningData(_ candidate: Candidate)
    func commitUpdateLearningData()
    func synchronizePersistedLearningData()
}

extension HazkeyCandidateLearning {
    func synchronizePersistedLearningData() {}
}

final class HazkeyKanaKanjiCandidateLearning: HazkeyCandidateLearning {
    private let converter: KanaKanjiConverter

    init(converter: KanaKanjiConverter) {
        self.converter = converter
    }

    func setCompletedData(_ candidate: Candidate) {
        converter.setCompletedData(candidate)
    }

    func updateLearningData(_ candidate: Candidate) {
        converter.updateLearningData(candidate)
    }

    func commitUpdateLearningData() {
        converter.commitUpdateLearningData()
    }

    func synchronizePersistedLearningData() {
        // AzooKey does not expose a dedicated public reload API. Its commit
        // operation is the public path that saves any local temporary memory
        // and then invalidates this converter's persisted-memory LOUDS cache.
        // Calling it only between compositions preserves converter-local
        // dynamic dictionaries while making another session's commit visible.
        converter.commitUpdateLearningData()
    }
}

final class HazkeyLearningRevisionStore: @unchecked Sendable {
    private let lock = NSLock()
    private var revision: UInt64 = 0

    func current() -> UInt64 {
        lock.lock()
        defer { lock.unlock() }
        return revision
    }

    @discardableResult
    func recordCommit() -> UInt64 {
        lock.lock()
        defer { lock.unlock() }
        revision &+= 1
        return revision
    }
}

struct GrimodexResolvedZenzaiConditions: Equatable, Sendable {
    let profile: String
    let topic: String
    let style: String
    let preference: String
}

enum GrimodexZenzaiConditionResolver {
    static func resolve(
        profile: String,
        topic: String,
        style: String,
        preference: String,
        project: GrimodexProjectConditions
    ) -> GrimodexResolvedZenzaiConditions {
        GrimodexResolvedZenzaiConditions(
            profile: profile,
            topic: project.topic ?? topic,
            style: project.style ?? style,
            preference: project.preference ?? preference
        )
    }
}

final class GrimodexEnvironmentDictionaryApplier: GrimodexDynamicDictionaryApplying {
    private unowned let environment: HazkeySessionEnvironment

    init(environment: HazkeySessionEnvironment) {
        self.environment = environment
    }

    func stopComposition() {
        environment.stopComposition()
    }

    func abortSessionComposition() {
        environment.stopComposition()
    }

    func replaceDynamicDictionary(_ entries: [GrimodexMappedDictionaryEntry]) {
        environment.replaceGrimodexDynamicDictionary(entries)
    }
}
