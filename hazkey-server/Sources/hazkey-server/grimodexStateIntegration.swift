import Foundation
import KanaKanjiConverterModule

protocol HazkeyCandidateLearning: AnyObject {
    func setCompletedData(_ candidate: Candidate)
    func updateLearningData(_ candidate: Candidate)
    func commitUpdateLearningData()
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

final class GrimodexStateCompositionApplier: GrimodexDynamicDictionaryApplying {
    private unowned let state: HazkeyServerState

    init(state: HazkeyServerState) {
        self.state = state
    }

    func stopComposition() {
        state.converter.stopComposition()
    }

    func abortSessionComposition() {
        state.converter.stopComposition()
        state.composingText = ComposingTextBox()
        state.currentCandidateList = nil
        state.isSubInputMode = false
        state.isShiftPressedAlone = false
        state.learningDataNeedsCommit = false
    }

    func replaceDynamicDictionary(_ entries: [GrimodexMappedDictionaryEntry]) {
        state.converter.importDynamicUserDictionary(entries.map(\.dictionaryElement))
    }
}
