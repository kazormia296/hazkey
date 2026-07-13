import Foundation
import KanaKanjiConverterModule
import KanaKanjiConverterModuleWithDefaultDictionary
import XCTest

@testable import hazkey_server

final class GrimodexConverterAdapterTests: XCTestCase {
  func testRomanizedCursorMovesOnlyAcrossVisibleKanaBoundaries() {
    let adapter = HazkeyKanaKanjiConverterAdapter(
      converter: .withDefaultDictionary(),
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
    let adapter = HazkeyKanaKanjiConverterAdapter(
      converter: .withDefaultDictionary(),
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
    let adapter = HazkeyKanaKanjiConverterAdapter(
      converter: .withDefaultDictionary(),
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
    requestOptions.zenzaiMode = .off
    let adapter = HazkeyKanaKanjiConverterAdapter(
      converter: .withDefaultDictionary(),
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
}
