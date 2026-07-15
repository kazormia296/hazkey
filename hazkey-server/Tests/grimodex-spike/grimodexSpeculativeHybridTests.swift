import Foundation
import XCTest

@testable import hazkey_server

private final class ManualSpeculativeExecutor: SpeculativeWorkExecuting, @unchecked Sendable {
  typealias Work = @Sendable () -> Void

  private let lock = NSLock()
  private var work: [Work] = []

  var pendingCount: Int {
    lock.lock()
    defer { lock.unlock() }
    return work.count
  }

  func submit(_ operation: @escaping Work) {
    lock.lock()
    work.append(operation)
    lock.unlock()
  }

  func run(at index: Int = 0) {
    let operation: Work
    lock.lock()
    precondition(work.indices.contains(index), "no speculative work at index \(index)")
    operation = work.remove(at: index)
    lock.unlock()
    operation()
  }
}

private final class HybridTestRevisionStore: @unchecked Sendable {
  private let lock = NSLock()
  private var revision: UInt64 = 0

  func current() -> UInt64 {
    lock.lock()
    defer { lock.unlock() }
    return revision
  }

  func advance() {
    lock.lock()
    revision &+= 1
    lock.unlock()
  }
}

private final class RecordingHybridChildConverter: KanaKanjiConverting {
  let supportsSegmentEditing = true

  var outputProvider: (CompositionInput) throws -> ConversionOutput
  private(set) var segmentRequests: [CompositionInput] = []
  private(set) var completedSourceIDs: [String?] = []
  private(set) var updatedSourceIDs: [String?] = []
  private(set) var stagedSourceIDs: [String?] = []
  private(set) var committedTokens: [ConverterLearningToken] = []
  private(set) var discardedTokens: [ConverterLearningToken] = []
  private(set) var forgottenSourceIDs: [String?] = []
  private(set) var commitLearningCount = 0
  private(set) var stopCount = 0
  private(set) var purgeCount = 0

  init(
    candidates: [ConverterCandidate],
    pageSize: Int? = nil
  ) {
    let output = ConversionOutput(
      candidates: candidates,
      pageSize: pageSize ?? candidates.count
    )
    self.outputProvider = { _ in output }
  }

  func candidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    try segmentCandidates(for: composition, options: options)
  }

  func segmentCandidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    segmentRequests.append(composition)
    return try outputProvider(composition)
  }

  func realtimeCandidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> RealtimeConversionOutput {
    let output = try segmentCandidates(for: composition, options: options)
    return RealtimeConversionOutput(
      liveCandidate: output.candidates.first,
      candidates: output.candidates,
      pageSize: output.pageSize
    )
  }

  func setCompletedData(_ candidate: ConverterCandidate) {
    completedSourceIDs.append(candidate.sourceID)
  }

  func updateLearningData(_ candidate: ConverterCandidate) {
    updatedSourceIDs.append(candidate.sourceID)
  }

  func commitLearning() {
    commitLearningCount += 1
  }

  func stageLearning(
    candidate: ConverterCandidate,
    reading: String
  ) -> ConverterLearningToken? {
    stagedSourceIDs.append(candidate.sourceID)
    guard let sourceID = candidate.sourceID else { return nil }
    return ConverterLearningToken(rawValue: "token-\(sourceID)-\(reading)")
  }

  func commitStagedLearning(_ token: ConverterLearningToken) {
    committedTokens.append(token)
  }

  func discardStagedLearning(_ token: ConverterLearningToken) {
    discardedTokens.append(token)
  }

  func forget(_ candidate: ConverterCandidate) {
    forgottenSourceIDs.append(candidate.sourceID)
  }

  func stopComposition() {
    stopCount += 1
  }

  func purgeSensitiveState() {
    purgeCount += 1
  }
}

final class GrimodexSpeculativeHybridTests: XCTestCase {
  func testFormalConversionDoesNotRunOrWaitForPendingHazkeyWork() throws {
    let fixture = makeFixture()
    let context = makeContext(revision: 1, text: "かな")

    fixture.hybrid.prepareSpeculativeConversion(context)
    XCTAssertEqual(fixture.executor.pendingCount, 1)
    XCTAssertTrue(fixture.hazkey.segmentRequests.isEmpty)

    fixture.hybrid.lockCandidateOrder(for: context.revision)
    let output = try fixture.hybrid.segmentCandidates(
      for: context.input,
      options: context.options
    )

    XCTAssertEqual(output.candidates.map(\.text), ["仮名-Mozc"])
    XCTAssertEqual(fixture.mozc.segmentRequests.count, 1)
    XCTAssertTrue(fixture.hazkey.segmentRequests.isEmpty)
    XCTAssertEqual(
      fixture.executor.pendingCount,
      1,
      "formal conversion must never execute or join pending Hazkey work"
    )
  }

  func testReadyExactRevisionMergesHazkeyAfterMozcTop1() throws {
    let fixture = makeFixture()
    let context = makeContext(revision: 7, text: "かな")

    fixture.hybrid.prepareSpeculativeConversion(context)
    fixture.executor.run()
    fixture.hybrid.lockCandidateOrder(for: context.revision)
    let output = try fixture.hybrid.segmentCandidates(
      for: context.input,
      options: context.options
    )

    XCTAssertEqual(output.candidates.map(\.text), ["仮名-Mozc", "仮名-Hazkey"])
    XCTAssertFalse(output.candidates[0].isLearnable)
    XCTAssertTrue(output.candidates[1].isLearnable)
    XCTAssertEqual(fixture.hazkey.segmentRequests, [context.input])
    XCTAssertEqual(fixture.hybrid.diagnosticsSnapshot().formalReadyConsumed, 1)
    XCTAssertEqual(fixture.hybrid.diagnosticsSnapshot().mergedRequests, 1)
  }

  func testHazkeyCompletionAfterOrderLockIsDiscarded() throws {
    let fixture = makeFixture()
    let context = makeContext(revision: 3, text: "かな")

    fixture.hybrid.prepareSpeculativeConversion(context)
    fixture.hybrid.lockCandidateOrder(for: context.revision)
    fixture.executor.run()

    let first = try fixture.hybrid.segmentCandidates(
      for: context.input,
      options: context.options
    )
    let second = try fixture.hybrid.segmentCandidates(
      for: context.input,
      options: context.options
    )

    XCTAssertEqual(first.candidates.map(\.text), ["仮名-Mozc"])
    XCTAssertEqual(second.candidates.map(\.text), ["仮名-Mozc"])
    XCTAssertEqual(fixture.hybrid.diagnosticsSnapshot().formalDeadlineMiss, 1)
    XCTAssertEqual(fixture.hybrid.diagnosticsSnapshot().staleResultDiscarded, 1)
  }

  func testSpaceDuringHazkeyRequestFallsBackToMozcAndDiscardsLateResult() throws {
    let mozc = RecordingHybridChildConverter(candidates: [
      hybridCandidate("仮-Mozc", count: 1, sourceID: "mozc-source")
    ])
    let hazkey = RecordingHybridChildConverter(candidates: [])
    let executor = ManualSpeculativeExecutor()
    let context = makeContext(revision: 4, text: "かなじ")
    var requestCount = 0
    var hybrid: MozcFirstHybridKanaKanjiConverter!
    hazkey.outputProvider = { _ in
      requestCount += 1
      // Model Space arriving while the only speculative Hazkey request is in
      // flight. A result is publishable only after this call and its shared
      // execution gate have both completed.
      hybrid.lockCandidateOrder(for: context.revision)
      return ConversionOutput(
        candidates: [
          hybridCandidate(
            "仮-Hazkey",
            count: 1,
            sourceID: "hazkey-\(requestCount)"
          )
        ],
        pageSize: 1
      )
    }
    hybrid = MozcFirstHybridKanaKanjiConverter(
      mozc: mozc,
      hazkey: hazkey,
      executor: executor,
      promotionPolicy: .preserveMozcTop1
    )

    hybrid.prepareSpeculativeConversion(context)
    executor.run()
    let output = try hybrid.segmentCandidates(
      for: context.input,
      options: context.options
    )

    XCTAssertEqual(output.candidates.map(\.text), ["仮-Mozc"])
    XCTAssertEqual(requestCount, 1, "the spike prepares only the first Hazkey segment")
    XCTAssertEqual(hybrid.diagnosticsSnapshot().formalReadyConsumed, 0)
    XCTAssertEqual(hybrid.diagnosticsSnapshot().formalDeadlineMiss, 1)
    XCTAssertEqual(hybrid.diagnosticsSnapshot().lateCompletionDiscarded, 1)
  }

  func testSecurePurgeWaitsForActiveWorkerToDropItsLocalResult() throws {
    let fixture = makeFixture()
    let context = makeContext(revision: 5, text: "かな")
    let entered = DispatchSemaphore(value: 0)
    let release = DispatchSemaphore(value: 0)
    let workerDone = DispatchSemaphore(value: 0)
    let purgeDone = DispatchSemaphore(value: 0)
    fixture.hazkey.outputProvider = { input in
      entered.signal()
      _ = release.wait(timeout: .now() + 2)
      return ConversionOutput(
        candidates: [
          hybridCandidate(
            "仮名-Hazkey",
            count: input.elements.count,
            sourceID: "hazkey-sensitive"
          )
        ],
        pageSize: 1
      )
    }

    let hybrid = fixture.hybrid
    let executor = fixture.executor
    hybrid.prepareSpeculativeConversion(context)
    DispatchQueue.global().async {
      executor.run()
      workerDone.signal()
    }
    XCTAssertEqual(entered.wait(timeout: .now() + 1), .success)
    DispatchQueue.global().async {
      hybrid.purgeSensitiveState()
      purgeDone.signal()
    }
    XCTAssertEqual(
      purgeDone.wait(timeout: .now() + 0.05),
      .timedOut,
      "secure purge must fence the worker's local plaintext result"
    )

    release.signal()
    XCTAssertEqual(workerDone.wait(timeout: .now() + 1), .success)
    XCTAssertEqual(purgeDone.wait(timeout: .now() + 1), .success)
    hybrid.lockCandidateOrder(for: context.revision)
    let output = try hybrid.segmentCandidates(
      for: context.input,
      options: context.options
    )
    XCTAssertEqual(output.candidates.map(\.text), ["仮名-Mozc"])
    XCTAssertEqual(fixture.hazkey.purgeCount, 1)
  }

  func testReadyInvalidationIsCountedExactlyOnce() {
    let fixture = makeFixture()
    let context = makeContext(revision: 6, text: "かな")
    fixture.hybrid.prepareSpeculativeConversion(context)
    fixture.executor.run()

    fixture.hybrid.invalidateSpeculativeConversion(reason: .edit)
    fixture.hybrid.invalidateSpeculativeConversion(reason: .cursorMove)

    let diagnostics = fixture.hybrid.diagnosticsSnapshot()
    XCTAssertEqual(diagnostics.readyDiscarded, 1)
    XCTAssertEqual(diagnostics.staleResultDiscarded, 1)
  }

  func testInvalidatedRevisionCannotPopulateNewerRevision() throws {
    let fixture = makeFixture()
    let old = makeContext(revision: 10, text: "かな")
    let current = makeContext(revision: 11, text: "かなに")

    fixture.hybrid.prepareSpeculativeConversion(old)
    fixture.hybrid.invalidateSpeculativeConversion(reason: .edit)
    fixture.hybrid.prepareSpeculativeConversion(current)
    XCTAssertEqual(fixture.executor.pendingCount, 2)

    fixture.executor.run(at: 0)
    fixture.executor.run(at: 0)
    fixture.hybrid.lockCandidateOrder(for: current.revision)
    let output = try fixture.hybrid.segmentCandidates(
      for: current.input,
      options: current.options
    )

    XCTAssertEqual(output.candidates.map(\.text), ["仮名-Mozc", "仮名-Hazkey"])
    XCTAssertEqual(fixture.hazkey.segmentRequests.first, current.input)
    XCTAssertFalse(fixture.hazkey.segmentRequests.contains(old.input))
    XCTAssertEqual(fixture.hybrid.diagnosticsSnapshot().staleResultDiscarded, 1)
    XCTAssertEqual(fixture.hybrid.diagnosticsSnapshot().pendingCancelled, 1)
  }

  func testABAReadingDoesNotReuseOldRevisionResult() throws {
    let fixture = makeFixture()
    let old = makeContext(revision: 20, text: "かな")
    let middle = makeContext(revision: 21, text: "かなに")
    let current = makeContext(revision: 22, text: "かな")

    fixture.hybrid.prepareSpeculativeConversion(old)
    fixture.hybrid.invalidateSpeculativeConversion(reason: .edit)
    fixture.hybrid.prepareSpeculativeConversion(middle)
    fixture.hybrid.invalidateSpeculativeConversion(reason: .edit)
    fixture.hybrid.prepareSpeculativeConversion(current)

    fixture.executor.run(at: 0)
    fixture.hybrid.lockCandidateOrder(for: current.revision)
    let output = try fixture.hybrid.segmentCandidates(
      for: current.input,
      options: current.options
    )

    XCTAssertEqual(output.candidates.map(\.text), ["仮名-Mozc"])
    XCTAssertEqual(
      fixture.executor.pendingCount,
      2,
      "the identical current reading still owns independent speculative work"
    )
  }

  func testReadyHazkeyCandidateWithDifferentBoundaryIsNotMerged() throws {
    let fixture = makeFixture(
      mozcCandidates: [
        hybridCandidate("仮名-Mozc", count: 2, sourceID: "mozc")
      ],
      hazkeyCandidates: [
        hybridCandidate("仮-Hazkey", count: 1, sourceID: "hazkey")
      ]
    )
    let context = makeContext(revision: 30, text: "かな")

    fixture.hybrid.prepareSpeculativeConversion(context)
    fixture.executor.run()
    fixture.hybrid.lockCandidateOrder(for: context.revision)
    let output = try fixture.hybrid.segmentCandidates(
      for: context.input,
      options: context.options
    )

    XCTAssertEqual(output.candidates.map(\.text), ["仮名-Mozc"])
  }

  func testReadyPlanFromOldLearningRevisionFallsBackToMozc() throws {
    let revisionStore = HybridTestRevisionStore()
    let fixture = makeFixture(
      learningRevisionProvider: { revisionStore.current() }
    )
    let context = makeContext(revision: 31, text: "かな")

    fixture.hybrid.prepareSpeculativeConversion(context)
    fixture.executor.run()
    revisionStore.advance()
    fixture.hybrid.lockCandidateOrder(for: context.revision)
    let output = try fixture.hybrid.segmentCandidates(
      for: context.input,
      options: context.options
    )

    XCTAssertEqual(output.candidates.map(\.text), ["仮名-Mozc"])
    XCTAssertEqual(
      fixture.hybrid.diagnosticsSnapshot().learningRevisionMismatch,
      1
    )
  }

  func testMergedCandidateLearningRoutesOnlyToItsOriginBackend() throws {
    let fixture = makeFixture()
    let context = makeContext(revision: 40, text: "かな")
    fixture.hybrid.prepareSpeculativeConversion(context)
    fixture.executor.run()
    fixture.hybrid.lockCandidateOrder(for: context.revision)
    let output = try fixture.hybrid.segmentCandidates(
      for: context.input,
      options: context.options
    )
    let mozc = try XCTUnwrap(output.candidates.first)
    let hazkey = try XCTUnwrap(output.candidates.dropFirst().first)

    XCTAssertFalse(mozc.isLearnable)
    XCTAssertTrue(hazkey.isLearnable)
    XCTAssertTrue(fixture.mozc.completedSourceIDs.isEmpty)
    XCTAssertTrue(fixture.mozc.updatedSourceIDs.isEmpty)
    XCTAssertTrue(fixture.mozc.stagedSourceIDs.isEmpty)
    XCTAssertTrue(fixture.mozc.forgottenSourceIDs.isEmpty)
    XCTAssertTrue(fixture.hazkey.completedSourceIDs.isEmpty)
    XCTAssertTrue(fixture.hazkey.updatedSourceIDs.isEmpty)
    XCTAssertTrue(fixture.hazkey.stagedSourceIDs.isEmpty)
    XCTAssertTrue(fixture.hazkey.forgottenSourceIDs.isEmpty)
    XCTAssertEqual(fixture.hazkey.commitLearningCount, 0)

    let token = try XCTUnwrap(
      fixture.hybrid.stageLearning(candidate: hazkey, reading: "かな")
    )
    fixture.hybrid.setCompletedData(hazkey)
    fixture.hybrid.updateLearningData(hazkey)
    fixture.hybrid.forget(hazkey)
    fixture.hybrid.commitStagedLearning(token)
    fixture.hybrid.commitLearning()

    XCTAssertTrue(fixture.mozc.completedSourceIDs.isEmpty)
    XCTAssertTrue(fixture.mozc.updatedSourceIDs.isEmpty)
    XCTAssertTrue(fixture.mozc.stagedSourceIDs.isEmpty)
    XCTAssertTrue(fixture.mozc.forgottenSourceIDs.isEmpty)
    XCTAssertEqual(fixture.hazkey.completedSourceIDs, ["hazkey-source"])
    XCTAssertEqual(fixture.hazkey.updatedSourceIDs, ["hazkey-source"])
    XCTAssertEqual(fixture.hazkey.stagedSourceIDs, ["hazkey-source"])
    XCTAssertEqual(fixture.hazkey.forgottenSourceIDs, ["hazkey-source"])
    XCTAssertEqual(fixture.hazkey.committedTokens.count, 1)
    XCTAssertEqual(fixture.hazkey.commitLearningCount, 1)
  }

  func testDiscardedHybridLearningTokenReturnsOnlyToHazkey() throws {
    let fixture = makeFixture()
    let context = makeContext(revision: 41, text: "かな")
    fixture.hybrid.prepareSpeculativeConversion(context)
    fixture.executor.run()
    fixture.hybrid.lockCandidateOrder(for: context.revision)
    let output = try fixture.hybrid.segmentCandidates(
      for: context.input,
      options: context.options
    )
    let hazkey = try XCTUnwrap(output.candidates.dropFirst().first)
    let token = try XCTUnwrap(
      fixture.hybrid.stageLearning(candidate: hazkey, reading: "かな")
    )

    fixture.hybrid.discardStagedLearning(token)

    XCTAssertEqual(fixture.hazkey.discardedTokens.count, 1)
    XCTAssertTrue(fixture.mozc.discardedTokens.isEmpty)
  }

  func testHazkeyFallbackWithoutLearningRouteIsPublishedAsUnlearnable() throws {
    let fixture = makeFixture(
      hazkeyCandidates: [
        ConverterCandidate(
          text: "かな",
          annotation: "読み",
          consumingCount: 2
        )
      ]
    )
    let context = makeContext(revision: 42, text: "かな")
    fixture.hybrid.prepareSpeculativeConversion(context)
    fixture.executor.run()
    fixture.hybrid.lockCandidateOrder(for: context.revision)

    let output = try fixture.hybrid.segmentCandidates(
      for: context.input,
      options: context.options
    )
    let fallback = try XCTUnwrap(output.candidates.dropFirst().first)

    XCTAssertEqual(fallback.text, "かな")
    XCTAssertFalse(fallback.isLearnable)
  }

  private func makeFixture(
    mozcCandidates: [ConverterCandidate] = [
      hybridCandidate("仮名-Mozc", count: 2, sourceID: "mozc-source")
    ],
    hazkeyCandidates: [ConverterCandidate] = [
      hybridCandidate("仮名-Hazkey", count: 2, sourceID: "hazkey-source")
    ],
    learningRevisionProvider: @escaping @Sendable () -> UInt64 = { 0 }
  ) -> (
    hybrid: MozcFirstHybridKanaKanjiConverter,
    mozc: RecordingHybridChildConverter,
    hazkey: RecordingHybridChildConverter,
    executor: ManualSpeculativeExecutor
  ) {
    let mozc = RecordingHybridChildConverter(candidates: mozcCandidates)
    let hazkey = RecordingHybridChildConverter(candidates: hazkeyCandidates)
    let executor = ManualSpeculativeExecutor()
    return (
      MozcFirstHybridKanaKanjiConverter(
        mozc: mozc,
        hazkey: hazkey,
        executor: executor,
        promotionPolicy: .preserveMozcTop1,
        learningRevisionProvider: learningRevisionProvider
      ),
      mozc,
      hazkey,
      executor
    )
  }

  private func makeContext(
    revision: UInt64,
    text: String
  ) -> SpeculativeConversionContext {
    SpeculativeConversionContext(
      revision: CompositionRevision(rawValue: revision),
      input: directInput(text),
      options: .default,
      projectRevision: 17
    )
  }

  private func directInput(_ text: String) -> CompositionInput {
    CompositionInput(
      elements: text.map {
        CompositionElement(text: String($0), inputStyle: .direct)
      },
      cursor: text.count,
      leftContext: ""
    )
  }
}

private func hybridCandidate(
  _ text: String,
  count: Int,
  sourceID: String
) -> ConverterCandidate {
  ConverterCandidate(
    text: text,
    consumingCount: count,
    sourceID: sourceID,
    provenance: .standard
  )
}
