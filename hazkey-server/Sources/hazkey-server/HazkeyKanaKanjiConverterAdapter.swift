import Foundation
import KanaKanjiConverterModule

/// Immutable lookup tables built when a Grimodex project dictionary snapshot
/// is applied. Candidate requests must not scan the entire project dictionary:
/// production snapshots may contain tens of thousands of entries and ranking
/// runs on every key press.
struct GrimodexProjectDictionaryIndex: Sendable {
    struct IndexedEntry: Sendable {
        let entry: GrimodexMappedDictionaryEntry
        let order: Int
    }

    private struct SurfaceKey: Hashable, Sendable {
        let ruby: String
        let word: String
    }

    private struct NodeKey: Hashable, Sendable {
        let ruby: String
        let word: String
        let cid: Int
    }

    static let empty = GrimodexProjectDictionaryIndex(entries: [])

    private let entries: [GrimodexMappedDictionaryEntry]
    private let entryOrdersByRuby: [String: [Int]]
    private let entryOrdersBySurface: [SurfaceKey: [Int]]
    private let entryOrdersByNode: [NodeKey: [Int]]
    let entryCount: Int

    init(entries: [GrimodexMappedDictionaryEntry]) {
        var entryOrdersByRuby: [String: [Int]] = [:]
        var entryOrdersBySurface: [SurfaceKey: [Int]] = [:]
        var entryOrdersByNode: [NodeKey: [Int]] = [:]
        for (order, entry) in entries.enumerated() {
            entryOrdersByRuby[entry.ruby, default: []].append(order)
            entryOrdersBySurface[
                SurfaceKey(ruby: entry.ruby, word: entry.word),
                default: []
            ].append(order)
            entryOrdersByNode[
                NodeKey(ruby: entry.ruby, word: entry.word, cid: entry.cid),
                default: []
            ].append(order)
        }
        self.entries = entries
        self.entryOrdersByRuby = entryOrdersByRuby
        self.entryOrdersBySurface = entryOrdersBySurface
        self.entryOrdersByNode = entryOrdersByNode
        entryCount = entries.count
    }

    var isEmpty: Bool { entryCount == 0 }

    func entries(forRuby ruby: String) -> [IndexedEntry] {
        indexedEntries(entryOrdersByRuby[ruby])
    }

    func entries(ruby: String, word: String) -> [IndexedEntry] {
        indexedEntries(entryOrdersBySurface[SurfaceKey(ruby: ruby, word: word)])
    }

    func entries(matching data: DicdataElement) -> [IndexedEntry] {
        guard data.lcid == data.rcid else { return [] }
        return indexedEntries(entryOrdersByNode[
            NodeKey(ruby: data.ruby, word: data.word, cid: data.lcid)
        ])
    }

    private func indexedEntries(_ orders: [Int]?) -> [IndexedEntry] {
        orders?.map { order in
            IndexedEntry(entry: entries[order], order: order)
        } ?? []
    }
}

/// Keeps the active Grimodex project dictionary authoritative after Zenzai
/// reranks (and can omit) AzooKey's user-dictionary candidates.
///
/// The sidecar entries are intentionally matched by their full dictionary
/// identity. AzooKey rewrites every dynamic entry's metadata to
/// `isFromUserDictionary`, so metadata alone cannot distinguish project terms
/// from the user's personal dictionary.
enum GrimodexProjectCandidateRanker {
    private struct RankedCandidate {
        let candidate: Candidate
        let priority: Int
        let exactInputMatch: Bool
        let stableOrder: Int
    }

    static func rank(
        _ candidates: [Candidate],
        for composingText: ComposingText,
        elementCount: Int,
        projectEntries: [GrimodexMappedDictionaryEntry]
    ) -> [Candidate] {
        rank(
            candidates,
            for: composingText,
            elementCount: elementCount,
            projectIndex: GrimodexProjectDictionaryIndex(entries: projectEntries)
        )
    }

    static func rank(
        _ candidates: [Candidate],
        for composingText: ComposingText,
        elementCount: Int,
        projectIndex: GrimodexProjectDictionaryIndex
    ) -> [Candidate] {
        guard !projectIndex.isEmpty else { return candidates }

        let inputRuby = composingText.convertTarget.toKatakana()
        var ranked: [RankedCandidate] = []
        var rankedIndices = Set<Int>()
        var representedExactEntryOrders = Set<Int>()

        for (index, candidate) in candidates.enumerated() {
            var matchingEntries: [Int: GrimodexProjectDictionaryIndex.IndexedEntry] = [:]
            for entry in projectIndex.entries(
                ruby: inputRuby,
                word: candidate.text
            ) {
                matchingEntries[entry.order] = entry
            }
            for data in candidate.data {
                for entry in projectIndex.entries(matching: data) {
                    matchingEntries[entry.order] = entry
                }
            }
            guard let priority = matchingEntries.values
                .map({ $0.entry.priority }).max() else {
                continue
            }
            let exactMatches = matchingEntries.values.filter { indexed in
                guard indexed.entry.priority == priority,
                      indexed.entry.ruby == inputRuby else {
                    return false
                }
                if indexed.entry.word == candidate.text {
                    return true
                }
                return candidate.data.count == 1
                    && candidate.data.contains { data in
                        data.word == indexed.entry.word
                            && data.ruby == indexed.entry.ruby
                            && data.lcid == indexed.entry.cid
                            && data.rcid == indexed.entry.cid
                    }
            }
            let exactInputMatch = !exactMatches.isEmpty
            representedExactEntryOrders.formUnion(exactMatches.map(\.order))

            var promotedCandidate = candidate
            if let canonical = exactMatches.min(by: { $0.order < $1.order }) {
                // A generic dictionary node can have the same surface and
                // reading as a project entry. Keep the displayed candidate in
                // Zenzai's position, but learn the authoritative project node.
                var data = canonical.entry.dictionaryElement
                data.metadata = .isFromUserDictionary
                promotedCandidate.value = data.value()
                promotedCandidate.lastMid = data.mid
                promotedCandidate.data = [data]
            }
            ranked.append(RankedCandidate(
                candidate: promotedCandidate,
                priority: priority,
                exactInputMatch: exactInputMatch,
                // Existing candidates retain Zenzai's contextual order when
                // project priorities tie.
                stableOrder: index
            ))
            rankedIndices.insert(index)
        }

        // Zenzai can remove a rejected user-dictionary candidate altogether.
        // Recreate exact project entries so their priority remains a hard
        // contract rather than a hint to the language model.
        for indexed in projectIndex.entries(forRuby: inputRuby)
        where !representedExactEntryOrders.contains(indexed.order)
        {
            let entry = indexed.entry
            var data = entry.dictionaryElement
            data.metadata = .isFromUserDictionary
            var candidate = Candidate(
                text: entry.word,
                value: data.value(),
                composingCount: .inputCount(elementCount),
                lastMid: data.mid,
                data: [data]
            )
            // Converter results normally pass through processResult(), which
            // expands date/random templates and disables learning for them.
            candidate.parseTemplate()
            ranked.append(RankedCandidate(
                candidate: candidate,
                priority: entry.priority,
                exactInputMatch: true,
                // Existing same-priority candidates stay ahead of candidates
                // that had to be restored after Zenzai omitted them.
                stableOrder: candidates.count + indexed.order
            ))
        }

        ranked.sort { left, right in
            if left.priority != right.priority {
                return left.priority > right.priority
            }
            if left.exactInputMatch != right.exactInputMatch {
                return left.exactInputMatch
            }
            return left.stableOrder < right.stableOrder
        }

        var result: [Candidate] = []
        var promotedTexts = Set<String>()
        for item in ranked where promotedTexts.insert(item.candidate.text).inserted {
            result.append(item.candidate)
        }
        for (index, candidate) in candidates.enumerated()
        where !rankedIndices.contains(index)
            && !promotedTexts.contains(candidate.text)
        {
            result.append(candidate)
        }
        return result
    }
}

/// Bridges the production AzooKey converter to the protocol-v2 reducer.
///
/// The reducer intentionally deals in a small, stable candidate value instead
/// of leaking KanaKanjiConverter's internal Candidate type into the protocol
/// layer.  Completed candidates are retained briefly so the converter can
/// receive the original value when learning is committed.
final class HazkeyKanaKanjiConverterAdapter: KanaKanjiConverting {
    let supportsSegmentEditing = true

    private let converter: KanaKanjiConverter
    private let boundaryConverter: KanaKanjiConverter
    private let optionsProvider: (ConversionOptions) -> ConvertRequestOptions
    private let surfaceMapper: HazkeyCompositionSurfaceMapper
    private let projectDictionaryIndexProvider: () -> GrimodexProjectDictionaryIndex
    private let zenzaiDiagnosticsReporter: (ConversionOptions, String) -> Void
    private var completedCandidates: [String: Candidate] = [:]
    private var completedCandidateOrder: [String] = []
    private var stagedLearningCandidates: [ConverterLearningToken: Candidate] = [:]
    private var nextCandidateSourceID: UInt64 = 1
    private var nextLearningTokenID: UInt64 = 1

    init(
        converter: KanaKanjiConverter,
        boundaryConverter: KanaKanjiConverter,
        optionsProvider: @escaping (ConversionOptions) -> ConvertRequestOptions,
        mappedInputStyleProvider: @escaping () -> InputStyle = { .roman2kana },
        predictionConfigurationProvider _: @escaping () -> (enabled: Bool, limit: Int) = {
            (false, 0)
        },
        suggestionListModeProvider _: @escaping () -> ImeSuggestionListMode = { .predictive },
        projectDictionaryIndexProvider: @escaping () -> GrimodexProjectDictionaryIndex = {
            .empty
        },
        zenzaiDiagnosticsReporter: @escaping (ConversionOptions, String) -> Void = {
            _, _ in
        }
    ) {
        precondition(
            converter !== boundaryConverter,
            "primary and boundary converters must be distinct instances"
        )
        self.converter = converter
        self.boundaryConverter = boundaryConverter
        self.optionsProvider = optionsProvider
        self.surfaceMapper = HazkeyCompositionSurfaceMapper(
            mappedInputStyleProvider: mappedInputStyleProvider
        )
        self.projectDictionaryIndexProvider = projectDictionaryIndexProvider
        self.zenzaiDiagnosticsReporter = zenzaiDiagnosticsReporter
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

        let result = requestPrimaryCandidates(
            composingText,
            requestOptions: requestOptions,
            conversionOptions: options
        )
        let rankedCandidates = GrimodexProjectCandidateRanker.rank(
            result.mainResults,
            for: composingText,
            elementCount: targetElements.count,
            projectIndex: projectDictionaryIndexProvider()
        )
        let candidates = rankedCandidates.map {
            makeConverterCandidate(
                $0,
                in: composingText,
                elementCount: targetElements.count
            )
        }
        let protectedCandidates = candidates.filter {
            ProtectedSurfacePolicy.allows($0, for: composingText.convertTarget)
        }
        let guardedCandidates = GrimodexBuiltInGuardDictionary.candidates(
            for: composingText.convertTarget,
            consumingCount: targetElements.count
        )
        let finalCandidates = mergeGuardCandidates(
            guardedCandidates,
            with: protectedCandidates
        )
        return ConversionOutput(
            candidates: finalCandidates,
            pageSize: min(max(requestOptions.N_best, 1), finalCandidates.count)
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
        var boundaryOptions = optionsProvider(options)
        boundaryOptions.N_best = 9
        boundaryOptions.needTypoCorrection = false
        boundaryOptions.requireJapanesePrediction = .disabled
        boundaryOptions.requireEnglishPrediction = .disabled
        boundaryOptions.englishCandidateInRoman2KanaInput = false
        boundaryOptions.fullWidthRomanCandidate = false
        boundaryOptions.halfWidthKanaCandidate = false
        boundaryOptions.learningType = .nothing
        boundaryOptions.shouldResetMemory = false
        boundaryOptions.specialCandidateProviders = []
        boundaryOptions.zenzaiMode = .off

        // AzooKey learns a completed multi-word sentence as an additional
        // single dictionary node. Such a node intentionally keeps no internal
        // clause metadata, so it must never define the automatic segment
        // layout. A separate converter shares the base dictionary cache but
        // owns an independent lattice and always discovers boundaries without
        // history or Zenzai. The primary converter still ranks the candidates
        // for the chosen boundary with the user's normal options below.
        boundaryConverter.stopComposition()
        let result = boundaryConverter.requestCandidates(
            composingText,
            options: boundaryOptions
        )

        // firstClauseResults mixes several clause lengths and is ordered with
        // longer readings first. Derive the natural boundary from the best
        // dictionary-only whole-sentence path, falling back to the longest
        // proper clause when that path exposes no POS boundary.
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
        let best = result.mainResults.first { candidate in
            // Personal/project dictionary entries can be marked as non-learning
            // targets by AzooKey even though they are authoritative one-node
            // surfaces. They still need to define the boundary; otherwise a
            // long user term is split at the first generic clause and the
            // exact dictionary candidate never reaches the visible window.
            let isAuthoritativeDictionaryNode = candidate.data.count == 1
                && candidate.data.contains {
                    $0.metadata.contains(.isFromUserDictionary)
                }
            return (candidate.isLearningTarget || isAuthoritativeDictionaryNode)
                && !candidate.data.isEmpty
                && consumedInputCount(candidate) == inputCount
        }
        if let best {
            let derived = Candidate.makePrefixClauseCandidate(data: best.data)
            let derivedCount = consumedInputCount(derived)
            if derivedCount < inputCount {
                preferredClause = result.firstClauseResults.first { candidate in
                    candidate.text == derived.text
                        && consumedInputCount(candidate) == derivedCount
                } ?? derived
            } else if best.data.count == 1 {
                // An exact dictionary entry is a word/term boundary in its own
                // right. In particular, do not split a long user-dictionary
                // term merely because shorter built-in clauses also exist.
                preferredClause = derived
            } else {
                // Some best paths contain no POS boundary and therefore make
                // makePrefixClauseCandidate return the whole sentence. In
                // that case choose the longest proper clause from the same
                // reading lattice. This keeps particles with the preceding
                // phrase without collapsing every segment.
                preferredClause = result.firstClauseResults
                    .filter { candidate in
                        consumedInputCount(candidate) < inputCount
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
        var requestOptions = optionsProvider(options)
        requestOptions.N_best = max(1, requestOptions.N_best)
        requestOptions.requireJapanesePrediction = .disabled
        requestOptions.requireEnglishPrediction = .disabled
        if !options.zenzaiEnabled {
            requestOptions.zenzaiMode = .off
        }
        let primaryResult = requestPrimaryCandidates(
            composingText,
            requestOptions: requestOptions,
            conversionOptions: options
        )
        let projectIndex = projectDictionaryIndexProvider()
        let rankedPrimaryResults = GrimodexProjectCandidateRanker.rank(
            primaryResult.mainResults,
            for: composingText,
            elementCount: inputCount,
            projectIndex: projectIndex
        )
        let unrankedPrimaryClauses = rankedPrimaryResults.compactMap { candidate in
            guard !candidate.data.isEmpty,
                  consumedInputCount(candidate) == inputCount else {
                return nil
            }
            let clause = segmentCount == inputCount
                ? candidate
                : Candidate.makePrefixClauseCandidate(data: candidate.data)
            return consumedInputCount(clause) == segmentCount ? clause : nil
        } + primaryResult.firstClauseResults.filter { candidate in
            consumedInputCount(candidate) == segmentCount
        }

        let prefixComposingText = makeComposingText(
            from: composition.elements.prefix(segmentCount)[...],
            mappedTableName: composition.mappedTableName
        )
        let primaryClauses = GrimodexProjectCandidateRanker.rank(
            unrankedPrimaryClauses,
            for: prefixComposingText,
            elementCount: segmentCount,
            projectIndex: projectIndex
        )

        var seenTexts = Set<String>()
        var candidates: [ConverterCandidate] = primaryClauses.compactMap { candidate in
            guard !candidate.text.isEmpty,
                  seenTexts.insert(candidate.text).inserted else {
                return nil
            }
            let converted = makeConverterCandidate(
                candidate,
                in: prefixComposingText,
                elementCount: segmentCount
            )
            return ProtectedSurfacePolicy.allows(
                converted,
                for: prefixComposingText.convertTarget
            ) ? converted : nil
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
        for candidate in targeted.candidates {
            guard candidate.consumingCount == segmentCount,
                  !candidate.text.isEmpty,
                  seenTexts.insert(candidate.text).inserted else {
                continue
            }
            candidates.append(candidate)
        }

        let guardedCandidates = GrimodexBuiltInGuardDictionary.candidates(
            for: prefixComposingText.convertTarget,
            consumingCount: segmentCount
        )
        candidates = mergeGuardCandidates(guardedCandidates, with: candidates)

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
        // The reducer passes the composition-start policy through the port.
        // Keeping this value out of the live provider closure prevents a
        // settings reload from changing an in-flight composition.
        let mode = options.suggestionListMode
        var requestOptions = optionsProvider(options)
        let limit = options.suggestionListLimit
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

        let result = requestPrimaryCandidates(
            composingText,
            requestOptions: requestOptions,
            conversionOptions: options
        )
        let rankedMainResults = GrimodexProjectCandidateRanker.rank(
            result.mainResults,
            for: composingText,
            elementCount: targetElements.count,
            projectIndex: projectDictionaryIndexProvider()
        )
        let mainCandidates = rankedMainResults.map {
            makeConverterCandidate(
                $0,
                in: composingText,
                elementCount: targetElements.count
            )
        }.filter {
            ProtectedSurfacePolicy.allows($0, for: composingText.convertTarget)
        }
        let guardedCandidates = GrimodexBuiltInGuardDictionary.candidates(
            for: composingText.convertTarget,
            consumingCount: targetElements.count
        )
        let orderedMainCandidates = mergeGuardCandidates(
            guardedCandidates,
            with: mainCandidates
        )
        let liveCandidate = orderedMainCandidates.first {
            $0.consumingCount == targetElements.count
        }

        let candidates: [ConverterCandidate]
        let pageSize: Int
        switch mode {
        case .disabled:
            candidates = []
            pageSize = 0
        case .normal:
            candidates = Array(orderedMainCandidates.prefix(limit))
            pageSize = min(limit, candidates.count)
        case .predictive:
            let predictionCandidates = result.predictionResults.prefix(limit).map {
                makePredictionCandidate(
                    $0,
                    in: composingText,
                    elementCount: targetElements.count
                )
            }.filter {
                ProtectedSurfacePolicy.allows($0, for: composingText.convertTarget)
            }
            candidates = Array(
                mergeGuardCandidates(guardedCandidates, with: predictionCandidates)
                    .prefix(limit)
            )
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
        guard options.suggestionListMode == .predictive,
              !composition.elements.isEmpty else {
            return ConversionOutput(candidates: [], pageSize: 0)
        }
        let limit = options.suggestionListLimit
        let composingText = makeComposingText(
            from: composition.elements[...],
            mappedTableName: composition.mappedTableName
        )
        var requestOptions = optionsProvider(options)
        requestOptions.N_best = limit
        requestOptions.requireJapanesePrediction = .manualMix
        requestOptions.requireEnglishPrediction = .disabled
        if !options.zenzaiEnabled {
            requestOptions.zenzaiMode = .off
        }
        let result = requestPrimaryCandidates(
            composingText,
            requestOptions: requestOptions,
            conversionOptions: options
        )
        let predictionCandidates = result.predictionResults.prefix(limit).map { candidate in
            makePredictionCandidate(
                candidate,
                in: composingText,
                elementCount: composition.elements.count
            )
        }.filter {
            ProtectedSurfacePolicy.allows($0, for: composingText.convertTarget)
        }
        let guardedCandidates = GrimodexBuiltInGuardDictionary.candidates(
            for: composingText.convertTarget,
            consumingCount: composition.elements.count
        )
        let candidates = Array(
            mergeGuardCandidates(guardedCandidates, with: predictionCandidates)
                .prefix(limit)
        )
        return ConversionOutput(
            candidates: candidates,
            pageSize: min(limit, candidates.count)
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

    func stageLearning(
        candidate: ConverterCandidate,
        reading: String
    ) -> ConverterLearningToken? {
        guard let sourceID = candidate.sourceID,
              let original = completedCandidates[sourceID] else {
            return nil
        }
        let tokenID = nextLearningTokenID
        nextLearningTokenID = nextLearningTokenID == UInt64.max
            ? 1
            : nextLearningTokenID + 1
        let token = ConverterLearningToken(
            rawValue: "learning-\(tokenID)-\(sourceID)-\(reading.count)"
        )
        stagedLearningCandidates[token] = original
        return token
    }

    func commitStagedLearning(_ token: ConverterLearningToken) {
        guard let original = stagedLearningCandidates.removeValue(forKey: token) else {
            return
        }
        converter.setCompletedData(original)
        converter.updateLearningData(original)
    }

    func discardStagedLearning(_ token: ConverterLearningToken) {
        stagedLearningCandidates.removeValue(forKey: token)
    }

    func forget(_ candidate: ConverterCandidate) {
        guard let sourceID = candidate.sourceID,
              let original = completedCandidates[sourceID] else { return }
        converter.forgetMemory(original)
    }

    func stopComposition() {
        converter.stopComposition()
        if boundaryConverter !== converter {
            boundaryConverter.stopComposition()
        }
        // Keep a bounded process-local tail so a live-converted prefix can be
        // committed after the next edit. The map never crosses the protocol
        // boundary and staged tokens still own their original Candidate.
        while completedCandidateOrder.count > 512 {
            let evicted = completedCandidateOrder.removeFirst()
            completedCandidates.removeValue(forKey: evicted)
        }
    }

    func purgeSensitiveState() {
        converter.stopComposition()
        if boundaryConverter !== converter {
            boundaryConverter.stopComposition()
        }
        completedCandidates.removeAll(keepingCapacity: false)
        completedCandidateOrder.removeAll(keepingCapacity: false)
        stagedLearningCandidates.removeAll(keepingCapacity: false)
    }

    private func allocateCandidateSourceID() -> String {
        let result = String(nextCandidateSourceID)
        nextCandidateSourceID = nextCandidateSourceID == UInt64.max
            ? 1
            : nextCandidateSourceID + 1
        return result
    }

    private func requestPrimaryCandidates(
        _ composingText: ComposingText,
        requestOptions: ConvertRequestOptions,
        conversionOptions: ConversionOptions
    ) -> ConversionResult {
        let result = converter.requestCandidates(
            composingText,
            options: requestOptions
        )
        zenzaiDiagnosticsReporter(conversionOptions, converter.zenzStatus)
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
            sourceID: sourceID,
            provenance: candidateProvenance(candidate)
        )
        completedCandidates[sourceID] = candidate
        completedCandidateOrder.append(sourceID)
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
            sourceID: value.sourceID,
            provenance: value.provenance
        )
    }

    func candidateProvenance(_ candidate: Candidate) -> CandidateProvenance {
        guard !candidate.data.isEmpty else { return .standard }
        let projectIndex = projectDictionaryIndexProvider()
        var containsProjectNode = false
        var containsPersonalNode = false
        for data in candidate.data {
            if !projectIndex.entries(matching: data).isEmpty {
                containsProjectNode = true
            } else if data.metadata.contains(.isFromUserDictionary) {
                containsPersonalNode = true
            } else {
                // Provenance is candidate-wide. A single generic node means
                // the dictionary did not authorize the entire rendered
                // surface, so the protected-surface policy must still run.
                return .standard
            }
        }
        if containsPersonalNode { return .personalDictionary }
        if containsProjectNode { return .projectDictionary }
        return .standard
    }

    private func uniqueCandidates(
        _ candidates: [ConverterCandidate]
    ) -> [ConverterCandidate] {
        var seen = Set<String>()
        return candidates.filter { seen.insert($0.text).inserted }
    }

    private func mergeGuardCandidates(
        _ guards: [ConverterCandidate],
        with candidates: [ConverterCandidate]
    ) -> [ConverterCandidate] {
        let trusted = candidates.filter { candidate in
            switch candidate.provenance {
            case .projectDictionary, .personalDictionary, .temporaryDictionary:
                return true
            case .standard, .zenzai, .builtInGuard, .unknown:
                return false
            }
        }
        let untrusted = candidates.filter { candidate in
            !trusted.contains(candidate)
        }
        return uniqueCandidates(trusted + guards + untrusted)
    }

    private func makeComposingText(
        from elements: ArraySlice<CompositionElement>,
        mappedTableName: String?
    ) -> ComposingText {
        surfaceMapper.composingText(
            from: elements,
            mappedTableName: mappedTableName
        )
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
