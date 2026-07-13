import Foundation
import XCTest

@testable import hazkey_server

private struct SegmentEditingRequest: Equatable {
  let remainingReading: String
  let targetCount: Int?
  let consumedReading: String
  let leftContext: String
}

private final class SegmentEditingFixtureConverter: KanaKanjiConverting {
  let supportsSegmentEditing = true

  var requests: [SegmentEditingRequest] = []
  var completedTexts: [String] = []
  var updatedTexts: [String] = []
  var commitLearningCount = 0

  func candidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    makeOutput(
      for: composition,
      count: composition.targetCount ?? composition.elements.count
    )
  }

  func segmentCandidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    let reading = composition.elements.map(\.text).joined()
    return makeOutput(for: composition, count: naturalSegmentCount(for: reading))
  }

  func realtimeCandidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> RealtimeConversionOutput {
    RealtimeConversionOutput(
      liveCandidate: ConverterCandidate(
        text: "東京に行く",
        consumingCount: composition.elements.count,
        sourceID: "live"
      ),
      candidates: [],
      pageSize: 0
    )
  }

  func setCompletedData(_ candidate: ConverterCandidate) {
    completedTexts.append(candidate.text)
  }

  func updateLearningData(_ candidate: ConverterCandidate) {
    updatedTexts.append(candidate.text)
  }

  func commitLearning() {
    commitLearningCount += 1
  }

  func forget(_ candidate: ConverterCandidate) {}
  func stopComposition() {}

  private func makeOutput(
    for composition: CompositionInput,
    count requestedCount: Int
  ) -> ConversionOutput {
    let reading = composition.elements.map(\.text).joined()
    let count = min(max(requestedCount, 1), composition.elements.count)
    let consumedReading = String(reading.prefix(count))
    requests.append(SegmentEditingRequest(
      remainingReading: reading,
      targetCount: composition.targetCount,
      consumedReading: consumedReading,
      leftContext: composition.leftContext
    ))
    let texts = candidateTexts(for: consumedReading)
    return ConversionOutput(
      candidates: texts.enumerated().map { index, text in
        ConverterCandidate(
          text: text,
          consumingCount: count,
          sourceID: "\(consumedReading)-\(index)"
        )
      },
      pageSize: texts.count
    )
  }

  private func naturalSegmentCount(for reading: String) -> Int {
    if reading.hasPrefix("とうきょう") { return 5 }
    if reading.hasPrefix("に") { return 1 }
    if reading.hasPrefix("いく") { return 2 }
    return max(reading.count, 1)
  }

  private func candidateTexts(for reading: String) -> [String] {
    switch reading {
    case "とうきょう":
      return ["東京", "東亰"]
    case "に":
      return ["に", "二"]
    case "いく":
      return ["行く", "往く"]
    case "とうきょうに":
      return ["東京に", "東亰に"]
    default:
      return [reading, "〈\(reading)〉"]
    }
  }
}

final class GrimodexSegmentEditingTests: XCTestCase {
  private let reading = "とうきょうにいく"

  func testStartConversionSnapshotsEverySegmentAndFocusMovesUtf8Caret() {
    let reducer = makeConvertedReducer()

    XCTAssertEqual(
      reducer.currentSnapshot().preedit,
      [
        PreeditSpan(text: "東京", style: .active),
        PreeditSpan(text: "に", style: .underline),
        PreeditSpan(text: "行く", style: .underline),
      ]
    )
    XCTAssertEqual(reducer.session.segments.map(\.inputCount), [5, 1, 2])
    XCTAssertEqual(
      reducer.currentSnapshot().caretUtf8ByteOffset,
      UInt32("東京".utf8.count)
    )

    let middle = reducer.reduce(.moveActiveSegment(1), requestID: "focus-middle")
    XCTAssertEqual(middle.status, .success)
    XCTAssertEqual(middle.snapshot.preedit.map(\.style), [.underline, .active, .underline])
    XCTAssertEqual(middle.snapshot.caretUtf8ByteOffset, UInt32("東京に".utf8.count))

    let right = reducer.reduce(.moveActiveSegment(1), requestID: "focus-right")
    XCTAssertEqual(right.status, .success)
    XCTAssertEqual(right.snapshot.preedit.map(\.style), [.underline, .underline, .active])
    XCTAssertEqual(right.snapshot.caretUtf8ByteOffset, UInt32("東京に行く".utf8.count))

    let left = reducer.reduce(.moveActiveSegment(-1), requestID: "focus-left")
    XCTAssertEqual(left.snapshot.preedit.map(\.style), [.underline, .active, .underline])
    XCTAssertEqual(left.snapshot.caretUtf8ByteOffset, UInt32("東京に".utf8.count))
  }

  func testCandidateChoiceChangesOnlyActiveSegmentAndSurvivesFocusRoundTrip() {
    let reducer = makeConvertedReducer()
    _ = reducer.reduce(.moveActiveSegment(1), requestID: "focus-middle")

    let selected = reducer.reduce(.navigateCandidate(1), requestID: "choose-middle")
    XCTAssertEqual(selected.snapshot.preedit.map(\.text), ["東京", "二", "行く"])
    XCTAssertEqual(reducer.session.segments.map { $0.candidates.selectedIndex }, [0, 1, 0])

    _ = reducer.reduce(.moveActiveSegment(1), requestID: "visit-right")
    let returned = reducer.reduce(.moveActiveSegment(-1), requestID: "return-middle")
    XCTAssertEqual(returned.snapshot.preedit.map(\.text), ["東京", "二", "行く"])
    XCTAssertEqual(returned.snapshot.candidateWindow.selectedIndex, 1)
  }

  func testSelectCandidateChangesSelectionWithoutCommitting() throws {
    let converter = SegmentEditingFixtureConverter()
    let reducer = makeConvertedReducer(converter: converter)
    let window = reducer.currentSnapshot().candidateWindow
    let alternate = try XCTUnwrap(window.items.dropFirst().first)

    let selected = reducer.reduce(
      .selectCandidate(id: alternate.id, generation: window.generation),
      requestID: "select-by-id"
    )

    XCTAssertEqual(selected.status, .success)
    XCTAssertEqual(selected.snapshot.phase, .selecting)
    XCTAssertEqual(selected.snapshot.preedit.map(\.text), ["東亰", "に", "行く"])
    XCTAssertTrue(selected.snapshot.effects.isEmpty)
    XCTAssertFalse(reducer.session.composingText.isEmpty)
    XCTAssertEqual(reducer.session.context.leftContext, "")
    XCTAssertTrue(converter.completedTexts.isEmpty)
    XCTAssertTrue(converter.updatedTexts.isEmpty)
    XCTAssertEqual(converter.commitLearningCount, 0)
  }

  func testShiftResizeRepartitionsTheWholeReadingWithoutOverlapOrGap() {
    let converter = SegmentEditingFixtureConverter()
    let reducer = makeConvertedReducer(converter: converter)

    let expanded = reducer.reduce(.resizeSegment(1), requestID: "expand-first")
    XCTAssertEqual(expanded.status, .success)
    XCTAssertEqual(reducer.session.segments.map(\.inputCount), [6, 2])
    assertLatestPartition(
      converter.requests,
      counts: [6, 2],
      expectedSlices: ["とうきょうに", "いく"]
    )

    let restored = reducer.reduce(.resizeSegment(-1), requestID: "shrink-first")
    XCTAssertEqual(restored.status, .success)
    XCTAssertEqual(reducer.session.segments.map(\.inputCount), [5, 1, 2])
    assertLatestPartition(
      converter.requests,
      counts: [5, 1, 2],
      expectedSlices: ["とうきょう", "に", "いく"]
    )
  }

  func testResizeReusesPrefixAndOnlyRebuildsActiveSuffix() throws {
    let converter = SegmentEditingFixtureConverter()
    let reducer = makeConvertedReducer(converter: converter)
    let firstWindow = reducer.currentSnapshot().candidateWindow
    let alternateFirst = try XCTUnwrap(firstWindow.items.dropFirst().first)
    _ = reducer.reduce(
      .selectCandidate(id: alternateFirst.id, generation: firstWindow.generation),
      requestID: "select-prefix"
    )
    _ = reducer.reduce(.moveActiveSegment(1), requestID: "focus-middle")
    let preservedPrefix = try XCTUnwrap(reducer.session.segments.first)
    converter.requests.removeAll()

    let resized = reducer.reduce(.resizeSegment(1), requestID: "expand-middle")

    XCTAssertEqual(resized.status, .success)
    XCTAssertEqual(reducer.session.activeSegmentIndex, 1)
    XCTAssertEqual(reducer.session.segments.map(\.inputCount), [5, 2, 1])
    XCTAssertEqual(reducer.session.segments.first, preservedPrefix)
    XCTAssertEqual(
      converter.requests,
      [
        SegmentEditingRequest(
          remainingReading: "にいく",
          targetCount: 2,
          consumedReading: "にい",
          leftContext: "東亰"
        ),
        SegmentEditingRequest(
          remainingReading: "く",
          targetCount: nil,
          consumedReading: "く",
          leftContext: "東亰にい"
        ),
      ]
    )
    XCTAssertEqual(resized.snapshot.preedit.map(\.text), ["東亰", "にい", "く"])
    XCTAssertEqual(resized.snapshot.preedit.map(\.style), [.underline, .active, .underline])
  }

  func testResizePreservesMatchingSelectionInRebuiltSuffix() {
    let converter = SegmentEditingFixtureConverter()
    let reducer = makeConvertedReducer(converter: converter)
    _ = reducer.reduce(.moveActiveSegment(1), requestID: "focus-middle")
    _ = reducer.reduce(.moveActiveSegment(1), requestID: "focus-last")
    _ = reducer.reduce(.navigateCandidate(1), requestID: "choose-last")
    _ = reducer.reduce(.moveActiveSegment(-2), requestID: "focus-first")

    let resized = reducer.reduce(.resizeSegment(1), requestID: "expand-first")

    XCTAssertEqual(resized.status, .success)
    XCTAssertEqual(reducer.session.activeSegmentIndex, 0)
    XCTAssertEqual(reducer.session.segments.map(\.inputCount), [6, 2])
    XCTAssertEqual(reducer.session.segments.map { $0.candidates.selectedIndex }, [0, 1])
    XCTAssertEqual(resized.snapshot.preedit.map(\.text), ["東京に", "往く"])
  }

  func testCommitAllEmitsOneJoinedEffectAndLearnsSegmentsLeftToRightOnce() {
    let converter = SegmentEditingFixtureConverter()
    let reducer = makeConvertedReducer(converter: converter)
    _ = reducer.reduce(.moveActiveSegment(1), requestID: "focus-middle")
    _ = reducer.reduce(.navigateCandidate(1), requestID: "choose-middle")
    _ = reducer.reduce(.moveActiveSegment(1), requestID: "focus-last")
    _ = reducer.reduce(.navigateCandidate(1), requestID: "choose-last")

    let committed = reducer.reduce(.commitAll, requestID: "commit-all")

    XCTAssertEqual(
      committed.snapshot.effects,
      [.commitText(effectID: 1, text: "東京二往く")]
    )
    XCTAssertEqual(converter.completedTexts, ["東京", "二", "往く"])
    XCTAssertEqual(converter.updatedTexts, ["東京", "二", "往く"])
    XCTAssertEqual(converter.commitLearningCount, 1)
    XCTAssertEqual(reducer.session.context.leftContext, "東京二往く")
    XCTAssertEqual(committed.snapshot.phase, .idle)
    XCTAssertTrue(committed.snapshot.preedit.isEmpty)
  }

  func testTwoStageEscapeReturnsFromSegmentSelectionToRawReading() {
    let reducer = makeConvertedReducer()
    _ = reducer.reduce(.moveActiveSegment(1), requestID: "enter-selection")

    let preview = reducer.reduce(.cancel, requestID: "escape-preview")
    XCTAssertEqual(preview.snapshot.phase, .previewing)
    XCTAssertEqual(preview.snapshot.preedit.map(\.text), ["東京", "に", "行く"])

    let raw = reducer.reduce(.cancel, requestID: "escape-raw")
    XCTAssertEqual(raw.snapshot.phase, .composing)
    XCTAssertEqual(raw.snapshot.preedit, [PreeditSpan(text: reading, style: .underline)])
    XCTAssertEqual(raw.snapshot.caretUtf8ByteOffset, UInt32(reading.utf8.count))
    XCTAssertTrue(reducer.session.segments.isEmpty)
    XCTAssertNil(reducer.session.activeSegmentIndex)
  }

  func testLeftDuringLiveAutoConversionKeepsCharacterCursorEditing() {
    let converter = SegmentEditingFixtureConverter()
    var session = CompositionSession()
    session.policy.autoConvertMode = .always
    let reducer = ImeReducer(session: session, converter: converter)
    let live = reducer.reduce(.insertText(reading), requestID: "insert-live")
    XCTAssertEqual(live.snapshot.preedit, [PreeditSpan(text: "東京に行く", style: .active)])

    let moved = reducer.reduce(.moveCursor(-1), requestID: "left-live")

    XCTAssertEqual(moved.status, .success)
    XCTAssertEqual(moved.snapshot.phase, .composing)
    XCTAssertEqual(moved.snapshot.preedit, [PreeditSpan(text: reading, style: .underline)])
    XCTAssertEqual(
      moved.snapshot.caretUtf8ByteOffset,
      UInt32(String(reading.dropLast()).utf8.count)
    )
    XCTAssertEqual(reducer.session.composingText.cursor, reading.count - 1)
    XCTAssertNil(reducer.session.activeSegmentIndex)
    XCTAssertTrue(reducer.session.segments.isEmpty)
  }

  private func makeConvertedReducer(
    converter: SegmentEditingFixtureConverter = SegmentEditingFixtureConverter()
  ) -> ImeReducer {
    let reducer = ImeReducer(converter: converter)
    _ = reducer.reduce(.insertText(reading), requestID: "insert")
    let converted = reducer.reduce(.startConversion, requestID: "start")
    XCTAssertEqual(converted.status, .success)
    XCTAssertEqual(converted.snapshot.phase, .previewing)
    return reducer
  }

  private func assertLatestPartition(
    _ requests: [SegmentEditingRequest],
    counts: [Int],
    expectedSlices: [String],
    file: StaticString = #filePath,
    line: UInt = #line
  ) {
    let latest = Array(requests.suffix(counts.count))
    XCTAssertEqual(
      latest.map(\.consumedReading),
      expectedSlices,
      file: file,
      line: line
    )
    XCTAssertEqual(
      latest.map(\.consumedReading).joined(),
      reading,
      "segment slices must cover every input element exactly once",
      file: file,
      line: line
    )
    XCTAssertEqual(
      latest.map { $0.consumedReading.count },
      counts,
      file: file,
      line: line
    )
  }
}
