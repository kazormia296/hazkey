import Foundation

private struct VisibleComposition {
    let spans: [PreeditSpan]
    let text: String
    let caretUtf8ByteOffset: UInt32?
    let learnableCandidates: [CandidateSnapshot]
}

final class ImeReducer {
    private struct CachedRequest {
        let action: ImeAction
        let expectedRevision: UInt64?
        let result: ImeReductionResult
    }

    private(set) var session: CompositionSession
    private let converter: any KanaKanjiConverting
    private let stagedLearningEnabled: Bool
    private var requestCache: [String: CachedRequest] = [:]
    private var requestOrder: [String] = []
    private let requestCacheLimit = 128

    init(
        session: CompositionSession = CompositionSession(),
        converter: any KanaKanjiConverting = NoopKanaKanjiConverter(),
        stagedLearningEnabled: Bool = true
    ) {
        self.session = session
        self.converter = converter
        self.stagedLearningEnabled = stagedLearningEnabled
    }

    func currentSnapshot() -> SessionSnapshot { snapshot() }

    @discardableResult
    func pinCompositionPolicy(_ policy: PinnedCompositionPolicy) -> Bool {
        guard session.phase == .idle, session.composingText.isEmpty else { return false }
        let securityDomainChanged = session.policy.secureInput != policy.secureInput
        if securityDomainChanged {
            // A pending transaction belongs to the security domain that
            // produced it. Resolve it before purging converter-owned tokens;
            // otherwise the session would retain an apparently resolvable
            // transaction whose adapter token has already been destroyed.
            resolvePendingLearning(commit: false)
            purgeConverterSensitiveState()
            clearSecurityDomainState()
        }
        session.policy = policy
        session.context.projectRevision = policy.projectRevision
        return securityDomainChanged
    }

    func invalidateCandidatesForExternalDictionaryChange() {
        guard session.candidates != nil
                || !session.segments.isEmpty
                || session.livePresentation.materializedPrefix != nil else {
            return
        }
        endConverterComposition()
        clearLivePresentation()
        clearConversionState()
        session.phase = session.composingText.isEmpty ? .idle : .composing
        session.advanceRevision()
        // Dictionary replacement is an out-of-band state transition. A retry
        // of an already processed request must retain its original effects,
        // but it must not resurrect the candidate window or revision that the
        // replacement just invalidated.
        rebaseRequestCacheToCurrentSnapshot()
    }

    func finalizePendingLearning(commit: Bool) {
        guard resolvePendingLearning(commit: commit) else { return }
        session.advanceRevision()
        rebaseRequestCacheToCurrentSnapshot()
    }

    func reduce(
        _ action: ImeAction,
        requestID: String,
        expectedRevision: UInt64? = nil,
        compositionStartPolicyProvider: (() -> PinnedCompositionPolicy)? = nil
    ) -> ImeReductionResult {
        if let cached = requestCache[requestID] {
            guard cached.action == action,
                  cached.expectedRevision == expectedRevision else {
                return failure(
                    .invalidAction,
                    "request_id was reused with a different action"
                )
            }
            return cached.result
        }
        if let expectedRevision, expectedRevision != session.revision {
            return cache(
                ImeReductionResult(
                    status: .staleRevision,
                    message: "expected revision does not match the current session",
                    snapshot: snapshot()
                ),
                action: action,
                requestID: requestID,
                expectedRevision: expectedRevision
            )
        }

        let result: ImeReductionResult
        switch action {
        case .insertText(let text):
            guard !text.isEmpty,
                  session.phase != .selecting,
                  session.phase != .unicodeInput else {
                result = failure(.invalidAction, "text input is not valid in the current phase")
                break
            }
            pinCompositionPolicyIfNeeded(compositionStartPolicyProvider)
            if session.phase == .idle {
                session.reconversionReplacement = nil
            }
            resolvePendingLearning(commit: true)
            preserveMaterializedLivePrefixForEditing()
            if session.candidates != nil { endConverterComposition() }
            session.phase = .composing
            session.composingText.insert(text, keymap: session.policy.keymap)
            clearConversionState()
            result = finishInteractiveEdit()

        case .deleteBackward:
            resolvePendingLearning(commit: false)
            if session.phase == .unicodeInput {
                if session.unicodeInputBuffer.isEmpty {
                    finishUnicodeInput(cancelled: true)
                } else {
                    session.unicodeInputBuffer.removeLast()
                }
                session.advanceRevision()
                result = success()
                break
            }
            session.composingText.deleteBackward()
            normalizeAfterEditing()
            result = finishInteractiveEdit()

        case .deleteForward:
            resolvePendingLearning(commit: false)
            guard session.phase != .unicodeInput else {
                result = failure(.invalidAction, "forward delete is not valid during Unicode input")
                break
            }
            session.composingText.deleteForward()
            normalizeAfterEditing()
            result = finishInteractiveEdit()

        case .moveCursor(let offset):
            guard session.phase == .composing || session.phase == .previewing else {
                result = failure(.invalidAction, "cursor movement requires a composing session")
                break
            }
            // Realtime conversion stays in `composing`, so Left/Right must keep
            // moving the editable reading cursor. Segment movement begins only
            // after an explicit transition to candidate selection.
            let input = CompositionInput(
                elements: session.composingText.elements,
                cursor: session.composingText.cursor,
                leftContext: session.context.leftContext,
                mappedTableName: session.policy.inputTableName
            )
            let nextCursor = converter.inputCursorPosition(
                for: input,
                movingBy: offset
            )
            session.composingText.moveCursor(
                by: nextCursor - session.composingText.cursor
            )
            endConverterComposition()
            session.phase = .composing
            clearLivePresentation()
            clearConversionState()
            result = finishInteractiveEdit()

        case .moveCursorToStart, .moveCursorToEnd:
            guard session.phase == .composing || session.phase == .previewing else {
                result = failure(.invalidAction, "edge movement requires a composing session")
                break
            }
            if case .moveCursorToStart = action {
                session.composingText.moveCursorToStart()
            } else {
                session.composingText.moveCursorToEnd()
            }
            endConverterComposition()
            session.phase = .composing
            clearLivePresentation()
            clearConversionState()
            result = finishInteractiveEdit()

        case .applyLiveConversion(let scheduledRevision):
            guard scheduledRevision == session.revision else {
                // The normal expected_revision field is refreshed by the C++
                // client before retries. Preserve the timer's original
                // revision here so an obsolete callback cannot convert newer
                // input, while still returning success to prevent a retry.
                result = success()
                break
            }
            guard shouldScheduleLiveConversion() else {
                session.livePresentation.pendingRevision = nil
                result = success()
                break
            }
            refreshRealtimeCandidates()
            session.livePresentation.pendingRevision = nil
            session.advanceRevision()
            result = success()

        case .startConversion:
            resolvePendingLearning(commit: true)
            clearLivePresentation()
            if session.candidates?.origin == .prediction {
                endConverterComposition()
                clearConversionState()
            }
            result = convert()

        case .navigateCandidate(let delta):
            clearLivePresentation()
            if session.phase == .composing,
               session.candidates?.items.isEmpty ?? true {
                endConverterComposition()
                clearConversionState()
                let conversion = convert(advanceRevision: false)
                guard conversion.status == .success else {
                    result = conversion
                    break
                }
                guard session.candidates != nil else {
                    session.advanceRevision()
                    result = success()
                    break
                }
            }
            guard var candidates = session.candidates, !candidates.items.isEmpty,
                  session.phase == .composing || session.phase == .previewing
                    || session.phase == .selecting || session.phase == .reconverting else {
                result = failure(.invalidAction, "candidate navigation requires candidates")
                break
            }
            let current = candidates.selectedIndex ?? 0
            candidates.selectedIndex = min(
                max(current + delta, 0), candidates.items.count - 1
            )
            session.candidates = candidates
            syncActiveSegmentCandidates(candidates)
            if session.activeSegmentIndex == nil {
                session.activeBoundary = candidates.items[candidates.selectedIndex ?? 0]
                    .consumingCount
            }
            session.phase = .selecting
            session.advanceRevision()
            result = success()

        case .navigateCandidatePage(let delta):
            clearLivePresentation()
            guard var candidates = session.candidates, !candidates.items.isEmpty,
                  session.phase == .composing || session.phase == .previewing
                    || session.phase == .selecting || session.phase == .reconverting else {
                result = failure(.invalidAction, "candidate paging requires candidates")
                break
            }
            let page = max(candidates.pageSize, 1)
            let current = candidates.selectedIndex ?? 0
            candidates.selectedIndex = min(
                max(current + delta * page, 0), candidates.items.count - 1
            )
            session.candidates = candidates
            syncActiveSegmentCandidates(candidates)
            if session.activeSegmentIndex == nil {
                session.activeBoundary = candidates.items[candidates.selectedIndex ?? 0]
                    .consumingCount
            }
            session.phase = .selecting
            session.advanceRevision()
            result = success()

        case .resizeSegment(let delta):
            result = resizeSegment(delta)

        case .moveActiveSegment(let delta):
            result = moveActiveSegment(delta)

        case .selectCandidate(let id, let generation):
            guard var candidates = session.candidates,
                  candidates.generation == generation,
                  let index = candidates.items.firstIndex(where: { $0.id == id }) else {
                result = failure(.staleCandidate, "candidate generation or id is stale")
                break
            }
            candidates.selectedIndex = index
            session.candidates = candidates
            syncActiveSegmentCandidates(candidates)
            clearLivePresentation()
            session.phase = .selecting
            session.advanceRevision()
            result = success()

        case .commitSelected:
            result = commitSelectedCandidate()

        case .commitAll:
            result = commitAll()

        case .cancel:
            result = cancel()

        case .transformActiveSegment(let transform):
            result = transformActiveSegment(transform)

        case .forgetCandidate(let id, let generation):
            clearLivePresentation()
            guard !session.policy.secureInput else {
                result = failure(.secureInputViolation, "learning is disabled for secure input")
                break
            }
            guard let candidate = candidate(id: id, generation: generation) else {
                result = failure(.staleCandidate, "candidate generation or id is stale")
                break
            }
            converter.forget(ConverterCandidate(
                text: candidate.text,
                annotation: candidate.annotation,
                consumingCount: candidate.consumingCount,
                sourceID: candidate.sourceID
            ))
            session.advanceRevision()
            result = success()

        case .reconvert(
            let text,
            let leftContext,
            let rightContext,
            let deleteBefore,
            let deleteAfter
        ):
            guard !text.isEmpty, deleteBefore >= 0, deleteAfter >= 0 else {
                result = failure(.invalidAction, "reconversion text must not be empty")
                break
            }
            let selectedCount = text.unicodeScalars.count
            let (replacementCount, replacementCountOverflow) =
                deleteBefore.addingReportingOverflow(deleteAfter)
            guard !replacementCountOverflow,
                  replacementCount == 0 || replacementCount == selectedCount else {
                result = failure(
                    .invalidAction,
                    "reconversion replacement range does not match selected text"
                )
                break
            }
            let securityDomainChanged = pinCompositionPolicyIfNeeded(
                compositionStartPolicyProvider
            )
            guard !session.policy.secureInput else {
                if securityDomainChanged {
                    // The secure transition already discarded pending
                    // learning and erased the previous domain. Publish that
                    // state transition even though reconversion itself is
                    // forbidden so the same revision never denotes two
                    // different snapshots.
                    session.advanceRevision()
                }
                result = failure(
                    .secureInputViolation,
                    "surrounding-text reconversion is disabled for secure input"
                )
                break
            }
            resolvePendingLearning(commit: true)
            clearLivePresentation()
            endConverterComposition()
            session.composingText = CompositionBuffer()
            session.composingText.insert(text, inputStyle: .direct)
            session.context.leftContext = leftContext
            session.context.rightContext = rightContext
            session.reconversionReplacement = if replacementCount > 0 {
                ReconversionReplacement(before: deleteBefore, after: deleteAfter)
            } else {
                nil
            }
            session.phase = .reconverting
            clearConversionState()
            result = convert()

        case .beginUnicodeInput:
            guard session.phase == .idle || session.phase == .composing else {
                result = failure(
                    .invalidAction,
                    "Unicode input can begin only while idle or composing"
                )
                break
            }
            pinCompositionPolicyIfNeeded(compositionStartPolicyProvider)
            endConverterComposition()
            resolvePendingLearning(commit: true)
            clearLivePresentation()
            session.phaseBeforeUnicodeInput = session.phase
            session.unicodeInputBuffer = ""
            clearConversionState()
            session.phase = .unicodeInput
            session.advanceRevision()
            result = success()

        case .appendUnicodeDigit(let digit):
            guard session.phase == .unicodeInput,
                  digit.count == 1,
                  digit.unicodeScalars.allSatisfy({
                      (0x30...0x39).contains($0.value)
                          || (0x41...0x46).contains($0.value)
                          || (0x61...0x66).contains($0.value)
                  }),
                  session.unicodeInputBuffer.count < 8 else {
                result = failure(.invalidAction, "Unicode input requires one hexadecimal digit")
                break
            }
            session.unicodeInputBuffer.append(digit.lowercased())
            session.advanceRevision()
            result = success()

        case .commitUnicodeInput:
            guard session.phase == .unicodeInput,
                  !session.unicodeInputBuffer.isEmpty,
                  let value = UInt32(session.unicodeInputBuffer, radix: 16),
                  let scalar = UnicodeScalar(value) else {
                result = failure(.invalidAction, "Unicode scalar is invalid")
                break
            }
            session.composingText.insert(String(scalar), inputStyle: .direct)
            finishUnicodeInput(cancelled: false)
            session.advanceRevision()
            result = success()

        case .updateContext(let leftContext, let rightContext):
            guard session.phase == .idle else {
                result = failure(
                    .invalidAction,
                    "surrounding context is pinned while composing"
                )
                break
            }
            if session.policy.secureInput {
                session.context.leftContext = ""
                session.context.rightContext = ""
            } else {
                session.context.leftContext = leftContext
                session.context.rightContext = rightContext
            }
            session.advanceRevision()
            result = success()

        case .restoreCheckpoint(let data):
            result = restoreCheckpoint(data)

        case .resolvePendingLearning(let commit):
            resolvePendingLearning(commit: commit)
            session.advanceRevision()
            result = success()

        case .lifecycle(let event):
            result = lifecycle(event)
        }
        return cache(
            result,
            action: action,
            requestID: requestID,
            expectedRevision: expectedRevision
        )
    }

    private func cache(
        _ result: ImeReductionResult,
        action: ImeAction,
        requestID: String,
        expectedRevision: UInt64?
    ) -> ImeReductionResult {
        guard !requestID.isEmpty else { return result }
        requestCache[requestID] = CachedRequest(
            action: action,
            expectedRevision: expectedRevision,
            result: result
        )
        requestOrder.append(requestID)
        // Secure input still needs one-result idempotency for the transport's
        // immediate retry, but retaining a long history would keep old secret
        // snapshots alive after the active composition has moved on.
        let limit = session.policy.secureInput ? 1 : requestCacheLimit
        while requestOrder.count > limit {
            requestCache.removeValue(forKey: requestOrder.removeFirst())
        }
        return result
    }

    private func clearRequestCache() {
        requestCache.removeAll(keepingCapacity: false)
        requestOrder.removeAll(keepingCapacity: false)
    }

    private func rebaseRequestCacheToCurrentSnapshot() {
        requestCache = requestCache.mapValues { cached in
            CachedRequest(
                action: cached.action,
                expectedRevision: cached.expectedRevision,
                result: ImeReductionResult(
                    status: cached.result.status,
                    message: cached.result.message,
                    snapshot: snapshot(effects: cached.result.snapshot.effects)
                )
            )
        }
    }

    @discardableResult
    private func pinCompositionPolicyIfNeeded(
        _ provider: (() -> PinnedCompositionPolicy)?
    ) -> Bool {
        guard session.phase == .idle,
              session.composingText.isEmpty,
              let provider else {
            return false
        }
        return pinCompositionPolicy(provider())
    }

    private func finishInteractiveEdit() -> ImeReductionResult {
        if shouldDirectCommitVisibleSuffix() {
            return commitAll(learningOrigin: .directCommit)
        }
        guard session.policy.autoConvertMode != .disabled else {
            refreshPredictions()
            session.livePresentation.pendingRevision = nil
            session.advanceRevision()
            return success()
        }
        guard shouldScheduleLiveConversion() else {
            session.livePresentation.pendingRevision = nil
            session.advanceRevision()
            return success()
        }

        let delay = min(session.policy.liveConversionDelayMilliseconds, 1_000)
        if delay == 0 {
            refreshRealtimeCandidates()
            session.advanceRevision()
            return success()
        }

        // Reserve the effect before checkpointing so recovery cannot reuse its
        // ID. The scheduled revision is the post-edit revision published with
        // this effect.
        let effectID = session.allocateEffectID()
        let scheduledRevision = session.revision &+ 1
        session.advanceRevision()
        session.livePresentation.pendingRevision = scheduledRevision
        return success(effects: [
            .scheduleLiveConversion(
                effectID: effectID,
                delayMilliseconds: delay,
                scheduledRevision: scheduledRevision
            )
        ])
    }

    private func shouldScheduleLiveConversion() -> Bool {
        guard session.phase == .composing,
              !session.composingText.isEmpty,
              !session.policy.secureInput,
              session.composingText.cursor == session.composingText.elements.count else {
            return false
        }
        switch session.policy.autoConvertMode {
        case .disabled:
            return false
        case .always:
            return true
        case .forMultipleChars:
            // Composition elements are keystrokes. Use the rendered reading so
            // Romaji such as "ka" still counts as one Japanese character.
            return currentDisplay().text.count > 1
        }
    }

    private func refreshRealtimeCandidates() {
        guard session.phase == .composing, !session.composingText.isEmpty else {
            return
        }
        let input = CompositionInput(
            elements: session.composingText.elements,
            cursor: session.composingText.cursor,
            leftContext: session.context.leftContext,
            mappedTableName: session.policy.inputTableName
        )
        let display = converter.display(for: input)
        do {
            let output = try converter.realtimeCandidates(
                for: input,
                options: ConversionOptions(
                    allowLearning: session.policy.allowsLearning
                        && !session.policy.secureInput,
                    zenzaiEnabled: session.policy.zenzaiEnabled
                        && !session.policy.secureInput,
                    secureInput: session.policy.secureInput,
                    leftContext: session.context.leftContext,
                    rightContext: session.context.rightContext,
                    suggestionListMode: session.policy.suggestionListMode,
                    suggestionListLimit: session.policy.suggestionListLimit
                )
            )
            let liveCandidate: CandidateSnapshot? = shouldPublishLiveCandidate(
                for: display
            )
                ? output.liveCandidate.map { makeSnapshot($0) }
                : nil
            guard !output.candidates.isEmpty || liveCandidate != nil else {
                clearLivePresentation()
                clearConversionState()
                return
            }
            clearSegmentedConversion()
            let generation = session.allocateCandidateGeneration()
            session.candidates = CandidateSet(
                generation: generation,
                items: output.candidates.enumerated().map { index, candidate in
                    makeSnapshot(candidate, generation: generation, index: index)
                },
                selectedIndex: nil,
                pageSize: output.candidates.isEmpty
                    ? 0
                    : max(1, min(output.pageSize, output.candidates.count)),
                origin: .prediction,
                liveCandidate: liveCandidate.map {
                    CandidateSnapshot(
                        id: "\(generation)-live",
                        text: $0.text,
                        annotation: $0.annotation,
                        consumingCount: $0.consumingCount,
                        sourceID: $0.sourceID,
                        provenance: $0.provenance
                    )
                }
            )
            session.activeBoundary = nil
            if let liveCandidate {
                let consumed = min(
                    max(liveCandidate.consumingCount, 1),
                    session.composingText.elements.count
                )
                let sourceElements = Array(
                    session.composingText.elements.prefix(consumed)
                )
                let sourceReading = converter.display(for: CompositionInput(
                    elements: sourceElements,
                    cursor: sourceElements.count,
                    leftContext: session.context.leftContext,
                    mappedTableName: session.policy.inputTableName
                )).text
                session.livePresentation.materializedPrefix = MaterializedLivePrefix(
                    text: liveCandidate.text,
                    consumedElementCount: consumed,
                    sourceElements: sourceElements,
                    sourceReading: sourceReading,
                    candidate: liveCandidate
                )
            } else {
                session.livePresentation.materializedPrefix = nil
            }
            session.livePresentation.pendingRevision = nil
        } catch {
            // Live conversion is opportunistic. Keep the reading editable when
            // the converter cannot produce a realtime result.
            clearLivePresentation()
            clearConversionState()
        }
    }

    private func shouldPublishLiveCandidate(
        for display: CompositionDisplay
    ) -> Bool {
        // A converted surface does not retain enough information to place an
        // editing caret inside it. Keep the reading visible while editing in
        // the middle, then resume live conversion when the caret returns to
        // the end.
        guard session.composingText.cursor == session.composingText.elements.count else {
            return false
        }
        switch session.policy.autoConvertMode {
        case .disabled:
            return false
        case .always:
            return true
        case .forMultipleChars:
            // Composition elements are keystrokes. With the default Romaji
            // table, "ka" is two elements but only one rendered kana.
            return display.text.count > 1
        }
    }

    private func makeSnapshot(
        _ candidate: ConverterCandidate,
        generation: UInt64 = 0,
        index: Int = 0
    ) -> CandidateSnapshot {
        CandidateSnapshot(
            id: generation == 0 ? "realtime-\(index)" : "\(generation)-\(index)",
            text: candidate.text,
            annotation: candidate.annotation,
            consumingCount: min(
                max(candidate.consumingCount, 1),
                session.composingText.elements.count
            ),
            sourceID: candidate.sourceID,
            provenance: candidate.provenance
        )
    }

    private func refreshPredictions() {
        guard session.phase == .composing, !session.composingText.isEmpty else {
            return
        }
        clearLivePresentation()
        let input = CompositionInput(
            elements: session.composingText.elements,
            cursor: session.composingText.cursor,
            leftContext: session.context.leftContext,
            mappedTableName: session.policy.inputTableName
        )
        do {
            let output = try converter.predictions(
                for: input,
                options: ConversionOptions(
                    allowLearning: session.policy.allowsLearning
                        && !session.policy.secureInput,
                    zenzaiEnabled: session.policy.zenzaiEnabled
                        && !session.policy.secureInput,
                    secureInput: session.policy.secureInput,
                    leftContext: session.context.leftContext,
                    rightContext: session.context.rightContext,
                    suggestionListMode: session.policy.suggestionListMode,
                    suggestionListLimit: session.policy.suggestionListLimit
                )
            )
            guard !output.candidates.isEmpty else {
                clearConversionState()
                clearLivePresentation()
                return
            }
            clearSegmentedConversion()
            let generation = session.allocateCandidateGeneration()
            session.candidates = CandidateSet(
                generation: generation,
                items: output.candidates.enumerated().map { index, candidate in
                    CandidateSnapshot(
                        id: "\(generation)-\(index)",
                        text: candidate.text,
                        annotation: candidate.annotation,
                        consumingCount: min(
                            max(candidate.consumingCount, 1),
                            session.composingText.elements.count
                        ),
                        sourceID: candidate.sourceID,
                        provenance: candidate.provenance
                    )
                },
                selectedIndex: nil,
                pageSize: max(1, min(output.pageSize, output.candidates.count)),
                origin: .prediction,
                liveCandidate: nil
            )
            session.activeBoundary = nil
        } catch {
            // Predictions are opportunistic. Conversion and the editable
            // reading remain available even when prediction generation fails.
            clearConversionState()
        }
    }

    private func convert(advanceRevision: Bool = true) -> ImeReductionResult {
        let isReconversion = session.phase == .reconverting
        guard !session.composingText.isEmpty else {
            session.phase = .idle
            clearConversionState()
            if advanceRevision { session.advanceRevision() }
            return success()
        }
        if !converter.supportsSegmentEditing {
            return convertLegacy(
                isReconversion: isReconversion,
                advanceRevision: advanceRevision
            )
        }
        endConverterComposition()
        do {
            let segments = try buildSegments(
                from: session.composingText.elements
            )
            session.segments = segments
            activateSegment(at: 0)
            session.composingText.moveCursorToEnd()
            session.phase = isReconversion ? .reconverting : .previewing
            if advanceRevision { session.advanceRevision() }
            return success()
        } catch {
            endConverterComposition()
            session.phase = isReconversion ? .reconverting : .composing
            clearConversionState()
            if advanceRevision { session.advanceRevision() }
            return failure(.converterUnavailable, "converter failed: \(error)")
        }
    }

    /// Compatibility path for converter implementations that only expose a
    /// single converted prefix. The production Hazkey adapter opts into the
    /// segmented path; this keeps the versioned cross-platform v1 contract
    /// valid for older ports without pretending they have clause metadata.
    private func convertLegacy(
        isReconversion: Bool,
        advanceRevision: Bool
    ) -> ImeReductionResult {
        let input = CompositionInput(
            elements: session.composingText.elements,
            cursor: session.composingText.cursor,
            leftContext: session.context.leftContext,
            targetCount: session.activeBoundary,
            mappedTableName: session.policy.inputTableName
        )
        do {
            let output = try converter.candidates(
                for: input,
                options: conversionOptions(leftContext: session.context.leftContext)
            )
            guard !output.candidates.isEmpty else {
                endConverterComposition()
                session.phase = isReconversion ? .reconverting : .composing
                clearConversionState()
                if advanceRevision { session.advanceRevision() }
                return success()
            }
            let generation = session.allocateCandidateGeneration()
            let items = output.candidates.enumerated().map { index, candidate in
                makeSnapshot(candidate, generation: generation, index: index)
            }
            session.activeBoundary = items[0].consumingCount
            session.candidates = CandidateSet(
                generation: generation,
                items: items,
                selectedIndex: 0,
                pageSize: max(1, min(output.pageSize, items.count)),
                origin: .conversion,
                liveCandidate: nil
            )
            clearSegmentedConversion()
            session.phase = isReconversion ? .reconverting : .previewing
            if advanceRevision { session.advanceRevision() }
            return success()
        } catch {
            endConverterComposition()
            session.phase = isReconversion ? .reconverting : .composing
            clearConversionState()
            if advanceRevision { session.advanceRevision() }
            return failure(.converterUnavailable, "converter failed: \(error)")
        }
    }

    /// Decomposes the supplied composition slice into first-clause results.
    /// Every segment owns its candidate set, allowing focus to move without
    /// losing choices already made in other segments. `initialLeftContext`
    /// lets callers rebuild a suffix while reusing the segments before it.
    private func buildSegments(
        from elements: [CompositionElement],
        forcedLeadingCounts: [Int] = [],
        preferredTextsByStart: [Int: String] = [:],
        initialLeftContext: String? = nil
    ) throws -> [CompositionSegment] {
        var result: [CompositionSegment] = []
        var offset = 0
        var forcedIndex = 0
        var leftContext = initialLeftContext ?? session.context.leftContext

        while offset < elements.count {
            let remaining = Array(elements.dropFirst(offset))
            let forcedCount: Int? = if forcedIndex < forcedLeadingCounts.count {
                min(max(forcedLeadingCounts[forcedIndex], 1), remaining.count)
            } else {
                nil
            }
            let input = CompositionInput(
                elements: remaining,
                cursor: remaining.count,
                leftContext: leftContext,
                targetCount: forcedCount,
                mappedTableName: session.policy.inputTableName
            )
            let options = conversionOptions(leftContext: leftContext)
            let output = if forcedCount != nil {
                try converter.candidates(for: input, options: options)
            } else {
                try converter.segmentCandidates(for: input, options: options)
            }
            let segmentCount = forcedCount ?? min(
                max(output.candidates.first?.consumingCount ?? remaining.count, 1),
                remaining.count
            )
            var matching = output.candidates.filter {
                min(max($0.consumingCount, 1), remaining.count) == segmentCount
            }
            if matching.isEmpty {
                let segmentElements = Array(remaining.prefix(segmentCount))
                let display = converter.display(for: CompositionInput(
                    elements: segmentElements,
                    cursor: segmentElements.count,
                    leftContext: leftContext,
                    mappedTableName: session.policy.inputTableName
                ))
                matching = [ConverterCandidate(
                    text: display.text,
                    consumingCount: segmentCount
                )]
            }

            let generation = session.allocateCandidateGeneration()
            let snapshots = matching.enumerated().map { index, candidate in
                CandidateSnapshot(
                    id: "\(generation)-\(index)",
                    text: candidate.text,
                    annotation: candidate.annotation,
                    consumingCount: segmentCount,
                    sourceID: candidate.sourceID,
                    provenance: candidate.provenance
                )
            }
            let preferredText = preferredTextsByStart[offset]
            let selectedIndex = preferredText.flatMap { text in
                snapshots.firstIndex(where: { $0.text == text })
            } ?? 0
            let candidateSet = CandidateSet(
                generation: generation,
                items: snapshots,
                selectedIndex: selectedIndex,
                pageSize: max(1, min(output.pageSize, snapshots.count)),
                origin: .conversion,
                liveCandidate: nil
            )
            result.append(CompositionSegment(
                inputCount: segmentCount,
                candidates: candidateSet
            ))
            leftContext.append(snapshots[selectedIndex].text)
            offset += segmentCount
            if forcedCount != nil { forcedIndex += 1 }
        }
        return result
    }

    private func conversionOptions(leftContext: String) -> ConversionOptions {
        ConversionOptions(
            allowLearning: session.policy.allowsLearning && !session.policy.secureInput,
            zenzaiEnabled: session.policy.zenzaiEnabled && !session.policy.secureInput,
            secureInput: session.policy.secureInput,
            leftContext: leftContext,
            rightContext: session.context.rightContext,
            suggestionListMode: session.policy.suggestionListMode,
            suggestionListLimit: session.policy.suggestionListLimit
        )
    }

    private func activateSegment(at requestedIndex: Int) {
        guard !session.segments.isEmpty else {
            clearConversionState()
            return
        }
        let index = min(max(requestedIndex, 0), session.segments.count - 1)
        session.activeSegmentIndex = index
        session.candidates = session.segments[index].candidates
        session.activeBoundary = session.segments[index].inputCount
    }

    private func syncActiveSegmentCandidates(_ candidates: CandidateSet) {
        guard let index = session.activeSegmentIndex,
              session.segments.indices.contains(index) else { return }
        session.segments[index].candidates = candidates
        session.activeBoundary = session.segments[index].inputCount
    }

    private func moveActiveSegment(_ delta: Int) -> ImeReductionResult {
        guard !session.segments.isEmpty,
              let activeIndex = session.activeSegmentIndex,
              session.phase == .previewing || session.phase == .selecting
                || session.phase == .reconverting else {
            return failure(.invalidAction, "segment movement requires converted segments")
        }
        let (requested, overflow) = activeIndex.addingReportingOverflow(delta)
        let index = overflow
            ? (delta < 0 ? 0 : session.segments.count - 1)
            : min(max(requested, 0), session.segments.count - 1)
        activateSegment(at: index)
        session.phase = .selecting
        session.advanceRevision()
        return success()
    }

    private func resizeSegment(_ delta: Int) -> ImeReductionResult {
        guard !session.composingText.isEmpty,
              session.phase == .composing || session.phase == .previewing
                || session.phase == .selecting || session.phase == .reconverting else {
            return failure(.invalidAction, "segment resizing requires an active segment")
        }
        if !converter.supportsSegmentEditing {
            return resizeLegacySegment(delta)
        }
        let oldSegments = session.segments
        let activeIndex = session.activeSegmentIndex ?? 0
        let prefixSegments: [CompositionSegment]
        let prefixTotal: Int
        let currentCount: Int?
        if oldSegments.indices.contains(activeIndex) {
            prefixSegments = Array(oldSegments.prefix(activeIndex))
            prefixTotal = prefixSegments.reduce(0) { $0 + $1.inputCount }
            currentCount = oldSegments[activeIndex].inputCount
        } else {
            prefixSegments = []
            prefixTotal = 0
            currentCount = nil
        }

        // A resize cannot affect converted text before the active boundary.
        // Keep those candidate sets (including their generation and selected
        // item) and feed their selected text into the suffix conversion.
        var suffixLeftContext = session.context.leftContext
        for segment in prefixSegments {
            if let selected = segment.selectedCandidate {
                suffixLeftContext.append(selected.text)
            }
        }
        let suffixElements = Array(
            session.composingText.elements.dropFirst(prefixTotal)
        )
        let startingCursor = currentCount ?? (delta > 0 ? 0 : suffixElements.count)
        let boundaryInput = CompositionInput(
            elements: suffixElements,
            cursor: startingCursor,
            leftContext: suffixLeftContext,
            mappedTableName: session.policy.inputTableName
        )
        let movedBoundary = converter.inputCursorPosition(
            for: boundaryInput,
            movingBy: delta
        )
        // The minimum segment is the first stable rendered boundary, which may
        // span multiple raw Romaji keystrokes (for example `kyo`). Never clamp
        // a movement to raw input index 1 because that can split a dependent
        // sequence and make the converter reject an otherwise valid resize.
        let firstStableBoundary = converter.inputCursorPosition(
            for: CompositionInput(
                elements: suffixElements,
                cursor: 0,
                leftContext: suffixLeftContext,
                mappedTableName: session.policy.inputTableName
            ),
            movingBy: 1
        )
        let stableBoundary = movedBoundary > 0
            ? movedBoundary
            : firstStableBoundary
        guard stableBoundary > 0 else { return success() }
        let newCount = min(stableBoundary, suffixElements.count)
        if let currentCount {
            guard newCount != currentCount else { return success() }
        }
        var preferredTextsByStart: [Int: String] = [:]
        var start = 0
        for segment in oldSegments {
            if start >= prefixTotal, let selected = segment.selectedCandidate {
                preferredTextsByStart[start - prefixTotal] = selected.text
            }
            start += segment.inputCount
        }
        endConverterComposition()
        do {
            let rebuiltSuffix = try buildSegments(
                from: suffixElements,
                forcedLeadingCounts: [newCount],
                preferredTextsByStart: preferredTextsByStart,
                initialLeftContext: suffixLeftContext
            )
            session.segments = prefixSegments + rebuiltSuffix
            activateSegment(at: activeIndex)
            session.phase = .selecting
            session.advanceRevision()
            return success()
        } catch {
            session.phase = session.reconversionReplacement == nil ? .composing : .reconverting
            clearConversionState()
            session.advanceRevision()
            return failure(.converterUnavailable, "converter failed: \(error)")
        }
    }

    private func resizeLegacySegment(_ delta: Int) -> ImeReductionResult {
        let count = session.composingText.elements.count
        let newBoundary: Int
        if let boundary = session.activeBoundary {
            newBoundary = min(max(boundary + delta, 1), count)
            guard newBoundary != boundary else { return success() }
        } else if delta > 0 {
            newBoundary = 1
        } else {
            newBoundary = min(max(count + delta, 1), count)
        }
        endConverterComposition()
        session.activeBoundary = newBoundary
        session.candidates = nil
        clearSegmentedConversion()
        session.phase = session.reconversionReplacement == nil ? .composing : .reconverting
        let converted = convertLegacy(
            isReconversion: session.phase == .reconverting,
            advanceRevision: false
        )
        guard converted.status == .success else { return converted }
        if session.candidates != nil { session.phase = .selecting }
        session.advanceRevision()
        return success()
    }

    private func commitSelectedCandidate() -> ImeReductionResult {
        let reading = currentDisplay().text
        if let activeIndex = session.activeSegmentIndex,
           session.segments.indices.contains(activeIndex) {
            let committedSegments = Array(session.segments.prefix(activeIndex + 1))
            let committedCandidates = committedSegments.compactMap(\.selectedCandidate)
            guard committedCandidates.count == committedSegments.count else {
                return failure(.invalidAction, "no candidate is selected")
            }
            resolvePendingLearning(commit: true)
            let count = committedSegments.reduce(0) { $0 + $1.inputCount }
            let text = committedCandidates.map(\.text).joined()
            let _ = session.composingText.removePrefix(count: count)
            var effects = takeReconversionReplacementEffect()
            effects.append(.commitText(effectID: session.allocateEffectID(), text: text))
            if !session.policy.secureInput {
                session.context.leftContext.append(text)
            }
            learn(
                committedCandidates,
                origin: .explicitConversion,
                reading: reading
            )
            if !session.composingText.isEmpty {
                // Application-level undo can only cancel a complete visible
                // commit. Once a suffix remains under IME control, editing or
                // cancelling that suffix must not discard learning for the
                // prefix that has already been delivered to the application.
                resolvePendingLearning(commit: true)
            }
            endConverterComposition()
            clearConversionState()
            session.composingText.moveCursorToEnd()
            if session.composingText.isEmpty {
                session.phase = .idle
            } else {
                session.phase = .composing
                // The prefix has already been committed. Re-converting the
                // remainder is best-effort; a converter failure must not hide
                // the commit effect or make the client and reducer diverge.
                _ = convert(advanceRevision: false)
            }
            session.advanceRevision()
            return success(effects: effects)
        }
        guard let candidates = session.candidates,
              let selected = candidates.selectedIndex,
              candidates.items.indices.contains(selected) else {
            return failure(.invalidAction, "no candidate is selected")
        }
        let candidate = candidates.items[selected]
        let count = min(candidate.consumingCount, session.composingText.elements.count)
        guard count > 0 else { return failure(.invalidAction, "candidate consumes no input") }
        resolvePendingLearning(commit: true)
        let _ = session.composingText.removePrefix(count: count)
        var effects = takeReconversionReplacementEffect()
        effects.append(.commitText(effectID: session.allocateEffectID(), text: candidate.text))
        if !session.policy.secureInput {
            session.context.leftContext.append(candidate.text)
        }
        learn(
            [candidate],
            origin: .explicitConversion,
            reading: reading
        )
        if !session.composingText.isEmpty {
            resolvePendingLearning(commit: true)
        }
        clearConversionState()
        clearLivePresentation()
        session.phase = session.composingText.isEmpty ? .idle : .composing
        session.composingText.moveCursorToEnd()
        session.advanceRevision()
        if !session.composingText.isEmpty {
            _ = convert(advanceRevision: false)
        } else {
            endConverterComposition()
        }
        return success(effects: effects)
    }

    private func commitAll(
        learningOrigin: LearningOrigin = .explicitConversion
    ) -> ImeReductionResult {
        let resolvedLearning = resolvePendingLearning(commit: true)
        guard !session.composingText.isEmpty else {
            if resolvedLearning {
                // pendingLearning is part of the published snapshot, so a
                // successful resolution is a semantic state transition even
                // when there is no preedit left to commit.
                session.advanceRevision()
            }
            return success()
        }
        let reading = currentDisplay().text
        var visible = visibleComposition()
        if !session.segments.isEmpty {
            let selectedCandidates = session.segments.compactMap(\.selectedCandidate)
            guard selectedCandidates.count == session.segments.count else {
                return failure(.invalidAction, "a converted segment has no selection")
            }
            visible = VisibleComposition(
                spans: visible.spans,
                text: selectedCandidates.map(\.text).joined(),
                caretUtf8ByteOffset: UInt32(
                    selectedCandidates.map(\.text).joined().utf8.count
                ),
                learnableCandidates: selectedCandidates
            )
        }
        let text = visible.text
        var effects = takeReconversionReplacementEffect()
        effects.append(.commitText(effectID: session.allocateEffectID(), text: text))
        if !session.policy.secureInput {
            session.context.leftContext.append(text)
        }
        learn(
            visible.learnableCandidates,
            origin: learningOrigin,
            reading: reading
        )
        session.composingText = CompositionBuffer()
        clearConversionState()
        clearLivePresentation()
        session.phase = .idle
        session.advanceRevision()
        endConverterComposition()
        return success(effects: effects)
    }

    private func cancel() -> ImeReductionResult {
        let resolvedLearning = resolvePendingLearning(commit: false)
        clearLivePresentation()
        switch session.phase {
        case .selecting:
            if session.candidates?.origin == .prediction {
                endConverterComposition()
                clearConversionState()
                session.phase = .composing
            } else if session.reconversionReplacement != nil {
                session.phase = .reconverting
            } else {
                session.phase = .previewing
            }
        case .previewing:
            endConverterComposition()
            session.phase = .composing
            clearConversionState()
        case .unicodeInput:
            finishUnicodeInput(cancelled: true)
        case .composing, .reconverting:
            endConverterComposition()
            session.composingText = CompositionBuffer()
            clearConversionState()
            session.reconversionReplacement = nil
            session.phase = .idle
        case .idle:
            if resolvedLearning {
                session.advanceRevision()
            }
            return success()
        }
        session.advanceRevision()
        return success()
    }

    private func transformActiveSegment(_ transform: ImeTextTransform) -> ImeReductionResult {
        let resolvedLearning = resolvePendingLearning(commit: session.phase == .idle)
        clearLivePresentation()
        let source: String
        if let candidates = session.candidates,
           let index = candidates.selectedIndex,
           candidates.items.indices.contains(index) {
            source = candidates.items[index].text
        } else {
            source = currentDisplay().text
        }
        let transformed = transformText(source, as: transform)
        guard !transformed.isEmpty else {
            if resolvedLearning {
                session.advanceRevision()
            }
            return success()
        }
        if var candidates = session.candidates,
           let index = candidates.selectedIndex,
           candidates.items.indices.contains(index) {
            let old = candidates.items[index]
            candidates.items[index] = CandidateSnapshot(
                id: old.id,
                text: transformed,
                annotation: old.annotation,
                consumingCount: old.consumingCount,
                // Transformed candidates are direct text and must not learn or
                // forget the converter's original, differently rendered item.
                sourceID: nil
            )
            session.candidates = candidates
            syncActiveSegmentCandidates(candidates)
            session.phase = .selecting
        } else {
            endConverterComposition()
            clearConversionState()
            session.composingText = CompositionBuffer()
            session.composingText.insert(transformed, inputStyle: .direct)
            session.phase = .composing
        }
        session.advanceRevision()
        return success()
    }

    private func lifecycle(_ event: ImeLifecycleEvent) -> ImeReductionResult {
        switch event {
        case .secureInputChanged(let secure):
            if session.policy.secureInput != secure {
                resolvePendingLearning(commit: false)
                purgeConverterSensitiveState()
                session.policy.secureInput = secure
                // Crossing either direction is a security-domain transition. Do
                // not carry text or context from the previous field into the new
                // domain, even if a non-Fcitx client sends lifecycle actions
                // without reopening the session.
                clearSecurityDomainState()
            }
        case .deactivate, .focusChanged:
            if session.policy.secureInput {
                resolvePendingLearning(commit: false)
                purgeConverterSensitiveState()
                clearSecurityDomainState()
            } else {
                resolvePendingLearning(commit: true)
                endConverterComposition()
                clearConversionState()
                clearLivePresentation()
                session.phase = session.composingText.isEmpty ? .idle : .composing
            }
        case .capabilityChanged:
            break
        case .serverRestarted:
            resolvePendingLearning(commit: false)
            purgeConverterSensitiveState()
            if session.policy.secureInput {
                // A secure session has no restorable checkpoint. Retaining its
                // preedit or surrounding context across a server boundary would
                // re-publish secrets in the replacement session.
                clearSecurityDomainState()
            } else {
                // Non-secure recovery keeps the editable reading and rebuilds
                // only converter-owned presentation state.
                clearConversionState()
                clearLivePresentation()
                session.phase = session.composingText.isEmpty ? .idle : .composing
            }
        }
        session.advanceRevision()
        return success()
    }

    private func restoreCheckpoint(_ data: Data) -> ImeReductionResult {
        guard !session.policy.secureInput else {
            return failure(
                .secureInputViolation,
                "secure input sessions cannot restore checkpoints"
            )
        }
        let checkpoint: RecoveryCheckpoint
        do {
            checkpoint = try JSONDecoder().decode(RecoveryCheckpoint.self, from: data)
        } catch {
            return failure(.invalidAction, "recovery checkpoint is malformed")
        }
        guard !checkpoint.policy.secureInput else {
            return failure(
                .secureInputViolation,
                "secure input checkpoints are not restorable"
            )
        }
        guard checkpoint.revision >= session.revision else {
            return failure(.staleRevision, "recovery checkpoint is stale")
        }
        guard checkpoint.revision < UInt64.max,
              checkpoint.nextCandidateGeneration < UInt64.max,
              checkpoint.nextEffectID > 0,
              checkpoint.nextEffectID < UInt64.max,
              checkpointIsWellFormed(checkpoint) else {
            return failure(.invalidAction, "recovery checkpoint contains invalid state")
        }

        // Validation above is intentionally side-effect free. A malformed,
        // stale, or forbidden checkpoint must not consume the current undo
        // window or alter presentation state at the same revision.
        resolvePendingLearning(commit: false)
        clearLivePresentation()
        endConverterComposition()
        session.composingText = checkpoint.composition
        clearConversionState()
        session.phase = checkpoint.composition.isEmpty ? .idle : .composing
        session.revision = checkpoint.revision
        session.nextCandidateGeneration = checkpoint.nextCandidateGeneration &+ 1
        session.nextEffectID = checkpoint.nextEffectID
        session.context.leftContext = checkpoint.leftContext
        session.context.rightContext = checkpoint.rightContext
        session.context.projectRevision = checkpoint.policy.projectRevision
        // Runtime table names identify process-local InputStyleManager data.
        // Preserve the composition's pinned semantic policy, but rebind that
        // one runtime handle to the newly opened server session.
        let currentPolicy = session.policy
        session.policy = checkpoint.policy
        session.policy.rebindLegacySuggestionListLimit(
            to: currentPolicy.suggestionListLimit
        )
        session.policy.inputTableName = currentPolicy.inputTableName
        // Opaque recovery bytes are client-controlled. They may preserve a
        // stricter pinned policy, but can never enable capabilities denied by
        // the newly opened server session.
        session.policy.allowsLearning = currentPolicy.allowsLearning
            && checkpoint.policy.allowsLearning
        session.policy.zenzaiEnabled = currentPolicy.zenzaiEnabled
            && checkpoint.policy.zenzaiEnabled
        session.policy.secureInput = false
        session.reconversionReplacement = checkpoint.reconversionReplacement
        session.unicodeInputBuffer = checkpoint.unicodeInputBuffer ?? ""
        session.phaseBeforeUnicodeInput = checkpoint.phaseBeforeUnicodeInput
        if checkpoint.phase == .unicodeInput {
            session.phase = .unicodeInput
        }
        session.advanceRevision()
        return success()
    }

    private func candidate(id: String, generation: UInt64) -> CandidateSnapshot? {
        guard let candidates = session.candidates, candidates.generation == generation else {
            return nil
        }
        return candidates.items.first { $0.id == id }
    }

    private func preserveMaterializedLivePrefixForEditing() {
        guard session.phase == .composing,
              session.composingText.cursor == session.composingText.elements.count,
              let liveCandidate = session.candidates?.liveCandidate else {
            return
        }
        let consumed = min(
            max(liveCandidate.consumingCount, 1),
            session.composingText.elements.count
        )
        let sourceElements = Array(session.composingText.elements.prefix(consumed))
        let sourceReading = converter.display(for: CompositionInput(
            elements: sourceElements,
            cursor: sourceElements.count,
            leftContext: session.context.leftContext,
            mappedTableName: session.policy.inputTableName
        )).text
        session.livePresentation.materializedPrefix = MaterializedLivePrefix(
            text: liveCandidate.text,
            consumedElementCount: consumed,
            sourceElements: sourceElements,
            sourceReading: sourceReading,
            candidate: liveCandidate
        )
    }

    private func clearLivePresentation() {
        session.livePresentation = .empty
    }

    private func validMaterializedLivePrefix() -> MaterializedLivePrefix? {
        guard !session.policy.secureInput,
              session.phase == .composing,
              session.composingText.cursor == session.composingText.elements.count,
              let prefix = session.livePresentation.materializedPrefix,
              prefix.consumedElementCount > 0,
              prefix.consumedElementCount <= session.composingText.elements.count,
              Array(session.composingText.elements.prefix(prefix.consumedElementCount))
                  == prefix.sourceElements else {
            return nil
        }
        let reading = converter.display(for: CompositionInput(
            elements: prefix.sourceElements,
            cursor: prefix.sourceElements.count,
            leftContext: session.context.leftContext,
            mappedTableName: session.policy.inputTableName
        )).text
        guard reading == prefix.sourceReading else { return nil }
        return prefix
    }

    private func shouldDirectCommitVisibleSuffix() -> Bool {
        guard !session.policy.directCommitTargets.isEmpty,
              session.phase == .composing,
              session.composingText.cursor == session.composingText.elements.count else {
            return false
        }
        let visible = visibleComposition().text
        guard let scalar = visible.unicodeScalars.last else { return false }
        return session.policy.directCommitTargets.contains(
            renderedSuffix: String(scalar)
        )
    }

    @discardableResult
    private func resolvePendingLearning(commit: Bool) -> Bool {
        guard !session.pendingLearningTransactions.isEmpty else { return false }
        let transactions = session.pendingLearningTransactions
        session.pendingLearningTransactions.removeAll(keepingCapacity: true)
        for transaction in transactions {
            if commit {
                converter.commitStagedLearning(transaction.token)
            } else {
                converter.discardStagedLearning(transaction.token)
            }
        }
        if commit {
            converter.commitLearning()
        }
        return true
    }

    private func learn(
        _ candidates: [CandidateSnapshot],
        origin: LearningOrigin,
        reading: String
    ) {
        guard !candidates.isEmpty else { return }
        let converterCandidates = candidates.map { candidate in
            ConverterCandidate(
                text: candidate.text,
                annotation: candidate.annotation,
                consumingCount: candidate.consumingCount,
                sourceID: candidate.sourceID,
                provenance: candidate.provenance
            )
        }
        for candidate in converterCandidates where candidate.provenance != .builtInGuard {
            // `setCompletedData` only updates the converter's process-local
            // completion cache. Keep that compatibility behavior even when
            // learning is disabled or secure input prevents persistence.
            converter.setCompletedData(candidate)
        }
        guard session.policy.allowsLearning && !session.policy.secureInput else { return }
        if !stagedLearningEnabled {
            // Compatibility path for clients that predate the explicit
            // pending-learning contract. They cannot safely drive the undo
            // transaction, so preserve the pre-v2 immediate behavior.
            var updated = false
            for candidate in converterCandidates where candidate.provenance != .builtInGuard {
                converter.updateLearningData(candidate)
                updated = true
            }
            if updated {
                converter.commitLearning()
            }
            return
        }
        var immediateLearning = false
        for candidate in converterCandidates {
            if let token = converter.stageLearning(
                candidate: candidate,
                reading: reading
            ) {
                session.pendingLearningTransactions.append(
                    PendingLearningTransaction(
                        token: token,
                        reading: reading,
                        surface: candidate.text,
                        origin: origin,
                        createdRevision: session.revision
                    )
                )
            } else if candidate.provenance != .builtInGuard {
                // Ports predating staged learning retain the old immediate
                // behavior. The production adapter returns a token for every
                // learnable converter candidate and keeps this compatibility
                // path out of the new transaction semantics.
                converter.updateLearningData(candidate)
                immediateLearning = true
            }
        }
        if immediateLearning {
            converter.commitLearning()
        }
    }

    private func visibleComposition() -> VisibleComposition {
        if !session.segments.isEmpty,
           let activeIndex = session.activeSegmentIndex,
           session.segments.indices.contains(activeIndex) {
            let spans: [PreeditSpan] = session.segments.enumerated().compactMap { entry in
                let index = entry.offset
                let segment = entry.element
                guard let candidate = segment.selectedCandidate,
                      !candidate.text.isEmpty else { return nil }
                return PreeditSpan(
                    text: candidate.text,
                    style: index == activeIndex ? .active : .underline
                )
            }
            let text = spans.map(\.text).joined()
            let caretText = session.segments
                .prefix(activeIndex + 1)
                .compactMap(\.selectedCandidate)
                .map(\.text)
                .joined()
            return VisibleComposition(
                spans: spans,
                text: text,
                caretUtf8ByteOffset: UInt32(caretText.utf8.count),
                learnableCandidates: session.segments.compactMap(\.selectedCandidate)
            )
        }
        if let candidates = session.candidates,
           let selectedIndex = candidates.selectedIndex,
           candidates.items.indices.contains(selectedIndex) {
            let selected = candidates.items[selectedIndex]
            let boundary = session.activeBoundary ?? selected.consumingCount
            let suffix = suffixDisplay(after: boundary)
            let spans = [
                PreeditSpan(text: selected.text, style: .active),
                PreeditSpan(text: suffix.text, style: .underline),
            ].filter { !$0.text.isEmpty }
            return VisibleComposition(
                spans: spans,
                text: spans.map(\.text).joined(),
                caretUtf8ByteOffset: UInt32(selected.text.utf8.count),
                learnableCandidates: [selected]
            )
        }
        if let liveCandidate = session.candidates?.liveCandidate,
           session.phase == .composing {
            let boundary = min(
                max(liveCandidate.consumingCount, 1),
                session.composingText.elements.count
            )
            let suffix = suffixDisplay(after: boundary)
            let spans = [
                PreeditSpan(text: liveCandidate.text, style: .active),
                PreeditSpan(text: suffix.text, style: .underline),
            ].filter { !$0.text.isEmpty }
            return VisibleComposition(
                spans: spans,
                text: spans.map(\.text).joined(),
                caretUtf8ByteOffset: UInt32(liveCandidate.text.utf8.count),
                learnableCandidates: [liveCandidate]
            )
        }
        if let prefix = validMaterializedLivePrefix() {
            let suffix = suffixDisplay(after: prefix.consumedElementCount)
            let spans = [
                PreeditSpan(text: prefix.text, style: .active),
                PreeditSpan(text: suffix.text, style: .underline),
            ].filter { !$0.text.isEmpty }
            return VisibleComposition(
                spans: spans,
                text: spans.map(\.text).joined(),
                caretUtf8ByteOffset: UInt32(
                    prefix.text.utf8.count
                ) + suffix.caretUtf8ByteOffset,
                learnableCandidates: prefix.candidate.map { [$0] } ?? []
            )
        }
        guard !session.composingText.isEmpty else {
            return VisibleComposition(
                spans: [],
                text: "",
                caretUtf8ByteOffset: nil,
                learnableCandidates: []
            )
        }
        let display = currentDisplay()
        return VisibleComposition(
            spans: [PreeditSpan(text: display.text, style: .underline)]
                .filter { !$0.text.isEmpty },
            text: display.text,
            caretUtf8ByteOffset: display.caretUtf8ByteOffset,
            learnableCandidates: []
        )
    }

    private func clearSegmentedConversion() {
        session.segments = []
        session.activeSegmentIndex = nil
    }

    private func clearConversionState() {
        session.candidates = nil
        session.activeBoundary = nil
        clearSegmentedConversion()
    }

    private func endConverterComposition() {
        if session.policy.secureInput {
            purgeConverterSensitiveState()
        } else {
            converter.stopComposition()
        }
    }

    private func purgeConverterSensitiveState() {
        converter.purgeSensitiveState()
        clearRequestCache()
    }

    private func clearSecurityDomainState() {
        session.composingText = CompositionBuffer()
        session.context.leftContext = ""
        session.context.rightContext = ""
        clearConversionState()
        session.reconversionReplacement = nil
        session.unicodeInputBuffer = ""
        session.phaseBeforeUnicodeInput = nil
        clearLivePresentation()
        session.recoveryCheckpoint = nil
        session.phase = .idle
    }

    private func normalizeAfterEditing() {
        if session.candidates != nil {
            endConverterComposition()
        }
        if session.composingText.isEmpty {
            if session.policy.secureInput {
                purgeConverterSensitiveState()
            }
            session.phase = .idle
            clearConversionState()
            clearLivePresentation()
            session.reconversionReplacement = nil
        } else {
            session.phase = .composing
            clearConversionState()
            if validMaterializedLivePrefix() == nil {
                clearLivePresentation()
            }
        }
    }

    private func checkpointIsWellFormed(_ checkpoint: RecoveryCheckpoint) -> Bool {
        guard checkpoint.composition.elements.allSatisfy({ element in
            element.text.count == 1
                && (element.mappedIntention?.count ?? 0) <= 1
                && (element.mappedInputOverride?.count ?? 0) <= 1
        }) else { return false }
        guard checkpoint.policy.liveConversionDelayMilliseconds <= 1_000,
              checkpoint.policy.keymap.count <= 4_096,
              checkpoint.policy.keymap.allSatisfy({ key, rule in
                  key.count == 1
                      && rule.intention.count == 1
                      && (rule.inputOverride?.count ?? 0) <= 1
              }) else { return false }

        if let replacement = checkpoint.reconversionReplacement {
            guard replacement.before >= 0,
                  replacement.after >= 0,
                  replacement.before <= Int(Int32.max),
                  replacement.after <= Int(Int32.max),
                  replacement.before + replacement.after
                    == checkpoint.composition.text.unicodeScalars.count else {
                return false
            }
        }

        let unicodeBuffer = checkpoint.unicodeInputBuffer ?? ""
        let validUnicodeBuffer = unicodeBuffer.count <= 8
            && unicodeBuffer.unicodeScalars.allSatisfy { scalar in
                (0x30...0x39).contains(scalar.value)
                    || (0x61...0x66).contains(scalar.value)
            }
        guard validUnicodeBuffer else { return false }
        if checkpoint.phase == .unicodeInput {
            guard checkpoint.phaseBeforeUnicodeInput == .idle
                    || checkpoint.phaseBeforeUnicodeInput == .composing else {
                return false
            }
        } else if !unicodeBuffer.isEmpty || checkpoint.phaseBeforeUnicodeInput != nil {
            return false
        }
        return true
    }

    private func transformText(_ text: String, as transform: ImeTextTransform) -> String {
        switch transform {
        case .hiragana:
            let fullwidth = text.applyingTransform(
                .fullwidthToHalfwidth,
                reverse: true
            ) ?? text
            return String(String.UnicodeScalarView(fullwidth.unicodeScalars.map { scalar in
                if (0x30A1...0x30F6).contains(scalar.value),
                   let converted = UnicodeScalar(scalar.value - 0x60) {
                    return converted
                }
                return scalar
            }))
        case .katakanaFullwidth:
            let fullwidth = text.applyingTransform(
                .fullwidthToHalfwidth,
                reverse: true
            ) ?? text
            return String(String.UnicodeScalarView(fullwidth.unicodeScalars.map { scalar in
                if (0x3041...0x3096).contains(scalar.value),
                   let converted = UnicodeScalar(scalar.value + 0x60) {
                    return converted
                }
                return scalar
            }))
        case .katakanaHalfwidth:
            let katakana = transformText(text, as: .katakanaFullwidth)
            return katakana.applyingTransform(
                .fullwidthToHalfwidth,
                reverse: false
            ) ?? katakana
        case .alphabetFullwidth:
            return text.map { character in
                let scalar = character.unicodeScalars.first?.value ?? 0
                if scalar == 0x20 { return "　" }
                if (0x21...0x7E).contains(scalar),
                   let unicode = UnicodeScalar(scalar + 0xFEE0) {
                    return String(unicode)
                }
                return String(character)
            }.joined()
        case .alphabetHalfwidth:
            return text.map { character in
                let scalar = character.unicodeScalars.first?.value ?? 0
                if scalar == 0x3000 { return " " }
                if (0xFF01...0xFF5E).contains(scalar),
                   let unicode = UnicodeScalar(scalar - 0xFEE0) {
                    return String(unicode)
                }
                return String(character)
            }.joined()
        }
    }

    private func success(effects: [ClientEffect] = []) -> ImeReductionResult {
        ImeReductionResult(status: .success, message: nil, snapshot: snapshot(effects: effects))
    }

    private func failure(_ status: ImeReductionStatus, _ message: String) -> ImeReductionResult {
        ImeReductionResult(status: status, message: message, snapshot: snapshot())
    }

    private func snapshot(effects: [ClientEffect] = []) -> SessionSnapshot {
        let preedit: [PreeditSpan]
        let caret: UInt32?
        let aux: String?
        if session.phase == .unicodeInput {
            let display = currentDisplay()
            let marker = "u" + session.unicodeInputBuffer
            preedit = [
                PreeditSpan(text: display.text, style: .underline),
                PreeditSpan(text: marker, style: .active),
            ].filter { !$0.text.isEmpty }
            caret = UInt32(display.text.utf8.count + marker.utf8.count)
            aux = "Unicode U+" + session.unicodeInputBuffer.uppercased()
        } else {
            let visible = visibleComposition()
            preedit = visible.spans
            caret = visible.caretUtf8ByteOffset
            aux = auxiliaryText(for: visible)
        }
        let checkpoint = session.policy.secureInput ? nil : session.recoveryCheckpoint
        return SessionSnapshot(
            revision: session.revision,
            phase: session.phase,
            preedit: preedit,
            caretUtf8ByteOffset: caret,
            candidateWindow: session.candidates?.snapshot() ?? .empty,
            aux: aux,
            pendingLearning: !session.pendingLearningTransactions.isEmpty,
            recovery: checkpoint,
            effects: effects
        )
    }

    private func auxiliaryText(for visible: VisibleComposition) -> String? {
        guard !session.policy.secureInput,
              !session.composingText.isEmpty,
              session.phase == .composing || session.phase == .reconverting else {
            return nil
        }
        let reading = currentDisplay().text
        guard !reading.isEmpty else { return nil }
        switch session.policy.auxTextMode {
        case .disabled:
            return nil
        case .always:
            return reading
        case .whenCursorNotAtEnd:
            guard session.composingText.cursor < session.composingText.elements.count
                    || session.livePresentation.pendingRevision != nil
                    || visible.text != reading else {
                return nil
            }
            return reading
        }
    }

    private func currentDisplay() -> CompositionDisplay {
        return converter.display(for: CompositionInput(
            elements: session.composingText.elements,
            cursor: session.composingText.cursor,
            leftContext: session.context.leftContext,
            mappedTableName: session.policy.inputTableName
        ))
    }

    private func suffixDisplay(after count: Int) -> CompositionDisplay {
        let boundary = min(max(count, 0), session.composingText.elements.count)
        let elements = Array(session.composingText.elements.dropFirst(boundary))
        let cursor = min(
            max(session.composingText.cursor - boundary, 0),
            elements.count
        )
        return converter.display(for: CompositionInput(
            elements: elements,
            cursor: cursor,
            leftContext: session.context.leftContext,
            mappedTableName: session.policy.inputTableName
        ))
    }

    private func takeReconversionReplacementEffect() -> [ClientEffect] {
        guard let replacement = session.reconversionReplacement else { return [] }
        session.reconversionReplacement = nil
        return [
            .deleteSurroundingText(
                effectID: session.allocateEffectID(),
                before: replacement.before,
                after: replacement.after
            )
        ]
    }

    private func finishUnicodeInput(cancelled: Bool) {
        let previous = session.phaseBeforeUnicodeInput
        session.unicodeInputBuffer = ""
        session.phaseBeforeUnicodeInput = nil
        if cancelled {
            session.phase = session.composingText.isEmpty
                ? .idle
                : (previous == .composing ? .composing : .composing)
        } else {
            session.phase = session.composingText.isEmpty ? .idle : .composing
        }
    }
}
