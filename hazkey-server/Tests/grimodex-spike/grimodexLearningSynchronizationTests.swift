import Dispatch
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

private final class BlockingLearningSynchronizationProbe: KanaKanjiConverting {
  let entered: DispatchSemaphore?
  let release: DispatchSemaphore?
  let committed: DispatchSemaphore?

  init(
    entered: DispatchSemaphore? = nil,
    release: DispatchSemaphore? = nil,
    committed: DispatchSemaphore? = nil
  ) {
    self.entered = entered
    self.release = release
    self.committed = committed
  }

  func candidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    entered?.signal()
    if let release {
      _ = release.wait(timeout: .now() + 2)
    }
    return ConversionOutput(candidates: [], pageSize: 0)
  }

  func setCompletedData(_ candidate: ConverterCandidate) {}
  func updateLearningData(_ candidate: ConverterCandidate) {}
  func commitLearning() { committed?.signal() }
  func forget(_ candidate: ConverterCandidate) {}
  func stopComposition() {}
}

private final class LearningConverterBox: @unchecked Sendable {
  let converter: LearningSynchronizedKanaKanjiConverter

  init(_ converter: LearningSynchronizedKanaKanjiConverter) {
    self.converter = converter
  }
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

  func testSharedExecutionGateSerializesReadAndCommitAcrossSessions() {
    let gate = HazkeyConverterExecutionGate()
    let store = HazkeyLearningRevisionStore()
    let readerEntered = DispatchSemaphore(value: 0)
    let releaseReader = DispatchSemaphore(value: 0)
    let readerDone = DispatchSemaphore(value: 0)
    let commitAttempted = DispatchSemaphore(value: 0)
    let commitDone = DispatchSemaphore(value: 0)
    let reader = LearningConverterBox(LearningSynchronizedKanaKanjiConverter(
      base: BlockingLearningSynchronizationProbe(
        entered: readerEntered,
        release: releaseReader
      ),
      revisionStore: store,
      executionGate: gate
    ))
    let writer = LearningConverterBox(LearningSynchronizedKanaKanjiConverter(
      base: BlockingLearningSynchronizationProbe(committed: commitDone),
      revisionStore: store,
      executionGate: gate
    ))
    let input = CompositionInput(
      elements: [CompositionElement(text: "か")],
      cursor: 1,
      leftContext: ""
    )

    DispatchQueue.global().async {
      _ = try? reader.converter.candidates(for: input, options: .default)
      readerDone.signal()
    }
    XCTAssertEqual(readerEntered.wait(timeout: .now() + 1), .success)
    DispatchQueue.global().async {
      commitAttempted.signal()
      writer.converter.commitLearning()
    }
    XCTAssertEqual(commitAttempted.wait(timeout: .now() + 1), .success)
    XCTAssertEqual(
      commitDone.wait(timeout: .now() + 0.05),
      .timedOut,
      "another session must not mutate shared history during conversion"
    )

    releaseReader.signal()
    XCTAssertEqual(readerDone.wait(timeout: .now() + 1), .success)
    XCTAssertEqual(commitDone.wait(timeout: .now() + 1), .success)
  }
}
