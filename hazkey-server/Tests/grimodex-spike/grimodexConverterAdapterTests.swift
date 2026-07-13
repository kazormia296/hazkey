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
      optionsProvider: { _ in requestOptions },
      predictionConfigurationProvider: { (false, 1) },
      suggestionListModeProvider: { .normal }
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
      options: .default
    )

    XCTAssertEqual(output.candidates.count, 1)
    XCTAssertEqual(output.pageSize, 1)
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
}
