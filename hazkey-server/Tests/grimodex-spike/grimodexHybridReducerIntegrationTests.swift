import Foundation
import XCTest

@testable import hazkey_server

private enum HybridReducerRecordingEvent: Equatable {
  case invalidate(SpeculationInvalidationReason)
  case prepare(CompositionRevision)
  case lock(CompositionRevision)
  case realtime
  case candidates
}

private final class HybridReducerRecordingConverter: KanaKanjiConverting {
  let supportsSegmentEditing = true

  private(set) var events: [HybridReducerRecordingEvent] = []
  private(set) var preparedContexts: [SpeculativeConversionContext] = []
  private(set) var invalidationReasons: [SpeculationInvalidationReason] = []
  private(set) var lockedRevisions: [CompositionRevision] = []
  private(set) var realtimeRequestCount = 0

  func candidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    events.append(.candidates)
    return output(for: composition)
  }

  func realtimeCandidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> RealtimeConversionOutput {
    events.append(.realtime)
    realtimeRequestCount += 1
    let output = output(for: composition)
    return RealtimeConversionOutput(
      liveCandidate: output.candidates.first,
      candidates: output.candidates,
      pageSize: output.pageSize
    )
  }

  func prepareSpeculativeConversion(_ context: SpeculativeConversionContext) {
    events.append(.prepare(context.revision))
    preparedContexts.append(context)
  }

  func invalidateSpeculativeConversion(reason: SpeculationInvalidationReason) {
    events.append(.invalidate(reason))
    invalidationReasons.append(reason)
  }

  func lockCandidateOrder(for revision: CompositionRevision) {
    events.append(.lock(revision))
    lockedRevisions.append(revision)
  }

  func setCompletedData(_ candidate: ConverterCandidate) {}
  func updateLearningData(_ candidate: ConverterCandidate) {}
  func commitLearning() {}
  func forget(_ candidate: ConverterCandidate) {}
  func stopComposition() {}

  func resetInvalidations() {
    invalidationReasons.removeAll()
  }

  private func output(for composition: CompositionInput) -> ConversionOutput {
    let count = min(
      max(composition.targetCount ?? composition.elements.count, 1),
      composition.elements.count
    )
    let reading = composition.elements.prefix(count).map(\.text).joined()
    return ConversionOutput(
      candidates: [
        ConverterCandidate(
          text: "Mozc:\(reading)",
          consumingCount: count,
          sourceID: "mozc-first-\(count)"
        ),
        ConverterCandidate(
          text: "Hazkey:\(reading)",
          consumingCount: count,
          sourceID: "hazkey-second-\(count)"
        ),
        ConverterCandidate(
          text: reading,
          consumingCount: count,
          sourceID: "reading-\(count)"
        ),
      ],
      pageSize: 3
    )
  }
}

final class GrimodexHybridReducerIntegrationTests: XCTestCase {
  func testHybridBackendPreservesHazkeyCapabilities() {
    let hybrid = HazkeyServerConfig(
      zenzaiBackendDevicesProvider: { [] },
      environment: ["FCITX5_GRIMODEX_CONVERTER": "mozc-hybrid"]
    )

    XCTAssertEqual(hybrid.converterBackend, .mozcHybrid)
    XCTAssertTrue(hybrid.converterBackend.usesMozcCore)
    XCTAssertTrue(hybrid.converterBackend.usesHazkeyCore)
    XCTAssertTrue(hybrid.converterBackend.allowsZenzai)
    XCTAssertEqual(hybrid.converterBackend.learningCapability, .persistent)
  }

  func testInsertPreparesSpeculationBeforeDebouncedLiveConversion() throws {
    let converter = HybridReducerRecordingConverter()
    var session = CompositionSession()
    session.policy.autoConvertMode = .always
    session.policy.liveConversionDelayMilliseconds = 228
    let reducer = ImeReducer(session: session, converter: converter)

    let inserted = reducer.reduce(.insertText("かな"), requestID: "insert")

    XCTAssertEqual(inserted.status, .success)
    XCTAssertEqual(
      inserted.snapshot.effects,
      [
        .scheduleLiveConversion(
          effectID: 1,
          delayMilliseconds: 228,
          scheduledRevision: inserted.snapshot.revision
        )
      ]
    )
    XCTAssertEqual(
      converter.events,
      [
        .invalidate(.edit),
        .prepare(CompositionRevision(rawValue: 1)),
      ],
      "Hazkey preparation must be enqueued synchronously before the live timer can fire"
    )
    XCTAssertEqual(converter.realtimeRequestCount, 0)
    let context = try XCTUnwrap(converter.preparedContexts.first)
    XCTAssertEqual(context.revision, CompositionRevision(rawValue: 1))
    XCTAssertEqual(context.input.elements.map(\.text).joined(), "かな")
    XCTAssertEqual(context.input.cursor, context.input.elements.count)
  }

  func testLiveRefreshKeepsCompositionRevisionAndSpaceLocksThatRevision() throws {
    let converter = HybridReducerRecordingConverter()
    var session = CompositionSession()
    session.policy.autoConvertMode = .always
    session.policy.liveConversionDelayMilliseconds = 228
    let reducer = ImeReducer(session: session, converter: converter)

    let inserted = reducer.reduce(.insertText("かな"), requestID: "insert")
    let preparedRevision = try XCTUnwrap(converter.preparedContexts.last).revision

    let live = reducer.reduce(
      .applyLiveConversion(scheduledRevision: inserted.snapshot.revision),
      requestID: "live",
      expectedRevision: inserted.snapshot.revision
    )

    XCTAssertEqual(live.status, .success)
    XCTAssertEqual(converter.realtimeRequestCount, 1)
    XCTAssertEqual(converter.invalidationReasons, [.edit])
    XCTAssertEqual(converter.preparedContexts.count, 1)

    let converted = reducer.reduce(
      .startConversion,
      requestID: "space",
      expectedRevision: live.snapshot.revision
    )

    XCTAssertEqual(converted.status, .success)
    XCTAssertEqual(converter.lockedRevisions.first, preparedRevision)
    XCTAssertTrue(converter.lockedRevisions.allSatisfy { $0 == preparedRevision })
    XCTAssertEqual(
      converter.invalidationReasons,
      [.edit],
      "presentation-only live conversion must not make prepared Hazkey work stale"
    )
  }

  func testSemanticBoundariesInvalidateSpeculationWithPreciseReasons() throws {
    do {
      let converter = HybridReducerRecordingConverter()
      let reducer = ImeReducer(converter: converter)
      _ = reducer.reduce(.insertText("かな"), requestID: "edit")
      XCTAssertEqual(converter.invalidationReasons, [.edit])
    }

    do {
      let converter = HybridReducerRecordingConverter()
      let reducer = ImeReducer(converter: converter)
      _ = reducer.reduce(.insertText("かな"), requestID: "insert")
      converter.resetInvalidations()
      let moved = reducer.reduce(.moveCursor(-1), requestID: "cursor")
      XCTAssertEqual(moved.status, .success)
      XCTAssertEqual(converter.invalidationReasons, [.cursorMove])
    }

    do {
      let converter = HybridReducerRecordingConverter()
      let reducer = ImeReducer(converter: converter)
      _ = reducer.reduce(.insertText("かなです"), requestID: "insert")
      _ = reducer.reduce(.startConversion, requestID: "convert")
      converter.resetInvalidations()
      let resized = reducer.reduce(.resizeSegment(-1), requestID: "resize")
      XCTAssertEqual(resized.status, .success)
      XCTAssertEqual(converter.invalidationReasons, [.segmentResize])
    }

    do {
      let converter = HybridReducerRecordingConverter()
      let reducer = ImeReducer(converter: converter)
      _ = reducer.reduce(.insertText("かな"), requestID: "insert")
      _ = reducer.reduce(.startConversion, requestID: "convert")
      converter.resetInvalidations()
      let committed = reducer.reduce(.commitAll, requestID: "commit")
      XCTAssertEqual(committed.status, .success)
      XCTAssertEqual(converter.invalidationReasons, [.commit])
    }

    do {
      let converter = HybridReducerRecordingConverter()
      let reducer = ImeReducer(converter: converter)
      _ = reducer.reduce(.insertText("かな"), requestID: "insert")
      converter.resetInvalidations()
      let cancelled = reducer.reduce(.cancel, requestID: "cancel")
      XCTAssertEqual(cancelled.status, .success)
      XCTAssertEqual(converter.invalidationReasons, [.cancel])
    }

    do {
      let converter = HybridReducerRecordingConverter()
      let reducer = ImeReducer(converter: converter)
      _ = reducer.reduce(.insertText("かな"), requestID: "insert")
      converter.resetInvalidations()
      reducer.invalidateCandidatesForExternalDictionaryChange()
      XCTAssertEqual(converter.invalidationReasons, [.dictionaryChange])
    }

    do {
      let converter = HybridReducerRecordingConverter()
      let reducer = ImeReducer(converter: converter)
      let checkpoint = RecoveryCheckpoint(
        revision: 1,
        phase: .composing,
        composition: CompositionBuffer(
          elements: [CompositionElement(text: "復")]
        ),
        nextCandidateGeneration: 1,
        nextEffectID: 1,
        leftContext: "",
        rightContext: "",
        policy: .default
      )
      let data = try XCTUnwrap(checkpoint.persistedData(isSecureInput: false))
      let restored = reducer.reduce(
        .restoreCheckpoint(data),
        requestID: "restore",
        expectedRevision: 0
      )
      XCTAssertEqual(restored.status, .success)
      XCTAssertEqual(converter.invalidationReasons, [.restore])
    }

    do {
      let converter = HybridReducerRecordingConverter()
      let reducer = ImeReducer(converter: converter)
      _ = reducer.reduce(.insertText("かな"), requestID: "insert")
      converter.resetInvalidations()
      let lifecycle = reducer.reduce(
        .lifecycle(.capabilityChanged(clientPreedit: true)),
        requestID: "lifecycle"
      )
      XCTAssertEqual(lifecycle.status, .success)
      XCTAssertEqual(converter.invalidationReasons, [.lifecycle])
    }
  }

  func testCandidateNavigationKeepsPublishedGenerationAndItemsFrozen() throws {
    let converter = HybridReducerRecordingConverter()
    let reducer = ImeReducer(converter: converter)
    _ = reducer.reduce(.insertText("かな"), requestID: "insert")
    let converted = reducer.reduce(.startConversion, requestID: "space")
    let preparedRevision = try XCTUnwrap(converter.preparedContexts.last).revision
    let generation = converted.snapshot.candidateWindow.generation
    let items = converted.snapshot.candidateWindow.items

    let navigated = reducer.reduce(.navigateCandidate(1), requestID: "next")

    XCTAssertEqual(navigated.status, .success)
    XCTAssertEqual(navigated.snapshot.candidateWindow.generation, generation)
    XCTAssertEqual(navigated.snapshot.candidateWindow.items, items)
    XCTAssertEqual(navigated.snapshot.candidateWindow.selectedIndex, 1)
    XCTAssertGreaterThanOrEqual(converter.lockedRevisions.count, 2)
    XCTAssertTrue(converter.lockedRevisions.allSatisfy { $0 == preparedRevision })
  }

  func testMaintenanceResumesPreparationAtTheNewCompositionRevision() throws {
    let converter = HybridReducerRecordingConverter()
    let reducer = ImeReducer(converter: converter)
    _ = reducer.reduce(.insertText("かな"), requestID: "insert")
    let oldRevision = try XCTUnwrap(converter.preparedContexts.last).revision

    reducer.invalidateCandidatesForExternalDictionaryChange()
    XCTAssertEqual(converter.preparedContexts.count, 1)
    reducer.resumeSpeculativeConversionAfterMaintenance()

    let resumed = try XCTUnwrap(converter.preparedContexts.last)
    XCTAssertEqual(converter.preparedContexts.count, 2)
    XCTAssertNotEqual(resumed.revision, oldRevision)
    XCTAssertEqual(resumed.input.elements.map(\.text).joined(), "かな")
  }
}
