import Foundation

final class ImeReducer {
    private struct CachedRequest {
        let action: ImeAction
        let expectedRevision: UInt64?
        let result: ImeReductionResult
    }

    private(set) var session: CompositionSession
    private let converter: any KanaKanjiConverting
    private var requestCache: [String: CachedRequest] = [:]
    private var requestOrder: [String] = []
    private let requestCacheLimit = 128

    init(
        session: CompositionSession = CompositionSession(),
        converter: any KanaKanjiConverting = NoopKanaKanjiConverter()
    ) {
        self.session = session
        self.converter = converter
    }

    func currentSnapshot() -> SessionSnapshot { snapshot() }

    func pinCompositionPolicy(_ policy: PinnedCompositionPolicy) {
        guard session.phase == .idle, session.composingText.isEmpty else { return }
        session.policy = policy
        session.context.projectRevision = policy.projectRevision
    }

    func invalidateCandidatesForExternalDictionaryChange() {
        guard session.candidates != nil else { return }
        converter.stopComposition()
        session.candidates = nil
        session.activeBoundary = nil
        session.phase = session.composingText.isEmpty ? .idle : .composing
        session.advanceRevision()
    }

    func reduce(
        _ action: ImeAction,
        requestID: String,
        expectedRevision: UInt64? = nil
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
            if session.phase == .idle {
                session.reconversionReplacement = nil
            }
            if session.candidates != nil { converter.stopComposition() }
            session.phase = .composing
            session.composingText.insert(text, keymap: session.policy.keymap)
            session.candidates = nil
            session.activeBoundary = nil
            refreshInteractiveCandidates()
            session.advanceRevision()
            result = success()

        case .deleteBackward:
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
            refreshInteractiveCandidates()
            session.advanceRevision()
            result = success()

        case .deleteForward:
            guard session.phase != .unicodeInput else {
                result = failure(.invalidAction, "forward delete is not valid during Unicode input")
                break
            }
            session.composingText.deleteForward()
            normalizeAfterEditing()
            refreshInteractiveCandidates()
            session.advanceRevision()
            result = success()

        case .moveCursor(let offset):
            guard session.phase == .composing || session.phase == .previewing else {
                result = failure(.invalidAction, "cursor movement requires a composing session")
                break
            }
            session.composingText.moveCursor(by: offset)
            converter.stopComposition()
            session.phase = .composing
            session.candidates = nil
            session.activeBoundary = nil
            refreshInteractiveCandidates()
            session.advanceRevision()
            result = success()

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
            converter.stopComposition()
            session.phase = .composing
            session.candidates = nil
            session.activeBoundary = nil
            refreshInteractiveCandidates()
            session.advanceRevision()
            result = success()

        case .startConversion:
            if session.candidates?.origin == .prediction {
                converter.stopComposition()
                session.candidates = nil
                session.activeBoundary = nil
            }
            result = convert()

        case .navigateCandidate(let delta):
            if session.phase == .composing,
               session.candidates?.items.isEmpty ?? true {
                converter.stopComposition()
                session.candidates = nil
                session.activeBoundary = nil
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
            session.activeBoundary = candidates.items[candidates.selectedIndex ?? 0].consumingCount
            session.phase = .selecting
            session.advanceRevision()
            result = success()

        case .navigateCandidatePage(let delta):
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
            session.activeBoundary = candidates.items[candidates.selectedIndex ?? 0].consumingCount
            session.phase = .selecting
            session.advanceRevision()
            result = success()

        case .resizeSegment(let delta):
            result = resizeSegment(delta)

        case .selectCandidate(let id, let generation):
            guard var candidates = session.candidates,
                  candidates.generation == generation,
                  let index = candidates.items.firstIndex(where: { $0.id == id }) else {
                result = failure(.staleCandidate, "candidate generation or id is stale")
                break
            }
            candidates.selectedIndex = index
            session.candidates = candidates
            session.activeBoundary = candidates.items[index].consumingCount
            session.phase = .selecting
            result = commitSelectedCandidate()

        case .commitSelected:
            result = commitSelectedCandidate()

        case .commitAll:
            result = commitAll()

        case .cancel:
            result = cancel()

        case .transformActiveSegment(let transform):
            result = transformActiveSegment(transform)

        case .forgetCandidate(let id, let generation):
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
            guard !session.policy.secureInput else {
                result = failure(
                    .secureInputViolation,
                    "surrounding-text reconversion is disabled for secure input"
                )
                break
            }
            guard !text.isEmpty, deleteBefore >= 0, deleteAfter >= 0 else {
                result = failure(.invalidAction, "reconversion text must not be empty")
                break
            }
            let selectedCount = text.unicodeScalars.count
            guard deleteBefore + deleteAfter == 0
                    || deleteBefore + deleteAfter == selectedCount else {
                result = failure(
                    .invalidAction,
                    "reconversion replacement range does not match selected text"
                )
                break
            }
            converter.stopComposition()
            session.composingText = CompositionBuffer()
            session.composingText.insert(text, inputStyle: .direct)
            session.context.leftContext = leftContext
            session.context.rightContext = rightContext
            session.reconversionReplacement = if deleteBefore + deleteAfter > 0 {
                ReconversionReplacement(before: deleteBefore, after: deleteAfter)
            } else {
                nil
            }
            session.phase = .reconverting
            session.candidates = nil
            session.activeBoundary = nil
            result = convert()

        case .beginUnicodeInput:
            guard session.phase == .idle || session.phase == .composing else {
                result = failure(
                    .invalidAction,
                    "Unicode input can begin only while idle or composing"
                )
                break
            }
            converter.stopComposition()
            session.phaseBeforeUnicodeInput = session.phase
            session.unicodeInputBuffer = ""
            session.candidates = nil
            session.activeBoundary = nil
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
        while requestOrder.count > requestCacheLimit {
            requestCache.removeValue(forKey: requestOrder.removeFirst())
        }
        return result
    }

    private func refreshInteractiveCandidates() {
        guard session.policy.autoConvertMode != .disabled else {
            refreshPredictions()
            return
        }
        refreshRealtimeCandidates()
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
                    leftContext: session.context.leftContext,
                    rightContext: session.context.rightContext
                )
            )
            let liveCandidate: CandidateSnapshot? = shouldPublishLiveCandidate(
                for: display
            )
                ? output.liveCandidate.map { makeSnapshot($0) }
                : nil
            guard !output.candidates.isEmpty || liveCandidate != nil else {
                session.candidates = nil
                session.activeBoundary = nil
                return
            }
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
                        sourceID: $0.sourceID
                    )
                }
            )
            session.activeBoundary = nil
        } catch {
            // Live conversion is opportunistic. Keep the reading editable when
            // the converter cannot produce a realtime result.
            session.candidates = nil
            session.activeBoundary = nil
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
            sourceID: candidate.sourceID
        )
    }

    private func refreshPredictions() {
        guard session.phase == .composing, !session.composingText.isEmpty else {
            return
        }
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
                    leftContext: session.context.leftContext,
                    rightContext: session.context.rightContext
                )
            )
            guard !output.candidates.isEmpty else {
                session.candidates = nil
                session.activeBoundary = nil
                return
            }
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
                        sourceID: candidate.sourceID
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
            session.candidates = nil
            session.activeBoundary = nil
        }
    }

    private func convert(advanceRevision: Bool = true) -> ImeReductionResult {
        let isReconversion = session.phase == .reconverting
        guard !session.composingText.isEmpty else {
            session.phase = .idle
            session.candidates = nil
            if advanceRevision { session.advanceRevision() }
            return success()
        }
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
                options: ConversionOptions(
                    allowLearning: session.policy.allowsLearning && !session.policy.secureInput,
                    zenzaiEnabled: session.policy.zenzaiEnabled && !session.policy.secureInput,
                    leftContext: session.context.leftContext,
                    rightContext: session.context.rightContext
                )
            )
            guard !output.candidates.isEmpty else {
                converter.stopComposition()
                session.phase = isReconversion ? .reconverting : .composing
                session.candidates = nil
                session.activeBoundary = nil
                if advanceRevision { session.advanceRevision() }
                return success()
            }
            let generation = session.allocateCandidateGeneration()
            session.activeBoundary = min(
                max(output.candidates[0].consumingCount, 1),
                session.composingText.elements.count
            )
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
                        sourceID: candidate.sourceID
                    )
                },
                selectedIndex: 0,
                pageSize: max(1, min(output.pageSize, output.candidates.count)),
                origin: .conversion,
                liveCandidate: nil
            )
            session.phase = isReconversion ? .reconverting : .previewing
            if advanceRevision { session.advanceRevision() }
            return success()
        } catch {
            converter.stopComposition()
            session.phase = isReconversion ? .reconverting : .composing
            session.candidates = nil
            session.activeBoundary = nil
            if advanceRevision { session.advanceRevision() }
            return failure(.converterUnavailable, "converter failed: \(error)")
        }
    }

    private func resizeSegment(_ delta: Int) -> ImeReductionResult {
        guard !session.composingText.isEmpty,
              session.phase == .composing || session.phase == .previewing
                || session.phase == .selecting || session.phase == .reconverting else {
            return failure(.invalidAction, "segment resizing requires an active segment")
        }
        let count = session.composingText.elements.count
        let newBoundary: Int
        if let boundary = session.activeBoundary {
            newBoundary = min(max(boundary + delta, 1), count)
            guard newBoundary != boundary else { return success() }
        } else if delta > 0 {
            // Match the azooKey editing convention: the first expansion from
            // raw composing starts with the smallest legal leading segment.
            newBoundary = 1
        } else {
            newBoundary = min(max(count + delta, 1), count)
        }
        converter.stopComposition()
        session.activeBoundary = newBoundary
        session.candidates = nil
        session.phase = session.reconversionReplacement == nil ? .composing : .reconverting
        let converted = convert(advanceRevision: false)
        guard converted.status == .success else { return converted }
        if session.candidates != nil {
            session.phase = .selecting
        }
        session.advanceRevision()
        return success()
    }

    private func commitSelectedCandidate() -> ImeReductionResult {
        guard let candidates = session.candidates,
              let selected = candidates.selectedIndex,
              candidates.items.indices.contains(selected) else {
            return failure(.invalidAction, "no candidate is selected")
        }
        let candidate = candidates.items[selected]
        let count = min(candidate.consumingCount, session.composingText.elements.count)
        guard count > 0 else { return failure(.invalidAction, "candidate consumes no input") }
        let _ = session.composingText.removePrefix(count: count)
        var effects = takeReconversionReplacementEffect()
        effects.append(.commitText(effectID: session.allocateEffectID(), text: candidate.text))
        if !session.policy.secureInput {
            session.context.leftContext.append(candidate.text)
        }
        learn(candidate)
        session.candidates = nil
        session.activeBoundary = nil
        session.phase = session.composingText.isEmpty ? .idle : .composing
        session.composingText.moveCursorToEnd()
        session.advanceRevision()
        if !session.composingText.isEmpty {
            _ = convert(advanceRevision: false)
        } else {
            converter.stopComposition()
        }
        return success(effects: effects)
    }

    private func commitAll() -> ImeReductionResult {
        guard !session.composingText.isEmpty else { return success() }
        var text = currentDisplay().text
        var selectedCandidate: CandidateSnapshot?
        if let candidates = session.candidates,
           let index = candidates.selectedIndex,
           candidates.items.indices.contains(index) {
            selectedCandidate = candidates.items[index]
            let count = min(selectedCandidate?.consumingCount ?? 0, session.composingText.elements.count)
            text = (selectedCandidate?.text ?? "") + suffixDisplay(after: count).text
        } else if let liveCandidate = session.candidates?.liveCandidate {
            selectedCandidate = liveCandidate
            let count = min(liveCandidate.consumingCount, session.composingText.elements.count)
            text = liveCandidate.text + suffixDisplay(after: count).text
        }
        var effects = takeReconversionReplacementEffect()
        effects.append(.commitText(effectID: session.allocateEffectID(), text: text))
        if !session.policy.secureInput {
            session.context.leftContext.append(text)
        }
        if let selectedCandidate { learn(selectedCandidate) }
        session.composingText = CompositionBuffer()
        session.candidates = nil
        session.activeBoundary = nil
        session.phase = .idle
        session.advanceRevision()
        converter.stopComposition()
        return success(effects: effects)
    }

    private func cancel() -> ImeReductionResult {
        switch session.phase {
        case .selecting:
            if session.candidates?.origin == .prediction {
                converter.stopComposition()
                session.candidates = nil
                session.activeBoundary = nil
                session.phase = .composing
            } else if session.reconversionReplacement != nil {
                session.phase = .reconverting
            } else {
                session.phase = .previewing
            }
        case .previewing:
            converter.stopComposition()
            session.phase = .composing
            session.candidates = nil
            session.activeBoundary = nil
        case .unicodeInput:
            finishUnicodeInput(cancelled: true)
        case .composing, .reconverting:
            converter.stopComposition()
            session.composingText = CompositionBuffer()
            session.candidates = nil
            session.activeBoundary = nil
            session.reconversionReplacement = nil
            session.phase = .idle
        case .idle:
            return success()
        }
        session.advanceRevision()
        return success()
    }

    private func transformActiveSegment(_ transform: ImeTextTransform) -> ImeReductionResult {
        let source: String
        if let candidates = session.candidates,
           let index = candidates.selectedIndex,
           candidates.items.indices.contains(index) {
            source = candidates.items[index].text
        } else {
            source = currentDisplay().text
        }
        let transformed = transformText(source, as: transform)
        guard !transformed.isEmpty else { return success() }
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
            session.phase = .selecting
        } else {
            converter.stopComposition()
            session.candidates = nil
            session.activeBoundary = nil
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
            converter.stopComposition()
            session.policy.secureInput = secure
            // Crossing either direction is a security-domain transition. Do
            // not carry text or context from the previous field into the new
            // domain, even if a non-Fcitx client sends lifecycle actions
            // without reopening the session.
            session.composingText = CompositionBuffer()
            session.context.leftContext = ""
            session.context.rightContext = ""
            session.candidates = nil
            session.activeBoundary = nil
            session.reconversionReplacement = nil
            session.unicodeInputBuffer = ""
            session.phaseBeforeUnicodeInput = nil
            session.phase = session.composingText.isEmpty ? .idle : .composing
        case .deactivate, .focusChanged:
            converter.stopComposition()
            session.candidates = nil
            session.activeBoundary = nil
            session.phase = session.composingText.isEmpty ? .idle : .composing
        case .capabilityChanged:
            break
        case .serverRestarted:
            converter.stopComposition()
            session.candidates = nil
            session.activeBoundary = nil
            session.phase = session.composingText.isEmpty ? .idle : .composing
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

        converter.stopComposition()
        session.composingText = checkpoint.composition
        session.candidates = nil
        session.activeBoundary = nil
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

    private func learn(_ candidate: CandidateSnapshot) {
        let converterCandidate = ConverterCandidate(
            text: candidate.text,
            annotation: candidate.annotation,
            consumingCount: candidate.consumingCount,
            sourceID: candidate.sourceID
        )
        converter.setCompletedData(converterCandidate)
        guard session.policy.allowsLearning && !session.policy.secureInput else { return }
        converter.updateLearningData(converterCandidate)
        converter.commitLearning()
    }

    private func normalizeAfterEditing() {
        if session.candidates != nil {
            converter.stopComposition()
        }
        if session.composingText.isEmpty {
            session.phase = .idle
            session.candidates = nil
            session.activeBoundary = nil
            session.reconversionReplacement = nil
        } else {
            session.phase = .composing
            session.candidates = nil
            session.activeBoundary = nil
        }
    }

    private func checkpointIsWellFormed(_ checkpoint: RecoveryCheckpoint) -> Bool {
        guard checkpoint.composition.elements.allSatisfy({ element in
            element.text.count == 1
                && (element.mappedIntention?.count ?? 0) <= 1
                && (element.mappedInputOverride?.count ?? 0) <= 1
        }) else { return false }
        guard checkpoint.policy.keymap.count <= 4_096,
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
        } else if let candidates = session.candidates,
           let selectedIndex = candidates.selectedIndex,
           candidates.items.indices.contains(selectedIndex) {
            let boundary = session.activeBoundary ?? candidates.items[selectedIndex].consumingCount
            preedit = [
                PreeditSpan(text: candidates.items[selectedIndex].text, style: .active),
                PreeditSpan(text: suffixDisplay(after: boundary).text, style: .underline),
            ].filter { !$0.text.isEmpty }
            caret = UInt32(candidates.items[selectedIndex].text.utf8.count)
            aux = nil
        } else if let liveCandidate = session.candidates?.liveCandidate,
                  session.phase == .composing {
            let boundary = min(
                max(liveCandidate.consumingCount, 1),
                session.composingText.elements.count
            )
            preedit = [
                PreeditSpan(text: liveCandidate.text, style: .active),
                PreeditSpan(text: suffixDisplay(after: boundary).text, style: .underline),
            ].filter { !$0.text.isEmpty }
            caret = UInt32(liveCandidate.text.utf8.count)
            aux = nil
        } else if session.composingText.isEmpty {
            preedit = []
            caret = nil
            aux = nil
        } else {
            let display = currentDisplay()
            preedit = [PreeditSpan(text: display.text, style: .underline)]
            caret = display.caretUtf8ByteOffset
            aux = nil
        }
        let checkpoint = session.policy.secureInput ? nil : session.recoveryCheckpoint
        return SessionSnapshot(
            revision: session.revision,
            phase: session.phase,
            preedit: preedit,
            caretUtf8ByteOffset: caret,
            candidateWindow: session.candidates?.snapshot() ?? .empty,
            aux: aux,
            recovery: checkpoint,
            effects: effects
        )
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
