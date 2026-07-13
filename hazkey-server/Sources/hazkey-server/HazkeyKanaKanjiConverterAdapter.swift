import Foundation
import KanaKanjiConverterModule

/// Bridges the production AzooKey converter to the protocol-v2 reducer.
///
/// The reducer intentionally deals in a small, stable candidate value instead
/// of leaking KanaKanjiConverter's internal Candidate type into the protocol
/// layer.  Completed candidates are retained briefly so the converter can
/// receive the original value when learning is committed.
final class HazkeyKanaKanjiConverterAdapter: KanaKanjiConverting {
    private let converter: KanaKanjiConverter
    private let optionsProvider: (ConversionOptions) -> ConvertRequestOptions
    private let mappedInputStyleProvider: () -> InputStyle
    private let predictionConfigurationProvider: () -> (enabled: Bool, limit: Int)
    private var completedCandidates: [String: Candidate] = [:]
    private var nextCandidateSourceID: UInt64 = 1

    init(
        converter: KanaKanjiConverter,
        optionsProvider: @escaping (ConversionOptions) -> ConvertRequestOptions,
        mappedInputStyleProvider: @escaping () -> InputStyle = { .roman2kana },
        predictionConfigurationProvider: @escaping () -> (enabled: Bool, limit: Int) = {
            (false, 0)
        }
    ) {
        self.converter = converter
        self.optionsProvider = optionsProvider
        self.mappedInputStyleProvider = mappedInputStyleProvider
        self.predictionConfigurationProvider = predictionConfigurationProvider
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
        let candidates = result.mainResults.map { candidate in
            let consumingCount = consumingInputCount(
                candidate.composingCount,
                in: composingText
            )
            let sourceID = allocateCandidateSourceID()
            let value = ConverterCandidate(
                text: candidate.text,
                consumingCount: min(max(consumingCount, 1), targetElements.count),
                sourceID: sourceID
            )
            completedCandidates[sourceID] = candidate
            return value
        }
        return ConversionOutput(
            candidates: candidates,
            pageSize: min(max(requestOptions.N_best, 1), candidates.count)
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
            let consumingCount = consumingInputCount(
                candidate.composingCount,
                in: composingText
            )
            let sourceID = allocateCandidateSourceID()
            let value = ConverterCandidate(
                text: candidate.text,
                annotation: "予測",
                consumingCount: min(
                    max(consumingCount, 1),
                    composition.elements.count
                ),
                sourceID: sourceID
            )
            completedCandidates[sourceID] = candidate
            return value
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
        let surfaceCursor = min(
            max(indexMap[inputCursor] ?? composingText.convertTarget.count, 0),
            composingText.convertTarget.count
        )
        let caretText = composingText.convertTarget.prefix(surfaceCursor)
        return CompositionDisplay(
            text: composingText.convertTarget,
            caretUtf8ByteOffset: UInt32(caretText.utf8.count)
        )
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
