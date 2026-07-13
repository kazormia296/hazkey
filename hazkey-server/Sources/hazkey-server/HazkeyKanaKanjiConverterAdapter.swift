import Foundation
import KanaKanjiConverterModule

/// Bridges the production AzooKey converter to the protocol-v2 reducer.
///
/// The reducer intentionally deals in a small, stable candidate value instead
/// of leaking KanaKanjiConverter's internal Candidate type into the protocol
/// layer.  Completed candidates are retained briefly so the converter can
/// receive the original value when learning is committed.
final class HazkeyKanaKanjiConverterAdapter: KanaKanjiConverting {
    let supportsSegmentEditing = true

    private let converter: KanaKanjiConverter
    private let optionsProvider: (ConversionOptions) -> ConvertRequestOptions
    private let mappedInputStyleProvider: () -> InputStyle
    private let predictionConfigurationProvider: () -> (enabled: Bool, limit: Int)
    private let suggestionListModeProvider: () -> ImeSuggestionListMode
    private var completedCandidates: [String: Candidate] = [:]
    private var nextCandidateSourceID: UInt64 = 1

    init(
        converter: KanaKanjiConverter,
        optionsProvider: @escaping (ConversionOptions) -> ConvertRequestOptions,
        mappedInputStyleProvider: @escaping () -> InputStyle = { .roman2kana },
        predictionConfigurationProvider: @escaping () -> (enabled: Bool, limit: Int) = {
            (false, 0)
        },
        suggestionListModeProvider: @escaping () -> ImeSuggestionListMode = { .predictive }
    ) {
        self.converter = converter
        self.optionsProvider = optionsProvider
        self.mappedInputStyleProvider = mappedInputStyleProvider
        self.predictionConfigurationProvider = predictionConfigurationProvider
        self.suggestionListModeProvider = suggestionListModeProvider
    }

    func candidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        let targetCount = composition.targetCount.map {
            min(max($0, 0), composition.elements.count)
        } ?? composition.elements.count
        let targetElements = composition.elements.prefix(targetCount)
        guard !targetElements.isEmpty else {
            return ConversionOutput(candidates: [], pageSize: 0)
        }

        let composingText = makeComposingText(
            from: targetElements,
            mappedTableName: composition.mappedTableName
        )

        var requestOptions = optionsProvider(options)
        requestOptions.N_best = max(1, requestOptions.N_best)
        requestOptions.requireJapanesePrediction = .disabled
        requestOptions.requireEnglishPrediction = .disabled
        if !options.zenzaiEnabled {
            requestOptions.zenzaiMode = .off
        }

        let result = converter.requestCandidates(composingText, options: requestOptions)
        let candidates = result.mainResults.map {
            makeConverterCandidate(
                $0,
                in: composingText,
                elementCount: targetElements.count
            )
        }
        return ConversionOutput(
            candidates: candidates,
            pageSize: min(max(requestOptions.N_best, 1), candidates.count)
        )
    }

    func segmentCandidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        guard !composition.elements.isEmpty else {
            return ConversionOutput(candidates: [], pageSize: 0)
        }
        let composingText = makeComposingText(
            from: composition.elements[...],
            mappedTableName: composition.mappedTableName
        )
        var requestOptions = optionsProvider(options)
        requestOptions.N_best = max(1, requestOptions.N_best)
        requestOptions.requireJapanesePrediction = .disabled
        requestOptions.requireEnglishPrediction = .disabled
        if !options.zenzaiEnabled {
            requestOptions.zenzaiMode = .off
        }

        let result = converter.requestCandidates(composingText, options: requestOptions)
        // firstClauseResults mixes several clause lengths and is ordered with
        // longer readings first. Filtering that list after the fact can leave
        // the initially active segment with only one candidate. Derive the
        // natural boundary from the best whole-sentence path, then collect all
        // alternatives for exactly that boundary.
        let inputCount = composition.elements.count
        func consumedInputCount(_ candidate: Candidate) -> Int {
            min(
                max(
                    consumingInputCount(
                        candidate.composingCount,
                        in: composingText
                    ),
                    1
                ),
                inputCount
            )
        }

        let fallbackClauses = result.firstClauseResults.isEmpty
            ? result.mainResults
            : result.firstClauseResults
        let preferredClause: Candidate?
        if let best = result.mainResults.first, !best.data.isEmpty {
            let derived = Candidate.makePrefixClauseCandidate(data: best.data)
            let derivedCount = consumedInputCount(derived)
            if derivedCount < inputCount {
                preferredClause = result.firstClauseResults.first { candidate in
                    candidate.text == derived.text
                        && consumedInputCount(candidate) == derivedCount
                } ?? derived
            } else {
                // Some best paths contain no POS boundary and therefore make
                // makePrefixClauseCandidate return the whole sentence. In
                // that case choose the longest proper clause whose surface is
                // still a prefix of the best sentence. This keeps particles
                // with the preceding phrase without collapsing every segment.
                let bestSurface = best.text
                preferredClause = result.firstClauseResults
                    .filter { candidate in
                        consumedInputCount(candidate) < inputCount
                            && bestSurface.hasPrefix(candidate.text)
                    }
                    .max { lhs, rhs in
                        let lhsCount = consumedInputCount(lhs)
                        let rhsCount = consumedInputCount(rhs)
                        return lhsCount == rhsCount
                            ? lhs.value < rhs.value
                            : lhsCount < rhsCount
                    } ?? derived
            }
        } else {
            preferredClause = fallbackClauses
                .filter { !$0.text.isEmpty }
                .max { lhs, rhs in
                    let lhsCount = consumedInputCount(lhs)
                    let rhsCount = consumedInputCount(rhs)
                    return lhsCount == rhsCount
                        ? lhs.value < rhs.value
                        : lhsCount < rhsCount
                }
        }
        guard let preferredClause else {
            return ConversionOutput(candidates: [], pageSize: 0)
        }

        let segmentCount = consumedInputCount(preferredClause)
        var seenTexts = Set<String>()
        let sameBoundary = ([preferredClause] + result.firstClauseResults)
            .filter { candidate in
                consumedInputCount(candidate) == segmentCount
                    && !candidate.text.isEmpty
                    && seenTexts.insert(candidate.text).inserted
            }
        var candidates = sameBoundary.map {
            makeConverterCandidate(
                $0,
                in: composingText,
                elementCount: composition.elements.count
            )
        }

        let targetedInput = CompositionInput(
            elements: composition.elements,
            cursor: composition.cursor,
            leftContext: composition.leftContext,
            targetCount: segmentCount,
            mappedTableName: composition.mappedTableName
        )
        let targeted = try self.candidates(
            for: targetedInput,
            options: options
        )
        for candidate in targeted.candidates
            where candidate.consumingCount == segmentCount
                && seenTexts.insert(candidate.text).inserted {
            candidates.append(candidate)
        }

        if candidates.count < 2 {
            let segmentElements = Array(composition.elements.prefix(segmentCount))
            let reading = display(for: CompositionInput(
                elements: segmentElements,
                cursor: segmentElements.count,
                leftContext: composition.leftContext,
                mappedTableName: composition.mappedTableName
            )).text
            if !reading.isEmpty, seenTexts.insert(reading).inserted {
                candidates.append(ConverterCandidate(
                    text: reading,
                    annotation: "読み",
                    consumingCount: segmentCount
                ))
            }
        }
        return ConversionOutput(
            candidates: candidates,
            pageSize: min(max(requestOptions.N_best, 1), candidates.count)
        )
    }

    func realtimeCandidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> RealtimeConversionOutput {
        let targetCount = composition.targetCount.map {
            min(max($0, 0), composition.elements.count)
        } ?? composition.elements.count
        let targetElements = composition.elements.prefix(targetCount)
        guard !targetElements.isEmpty else {
            return RealtimeConversionOutput(
                liveCandidate: nil,
                candidates: [],
                pageSize: 0
            )
        }

        let composingText = makeComposingText(
            from: targetElements,
            mappedTableName: composition.mappedTableName
        )
        let mode = suggestionListModeProvider()
        let configuration = predictionConfigurationProvider()
        var requestOptions = optionsProvider(options)
        let limit = max(configuration.limit, 1)
        requestOptions.N_best = switch mode {
        case .disabled:
            1
        case .normal, .predictive:
            limit
        }
        requestOptions.requireJapanesePrediction = mode == .predictive
            ? .manualMix
            : .disabled
        requestOptions.requireEnglishPrediction = .disabled
        if !options.zenzaiEnabled {
            requestOptions.zenzaiMode = .off
        }

        let result = converter.requestCandidates(composingText, options: requestOptions)
        let mainCandidates = result.mainResults.map {
            makeConverterCandidate(
                $0,
                in: composingText,
                elementCount: targetElements.count
            )
        }
        let liveCandidate = mainCandidates.first {
            $0.consumingCount == targetElements.count
        }

        let candidates: [ConverterCandidate]
        let pageSize: Int
        switch mode {
        case .disabled:
            candidates = []
            pageSize = 0
        case .normal:
            candidates = Array(mainCandidates.prefix(limit))
            pageSize = min(limit, candidates.count)
        case .predictive:
            candidates = result.predictionResults.prefix(limit).map {
                makePredictionCandidate(
                    $0,
                    in: composingText,
                    elementCount: targetElements.count
                )
            }
            pageSize = min(limit, candidates.count)
        }
        return RealtimeConversionOutput(
            liveCandidate: liveCandidate,
            candidates: candidates,
            pageSize: pageSize
        )
    }

    func predictions(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        let configuration = predictionConfigurationProvider()
        guard configuration.enabled, configuration.limit > 0,
              !composition.elements.isEmpty else {
            return ConversionOutput(candidates: [], pageSize: 0)
        }
        let composingText = makeComposingText(
            from: composition.elements[...],
            mappedTableName: composition.mappedTableName
        )
        var requestOptions = optionsProvider(options)
        requestOptions.N_best = max(1, configuration.limit)
        requestOptions.requireJapanesePrediction = .manualMix
        requestOptions.requireEnglishPrediction = .disabled
        if !options.zenzaiEnabled {
            requestOptions.zenzaiMode = .off
        }
        let result = converter.requestCandidates(composingText, options: requestOptions)
        let candidates = result.predictionResults.prefix(configuration.limit).map { candidate in
            makePredictionCandidate(
                candidate,
                in: composingText,
                elementCount: composition.elements.count
            )
        }
        return ConversionOutput(
            candidates: candidates,
            pageSize: min(configuration.limit, candidates.count)
        )
    }

    func display(for composition: CompositionInput) -> CompositionDisplay {
        let composingText = makeComposingText(
            from: composition.elements[...],
            mappedTableName: composition.mappedTableName
        )
        let inputCursor = min(max(composition.cursor, 0), composingText.input.count)
        let indexMap = composingText.inputIndexToSurfaceIndexMap()
        let mappedCursor = indexMap[inputCursor]
            ?? indexMap
                .filter { $0.key < inputCursor }
                .max(by: { $0.key < $1.key })?
                .value
            ?? 0
        let surfaceCursor = min(
            max(mappedCursor, 0),
            composingText.convertTarget.count
        )
        let caretText = composingText.convertTarget.prefix(surfaceCursor)
        return CompositionDisplay(
            text: composingText.convertTarget,
            caretUtf8ByteOffset: UInt32(caretText.utf8.count)
        )
    }

    func inputCursorPosition(
        for composition: CompositionInput,
        movingBy offset: Int
    ) -> Int {
        let composingText = makeComposingText(
            from: composition.elements[...],
            mappedTableName: composition.mappedTableName
        )
        let cursor = min(max(composition.cursor, 0), composingText.input.count)
        guard offset != 0 else { return cursor }

        var boundarySet = Set(
            composingText.inputIndexToSurfaceIndexMap().keys.filter {
                (0...composingText.input.count).contains($0)
            }
        )
        boundarySet.insert(0)
        boundarySet.insert(composingText.input.count)
        let boundaries = boundarySet.sorted()

        if offset > 0 {
            let following = boundaries.filter { $0 > cursor }
            guard !following.isEmpty else { return cursor }
            return following[min(offset - 1, following.count - 1)]
        }

        let preceding = boundaries.filter { $0 < cursor }
        guard !preceding.isEmpty else { return cursor }
        let additionalSteps = min(-(offset + 1), preceding.count - 1)
        return preceding[preceding.count - 1 - additionalSteps]
    }

    func setCompletedData(_ candidate: ConverterCandidate) {
        guard let sourceID = candidate.sourceID,
              let original = completedCandidates[sourceID] else { return }
        converter.setCompletedData(original)
    }

    func updateLearningData(_ candidate: ConverterCandidate) {
        guard let sourceID = candidate.sourceID,
              let original = completedCandidates[sourceID] else { return }
        converter.updateLearningData(original)
    }

    func commitLearning() {
        converter.commitUpdateLearningData()
    }

    func forget(_ candidate: ConverterCandidate) {
        guard let sourceID = candidate.sourceID,
              let original = completedCandidates[sourceID] else { return }
        converter.forgetMemory(original)
    }

    func stopComposition() {
        converter.stopComposition()
        completedCandidates.removeAll(keepingCapacity: true)
    }

    private func allocateCandidateSourceID() -> String {
        let result = String(nextCandidateSourceID)
        nextCandidateSourceID = nextCandidateSourceID == UInt64.max
            ? 1
            : nextCandidateSourceID + 1
        return result
    }

    private func makeConverterCandidate(
        _ candidate: Candidate,
        in composingText: ComposingText,
        elementCount: Int
    ) -> ConverterCandidate {
        let consumingCount = consumingInputCount(
            candidate.composingCount,
            in: composingText
        )
        let sourceID = allocateCandidateSourceID()
        let value = ConverterCandidate(
            text: candidate.text,
            consumingCount: min(max(consumingCount, 1), elementCount),
            sourceID: sourceID
        )
        completedCandidates[sourceID] = candidate
        return value
    }

    private func makePredictionCandidate(
        _ candidate: Candidate,
        in composingText: ComposingText,
        elementCount: Int
    ) -> ConverterCandidate {
        let value = makeConverterCandidate(
            candidate,
            in: composingText,
            elementCount: elementCount
        )
        return ConverterCandidate(
            text: value.text,
            annotation: "予測",
            consumingCount: value.consumingCount,
            sourceID: value.sourceID
        )
    }

    private func makeComposingText(
        from elements: ArraySlice<CompositionElement>,
        mappedTableName: String?
    ) -> ComposingText {
        var composingText = ComposingText()
        for element in elements {
            let inputStyle: InputStyle
            switch element.inputStyle {
            case .direct:
                inputStyle = .direct
            case .mapped:
                if let mappedTableName {
                    inputStyle = .mapped(id: .tableName(mappedTableName))
                } else {
                    inputStyle = mappedInputStyleProvider()
                }
            }
            if element.inputStyle == .mapped,
               let intention = element.mappedIntention?.first,
               let input = (element.mappedInputOverride ?? element.text).first {
                composingText.insertAtCursorPosition([
                    ComposingText.InputElement(
                        piece: .key(
                            intention: intention,
                            input: input,
                            modifiers: []
                        ),
                        inputStyle: inputStyle
                    )
                ])
            } else {
                composingText.insertAtCursorPosition(element.text, inputStyle: inputStyle)
            }
        }
        return composingText
    }

    private func consumingInputCount(
        _ count: ComposingCount,
        in composingText: ComposingText
    ) -> Int {
        var remaining = composingText
        let originalCount = remaining.input.count
        remaining.prefixComplete(composingCount: count)
        return max(1, originalCount - remaining.input.count)
    }
}
