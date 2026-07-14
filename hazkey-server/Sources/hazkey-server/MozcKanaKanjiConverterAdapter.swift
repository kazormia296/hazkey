import Foundation
import KanaKanjiConverterModule

enum MozcConverterAdapterError: Error, Equatable {
    case unstableInputBoundary
    case invalidCoreResponse
}

/// Immutable session-local view of dictionaries that remain authoritative
/// even though the Mozc core itself owns no Grimodex or personal dictionary
/// state. Only readings and candidates are merged here; dictionary contents
/// never cross the private helper boundary.
struct MozcDictionaryCandidateOverlay: Sendable {
    static let empty = MozcDictionaryCandidateOverlay(
        projectIndex: .empty,
        userIndex: .empty
    )

    let projectIndex: GrimodexProjectDictionaryIndex
    let userIndex: UserDictionaryCandidateIndex

    var isEmpty: Bool {
        projectIndex.isEmpty && userIndex.entryCount == 0
    }

    func hasEntries(for reading: String) -> Bool {
        let ruby = canonicalRuby(reading)
        return !projectIndex.entries(forRuby: ruby).isEmpty
            || !userIndex.entries(forRuby: ruby).isEmpty
    }

    func candidates(
        for reading: String,
        consumingCount: Int
    ) -> [ConverterCandidate] {
        let ruby = canonicalRuby(reading)
        let project = projectIndex.entries(forRuby: ruby).sorted { left, right in
            if left.entry.priority != right.entry.priority {
                return left.entry.priority > right.entry.priority
            }
            return left.order < right.order
        }.map { indexed in
            ConverterCandidate(
                text: indexed.entry.word,
                consumingCount: consumingCount,
                provenance: .projectDictionary
            )
        }
        let personal = userIndex.entries(forRuby: ruby).map { entry in
            ConverterCandidate(
                text: entry.surface.precomposedStringWithCompatibilityMapping,
                consumingCount: consumingCount,
                provenance: provenance(for: entry.layer)
            )
        }
        var seen = Set<String>()
        return (project + personal).filter { candidate in
            seen.insert(
                candidate.text.precomposedStringWithCanonicalMapping
            ).inserted
        }
    }

    private func canonicalRuby(_ reading: String) -> String {
        reading.precomposedStringWithCompatibilityMapping.toKatakana()
    }

    private func provenance(
        for layer: UserDictionaryLayer
    ) -> CandidateProvenance {
        switch layer {
        case .system: .standard
        case .personal: .personalDictionary
        case .project: .projectDictionary
        case .temporary: .temporaryDictionary
        }
    }
}

/// Experimental B0 adapter. The reducer continues to own the composition,
/// segmentation, stale-candidate and commit contracts; Mozc only supplies
/// candidates for a rendered reading. Learning and prediction are explicitly
/// outside this first slice.
final class MozcKanaKanjiConverterAdapter: KanaKanjiConverting {
    let supportsSegmentEditing = true

    private let core: any MozcCoreConverting
    private let surfaceMapper: HazkeyCompositionSurfaceMapper
    private let projectDictionaryIndexProvider: () -> GrimodexProjectDictionaryIndex
    private let userDictionaryIndexProvider: () -> UserDictionaryCandidateIndex

    init(
        core: any MozcCoreConverting,
        mappedInputStyleProvider: @escaping () -> InputStyle = { .roman2kana },
        projectDictionaryIndexProvider: @escaping () -> GrimodexProjectDictionaryIndex = {
            .empty
        },
        userDictionaryIndexProvider: @escaping () -> UserDictionaryCandidateIndex = {
            .empty
        }
    ) {
        self.core = core
        self.surfaceMapper = HazkeyCompositionSurfaceMapper(
            mappedInputStyleProvider: mappedInputStyleProvider
        )
        self.projectDictionaryIndexProvider = projectDictionaryIndexProvider
        self.userDictionaryIndexProvider = userDictionaryIndexProvider
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
        let overlay = dictionaryOverlay()
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
            options: options,
            overlay: overlay
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
        let overlay = dictionaryOverlay()
        let targetKeySize = preferredOverlayTargetKeySize(
            for: composition,
            reading: reading,
            overlay: overlay
        )
        return try conversionOutput(
            for: composition,
            reading: reading,
            targetKeySize: targetKeySize,
            options: options,
            overlay: overlay
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
        options: ConversionOptions,
        overlay: MozcDictionaryCandidateOverlay
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
        let dictionaryCandidates = overlay.candidates(
            for: segmentReading,
            consumingCount: segmentInputCount
        )
        let guards = GrimodexBuiltInGuardDictionary.candidates(
            for: segmentReading,
            consumingCount: segmentInputCount
        )
        var seen = Set<String>()
        let candidates = Array(
            (dictionaryCandidates + guards + mapped).filter { candidate in
                seen.insert(
                    candidate.text.precomposedStringWithCanonicalMapping
                ).inserted
            }.prefix(limit)
        )
        return ConversionOutput(
            candidates: candidates,
            pageSize: min(limit, candidates.count)
        )
    }

    private func dictionaryOverlay() -> MozcDictionaryCandidateOverlay {
        MozcDictionaryCandidateOverlay(
            projectIndex: projectDictionaryIndexProvider(),
            userIndex: userDictionaryIndexProvider()
        )
    }

    /// A dictionary term must be able to own a longer segment than Mozc's
    /// natural first clause, otherwise an exact project/personal entry can
    /// never enter the candidate window. Iterate only stable input boundaries
    /// and select the longest matching rendered prefix. Explicit resize calls
    /// bypass this helper and remain authoritative.
    private func preferredOverlayTargetKeySize(
        for composition: CompositionInput,
        reading: String,
        overlay: MozcDictionaryCandidateOverlay
    ) -> Int? {
        guard !composition.elements.isEmpty, !overlay.isEmpty else { return nil }
        var longest: Int?
        for boundary in surfaceMapper.stableKeySizeBoundaries(in: composition) {
            let keySize = boundary.keySize
            guard keySize > 0 else { continue }
            let prefix = String(reading.unicodeScalars.prefix(keySize))
            if overlay.hasEntries(for: prefix) {
                longest = max(longest ?? 0, keySize)
            }
        }
        return longest
    }
}
