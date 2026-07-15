import Foundation

/// Publishes learning mutations across otherwise independent converter
/// instances.  AzooKey intentionally keeps a per-instance persisted-memory
/// cache, so a long-lived session must invalidate that cache before the first
/// conversion following another session's commit.
final class LearningSynchronizedKanaKanjiConverter: KanaKanjiConverting {
    private let base: any KanaKanjiConverting
    private let revisionStore: HazkeyLearningRevisionStore
    private let executionGate: HazkeyConverterExecutionGate
    private var observedRevision: UInt64

    init(
        base: any KanaKanjiConverting,
        revisionStore: HazkeyLearningRevisionStore,
        executionGate: HazkeyConverterExecutionGate = HazkeyConverterExecutionGate()
    ) {
        self.base = base
        self.revisionStore = revisionStore
        self.executionGate = executionGate
        self.observedRevision = revisionStore.current()
    }

    var supportsSegmentEditing: Bool { base.supportsSegmentEditing }

    func display(for composition: CompositionInput) -> CompositionDisplay {
        executionGate.withLock { base.display(for: composition) }
    }

    func inputCursorPosition(
        for composition: CompositionInput,
        movingBy offset: Int
    ) -> Int {
        executionGate.withLock {
            base.inputCursorPosition(for: composition, movingBy: offset)
        }
    }

    func candidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        try executionGate.withLock {
            synchronizePersistedLearningIfNeeded()
            return try base.candidates(for: composition, options: options)
        }
    }

    func segmentCandidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        try executionGate.withLock {
            synchronizePersistedLearningIfNeeded()
            return try base.segmentCandidates(for: composition, options: options)
        }
    }

    func predictions(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        try executionGate.withLock {
            synchronizePersistedLearningIfNeeded()
            return try base.predictions(for: composition, options: options)
        }
    }

    func realtimeCandidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> RealtimeConversionOutput {
        try executionGate.withLock {
            synchronizePersistedLearningIfNeeded()
            return try base.realtimeCandidates(for: composition, options: options)
        }
    }

    func setCompletedData(_ candidate: ConverterCandidate) {
        executionGate.withLock { base.setCompletedData(candidate) }
    }

    func updateLearningData(_ candidate: ConverterCandidate) {
        executionGate.withLock { base.updateLearningData(candidate) }
    }

    func commitLearning() {
        executionGate.withLock {
            base.commitLearning()
            observedRevision = revisionStore.recordCommit()
        }
    }

    func stageLearning(
        candidate: ConverterCandidate,
        reading: String
    ) -> ConverterLearningToken? {
        executionGate.withLock {
            base.stageLearning(candidate: candidate, reading: reading)
        }
    }

    func commitStagedLearning(_ token: ConverterLearningToken) {
        executionGate.withLock { base.commitStagedLearning(token) }
    }

    func discardStagedLearning(_ token: ConverterLearningToken) {
        executionGate.withLock { base.discardStagedLearning(token) }
    }

    func forget(_ candidate: ConverterCandidate) {
        executionGate.withLock {
            base.forget(candidate)
            // AzooKey's forget operation merges directly into long-term memory.
            observedRevision = revisionStore.recordCommit()
        }
    }

    func stopComposition() {
        executionGate.withLock { base.stopComposition() }
    }

    func purgeSensitiveState() {
        executionGate.withLock { base.purgeSensitiveState() }
    }

    func prepareSpeculativeConversion(_ context: SpeculativeConversionContext) {
        base.prepareSpeculativeConversion(context)
    }

    func invalidateSpeculativeConversion(reason: SpeculationInvalidationReason) {
        base.invalidateSpeculativeConversion(reason: reason)
    }

    func lockCandidateOrder(for revision: CompositionRevision) {
        base.lockCandidateOrder(for: revision)
    }

    func retireCandidateWindow() {
        base.retireCandidateWindow()
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
