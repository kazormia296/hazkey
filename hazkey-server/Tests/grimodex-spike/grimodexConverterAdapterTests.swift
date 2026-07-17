import Foundation
import KanaKanjiConverterModule
import KanaKanjiConverterModuleWithDefaultDictionary
import XCTest

@testable import hazkey_server

private func makeDefaultConverterPair() -> (
  primary: KanaKanjiConverter,
  boundary: KanaKanjiConverter
) {
  let store = DicdataStore.withDefaultDictionary()
  return (
    KanaKanjiConverter(dicdataStore: store),
    KanaKanjiConverter(dicdataStore: store)
  )
}

final class GrimodexConverterAdapterTests: XCTestCase {
  func testProjectRankerSynthesizesMissingEntriesInPriorityOrder() {
    var composingText = ComposingText()
    composingText.insertAtCursorPosition("せつな", inputStyle: .direct)
    let generic = makeCandidate(text: "せつ菜", ruby: "セツナ")
    let entries = [
      makeProjectEntry(word: "Grimodex工程B", priority: 2),
      makeProjectEntry(word: "Grimodex工程A", priority: 3),
    ]

    let ranked = GrimodexProjectCandidateRanker.rank(
      [generic],
      for: composingText,
      elementCount: 3,
      projectEntries: entries
    )

    XCTAssertEqual(
      ranked.map(\.text),
      ["Grimodex工程A", "Grimodex工程B", "せつ菜"]
    )
    XCTAssertTrue(
      ranked[0].data[0].metadata.contains(.isFromUserDictionary)
    )
  }

  func testProjectRankerPromotesAndDeduplicatesAnExistingExactCandidate() {
    var composingText = ComposingText()
    composingText.insertAtCursorPosition("せつな", inputStyle: .direct)
    let generic = makeCandidate(text: "せつ菜", ruby: "セツナ")
    let project = makeCandidate(text: "Grimodex工程A", ruby: "セツナ")
    let duplicate = makeCandidate(text: "Grimodex工程A", ruby: "セツナ")

    let ranked = GrimodexProjectCandidateRanker.rank(
      [generic, project, duplicate],
      for: composingText,
      elementCount: 3,
      projectEntries: [makeProjectEntry(word: "Grimodex工程A", priority: 3)]
    )

    XCTAssertEqual(ranked.first?.text, "Grimodex工程A")
    XCTAssertEqual(ranked.filter { $0.text == "Grimodex工程A" }.count, 1)
    XCTAssertEqual(ranked.last?.text, "せつ菜")
  }

  func testProjectRankerKeepsZenzaiOrderBeforeRestoredEntriesAtEqualPriority() {
    var composingText = ComposingText()
    composingText.insertAtCursorPosition("せつな", inputStyle: .direct)
    let genericA = makeCandidate(text: "せつ菜", ruby: "セツナ")
    let genericB = makeCandidate(text: "節名", ruby: "セツナ")
    let projectA = makeCandidate(text: "Grimodex工程A", ruby: "セツナ")
    let projectB = makeCandidate(text: "Grimodex工程B", ruby: "セツナ")
    let entries = [
      makeProjectEntry(word: "Grimodex工程A", priority: 3),
      makeProjectEntry(word: "Grimodex工程B", priority: 3),
      makeProjectEntry(word: "Grimodex工程C", priority: 3),
    ]

    let ranked = GrimodexProjectCandidateRanker.rank(
      [genericA, projectB, genericB, projectA],
      for: composingText,
      elementCount: 3,
      projectEntries: entries
    )

    XCTAssertEqual(
      ranked.map(\.text),
      [
        "Grimodex工程B", "Grimodex工程A", "Grimodex工程C", "せつ菜",
        "節名",
      ]
    )
  }

  func testProjectRankerRestoresAnExactProjectCandidateForASegmentPrefix() {
    var prefixText = ComposingText()
    prefixText.insertAtCursorPosition("せつな", inputStyle: .direct)
    let generic = makeCandidate(text: "せつ菜", ruby: "セツナ")
    let index = GrimodexProjectDictionaryIndex(entries: [
      makeProjectEntry(word: "Grimodex工程A", priority: 3)
    ])

    let ranked = GrimodexProjectCandidateRanker.rank(
      [generic],
      for: prefixText,
      elementCount: 3,
      projectIndex: index
    )

    XCTAssertEqual(ranked.map(\.text), ["Grimodex工程A", "せつ菜"])
    XCTAssertEqual(ranked.first?.composingCount, .inputCount(3))
  }

  func testProjectRankerUsesCanonicalProjectDataForASurfaceMatch() {
    var composingText = ComposingText()
    composingText.insertAtCursorPosition("せつな", inputStyle: .direct)
    let generic = makeCandidate(text: "同じ表記", ruby: "セツナ", cid: 1288)
    let project = makeProjectEntry(word: "同じ表記", priority: 3)

    let ranked = GrimodexProjectCandidateRanker.rank(
      [generic],
      for: composingText,
      elementCount: 3,
      projectEntries: [project]
    )

    XCTAssertEqual(ranked.count, 1)
    XCTAssertEqual(ranked[0].data.map(\.lcid), [project.cid])
    XCTAssertTrue(ranked[0].data[0].metadata.contains(.isFromUserDictionary))
  }

  func testProjectRankerParsesRestoredTemplatesAndDisablesLearning() {
    var composingText = ComposingText()
    composingText.insertAtCursorPosition("せつな", inputStyle: .direct)
    let template = #"<random type="int" value="1,1">"#
    let entry = makeProjectEntry(word: template, priority: 3)

    let ranked = GrimodexProjectCandidateRanker.rank(
      [],
      for: composingText,
      elementCount: 3,
      projectEntries: [entry]
    )

    XCTAssertEqual(ranked.first?.text, "1")
    XCTAssertEqual(ranked.first?.data.first?.word, template)
    XCTAssertEqual(ranked.first?.isLearningTarget, false)
  }

  func testProjectIndexLookupCostDoesNotDependOnUnrelatedEntries() {
    var composingText = ComposingText()
    composingText.insertAtCursorPosition("せつな", inputStyle: .direct)
    let unrelated = (0..<20_000).map { index in
      GrimodexMappedDictionaryEntry(
        ruby: "ヨミ\(index)",
        word: "語\(index)",
        cid: 1289,
        mid: 501,
        value: -5,
        priority: 1,
        entryID: "unrelated-\(index)"
      )
    }
    let target = makeProjectEntry(word: "Grimodex工程A", priority: 3)
    let index = GrimodexProjectDictionaryIndex(entries: unrelated + [target])

    let ranked = GrimodexProjectCandidateRanker.rank(
      [makeCandidate(text: "せつ菜", ruby: "セツナ")],
      for: composingText,
      elementCount: 3,
      projectIndex: index
    )

    XCTAssertEqual(index.entryCount, 20_001)
    XCTAssertEqual(ranked.prefix(2).map(\.text), ["Grimodex工程A", "せつ菜"])
  }

  func testProjectRankerPromotesEmbeddedProjectNodesButLeavesMismatchesAlone() {
    var composingText = ComposingText()
    composingText.insertAtCursorPosition("こうていえーです", inputStyle: .direct)
    let generic = makeCandidate(
      text: "工程Aです",
      ruby: "コウテイエーデス",
      cid: 1288
    )
    let projectData = DicdataElement(
      word: "Grimodex工程A",
      ruby: "コウテイエー",
      cid: 1289,
      mid: 501,
      value: -4,
      metadata: .isFromUserDictionary
    )
    let suffixData = DicdataElement(
      word: "です",
      ruby: "デス",
      cid: 1288,
      mid: 501,
      value: -10
    )
    let embedded = Candidate(
      text: "Grimodex工程Aです",
      value: projectData.value() + suffixData.value(),
      composingCount: .inputCount(8),
      lastMid: suffixData.mid,
      data: [projectData, suffixData]
    )
    let entry = GrimodexMappedDictionaryEntry(
      ruby: "コウテイエー",
      word: "Grimodex工程A",
      cid: 1289,
      mid: 501,
      value: -4,
      priority: 3,
      entryID: "project-a"
    )

    let ranked = GrimodexProjectCandidateRanker.rank(
      [generic, embedded],
      for: composingText,
      elementCount: 8,
      projectEntries: [entry]
    )
    XCTAssertEqual(ranked.map(\.text), ["Grimodex工程Aです", "工程Aです"])

    let unchanged = GrimodexProjectCandidateRanker.rank(
      [generic, embedded],
      for: composingText,
      elementCount: 8,
      projectEntries: [makeProjectEntry(word: "別候補", priority: 3)]
    )
    XCTAssertEqual(unchanged.map(\.text), ["工程Aです", "Grimodex工程Aです"])
  }

  func testRomanizedCursorMovesOnlyAcrossVisibleKanaBoundaries() {
    let converters = makeDefaultConverterPair()
    let adapter = HazkeyKanaKanjiConverterAdapter(
      converter: converters.primary,
      boundaryConverter: converters.boundary,
      optionsProvider: { _ in HazkeyServerConfig().genBaseConvertRequestOptions() }
    )
    let reducer = ImeReducer(converter: adapter)
    let inserted = reducer.reduce(.insertText("kana"), requestID: "insert")
    XCTAssertEqual(inserted.snapshot.preedit.map(\.text), ["かな"])

    let firstLeft = reducer.reduce(.moveCursor(-1), requestID: "left-1")
    XCTAssertEqual(reducer.session.composingText.cursor, 2)
    XCTAssertEqual(
      firstLeft.snapshot.caretUtf8ByteOffset,
      UInt32("か".utf8.count)
    )

    let secondLeft = reducer.reduce(.moveCursor(-1), requestID: "left-2")
    XCTAssertEqual(reducer.session.composingText.cursor, 0)
    XCTAssertEqual(secondLeft.snapshot.caretUtf8ByteOffset, 0)

    let firstRight = reducer.reduce(.moveCursor(1), requestID: "right-1")
    XCTAssertEqual(reducer.session.composingText.cursor, 2)
    XCTAssertEqual(
      firstRight.snapshot.caretUtf8ByteOffset,
      UInt32("か".utf8.count)
    )

    let secondRight = reducer.reduce(.moveCursor(1), requestID: "right-2")
    XCTAssertEqual(reducer.session.composingText.cursor, 4)
    XCTAssertEqual(
      secondRight.snapshot.caretUtf8ByteOffset,
      UInt32("かな".utf8.count)
    )
  }

  func testRomanizedDisplayDoesNotFallBackToTheRightEdge() {
    let converters = makeDefaultConverterPair()
    let adapter = HazkeyKanaKanjiConverterAdapter(
      converter: converters.primary,
      boundaryConverter: converters.boundary,
      optionsProvider: { _ in HazkeyServerConfig().genBaseConvertRequestOptions() }
    )
    let elements = "kana".map {
      CompositionElement(text: String($0), inputStyle: .mapped)
    }

    let display = adapter.display(
      for: CompositionInput(
        elements: elements,
        cursor: 3,
        leftContext: ""
      )
    )

    XCTAssertEqual(display.text, "かな")
    XCTAssertEqual(display.caretUtf8ByteOffset, UInt32("か".utf8.count))
  }

  func testNormalRealtimeSuggestionsRespectConfiguredLimit() throws {
    var requestOptions = HazkeyServerConfig().genBaseConvertRequestOptions()
    requestOptions.N_best = 9
    requestOptions.zenzaiMode = .off
    let converters = makeDefaultConverterPair()
    let adapter = HazkeyKanaKanjiConverterAdapter(
      converter: converters.primary,
      boundaryConverter: converters.boundary,
      optionsProvider: { _ in requestOptions }
    )
    let elements = "かな".map {
      CompositionElement(text: String($0), inputStyle: .direct)
    }

    let output = try adapter.realtimeCandidates(
      for: CompositionInput(
        elements: elements,
        cursor: elements.count,
        leftContext: ""
      ),
      options: ConversionOptions(
        allowLearning: true,
        zenzaiEnabled: true,
        leftContext: "",
        rightContext: "",
        suggestionListMode: .normal,
        suggestionListLimit: 1
      )
    )

    XCTAssertEqual(output.candidates.count, 1)
    XCTAssertEqual(output.pageSize, 1)
  }

  func testZenzaiRankingInfluenceIsIndependentFromProvenanceAndPassScore() throws {
    var requestOptions = HazkeyServerConfig().genBaseConvertRequestOptions()
    requestOptions.learningType = .nothing
    requestOptions.zenzaiMode = .off
    let converters = makeDefaultConverterPair()
    var composingText = ComposingText()
    composingText.insertAtCursorPosition("かんそくせい", inputStyle: .direct)
    let reference = converters.primary.requestCandidates(
      composingText,
      options: requestOptions
    )
    let scoredReference = try XCTUnwrap(reference.mainResults.first)
    let scoredText = scoredReference.text
    let scoredIdentity = ZenzaiCandidateIdentity(scoredReference)
    let emptySuffixReference = try XCTUnwrap(reference.mainResults.dropFirst().first)
    let emptySuffixIdentity = ZenzaiCandidateIdentity(emptySuffixReference)
    converters.primary.stopComposition()

    requestOptions.zenzaiMode = .on(
      weight: FileManager.default.temporaryDirectory.appendingPathComponent(
        "missing-zenzai-\(UUID().uuidString).gguf"
      ),
      personalizationMode: nil
    )
    let adapter = HazkeyKanaKanjiConverterAdapter(
      converter: converters.primary,
      boundaryConverter: converters.boundary,
      optionsProvider: { _ in requestOptions },
      zenzaiCandidateEvaluationMetadataProvider: {
        [scoredIdentity, emptySuffixIdentity] in
        [
          scoredIdentity: ZenzaiCandidateEvaluationMetadata(
            score: -1.25,
            scoredTokenCount: 4,
            coversFullCandidate: false
          ),
          emptySuffixIdentity: ZenzaiCandidateEvaluationMetadata(
            score: 0,
            scoredTokenCount: 0,
            coversFullCandidate: false
          )
        ]
      }
    )
    let elements = "かんそくせい".map {
      CompositionElement(text: String($0), inputStyle: .direct)
    }

    let input = CompositionInput(
      elements: elements,
      cursor: elements.count,
      leftContext: ""
    )
    let output = try adapter.candidates(
      for: input,
      options: .default
    )

    let scored = try XCTUnwrap(output.candidates.first { $0.text == scoredText })
    XCTAssertEqual(scored.provenance, .standard)
    XCTAssertEqual(scored.rankingInfluence, .zenzai)
    XCTAssertEqual(scored.zenzaiScore, -1.25)
    XCTAssertEqual(scored.zenzaiScoredTokenCount, 4)
    XCTAssertEqual(scored.zenzaiScoreScope, .constraintSuffix)

    let unscored = try XCTUnwrap(output.candidates.first {
      $0.text == emptySuffixReference.text
    })
    XCTAssertEqual(unscored.rankingInfluence, .zenzai)
    XCTAssertNil(unscored.zenzaiScore)
    XCTAssertNil(unscored.zenzaiScoredTokenCount)
    XCTAssertNil(unscored.zenzaiScoreScope)

    let guardCandidate = try XCTUnwrap(output.candidates.first {
      $0.text == "可観測性"
    })
    XCTAssertEqual(guardCandidate.provenance, .builtInGuard)
    XCTAssertEqual(guardCandidate.rankingInfluence, .standard)
    XCTAssertNil(guardCandidate.zenzaiScore)
    XCTAssertNil(guardCandidate.zenzaiScoredTokenCount)
    XCTAssertNil(guardCandidate.zenzaiScoreScope)
  }

  func testProjectInjectedSameSurfaceDoesNotBorrowZenzaiEvaluationMetadata() throws {
    let reading = "せつな"
    var requestOptions = HazkeyServerConfig().genBaseConvertRequestOptions()
    requestOptions.learningType = .nothing
    requestOptions.zenzaiMode = .off
    let converters = makeDefaultConverterPair()
    var composingText = ComposingText()
    composingText.insertAtCursorPosition(reading, inputStyle: .direct)
    let reference = converters.primary.requestCandidates(
      composingText,
      options: requestOptions
    )
    let evaluatedCandidate = try XCTUnwrap(reference.mainResults.first)
    let evaluatedIdentity = ZenzaiCandidateIdentity(evaluatedCandidate)
    converters.primary.stopComposition()

    requestOptions.zenzaiMode = .on(
      weight: FileManager.default.temporaryDirectory.appendingPathComponent(
        "missing-zenzai-\(UUID().uuidString).gguf"
      ),
      personalizationMode: nil
    )
    let projectEntry = GrimodexMappedDictionaryEntry(
      ruby: "セツナ",
      word: evaluatedCandidate.text,
      cid: 1289,
      mid: 501,
      value: -4,
      priority: 3,
      entryID: "same-surface-project"
    )
    let adapter = HazkeyKanaKanjiConverterAdapter(
      converter: converters.primary,
      boundaryConverter: converters.boundary,
      optionsProvider: { _ in requestOptions },
      projectDictionaryIndexProvider: {
        GrimodexProjectDictionaryIndex(entries: [projectEntry])
      },
      zenzaiCandidateEvaluationMetadataProvider: { [evaluatedIdentity] in
        [
          evaluatedIdentity: ZenzaiCandidateEvaluationMetadata(
            score: -0.5,
            scoredTokenCount: 2,
            coversFullCandidate: true
          )
        ]
      }
    )
    let elements = reading.map {
      CompositionElement(text: String($0), inputStyle: .direct)
    }

    let input = CompositionInput(
      elements: elements,
      cursor: elements.count,
      leftContext: ""
    )
    let output = try adapter.candidates(
      for: input,
      options: .default
    )

    let projectCandidate = try XCTUnwrap(output.candidates.first {
      $0.text == evaluatedCandidate.text
    })
    XCTAssertEqual(projectCandidate.provenance, .projectDictionary)
    XCTAssertEqual(projectCandidate.rankingInfluence, .standard)
    XCTAssertNil(projectCandidate.zenzaiScore)
    XCTAssertNil(projectCandidate.zenzaiScoredTokenCount)
    XCTAssertNil(projectCandidate.zenzaiScoreScope)

    let realtime = try adapter.realtimeCandidates(
      for: input,
      options: .default
    )
    let realtimeProjectCandidate = try XCTUnwrap(realtime.liveCandidate)
    XCTAssertEqual(realtimeProjectCandidate.text, evaluatedCandidate.text)
    XCTAssertEqual(realtimeProjectCandidate.rankingInfluence, .standard)
    XCTAssertNil(realtimeProjectCandidate.zenzaiScore)

    let segment = try adapter.segmentCandidates(
      for: input,
      options: .default
    )
    let segmentProjectCandidate = try XCTUnwrap(segment.candidates.first {
      $0.text == evaluatedCandidate.text
    })
    XCTAssertEqual(segmentProjectCandidate.rankingInfluence, .standard)
    XCTAssertNil(segmentProjectCandidate.zenzaiScore)
  }

  func testSegmentPrefixUsesOnlyItsTargetedExactPassScore() throws {
    let reading = "きょうはいしゃにいく"
    var requestOptions = HazkeyServerConfig().genBaseConvertRequestOptions()
    requestOptions.learningType = .nothing
    requestOptions.zenzaiMode = .off
    let converters = makeDefaultConverterPair()
    var composingText = ComposingText()
    composingText.insertAtCursorPosition(reading, inputStyle: .direct)
    let reference = converters.primary.requestCandidates(
      composingText,
      options: requestOptions
    )
    let scoredFullCandidate = try XCTUnwrap(reference.mainResults.first { candidate in
      !candidate.data.isEmpty
        && Candidate.makePrefixClauseCandidate(data: candidate.data).text
          != candidate.text
    })
    let derivedPrefixText = Candidate.makePrefixClauseCandidate(
      data: scoredFullCandidate.data
    ).text
    let scoredFullIdentity = ZenzaiCandidateIdentity(scoredFullCandidate)
    let derivedPrefixIdentity = ZenzaiCandidateIdentity(
      Candidate.makePrefixClauseCandidate(data: scoredFullCandidate.data)
    )
    let metadataResponses: [[
      ZenzaiCandidateIdentity: ZenzaiCandidateEvaluationMetadata
    ]] = [
      [
        scoredFullIdentity: ZenzaiCandidateEvaluationMetadata(
          score: -1,
          scoredTokenCount: 8,
          coversFullCandidate: true
        )
      ],
      [
        derivedPrefixIdentity: ZenzaiCandidateEvaluationMetadata(
          score: -3,
          scoredTokenCount: 2,
          coversFullCandidate: false
        )
      ],
    ]
    var metadataResponseIndex = 0
    converters.primary.stopComposition()

    requestOptions.zenzaiMode = .on(
      weight: FileManager.default.temporaryDirectory.appendingPathComponent(
        "missing-zenzai-\(UUID().uuidString).gguf"
      ),
      personalizationMode: nil
    )
    let adapter = HazkeyKanaKanjiConverterAdapter(
      converter: converters.primary,
      boundaryConverter: converters.boundary,
      optionsProvider: { _ in requestOptions },
      zenzaiCandidateEvaluationMetadataProvider: {
        defer { metadataResponseIndex += 1 }
        return metadataResponses[
          min(metadataResponseIndex, metadataResponses.count - 1)
        ]
      }
    )
    let elements = reading.map {
      CompositionElement(text: String($0), inputStyle: .direct)
    }

    let output = try adapter.segmentCandidates(
      for: CompositionInput(
        elements: elements,
        cursor: elements.count,
        leftContext: ""
      ),
      options: .default
    )

    let derivedPrefix = try XCTUnwrap(output.candidates.first {
      $0.text == derivedPrefixText && $0.consumingCount < elements.count
    })
    XCTAssertEqual(derivedPrefix.rankingInfluence, .zenzai)
    XCTAssertEqual(derivedPrefix.zenzaiScore, -3)
    XCTAssertEqual(derivedPrefix.zenzaiScoredTokenCount, 2)
    XCTAssertEqual(derivedPrefix.zenzaiScoreScope, .constraintSuffix)

    let exactReading = "とうきょう"
    var exactComposingText = ComposingText()
    exactComposingText.insertAtCursorPosition(exactReading, inputStyle: .direct)
    requestOptions.zenzaiMode = .off
    let exactConverters = makeDefaultConverterPair()
    let exactReference = exactConverters.primary.requestCandidates(
      exactComposingText,
      options: requestOptions
    )
    let exactReferenceCandidate = try XCTUnwrap(exactReference.mainResults.first)
    let exactText = exactReferenceCandidate.text
    let exactIdentity = ZenzaiCandidateIdentity(exactReferenceCandidate)
    exactConverters.primary.stopComposition()
    requestOptions.zenzaiMode = .on(
      weight: FileManager.default.temporaryDirectory.appendingPathComponent(
        "missing-zenzai-\(UUID().uuidString).gguf"
      ),
      personalizationMode: nil
    )
    let exactAdapter = HazkeyKanaKanjiConverterAdapter(
      converter: exactConverters.primary,
      boundaryConverter: exactConverters.boundary,
      optionsProvider: { _ in requestOptions },
      zenzaiCandidateEvaluationMetadataProvider: { [exactIdentity] in
        [
          exactIdentity: ZenzaiCandidateEvaluationMetadata(
            score: -7.5,
            scoredTokenCount: 3,
            coversFullCandidate: true
          )
        ]
      }
    )
    let exactElements = exactReading.map {
      CompositionElement(text: String($0), inputStyle: .direct)
    }
    let exactOutput = try exactAdapter.segmentCandidates(
      for: CompositionInput(
        elements: exactElements,
        cursor: exactElements.count,
        leftContext: ""
      ),
      options: .default
    )
    let exactCandidate = try XCTUnwrap(exactOutput.candidates.first {
      $0.text == exactText && $0.consumingCount == exactElements.count
    })
    XCTAssertEqual(exactCandidate.rankingInfluence, .zenzai)
    XCTAssertEqual(exactCandidate.zenzaiScore, -7.5)
    XCTAssertEqual(exactCandidate.zenzaiScoredTokenCount, 3)
    XCTAssertEqual(exactCandidate.zenzaiScoreScope, .fullCandidate)
  }

  func testZenzaiExecutionEvidenceMergesAllPrimaryRequests() {
    let first = ZenzaiExecutionEvidence(
      requestCount: 1,
      evaluationAttemptCount: 1,
      attemptOutcomes: ZenzaiEvaluationOutcomeCounts(
        pass: 1,
        fixRequired: 0,
        wholeResult: 0,
        error: 0
      ),
      terminalOutcomes: ZenzaiTerminalOutcomeCounts(
        pass: 1,
        fixRequired: 0,
        wholeResult: 0,
        error: 0,
        inferenceLimit: 0,
        noCandidate: 0
      )
    )
    let second = ZenzaiExecutionEvidence(
      requestCount: 1,
      evaluationAttemptCount: 2,
      attemptOutcomes: ZenzaiEvaluationOutcomeCounts(
        pass: 0,
        fixRequired: 1,
        wholeResult: 1,
        error: 0
      ),
      terminalOutcomes: ZenzaiTerminalOutcomeCounts(
        pass: 0,
        fixRequired: 0,
        wholeResult: 1,
        error: 0,
        inferenceLimit: 0,
        noCandidate: 0
      )
    )
    XCTAssertEqual(
      HazkeyKanaKanjiConverterAdapter.mergeZenzaiExecutionEvidence(
        first,
        second
      ),
      first.merged(with: second)
    )
    XCTAssertEqual(
      HazkeyKanaKanjiConverterAdapter.mergeZenzaiExecutionEvidence(first, nil),
      first
    )
    XCTAssertEqual(
      HazkeyKanaKanjiConverterAdapter.mergeZenzaiExecutionEvidence(nil, second),
      second
    )
    XCTAssertNil(
      HazkeyKanaKanjiConverterAdapter.mergeZenzaiExecutionEvidence(nil, nil)
    )
  }

  func testZenzaiExecutionEvidenceFollowsMeasuredPrimaryRequestCount() throws {
    let reading = "きょうはいしゃにいく"
    let elements = reading.map {
      CompositionElement(text: String($0), inputStyle: .direct)
    }
    let input = CompositionInput(
      elements: elements,
      cursor: elements.count,
      leftContext: ""
    )
    var dictionaryOptions = HazkeyServerConfig().genBaseConvertRequestOptions()
    dictionaryOptions.learningType = .nothing
    dictionaryOptions.zenzaiMode = .off
    var composingText = ComposingText()
    composingText.insertAtCursorPosition(reading, inputStyle: .direct)

    let first = ZenzaiExecutionEvidence(
      requestCount: 1,
      evaluationAttemptCount: 1,
      attemptOutcomes: ZenzaiEvaluationOutcomeCounts(
        pass: 1,
        fixRequired: 0,
        wholeResult: 0,
        error: 0
      ),
      terminalOutcomes: ZenzaiTerminalOutcomeCounts(
        pass: 1,
        fixRequired: 0,
        wholeResult: 0,
        error: 0,
        inferenceLimit: 0,
        noCandidate: 0
      )
    )
    let second = ZenzaiExecutionEvidence(
      requestCount: 1,
      evaluationAttemptCount: 2,
      attemptOutcomes: ZenzaiEvaluationOutcomeCounts(
        pass: 0,
        fixRequired: 1,
        wholeResult: 1,
        error: 0
      ),
      terminalOutcomes: ZenzaiTerminalOutcomeCounts(
        pass: 0,
        fixRequired: 0,
        wholeResult: 1,
        error: 0,
        inferenceLimit: 0,
        noCandidate: 0
      )
    )

    func makeAdapter(
      evidence: [ZenzaiExecutionEvidence]
    ) -> (
      adapter: HazkeyKanaKanjiConverterAdapter,
      requestCount: () -> Int,
      evidenceCount: () -> Int
    ) {
      let converters = makeDefaultConverterPair()
      let reference = converters.primary.requestCandidates(
        composingText,
        options: dictionaryOptions
      )
      converters.primary.stopComposition()
      var requestCount = 0
      var evidenceCount = 0
      var zenzaiOptions = dictionaryOptions
      zenzaiOptions.zenzaiMode = .on(
        weight: FileManager.default.temporaryDirectory.appendingPathComponent(
          "unopened-zenzai-\(UUID().uuidString).gguf"
        ),
        personalizationMode: nil
      )
      let adapter = HazkeyKanaKanjiConverterAdapter(
        converter: converters.primary,
        boundaryConverter: converters.boundary,
        optionsProvider: { _ in zenzaiOptions },
        primaryCandidateRequestProvider: { _, _ in
          requestCount += 1
          return reference
        },
        zenzaiExecutionEvidenceProvider: {
          defer { evidenceCount += 1 }
          return evidence[min(evidenceCount, evidence.count - 1)]
        }
      )
      return (adapter, { requestCount }, { evidenceCount })
    }

    do {
      let harness = makeAdapter(evidence: [first])
      let output = try harness.adapter.candidates(for: input, options: .default)
      XCTAssertEqual(harness.requestCount(), 1)
      XCTAssertEqual(harness.evidenceCount(), 1)
      XCTAssertEqual(output.zenzaiExecutionEvidence, first)
    }

    do {
      let harness = makeAdapter(evidence: [first])
      let output = harness.adapter.nativeZenzaiSegmentCandidatesForProbe(
        for: input,
        options: .default
      )
      XCTAssertEqual(harness.requestCount(), 1)
      XCTAssertEqual(harness.evidenceCount(), 1)
      XCTAssertEqual(output.zenzaiExecutionEvidence, first)
    }

    do {
      let harness = makeAdapter(evidence: [first, second])
      let output = try harness.adapter.segmentCandidates(
        for: input,
        options: .default
      )
      XCTAssertEqual(harness.requestCount(), 2)
      XCTAssertEqual(harness.evidenceCount(), 2)
      XCTAssertEqual(
        output.zenzaiExecutionEvidence,
        first.merged(with: second)
      )
    }
  }

  func testNativeBoundaryProbeMirrorsPrimaryFirstClauseResults() throws {
    let reading = "きょうはいしゃにいく"
    var requestOptions = HazkeyServerConfig().genBaseConvertRequestOptions()
    requestOptions.N_best = 10
    requestOptions.learningType = .nothing
    requestOptions.zenzaiMode = .off

    let converters = makeDefaultConverterPair()
    let boundaryOnlyText = "境界専用"
    converters.boundary.importDynamicUserDictionary([
      DicdataElement(
        word: boundaryOnlyText,
        ruby: "キョウハ",
        cid: CIDData.一般名詞.cid,
        mid: MIDData.一般.mid,
        value: -1
      ),
    ])
    var composingText = ComposingText()
    composingText.insertAtCursorPosition(reading, inputStyle: .direct)
    let primaryReference = converters.primary.requestCandidates(
      composingText,
      options: requestOptions
    )
    let expectedTexts = Array(
      primaryReference.firstClauseResults.prefix(requestOptions.N_best)
    ).map(\.text)
    converters.primary.stopComposition()

    let boundaryReference = converters.boundary.requestCandidates(
      composingText,
      options: requestOptions
    )
    let boundaryTexts = Array(
      boundaryReference.firstClauseResults.prefix(requestOptions.N_best)
    ).map(\.text)
    converters.boundary.stopComposition()

    XCTAssertTrue(boundaryTexts.contains(boundaryOnlyText))
    XCTAssertNotEqual(boundaryTexts, expectedTexts)

    let adapter = HazkeyKanaKanjiConverterAdapter(
      converter: converters.primary,
      boundaryConverter: converters.boundary,
      optionsProvider: { _ in requestOptions }
    )
    let elements = reading.map {
      CompositionElement(text: String($0), inputStyle: .direct)
    }
    let output = adapter.nativeZenzaiSegmentCandidatesForProbe(
      for: CompositionInput(
        elements: elements,
        cursor: elements.count,
        leftContext: ""
      ),
      options: ConversionOptions(
        allowLearning: false,
        zenzaiEnabled: false,
        leftContext: "",
        rightContext: "",
        suggestionListMode: .normal,
        suggestionListLimit: 10
      )
    )

    XCTAssertEqual(output.candidates.map(\.text), expectedTexts)
    XCTAssertNotEqual(output.candidates.map(\.text), boundaryTexts)
    XCTAssertFalse(output.candidates.map(\.text).contains(boundaryOnlyText))
    XCTAssertFalse(output.candidates.isEmpty)
    XCTAssertTrue(output.candidates.allSatisfy {
      $0.rankingInfluence == .standard
        && $0.consumingCount <= elements.count
    })
  }

  func testMissingZenzaiScoreClearsDependentMetadata() {
    let candidate = ConverterCandidate(
      text: "候補",
      consumingCount: 2,
      zenzaiScore: nil,
      zenzaiScoredTokenCount: 5,
      zenzaiScoreScope: .fullCandidate
    )

    XCTAssertNil(candidate.zenzaiScore)
    XCTAssertNil(candidate.zenzaiScoredTokenCount)
    XCTAssertNil(candidate.zenzaiScoreScope)
  }

  func testTargetedMetadataSupplementDoesNotDowngradeInfluenceOrIdentity() {
    let existing = ConverterCandidate(
      text: "既存候補",
      annotation: "既存注釈",
      consumingCount: 3,
      sourceID: "existing-source",
      provenance: .projectDictionary,
      rankingInfluence: .zenzai,
      zenzaiScore: -4,
      zenzaiScoredTokenCount: 5,
      zenzaiScoreScope: .fullCandidate,
      isLearnable: false
    )
    let targeted = ConverterCandidate(
      text: "既存候補",
      annotation: "targeted annotation",
      consumingCount: 3,
      sourceID: "targeted-source",
      provenance: .standard,
      rankingInfluence: .standard
    )

    let supplemented = HazkeyKanaKanjiConverterAdapter
      .supplementRankingMetadata(of: existing, from: targeted)

    XCTAssertEqual(supplemented.text, existing.text)
    XCTAssertEqual(supplemented.annotation, existing.annotation)
    XCTAssertEqual(supplemented.consumingCount, existing.consumingCount)
    XCTAssertEqual(supplemented.sourceID, existing.sourceID)
    XCTAssertEqual(supplemented.provenance, existing.provenance)
    XCTAssertEqual(supplemented.isLearnable, existing.isLearnable)
    XCTAssertEqual(supplemented.rankingInfluence, .zenzai)
    XCTAssertEqual(supplemented.zenzaiScore, -4)
    XCTAssertEqual(supplemented.zenzaiScoredTokenCount, 5)
    XCTAssertEqual(supplemented.zenzaiScoreScope, .fullCandidate)
  }

  func testDefaultDictionaryBuildsACompleteEditableSegmentPlan() {
    var requestOptions = HazkeyServerConfig().genBaseConvertRequestOptions()
    requestOptions.learningType = .nothing
    requestOptions.zenzaiMode = .off
    let converters = makeDefaultConverterPair()
    let adapter = HazkeyKanaKanjiConverterAdapter(
      converter: converters.primary,
      boundaryConverter: converters.boundary,
      optionsProvider: { _ in requestOptions }
    )
    var session = CompositionSession()
    session.composingText.insert("きょうはいしゃにいく", inputStyle: .direct)
    session.phase = .composing
    let reducer = ImeReducer(session: session, converter: adapter)

    let converted = reducer.reduce(.startConversion, requestID: "convert-segments")

    XCTAssertEqual(converted.status, .success)
    XCTAssertGreaterThan(reducer.session.segments.count, 1)
    XCTAssertEqual(
      reducer.session.segments.map(\.inputCount).reduce(0, +),
      reducer.session.composingText.elements.count
    )
    XCTAssertGreaterThan(
      converted.snapshot.candidateWindow.items.count,
      1,
      "the initially active natural segment must expose conversion alternatives"
    )
    XCTAssertEqual(converted.snapshot.candidateWindow.selectedIndex, 0)
    for segment in reducer.session.segments {
      XCTAssertFalse(segment.candidates.items.isEmpty)
      XCTAssertTrue(segment.candidates.items.allSatisfy {
        $0.consumingCount == segment.inputCount
      })
    }
  }

  func testDefaultDictionaryKeepsKnownSingleWordsInOneSegment() {
    for reading in ["とうきょう", "かぶしきがいしゃ"] {
      var requestOptions = HazkeyServerConfig().genBaseConvertRequestOptions()
      requestOptions.learningType = .nothing
      requestOptions.zenzaiMode = .off
      let converters = makeDefaultConverterPair()
      let adapter = HazkeyKanaKanjiConverterAdapter(
        converter: converters.primary,
        boundaryConverter: converters.boundary,
        optionsProvider: { _ in requestOptions }
      )
      var session = CompositionSession()
      session.composingText.insert(reading, inputStyle: .direct)
      session.phase = .composing
      let reducer = ImeReducer(session: session, converter: adapter)

      let converted = reducer.reduce(
        .startConversion,
        requestID: "convert-single-word-\(reading)"
      )

      XCTAssertEqual(converted.status, .success, reading)
      XCTAssertEqual(
        reducer.session.segments.map(\.inputCount),
        [reading.count],
        reading
      )
    }
  }

  func testExactDictionaryTermKeepsItsOwnBoundary() {
    var requestOptions = HazkeyServerConfig().genBaseConvertRequestOptions()
    requestOptions.learningType = .nothing
    requestOptions.zenzaiMode = .off
    let converters = makeDefaultConverterPair()
    let userTerm = DicdataElement(
      word: "国際連合",
      ruby: "コクサイレンゴウ",
      cid: CIDData.一般名詞.cid,
      mid: MIDData.一般.mid,
      value: -30
    )
    converters.primary.importDynamicUserDictionary([userTerm])
    converters.boundary.importDynamicUserDictionary([userTerm])
    let adapter = HazkeyKanaKanjiConverterAdapter(
      converter: converters.primary,
      boundaryConverter: converters.boundary,
      optionsProvider: { _ in requestOptions }
    )
    var session = CompositionSession()
    session.composingText.insert("こくさいれんごう", inputStyle: .direct)
    session.phase = .composing
    let reducer = ImeReducer(session: session, converter: adapter)

    let converted = reducer.reduce(
      .startConversion,
      requestID: "convert-exact-dictionary-term"
    )

    XCTAssertEqual(converted.status, .success)
    XCTAssertEqual(reducer.session.segments.map(\.inputCount), [8])
    XCTAssertEqual(
      reducer.session.segments.first?.selectedCandidate?.text,
      "国際連合"
    )
  }

  func testLearnedWholeSentenceDoesNotReplaceNaturalSegmentBoundary() throws {
    let fileManager = FileManager.default
    let root = fileManager.temporaryDirectory.appendingPathComponent(
      "hazkey-learned-boundary-\(UUID().uuidString)",
      isDirectory: true
    )
    let memory = root.appendingPathComponent("memory", isDirectory: true)
    let shared = root.appendingPathComponent("shared", isDirectory: true)
    try fileManager.createDirectory(
      at: memory,
      withIntermediateDirectories: true
    )
    try fileManager.createDirectory(
      at: shared,
      withIntermediateDirectories: true
    )
    defer { try? fileManager.removeItem(at: root) }

    var requestOptions = HazkeyServerConfig().genBaseConvertRequestOptions()
    requestOptions.N_best = 9
    requestOptions.learningType = .inputAndOutput
    requestOptions.memoryDirectoryURL = memory
    requestOptions.sharedContainerURL = shared
    requestOptions.zenzaiMode = .off

    let converters = makeDefaultConverterPair()
    let converter = converters.primary
    var composingText = ComposingText()
    composingText.insertAtCursorPosition(
      "きょうはいしゃにいく",
      inputStyle: .direct
    )
    _ = converter.requestCandidates(
      composingText,
      options: requestOptions
    )
    let learnedData = DicdataElement(
      word: "全文固定候補",
      ruby: "キョウハイシャニイク",
      cid: CIDData.一般名詞.cid,
      mid: MIDData.一般.mid,
      value: -30
    )
    let learnedWholeSentence = Candidate(
      text: learnedData.word,
      value: learnedData.value(),
      composingCount: .inputCount(10),
      lastMid: learnedData.mid,
      data: [learnedData]
    )
    let learnedSegmentData = DicdataElement(
      word: "今日派",
      ruby: "キョウハ",
      cid: CIDData.一般名詞.cid,
      mid: MIDData.一般.mid,
      value: -30
    )
    let learnedSegment = Candidate(
      text: learnedSegmentData.word,
      value: learnedSegmentData.value(),
      composingCount: .inputCount(4),
      lastMid: learnedSegmentData.mid,
      data: [learnedSegmentData]
    )
    converter.stopComposition()
    converter.updateLearningData(learnedSegment)
    converter.stopComposition()
    converter.updateLearningData(learnedWholeSentence)
    converter.commitUpdateLearningData()
    converter.stopComposition()

    let learnedResult = converter.requestCandidates(
      composingText,
      options: requestOptions
    )
    XCTAssertEqual(learnedResult.mainResults.first?.text, "全文固定候補")
    XCTAssertTrue(learnedResult.firstClauseResults.contains {
      $0.rubyCount < composingText.convertTarget.count
    })

    let adapter = HazkeyKanaKanjiConverterAdapter(
      converter: converter,
      boundaryConverter: converters.boundary,
      optionsProvider: { _ in requestOptions }
    )
    var session = CompositionSession()
    session.composingText.insert("きょうはいしゃにいく", inputStyle: .direct)
    session.phase = .composing
    let reducer = ImeReducer(session: session, converter: adapter)

    let converted = reducer.reduce(
      .startConversion,
      requestID: "convert-after-whole-sentence-learning"
    )

    XCTAssertEqual(converted.status, .success)
    XCTAssertEqual(
      reducer.session.segments.map(\.inputCount),
      [4, 6],
      "learned whole-sentence candidates must not own automatic boundaries"
    )
    XCTAssertEqual(
      reducer.session.segments.first?.selectedCandidate?.text,
      "今日派",
      "learning must still rank candidates inside the dictionary-defined boundary"
    )
    XCTAssertEqual(reducer.reduce(.commitAll, requestID: "commit-once").status, .success)

    XCTAssertEqual(
      reducer.reduce(
        .insertText("きょうはいしゃにいく"),
        requestID: "insert-again"
      ).status,
      .success
    )
    let repeated = reducer.reduce(
      .startConversion,
      requestID: "convert-after-repeated-learning"
    )
    XCTAssertEqual(repeated.status, .success)
    XCTAssertEqual(
      reducer.session.segments.map(\.inputCount),
      [4, 6],
      "committing the segmented result must not collapse the next conversion"
    )
  }

  private func makeCandidate(
    text: String,
    ruby: String,
    cid: Int = 1288
  ) -> Candidate {
    let data = DicdataElement(
      word: text,
      ruby: ruby,
      cid: cid,
      mid: 501,
      value: -10
    )
    return Candidate(
      text: text,
      value: data.value(),
      composingCount: .inputCount(ruby.count),
      lastMid: data.mid,
      data: [data]
    )
  }

  private func makeProjectEntry(
    word: String,
    priority: Int
  ) -> GrimodexMappedDictionaryEntry {
    GrimodexMappedDictionaryEntry(
      ruby: "セツナ",
      word: word,
      cid: 1289,
      mid: 501,
      value: priority == 3 ? -4 : -5,
      priority: priority,
      entryID: "entry-\(priority)-\(word)"
    )
  }
}
