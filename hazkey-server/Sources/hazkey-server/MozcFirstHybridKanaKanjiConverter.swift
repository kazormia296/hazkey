import Dispatch
import Foundation

/// Serializes access to AzooKey state shared by all sessions in one registry.
/// Workers, learning, and configuration/dictionary maintenance share this
/// recursive gate; ordinary Mozc UI work never acquires it.
final class HazkeyConverterExecutionGate: @unchecked Sendable {
    private let lock = NSRecursiveLock()

    func withLock<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock()
        defer { lock.unlock() }
        return try body()
    }
}

protocol SpeculativeWorkExecuting: Sendable {
    func submit(_ work: @escaping @Sendable () -> Void)
}

final class SerialSpeculativeWorkExecutor: SpeculativeWorkExecuting, @unchecked Sendable {
    static let shared = SerialSpeculativeWorkExecutor()

    private let queue = DispatchQueue(
        label: "com.miyakey.grimodex.ime.hazkey-speculation",
        qos: .userInitiated
    )

    private init() {}

    func submit(_ work: @escaping @Sendable () -> Void) {
        queue.async(execute: work)
    }
}

enum HybridPromotionPolicy: String, Equatable, Sendable {
    /// H0: preserve the backend whose latency and Top-1 behavior motivated the
    /// experiment. Hazkey can still fill holes below the stable Mozc Top-3.
    case preserveMozcTop1

    /// Diagnostic-only H1. Current disclosed-corpus evaluation regresses more
    /// cases than it rescues, so production wiring must not select this policy
    /// without a new holdout decision.
    case oneSidedConsensus
}

struct MozcFirstHybridDiagnostics: Equatable, Sendable {
    var prefetchStarted = 0
    var prefetchReady = 0
    var formalReadyConsumed = 0
    var formalDeadlineMiss = 0
    var staleResultDiscarded = 0
    var pendingCancelled = 0
    var readyDiscarded = 0
    var lateCompletionDiscarded = 0
    var hazkeyFailure = 0
    var mergedRequests = 0
    var boundaryMismatch = 0
    var learningRevisionMismatch = 0
    var top1Promotions = 0
    var realtimeRequestCount = 0
    var realtimeTotalNanoseconds: UInt64 = 0
    var formalRequestCount = 0
    var formalTotalNanoseconds: UInt64 = 0
    var hazkeyRequestCount = 0
    var hazkeyTotalNanoseconds: UInt64 = 0
    var outstandingWork = 0
    var invalidations: [SpeculationInvalidationReason: Int] = [:]

    mutating func merge(_ other: MozcFirstHybridDiagnostics) {
        prefetchStarted += other.prefetchStarted
        prefetchReady += other.prefetchReady
        formalReadyConsumed += other.formalReadyConsumed
        formalDeadlineMiss += other.formalDeadlineMiss
        staleResultDiscarded += other.staleResultDiscarded
        pendingCancelled += other.pendingCancelled
        readyDiscarded += other.readyDiscarded
        lateCompletionDiscarded += other.lateCompletionDiscarded
        hazkeyFailure += other.hazkeyFailure
        mergedRequests += other.mergedRequests
        boundaryMismatch += other.boundaryMismatch
        learningRevisionMismatch += other.learningRevisionMismatch
        top1Promotions += other.top1Promotions
        realtimeRequestCount += other.realtimeRequestCount
        realtimeTotalNanoseconds &+= other.realtimeTotalNanoseconds
        formalRequestCount += other.formalRequestCount
        formalTotalNanoseconds &+= other.formalTotalNanoseconds
        hazkeyRequestCount += other.hazkeyRequestCount
        hazkeyTotalNanoseconds &+= other.hazkeyTotalNanoseconds
        outstandingWork += other.outstandingWork
        for (reason, count) in other.invalidations {
            invalidations[reason, default: 0] += count
        }
    }

    var structuredLogLine: String {
        let orderedInvalidations = invalidations
            .map { "\($0.key)=\($0.value)" }
            .sorted()
            .joined(separator: ",")
        return [
            "prefetch_started=\(prefetchStarted)",
            "prefetch_ready=\(prefetchReady)",
            "formal_ready_consumed=\(formalReadyConsumed)",
            "formal_deadline_miss=\(formalDeadlineMiss)",
            "stale_discarded=\(staleResultDiscarded)",
            "pending_cancelled=\(pendingCancelled)",
            "ready_discarded=\(readyDiscarded)",
            "late_completion_discarded=\(lateCompletionDiscarded)",
            "hazkey_failure=\(hazkeyFailure)",
            "merged_requests=\(mergedRequests)",
            "boundary_mismatch=\(boundaryMismatch)",
            "learning_revision_mismatch=\(learningRevisionMismatch)",
            "top1_promotions=\(top1Promotions)",
            "realtime_requests=\(realtimeRequestCount)",
            "realtime_total_ns=\(realtimeTotalNanoseconds)",
            "formal_requests=\(formalRequestCount)",
            "formal_total_ns=\(formalTotalNanoseconds)",
            "hazkey_requests=\(hazkeyRequestCount)",
            "hazkey_total_ns=\(hazkeyTotalNanoseconds)",
            "outstanding_work=\(outstandingWork)",
            "invalidations=\(orderedInvalidations)",
        ].joined(separator: " ")
    }
}

/// A Mozc-first speculative converter.
///
/// Editable input is always rendered and converted synchronously by Mozc.
/// Hazkey prepares the first natural segment for the complete input on a
/// process-wide serial worker. At formal conversion the reducer freezes the
/// current composition revision: only a result that is fully ready after the
/// worker released the Hazkey execution gate can be consulted. Unfinished work
/// is an immediate cache miss. No worker completion ever calls back into the
/// reducer or changes a published generation.
final class MozcFirstHybridKanaKanjiConverter: KanaKanjiConverting, @unchecked Sendable {
    private enum SpeculationError: Error {
        case stale
    }

    private enum Backend: Hashable, Sendable {
        case mozc
        case hazkey
    }

    private struct CandidateRoute: Sendable {
        let backend: Backend
        let original: ConverterCandidate
    }

    private struct StagedRoute: Sendable {
        let backend: Backend
        let original: ConverterLearningToken
    }

    private struct PreparedStep: Equatable, Sendable {
        let input: CompositionInput
        let options: ConversionOptions
        let output: ConversionOutput
    }

    private enum SpeculationState: Sendable {
        case idle
        case pending(UInt64, SpeculativeConversionContext)
        case ready(UInt64, SpeculativeConversionContext, PreparedStep)
        case frozen(
            revision: CompositionRevision,
            context: SpeculativeConversionContext?,
            step: PreparedStep?
        )
    }

    private let mozc: any KanaKanjiConverting
    private let hazkey: any KanaKanjiConverting
    private let executor: any SpeculativeWorkExecuting
    private let promotionPolicy: HybridPromotionPolicy
    private let hazkeyExecutionGate: HazkeyConverterExecutionGate
    private let learningRevisionProvider: @Sendable () -> UInt64
    private let stateLock = NSLock()
    private let activeWorkCondition = NSCondition()

    private var speculationState: SpeculationState = .idle
    private var activeWorkCount = 0
    private var outstandingWorkCountValue = 0
    private var nextSpeculationWorkID: UInt64 = 1
    private var diagnostics = MozcFirstHybridDiagnostics()
    private var nextSourceID: UInt64 = 1
    private var candidateRoutes: [String: CandidateRoute] = [:]
    private var candidateRouteOrder: [String] = []
    private var nextLearningTokenID: UInt64 = 1
    private var stagedRoutes: [ConverterLearningToken: StagedRoute] = [:]
    private var dirtyBackends = Set<Backend>()

    init(
        mozc: any KanaKanjiConverting,
        hazkey: any KanaKanjiConverting,
        executor: any SpeculativeWorkExecuting = SerialSpeculativeWorkExecutor.shared,
        promotionPolicy: HybridPromotionPolicy = .preserveMozcTop1,
        hazkeyExecutionGate: HazkeyConverterExecutionGate = HazkeyConverterExecutionGate(),
        learningRevisionProvider: @escaping @Sendable () -> UInt64 = { 0 }
    ) {
        self.mozc = mozc
        self.hazkey = hazkey
        self.executor = executor
        self.promotionPolicy = promotionPolicy
        self.hazkeyExecutionGate = hazkeyExecutionGate
        self.learningRevisionProvider = learningRevisionProvider
    }

    var supportsSegmentEditing: Bool { mozc.supportsSegmentEditing }

    func display(for composition: CompositionInput) -> CompositionDisplay {
        mozc.display(for: composition)
    }

    func inputCursorPosition(
        for composition: CompositionInput,
        movingBy offset: Int
    ) -> Int {
        mozc.inputCursorPosition(for: composition, movingBy: offset)
    }

    func candidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        let started = DispatchTime.now().uptimeNanoseconds
        defer { recordFormalDuration(since: started) }
        // A forced target represents a segment-boundary edit. The initial
        // spike deliberately keeps that operation Mozc-only; a cached natural
        // Hazkey segment cannot safely be projected onto a different boundary.
        return route(
            try mozc.candidates(for: composition, options: options),
            backend: .mozc
        )
    }

    func segmentCandidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        let started = DispatchTime.now().uptimeNanoseconds
        defer { recordFormalDuration(since: started) }

        let primary = try mozc.segmentCandidates(
            for: composition,
            options: options
        )
        guard let secondary = frozenOutput(for: composition, options: options) else {
            return route(primary, backend: .mozc)
        }
        return merge(primary: primary, secondary: secondary, options: options)
    }

    func realtimeCandidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> RealtimeConversionOutput {
        let started = DispatchTime.now().uptimeNanoseconds
        defer {
            let elapsed = elapsedNanoseconds(since: started)
            withStateLock {
                diagnostics.realtimeRequestCount += 1
                diagnostics.realtimeTotalNanoseconds &+= elapsed
            }
        }
        let output = try mozc.realtimeCandidates(
            for: composition,
            options: options
        )
        return RealtimeConversionOutput(
            liveCandidate: output.liveCandidate.map {
                route($0, backend: .mozc)
            },
            candidates: output.candidates.map {
                route($0, backend: .mozc)
            },
            pageSize: min(output.pageSize, output.candidates.count)
        )
    }

    func predictions(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        route(
            try mozc.predictions(for: composition, options: options),
            backend: .mozc
        )
    }

    func prepareSpeculativeConversion(_ context: SpeculativeConversionContext) {
        guard !context.options.secureInput,
              !context.input.elements.isEmpty,
              context.input.cursor == context.input.elements.count else {
            return
        }
        let context = SpeculativeConversionContext(
            revision: context.revision,
            input: context.input,
            options: context.options,
            projectRevision: context.projectRevision,
            learningRevision: learningRevisionProvider()
        )

        let workID = withStateLock { () -> UInt64? in
            switch speculationState {
            case .pending(_, let current) where current == context:
                return nil
            case .ready(_, let current, _) where current == context:
                return nil
            case .frozen(let revision, _, _) where revision == context.revision:
                return nil
            default:
                recordDiscardForReplacementLocked()
                let workID = nextSpeculationWorkID
                nextSpeculationWorkID = nextSpeculationWorkID == UInt64.max
                    ? 1
                    : nextSpeculationWorkID + 1
                speculationState = .pending(workID, context)
                diagnostics.prefetchStarted += 1
                return workID
            }
        }
        guard let workID else { return }

        beginOutstandingWork()
        executor.submit { [weak self] in
            guard let self else { return }
            defer { self.endOutstandingWork() }
            self.prepareHazkeyStep(workID: workID)
        }
    }

    func invalidateSpeculativeConversion(reason: SpeculationInvalidationReason) {
        withStateLock {
            recordDiscardForReplacementLocked()
            speculationState = .idle
            diagnostics.invalidations[reason, default: 0] += 1
        }
    }

    func lockCandidateOrder(for revision: CompositionRevision) {
        withStateLock {
            switch speculationState {
            case .frozen(let current, _, _) where current == revision:
                return
            case .ready(_, let context, let step) where context.revision == revision:
                guard context.learningRevision == learningRevisionProvider() else {
                    speculationState = .frozen(
                        revision: revision,
                        context: context,
                        step: nil
                    )
                    diagnostics.learningRevisionMismatch += 1
                    diagnostics.staleResultDiscarded += 1
                    diagnostics.readyDiscarded += 1
                    return
                }
                speculationState = .frozen(
                    revision: revision,
                    context: context,
                    step: step
                )
                diagnostics.formalReadyConsumed += 1
            case .pending(_, let context) where context.revision == revision:
                if context.learningRevision != learningRevisionProvider() {
                    speculationState = .frozen(
                        revision: revision,
                        context: context,
                        step: nil
                    )
                    diagnostics.learningRevisionMismatch += 1
                } else {
                    speculationState = .frozen(
                        revision: revision,
                        context: context,
                        step: nil
                    )
                }
                diagnostics.formalDeadlineMiss += 1
                diagnostics.staleResultDiscarded += 1
                diagnostics.pendingCancelled += 1
            default:
                recordDiscardForReplacementLocked()
                speculationState = .frozen(
                    revision: revision,
                    context: nil,
                    step: nil
                )
                diagnostics.formalDeadlineMiss += 1
            }
        }
    }

    func setCompletedData(_ candidate: ConverterCandidate) {
        guard let route = candidateRoute(for: candidate) else { return }
        call(route.backend) { converter in
            converter.setCompletedData(route.original)
        }
    }

    func updateLearningData(_ candidate: ConverterCandidate) {
        guard let route = candidateRoute(for: candidate) else { return }
        call(route.backend) { converter in
            converter.updateLearningData(route.original)
        }
        withStateLock { _ = dirtyBackends.insert(route.backend) }
    }

    func commitLearning() {
        let backends = withStateLock { () -> Set<Backend> in
            let result = dirtyBackends
            dirtyBackends.removeAll(keepingCapacity: true)
            return result
        }
        if backends.contains(.mozc) {
            mozc.commitLearning()
        }
        if backends.contains(.hazkey) {
            withHazkeyLock { hazkey.commitLearning() }
        }
    }

    func stageLearning(
        candidate: ConverterCandidate,
        reading: String
    ) -> ConverterLearningToken? {
        guard let route = candidateRoute(for: candidate),
              let childToken = call(route.backend, body: { converter in
                  converter.stageLearning(candidate: route.original, reading: reading)
              }) else {
            return nil
        }
        return withStateLock {
            let token = ConverterLearningToken(
                rawValue: "hybrid-learning-\(nextLearningTokenID)"
            )
            nextLearningTokenID = nextLearningTokenID == UInt64.max
                ? 1
                : nextLearningTokenID + 1
            stagedRoutes[token] = StagedRoute(
                backend: route.backend,
                original: childToken
            )
            return token
        }
    }

    func commitStagedLearning(_ token: ConverterLearningToken) {
        guard let route = withStateLock({ stagedRoutes.removeValue(forKey: token) }) else {
            return
        }
        call(route.backend) { converter in
            converter.commitStagedLearning(route.original)
        }
        withStateLock { _ = dirtyBackends.insert(route.backend) }
    }

    func discardStagedLearning(_ token: ConverterLearningToken) {
        guard let route = withStateLock({ stagedRoutes.removeValue(forKey: token) }) else {
            return
        }
        call(route.backend) { converter in
            converter.discardStagedLearning(route.original)
        }
    }

    func forget(_ candidate: ConverterCandidate) {
        guard let route = candidateRoute(for: candidate) else { return }
        call(route.backend) { converter in
            converter.forget(route.original)
        }
    }

    func stopComposition() {
        // Formal conversion calls stop before asking for natural segments. Do
        // not clear or wait on the prepared Hazkey snapshot here.
        mozc.stopComposition()
        withStateLock { trimCandidateRoutes() }
    }

    func purgeSensitiveState() {
        withStateLock {
            speculationState = .idle
            candidateRoutes.removeAll(keepingCapacity: false)
            candidateRouteOrder.removeAll(keepingCapacity: false)
            stagedRoutes.removeAll(keepingCapacity: false)
            dirtyBackends.removeAll(keepingCapacity: false)
        }
        mozc.purgeSensitiveState()
        // Unlike ordinary UI operations, a security-domain crossing waits for
        // any active Hazkey call before erasing its process-local candidates.
        withHazkeyLock { hazkey.purgeSensitiveState() }
        waitForActiveWorkToFinish()
    }

    func diagnosticsSnapshot() -> MozcFirstHybridDiagnostics {
        var snapshot = withStateLock { diagnostics }
        snapshot.outstandingWork = outstandingWorkCount()
        return snapshot
    }

    private func prepareHazkeyStep(workID: UInt64) {
        beginActiveWork()
        defer { endActiveWork() }
        // Keep plaintext context and prepared candidates in a nested frame.
        // It returns before the active-work fence is released, so a secure
        // purge cannot return while that local result is still live.
        performHazkeyStep(workID: workID)
    }

    private func performHazkeyStep(workID: UInt64) {
        let started = DispatchTime.now().uptimeNanoseconds
        var preparedContext: SpeculativeConversionContext?
        var invokedHazkey = false
        let prepared: Result<PreparedStep, Error> = Result {
            try withHazkeyLock {
                guard let context = withStateLock({ () -> SpeculativeConversionContext? in
                    if case .pending(let currentWorkID, let current) = speculationState,
                       currentWorkID == workID {
                        return current
                    }
                    return nil
                }) else {
                    throw SpeculationError.stale
                }
                preparedContext = context
                invokedHazkey = true
                defer { hazkey.stopComposition() }
                return try makeHazkeyStep(for: context, workID: workID)
            }
        }
        let elapsed = elapsedNanoseconds(since: started)

        withStateLock {
            if invokedHazkey {
                diagnostics.hazkeyRequestCount += 1
                diagnostics.hazkeyTotalNanoseconds &+= elapsed
            }
            guard let context = preparedContext,
                  case .pending(let currentWorkID, let current) = speculationState,
                  currentWorkID == workID,
                  current == context else {
                if invokedHazkey {
                    diagnostics.lateCompletionDiscarded += 1
                }
                return
            }
            switch prepared {
            case .success(let step):
                speculationState = .ready(workID, context, step)
                diagnostics.prefetchReady += 1
            case .failure:
                speculationState = .idle
                diagnostics.hazkeyFailure += 1
            }
        }
    }

    private func makeHazkeyStep(
        for context: SpeculativeConversionContext,
        workID: UInt64
    ) throws -> PreparedStep {
        guard speculationIsPending(workID: workID, context: context) else {
            throw SpeculationError.stale
        }
        let requestedOutput = try hazkey.segmentCandidates(
            for: context.input,
            options: context.options
        )
        guard speculationIsPending(workID: workID, context: context) else {
            throw SpeculationError.stale
        }
        let candidates = Array(
            requestedOutput.candidates.prefix(context.options.suggestionListLimit)
        )
        return PreparedStep(
            input: context.input,
            options: context.options,
            output: ConversionOutput(
                candidates: candidates,
                pageSize: min(requestedOutput.pageSize, candidates.count)
            )
        )
    }

    private func speculationIsPending(
        workID: UInt64,
        context: SpeculativeConversionContext
    ) -> Bool {
        withStateLock {
            guard case .pending(let currentWorkID, let current) = speculationState else {
                return false
            }
            return currentWorkID == workID && current == context
        }
    }

    private func frozenOutput(
        for input: CompositionInput,
        options: ConversionOptions
    ) -> ConversionOutput? {
        withStateLock {
            guard case .frozen(_, _, let step?) = speculationState,
                  step.input == input,
                  step.options == options else {
                return nil
            }
            return step.output
        }
    }

    private func merge(
        primary: ConversionOutput,
        secondary: ConversionOutput,
        options: ConversionOptions
    ) -> ConversionOutput {
        guard let primaryFirst = primary.candidates.first else {
            let limited = Array(secondary.candidates.prefix(options.suggestionListLimit))
            return ConversionOutput(
                candidates: limited.map { route($0, backend: .hazkey) },
                pageSize: min(secondary.pageSize, limited.count)
            )
        }

        let boundary = primaryFirst.consumingCount
        let eligibleSecondary = secondary.candidates.filter {
            $0.consumingCount == boundary
        }
        guard !eligibleSecondary.isEmpty else {
            if !secondary.candidates.isEmpty {
                withStateLock { diagnostics.boundaryMismatch += 1 }
            }
            return route(primary, backend: .mozc)
        }

        let promote = shouldPromote(
            primary: primary.candidates,
            secondary: eligibleSecondary
        )
        var ordered: [(Backend, ConverterCandidate)] = []
        if promote, let secondaryFirst = eligibleSecondary.first {
            ordered.append((.hazkey, secondaryFirst))
            ordered.append(contentsOf: primary.candidates.map { (.mozc, $0) })
            ordered.append(contentsOf: eligibleSecondary.dropFirst().map { (.hazkey, $0) })
        } else {
            let stablePrimaryCount = min(3, primary.candidates.count)
            ordered.append(contentsOf: primary.candidates.prefix(stablePrimaryCount).map {
                (.mozc, $0)
            })
            ordered.append(contentsOf: eligibleSecondary.map { (.hazkey, $0) })
            ordered.append(contentsOf: primary.candidates.dropFirst(stablePrimaryCount).map {
                (.mozc, $0)
            })
        }

        var seen = Set<String>()
        let limited = ordered.compactMap { backend, candidate -> ConverterCandidate? in
            let key = deduplicationKey(candidate)
            guard seen.insert(key).inserted else { return nil }
            return route(candidate, backend: backend)
        }.prefix(options.suggestionListLimit)
        let candidates = Array(limited)
        withStateLock {
            diagnostics.mergedRequests += 1
            if promote { diagnostics.top1Promotions += 1 }
        }
        return ConversionOutput(
            candidates: candidates,
            pageSize: min(options.suggestionListLimit, candidates.count)
        )
    }

    private func shouldPromote(
        primary: [ConverterCandidate],
        secondary: [ConverterCandidate]
    ) -> Bool {
        guard promotionPolicy == .oneSidedConsensus,
              let primaryFirst = primary.first,
              let secondaryFirst = secondary.first else {
            return false
        }
        let secondaryKey = deduplicationKey(secondaryFirst)
        let secondaryAppearsBelowPrimary = primary.dropFirst().contains {
            deduplicationKey($0) == secondaryKey
        }
        let primaryKey = deduplicationKey(primaryFirst)
        let primaryAbsentFromSecondary = !secondary.contains {
            deduplicationKey($0) == primaryKey
        }
        return secondaryAppearsBelowPrimary && primaryAbsentFromSecondary
    }

    private func route(
        _ output: ConversionOutput,
        backend: Backend
    ) -> ConversionOutput {
        ConversionOutput(
            candidates: output.candidates.map { route($0, backend: backend) },
            pageSize: min(output.pageSize, output.candidates.count)
        )
    }

    private func route(
        _ candidate: ConverterCandidate,
        backend: Backend
    ) -> ConverterCandidate {
        withStateLock {
            let sourceID = "hybrid-\(nextSourceID)"
            nextSourceID = nextSourceID == UInt64.max ? 1 : nextSourceID + 1
            candidateRoutes[sourceID] = CandidateRoute(
                backend: backend,
                original: candidate
            )
            candidateRouteOrder.append(sourceID)
            return ConverterCandidate(
                text: candidate.text,
                annotation: candidate.annotation,
                consumingCount: candidate.consumingCount,
                sourceID: sourceID,
                provenance: candidate.provenance,
                isLearnable: backend == .hazkey
                    && candidate.isLearnable
                    && candidate.sourceID != nil
            )
        }
    }

    private func candidateRoute(
        for candidate: ConverterCandidate
    ) -> CandidateRoute? {
        guard let sourceID = candidate.sourceID else { return nil }
        return withStateLock { candidateRoutes[sourceID] }
    }

    private func trimCandidateRoutes() {
        // Current-generation routes are never trimmed. Once a composition
        // ends, retain only the same bounded tail as the Hazkey adapter for a
        // live-converted prefix that may be committed after the next edit.
        while candidateRouteOrder.count > 512 {
            let sourceID = candidateRouteOrder.removeFirst()
            candidateRoutes.removeValue(forKey: sourceID)
        }
    }

    private func recordDiscardForReplacementLocked() {
        switch speculationState {
        case .pending:
            diagnostics.pendingCancelled += 1
            diagnostics.staleResultDiscarded += 1
        case .ready:
            diagnostics.readyDiscarded += 1
            diagnostics.staleResultDiscarded += 1
        case .idle, .frozen:
            break
        }
    }

    private func call<T>(
        _ backend: Backend,
        body: (any KanaKanjiConverting) throws -> T
    ) rethrows -> T {
        switch backend {
        case .mozc:
            return try body(mozc)
        case .hazkey:
            return try withHazkeyLock { try body(hazkey) }
        }
    }

    private func withStateLock<T>(_ body: () throws -> T) rethrows -> T {
        stateLock.lock()
        defer { stateLock.unlock() }
        return try body()
    }

    private func withHazkeyLock<T>(_ body: () throws -> T) rethrows -> T {
        try hazkeyExecutionGate.withLock(body)
    }

    private func recordFormalDuration(since started: UInt64) {
        let elapsed = elapsedNanoseconds(since: started)
        withStateLock {
            diagnostics.formalRequestCount += 1
            diagnostics.formalTotalNanoseconds &+= elapsed
        }
    }

    private func elapsedNanoseconds(since started: UInt64) -> UInt64 {
        let now = DispatchTime.now().uptimeNanoseconds
        return now >= started ? now - started : 0
    }

    private func beginActiveWork() {
        activeWorkCondition.lock()
        activeWorkCount += 1
        activeWorkCondition.unlock()
    }

    private func beginOutstandingWork() {
        activeWorkCondition.lock()
        outstandingWorkCountValue += 1
        activeWorkCondition.unlock()
    }

    private func endOutstandingWork() {
        activeWorkCondition.lock()
        outstandingWorkCountValue -= 1
        activeWorkCondition.broadcast()
        activeWorkCondition.unlock()
    }

    private func endActiveWork() {
        activeWorkCondition.lock()
        activeWorkCount -= 1
        activeWorkCondition.broadcast()
        activeWorkCondition.unlock()
    }

    private func waitForActiveWorkToFinish() {
        activeWorkCondition.lock()
        while activeWorkCount > 0 {
            activeWorkCondition.wait()
        }
        activeWorkCondition.unlock()
    }

    private func outstandingWorkCount() -> Int {
        activeWorkCondition.lock()
        defer { activeWorkCondition.unlock() }
        return outstandingWorkCountValue
    }

    private func deduplicationKey(_ candidate: ConverterCandidate) -> String {
        "\(candidate.consumingCount):\(candidate.text.precomposedStringWithCanonicalMapping)"
    }

}
