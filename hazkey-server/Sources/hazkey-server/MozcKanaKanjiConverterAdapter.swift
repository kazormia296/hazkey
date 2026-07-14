import Foundation
import KanaKanjiConverterModule

enum MozcConverterAdapterError: Error, Equatable {
    case unstableInputBoundary
    case invalidCoreResponse
}

/// Experimental B0 adapter. The reducer continues to own the composition,
/// segmentation, stale-candidate and commit contracts; Mozc only supplies
/// candidates for a rendered reading. Learning and prediction are explicitly
/// outside this first slice.
final class MozcKanaKanjiConverterAdapter: KanaKanjiConverting {
    let supportsSegmentEditing = true

    private let core: any MozcCoreConverting
    private let surfaceMapper: HazkeyCompositionSurfaceMapper

    init(
        core: any MozcCoreConverting,
        mappedInputStyleProvider: @escaping () -> InputStyle = { .roman2kana }
    ) {
        self.core = core
        self.surfaceMapper = HazkeyCompositionSurfaceMapper(
            mappedInputStyleProvider: mappedInputStyleProvider
        )
    }

    func display(for composition: CompositionInput) -> CompositionDisplay {
        surfaceMapper.display(for: composition)
    }

    func inputCursorPosition(
        for composition: CompositionInput,
        movingBy offset: Int
    ) -> Int {
        surfaceMapper.inputCursorPosition(
            for: composition,
            movingBy: offset
        )
    }

    func candidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        guard !options.secureInput else {
            return ConversionOutput(candidates: [], pageSize: 0)
        }
        guard !composition.elements.isEmpty else {
            return ConversionOutput(candidates: [], pageSize: 0)
        }
        let display = surfaceMapper.display(for: composition)
        guard !display.text.isEmpty else {
            return ConversionOutput(candidates: [], pageSize: 0)
        }
        let requestedInputCount = min(
            max(composition.targetCount ?? composition.elements.count, 1),
            composition.elements.count
        )
        guard let targetKeySize = surfaceMapper.keySize(
            forInputCount: requestedInputCount,
            in: composition
        ), targetKeySize > 0 else {
            throw MozcConverterAdapterError.unstableInputBoundary
        }
        return try conversionOutput(
            for: composition,
            reading: display.text,
            targetKeySize: targetKeySize,
            options: options
        )
    }

    func segmentCandidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        guard !options.secureInput else {
            return ConversionOutput(candidates: [], pageSize: 0)
        }
        guard !composition.elements.isEmpty else {
            return ConversionOutput(candidates: [], pageSize: 0)
        }
        let reading = surfaceMapper.display(for: composition).text
        guard !reading.isEmpty else {
            return ConversionOutput(candidates: [], pageSize: 0)
        }
        return try conversionOutput(
            for: composition,
            reading: reading,
            targetKeySize: nil,
            options: options
        )
    }

    func realtimeCandidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> RealtimeConversionOutput {
        let output = try candidates(for: composition, options: options)
        let exposesSuggestionList = options.suggestionListMode != .disabled
        return RealtimeConversionOutput(
            liveCandidate: output.candidates.first,
            candidates: exposesSuggestionList ? output.candidates : [],
            pageSize: exposesSuggestionList ? output.pageSize : 0
        )
    }

    /// B0 intentionally has no Mozc prediction/history semantics. Returning an
    /// empty list also avoids sending a reading merely because the normal
    /// Hazkey suggestion mode is predictive.
    func predictions(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        ConversionOutput(candidates: [], pageSize: 0)
    }

    func setCompletedData(_ candidate: ConverterCandidate) {}
    func updateLearningData(_ candidate: ConverterCandidate) {}
    func commitLearning() {}
    func stageLearning(
        candidate: ConverterCandidate,
        reading: String
    ) -> ConverterLearningToken? { nil }
    func commitStagedLearning(_ token: ConverterLearningToken) {}
    func discardStagedLearning(_ token: ConverterLearningToken) {}
    func forget(_ candidate: ConverterCandidate) {}

    /// Converter requests are stateless in B0. Frequent edit/resize boundaries
    /// therefore do not pay a process restart cost.
    func stopComposition() {}

    /// Secure transitions terminate the helper without serializing a purge
    /// request, so no reading or context can cross the process boundary.
    func purgeSensitiveState() {
        core.purgeSensitiveState()
    }

    private func conversionOutput(
        for composition: CompositionInput,
        reading: String,
        targetKeySize: Int?,
        options: ConversionOptions
    ) throws -> ConversionOutput {
        let limit = ConversionOptions.clampSuggestionListLimit(
            options.suggestionListLimit
        )
        let result = try core.convert(
            reading: reading,
            targetKeySize: targetKeySize,
            maxCandidates: limit
        )
        guard result.segmentKeySize > 0,
              let segmentInputCount = surfaceMapper.inputCount(
                forKeySize: result.segmentKeySize,
                in: composition
              ) else {
            throw MozcConverterAdapterError.invalidCoreResponse
        }
        if let targetKeySize, targetKeySize != result.segmentKeySize {
            throw MozcConverterAdapterError.invalidCoreResponse
        }

        let segmentReading = String(
            reading.unicodeScalars.prefix(result.segmentKeySize)
        )
        let mapped = try result.candidates.map { item in
            guard item.consumedKeySize == result.segmentKeySize,
                  !item.value.isEmpty else {
                throw MozcConverterAdapterError.invalidCoreResponse
            }
            return ConverterCandidate(
                text: item.value,
                annotation: item.description,
                consumingCount: segmentInputCount,
                provenance: .standard
            )
        }.filter {
            ProtectedSurfacePolicy.allows($0, for: segmentReading)
        }
        let guards = GrimodexBuiltInGuardDictionary.candidates(
            for: segmentReading,
            consumingCount: segmentInputCount
        )
        var seen = Set<String>()
        let candidates = (guards + mapped).filter {
            seen.insert($0.text).inserted
        }
        return ConversionOutput(
            candidates: candidates,
            pageSize: min(limit, candidates.count)
        )
    }
}
