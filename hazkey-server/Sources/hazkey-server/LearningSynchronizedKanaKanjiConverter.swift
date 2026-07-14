import Foundation

/// Publishes learning mutations across otherwise independent converter
/// instances.  AzooKey intentionally keeps a per-instance persisted-memory
/// cache, so a long-lived session must invalidate that cache before the first
/// conversion following another session's commit.
final class LearningSynchronizedKanaKanjiConverter: KanaKanjiConverting {
    private let base: any KanaKanjiConverting
    private let revisionStore: HazkeyLearningRevisionStore
    private var observedRevision: UInt64

    init(
        base: any KanaKanjiConverting,
        revisionStore: HazkeyLearningRevisionStore
    ) {
        self.base = base
        self.revisionStore = revisionStore
        self.observedRevision = revisionStore.current()
    }

    var supportsSegmentEditing: Bool { base.supportsSegmentEditing }

    func display(for composition: CompositionInput) -> CompositionDisplay {
        base.display(for: composition)
    }

    func inputCursorPosition(
        for composition: CompositionInput,
        movingBy offset: Int
    ) -> Int {
        base.inputCursorPosition(for: composition, movingBy: offset)
    }

    func candidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        synchronizePersistedLearningIfNeeded()
        return try base.candidates(for: composition, options: options)
    }

    func segmentCandidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        synchronizePersistedLearningIfNeeded()
        return try base.segmentCandidates(for: composition, options: options)
    }

    func predictions(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        synchronizePersistedLearningIfNeeded()
        return try base.predictions(for: composition, options: options)
    }

    func realtimeCandidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> RealtimeConversionOutput {
        synchronizePersistedLearningIfNeeded()
        return try base.realtimeCandidates(for: composition, options: options)
    }

    func setCompletedData(_ candidate: ConverterCandidate) {
        base.setCompletedData(candidate)
    }

    func updateLearningData(_ candidate: ConverterCandidate) {
        base.updateLearningData(candidate)
    }

    func commitLearning() {
        base.commitLearning()
        observedRevision = revisionStore.recordCommit()
    }

    func stageLearning(
        candidate: ConverterCandidate,
        reading: String
    ) -> ConverterLearningToken? {
        base.stageLearning(candidate: candidate, reading: reading)
    }

    func commitStagedLearning(_ token: ConverterLearningToken) {
        base.commitStagedLearning(token)
    }

    func discardStagedLearning(_ token: ConverterLearningToken) {
        base.discardStagedLearning(token)
    }

    func forget(_ candidate: ConverterCandidate) {
        base.forget(candidate)
        // AzooKey's forget operation merges directly into long-term memory.
        observedRevision = revisionStore.recordCommit()
    }

    func stopComposition() {
        base.stopComposition()
    }

    func purgeSensitiveState() {
        base.purgeSensitiveState()
    }

    private func synchronizePersistedLearningIfNeeded() {
        let currentRevision = revisionStore.current()
        guard currentRevision != observedRevision else { return }
        // AzooKey exposes cache invalidation through the same public commit
        // operation used to flush temporary learning. Reducer commits are
        // immediate, so there is no local pending update at this boundary.
        base.commitLearning()
        observedRevision = currentRevision
    }
}
