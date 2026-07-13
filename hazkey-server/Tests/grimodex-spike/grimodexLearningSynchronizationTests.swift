import XCTest

@testable import hazkey_server

private final class LearningSynchronizationProbe: KanaKanjiConverting {
  var candidateRequests = 0
  var predictionRequests = 0
  var commits = 0
  var forgets = 0

  func candidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    candidateRequests += 1
    return ConversionOutput(candidates: [], pageSize: 0)
  }

  func predictions(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    predictionRequests += 1
    return ConversionOutput(candidates: [], pageSize: 0)
  }

  func setCompletedData(_ candidate: ConverterCandidate) {}
  func updateLearningData(_ candidate: ConverterCandidate) {}
  func commitLearning() { commits += 1 }
  func forget(_ candidate: ConverterCandidate) { forgets += 1 }
  func stopComposition() {}
}

final class GrimodexLearningSynchronizationTests: XCTestCase {
  func testLongLivedConvertersReloadOnceAfterAnotherSessionMutatesLearning() throws {
    let store = HazkeyLearningRevisionStore()
    let probeA = LearningSynchronizationProbe()
    let probeB = LearningSynchronizationProbe()
    let converterA = LearningSynchronizedKanaKanjiConverter(
      base: probeA,
      revisionStore: store
    )
    let converterB = LearningSynchronizedKanaKanjiConverter(
      base: probeB,
      revisionStore: store
    )
    let input = CompositionInput(
      elements: [CompositionElement(text: "か")],
      cursor: 1,
      leftContext: ""
    )

    _ = try converterA.candidates(for: input, options: .default)
    _ = try converterB.candidates(for: input, options: .default)
    XCTAssertEqual(probeA.commits, 0)
    XCTAssertEqual(probeB.commits, 0)

    converterA.commitLearning()
    XCTAssertEqual(store.current(), 1)
    XCTAssertEqual(probeA.commits, 1)

    _ = try converterA.candidates(for: input, options: .default)
    XCTAssertEqual(probeA.commits, 1, "the committing session is already current")

    _ = try converterB.predictions(for: input, options: .default)
    XCTAssertEqual(probeB.commits, 1, "the other session must invalidate its cache")
    _ = try converterB.predictions(for: input, options: .default)
    XCTAssertEqual(probeB.commits, 1, "one revision must be observed only once")

    converterB.forget(ConverterCandidate(text: "仮", consumingCount: 1))
    XCTAssertEqual(probeB.forgets, 1)
    XCTAssertEqual(store.current(), 2)

    _ = try converterA.candidates(for: input, options: .default)
    XCTAssertEqual(probeA.commits, 2, "forget must be visible to other sessions")
  }
}
