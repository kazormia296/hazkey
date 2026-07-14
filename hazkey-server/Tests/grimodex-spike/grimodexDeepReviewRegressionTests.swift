import Foundation
import XCTest

@testable import hazkey_server

private final class DeepReviewRegressionConverter: KanaKanjiConverting {
  var stagedLearningCalls = 0
  var committedStagedLearning = 0
  var discardedStagedLearning = 0
  var immediateLearningUpdates = 0
  var learningCommits = 0
  var stopCalls = 0
  var purgeCalls = 0
  var lastOptions: ConversionOptions?
  var events: [String] = []

  func candidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    lastOptions = options
    let count = min(max(composition.elements.count, 1), 1)
    return ConversionOutput(
      candidates: [
        ConverterCandidate(
          text: "変換",
          consumingCount: count,
          sourceID: "fixture",
          provenance: .standard
        )
      ],
      pageSize: 1
    )
  }

  func realtimeCandidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> RealtimeConversionOutput {
    let output = try candidates(for: composition, options: options)
    return RealtimeConversionOutput(
      liveCandidate: output.candidates.first,
      candidates: output.candidates,
      pageSize: output.pageSize
    )
  }

  func setCompletedData(_ candidate: ConverterCandidate) {}

  func updateLearningData(_ candidate: ConverterCandidate) {
    immediateLearningUpdates += 1
  }

  func commitLearning() {
    learningCommits += 1
  }

  func stageLearning(
    candidate: ConverterCandidate,
    reading: String
  ) -> ConverterLearningToken? {
    stagedLearningCalls += 1
    events.append("stage")
    return ConverterLearningToken(rawValue: "token-\(stagedLearningCalls)")
  }

  func commitStagedLearning(_ token: ConverterLearningToken) {
    committedStagedLearning += 1
    events.append("commit-staged")
  }

  func discardStagedLearning(_ token: ConverterLearningToken) {
    discardedStagedLearning += 1
    events.append("discard-staged")
  }

  func forget(_ candidate: ConverterCandidate) {}

  func stopComposition() {
    stopCalls += 1
  }

  func purgeSensitiveState() {
    purgeCalls += 1
    events.append("purge")
  }
}

final class GrimodexDeepReviewRegressionTests: XCTestCase {
  func testLegacyClientUsesImmediateLearningInsteadOfPublishingPendingState() {
    let converter = DeepReviewRegressionConverter()
    let reducer = ImeReducer(
      converter: converter,
      stagedLearningEnabled: false
    )

    _ = reducer.reduce(.insertText("a"), requestID: "insert")
    _ = reducer.reduce(.startConversion, requestID: "convert")
    let committed = reducer.reduce(.commitAll, requestID: "commit")

    XCTAssertFalse(committed.snapshot.pendingLearning)
    XCTAssertEqual(converter.stagedLearningCalls, 0)
    XCTAssertEqual(converter.immediateLearningUpdates, 1)
    XCTAssertEqual(converter.learningCommits, 1)
  }

  func testPartialCommitFinalizesPrefixLearningBeforeSuffixEditing() {
    let converter = DeepReviewRegressionConverter()
    let reducer = ImeReducer(converter: converter)

    _ = reducer.reduce(.insertText("ab"), requestID: "insert")
    _ = reducer.reduce(.startConversion, requestID: "convert")
    let partial = reducer.reduce(.commitSelected, requestID: "partial")

    XCTAssertFalse(reducer.session.composingText.isEmpty)
    XCTAssertFalse(partial.snapshot.pendingLearning)
    XCTAssertEqual(converter.committedStagedLearning, 1)

    _ = reducer.reduce(
      .deleteBackward,
      requestID: "edit-suffix",
      expectedRevision: partial.snapshot.revision
    )
    XCTAssertEqual(converter.discardedStagedLearning, 0)
  }

  func testIdleCommitAllAdvancesRevisionWhenItOnlyResolvesLearning() {
    let converter = DeepReviewRegressionConverter()
    let reducer = ImeReducer(converter: converter)

    _ = reducer.reduce(.insertText("a"), requestID: "insert")
    _ = reducer.reduce(.startConversion, requestID: "convert")
    let committed = reducer.reduce(.commitAll, requestID: "commit")
    XCTAssertTrue(committed.snapshot.pendingLearning)

    let resolved = reducer.reduce(
      .commitAll,
      requestID: "resolve",
      expectedRevision: committed.snapshot.revision
    )
    XCTAssertFalse(resolved.snapshot.pendingLearning)
    XCTAssertEqual(resolved.snapshot.revision, committed.snapshot.revision + 1)
    XCTAssertEqual(converter.committedStagedLearning, 1)
  }

  func testSessionFinalizationCommitsAcceptedLearning() {
    let converter = DeepReviewRegressionConverter()
    let reducer = ImeReducer(converter: converter)
    let controller = ImeV2SessionController(reducer: reducer)

    _ = reducer.reduce(.insertText("a"), requestID: "insert")
    _ = reducer.reduce(.startConversion, requestID: "convert")
    let committed = reducer.reduce(.commitAll, requestID: "commit")
    XCTAssertTrue(committed.snapshot.pendingLearning)

    controller.finalizePendingLearning(commit: true)

    XCTAssertFalse(controller.snapshot.pendingLearning)
    XCTAssertEqual(converter.committedStagedLearning, 1)
    XCTAssertEqual(controller.snapshot.revision, committed.snapshot.revision + 1)
  }

  func testSessionFinalizationCanDiscardUnconfirmedLearning() {
    let converter = DeepReviewRegressionConverter()
    let reducer = ImeReducer(converter: converter)
    let controller = ImeV2SessionController(reducer: reducer)

    _ = reducer.reduce(.insertText("a"), requestID: "insert")
    _ = reducer.reduce(.startConversion, requestID: "convert")
    let committed = reducer.reduce(.commitAll, requestID: "commit")
    XCTAssertTrue(committed.snapshot.pendingLearning)

    controller.finalizePendingLearning(commit: false)

    XCTAssertFalse(controller.snapshot.pendingLearning)
    XCTAssertEqual(converter.committedStagedLearning, 0)
    XCTAssertEqual(converter.discardedStagedLearning, 1)
    XCTAssertEqual(controller.snapshot.revision, committed.snapshot.revision + 1)
  }

  func testSessionFinalizationRebasesCachedCommitAndPreservesItsEffect() {
    let converter = DeepReviewRegressionConverter()
    let reducer = ImeReducer(converter: converter)
    let controller = ImeV2SessionController(reducer: reducer)

    _ = reducer.reduce(.insertText("a"), requestID: "insert")
    _ = reducer.reduce(.startConversion, requestID: "convert")
    let committed = reducer.reduce(.commitAll, requestID: "commit")
    XCTAssertTrue(committed.snapshot.pendingLearning)
    XCTAssertFalse(committed.snapshot.effects.isEmpty)

    controller.finalizePendingLearning(commit: false)
    let current = controller.snapshot
    let replayed = reducer.reduce(.commitAll, requestID: "commit")

    XCTAssertEqual(replayed.status, .success)
    XCTAssertEqual(replayed.snapshot.revision, current.revision)
    XCTAssertFalse(replayed.snapshot.pendingLearning)
    XCTAssertEqual(replayed.snapshot.effects, committed.snapshot.effects)
    XCTAssertEqual(replayed.snapshot.effects.first, .commitText(effectID: 1, text: "変換"))
    XCTAssertEqual(converter.discardedStagedLearning, 1)
  }

  func testPolicyProviderRunsOnlyAfterRequestAndActionValidation() {
    let converter = DeepReviewRegressionConverter()
    var secureInput = false
    var policyProviderCalls = 0
    let reducer = ImeReducer(converter: converter)
    let controller = ImeV2SessionController(
      reducer: reducer,
      policyProvider: {
        policyProviderCalls += 1
        return PinnedCompositionPolicy(
          allowsLearning: true,
          secureInput: secureInput,
          zenzaiEnabled: !secureInput,
          projectRevision: UInt64(policyProviderCalls)
        )
      }
    )

    let originalInsert = ImeV2Request(
      requestID: "insert",
      expectedRevision: 0,
      action: .insertText("a")
    )
    let inserted = controller.handle(originalInsert)
    let converted = controller.handle(ImeV2Request(
      requestID: "convert",
      expectedRevision: inserted.snapshot.revision,
      action: .startConversion
    ))
    let committed = controller.handle(ImeV2Request(
      requestID: "commit",
      expectedRevision: converted.snapshot.revision,
      action: .commitAll
    ))
    XCTAssertTrue(committed.snapshot.pendingLearning)
    XCTAssertEqual(policyProviderCalls, 1)

    secureInput = true
    let eventsBeforeRejectedRequests = converter.events
    let duplicate = controller.handle(originalInsert)
    XCTAssertEqual(duplicate.status, .success)
    XCTAssertEqual(policyProviderCalls, 1)
    XCTAssertEqual(converter.events, eventsBeforeRejectedRequests)
    XCTAssertTrue(controller.snapshot.pendingLearning)

    let stale = controller.handle(ImeV2Request(
      requestID: "stale",
      expectedRevision: committed.snapshot.revision - 1,
      action: .insertText("stale")
    ))
    XCTAssertEqual(stale.status, .staleRevision)
    XCTAssertEqual(policyProviderCalls, 1)
    XCTAssertEqual(converter.events, eventsBeforeRejectedRequests)
    XCTAssertTrue(controller.snapshot.pendingLearning)

    let invalid = controller.handle(ImeV2Request(
      requestID: "invalid",
      expectedRevision: committed.snapshot.revision,
      action: .insertText("")
    ))
    XCTAssertEqual(invalid.status, .invalidAction)
    XCTAssertEqual(policyProviderCalls, 1)
    XCTAssertEqual(converter.events, eventsBeforeRejectedRequests)
    XCTAssertTrue(controller.snapshot.pendingLearning)

    let secureReconversion = controller.handle(ImeV2Request(
      requestID: "secure-reconversion",
      expectedRevision: committed.snapshot.revision,
      action: .reconvert(
        text: "accepted",
        leftContext: "private-left",
        rightContext: "private-right",
        deleteBefore: 0,
        deleteAfter: 0
      )
    ))
    XCTAssertEqual(secureReconversion.status, .secureInputViolation)
    XCTAssertEqual(
      secureReconversion.snapshot.revision,
      committed.snapshot.revision + 1
    )
    XCTAssertEqual(policyProviderCalls, 2)
    XCTAssertFalse(secureReconversion.snapshot.pendingLearning)
    XCTAssertTrue(secureReconversion.snapshot.preedit.isEmpty)
    XCTAssertNil(secureReconversion.snapshot.recovery)
    XCTAssertTrue(reducer.session.policy.secureInput)
    XCTAssertEqual(reducer.session.context.leftContext, "")
    XCTAssertEqual(reducer.session.context.rightContext, "")
    XCTAssertNil(reducer.session.recoveryCheckpoint)
    XCTAssertEqual(Array(converter.events.suffix(2)), ["discard-staged", "purge"])
    XCTAssertEqual(converter.committedStagedLearning, 0)
  }

  func testIdleCancelAndEmptyTransformAdvanceRevisionWhenResolvingLearning() {
    for (suffix, action, expectedCommit, expectedDiscard) in [
      ("cancel", ImeAction.cancel, 0, 1),
      ("transform", ImeAction.transformActiveSegment(.hiragana), 1, 0),
    ] {
      let converter = DeepReviewRegressionConverter()
      let reducer = ImeReducer(converter: converter)
      _ = reducer.reduce(.insertText("a"), requestID: "insert-\(suffix)")
      _ = reducer.reduce(.startConversion, requestID: "convert-\(suffix)")
      let committed = reducer.reduce(.commitAll, requestID: "commit-\(suffix)")
      XCTAssertTrue(committed.snapshot.pendingLearning)

      let resolved = reducer.reduce(
        action,
        requestID: "resolve-\(suffix)",
        expectedRevision: committed.snapshot.revision
      )

      XCTAssertEqual(resolved.status, .success)
      XCTAssertFalse(resolved.snapshot.pendingLearning)
      XCTAssertEqual(resolved.snapshot.revision, committed.snapshot.revision + 1)
      XCTAssertEqual(converter.committedStagedLearning, expectedCommit)
      XCTAssertEqual(converter.discardedStagedLearning, expectedDiscard)
    }
  }

  func testInvalidCommitSelectedDoesNotConsumePendingLearning() {
    let converter = DeepReviewRegressionConverter()
    let reducer = ImeReducer(converter: converter)
    _ = reducer.reduce(.insertText("a"), requestID: "insert")
    _ = reducer.reduce(.startConversion, requestID: "convert")
    let committed = reducer.reduce(.commitAll, requestID: "commit")
    XCTAssertTrue(committed.snapshot.pendingLearning)

    let invalid = reducer.reduce(
      .commitSelected,
      requestID: "invalid-selected",
      expectedRevision: committed.snapshot.revision
    )

    XCTAssertEqual(invalid.status, .invalidAction)
    XCTAssertEqual(invalid.snapshot.revision, committed.snapshot.revision)
    XCTAssertTrue(invalid.snapshot.pendingLearning)
    XCTAssertEqual(converter.committedStagedLearning, 0)
    XCTAssertEqual(converter.discardedStagedLearning, 0)
  }

  func testInvalidReconversionAndRestoreDoNotConsumeUndoWindow() {
    let converter = DeepReviewRegressionConverter()
    let reducer = ImeReducer(converter: converter)

    _ = reducer.reduce(.insertText("a"), requestID: "insert")
    _ = reducer.reduce(.startConversion, requestID: "convert")
    let committed = reducer.reduce(.commitAll, requestID: "commit")
    XCTAssertTrue(committed.snapshot.pendingLearning)

    let invalidReconversion = reducer.reduce(
      .reconvert(
        text: "",
        leftContext: "",
        rightContext: "",
        deleteBefore: 0,
        deleteAfter: 0
      ),
      requestID: "bad-reconversion",
      expectedRevision: committed.snapshot.revision
    )
    XCTAssertEqual(invalidReconversion.status, .invalidAction)
    XCTAssertEqual(invalidReconversion.snapshot.revision, committed.snapshot.revision)
    XCTAssertTrue(invalidReconversion.snapshot.pendingLearning)

    let malformedRestore = reducer.reduce(
      .restoreCheckpoint(Data("not-json".utf8)),
      requestID: "bad-restore",
      expectedRevision: committed.snapshot.revision
    )
    XCTAssertEqual(malformedRestore.status, .invalidAction)
    XCTAssertEqual(malformedRestore.snapshot.revision, committed.snapshot.revision)
    XCTAssertTrue(malformedRestore.snapshot.pendingLearning)
    XCTAssertEqual(converter.discardedStagedLearning, 0)
  }

  func testDictionaryInvalidationClearsMaterializedLivePrefixWithoutCandidateWindow() {
    let converter = DeepReviewRegressionConverter()
    var session = CompositionSession()
    session.policy.autoConvertMode = .always
    session.policy.liveConversionDelayMilliseconds = 228
    let reducer = ImeReducer(session: session, converter: converter)

    let inserted = reducer.reduce(.insertText("ab"), requestID: "insert")
    let live = reducer.reduce(
      .applyLiveConversion(scheduledRevision: inserted.snapshot.revision),
      requestID: "live",
      expectedRevision: inserted.snapshot.revision
    )
    XCTAssertEqual(live.snapshot.preedit.first?.text, "変換")

    let suffix = reducer.reduce(
      .insertText("c"),
      requestID: "suffix",
      expectedRevision: live.snapshot.revision
    )
    XCTAssertNil(reducer.session.candidates)
    XCTAssertNotNil(reducer.session.livePresentation.materializedPrefix)

    reducer.invalidateCandidatesForExternalDictionaryChange()

    XCTAssertNil(reducer.session.livePresentation.materializedPrefix)
    XCTAssertEqual(reducer.currentSnapshot().revision, suffix.snapshot.revision + 1)
    XCTAssertEqual(reducer.currentSnapshot().preedit.map(\.text).joined(), "abc")

    let replayedSuffix = reducer.reduce(
      .insertText("c"),
      requestID: "suffix",
      expectedRevision: live.snapshot.revision
    )
    XCTAssertEqual(replayedSuffix.snapshot.revision, reducer.currentSnapshot().revision)
    XCTAssertEqual(replayedSuffix.snapshot.preedit.map(\.text).joined(), "abc")
    XCTAssertEqual(replayedSuffix.snapshot.candidateWindow, .empty)
    XCTAssertEqual(replayedSuffix.snapshot.effects, suffix.snapshot.effects)
  }

  func testSecureCompositionEndPurgesConverterCandidateState() {
    let converter = DeepReviewRegressionConverter()
    var session = CompositionSession()
    session.policy.secureInput = true
    let reducer = ImeReducer(session: session, converter: converter)

    _ = reducer.reduce(.insertText("secret"), requestID: "insert")
    _ = reducer.reduce(.startConversion, requestID: "convert")
    let committed = reducer.reduce(.commitAll, requestID: "commit")

    XCTAssertEqual(committed.snapshot.phase, .idle)
    XCTAssertEqual(converter.purgeCalls, 1)
    XCTAssertFalse(committed.snapshot.pendingLearning)
  }

  func testSecureCancelAndDeactivateUseSensitivePurge() {
    for (suffix, action) in [("cancel", ImeAction.cancel)] {
      let converter = DeepReviewRegressionConverter()
      var session = CompositionSession()
      session.policy.secureInput = true
      let reducer = ImeReducer(session: session, converter: converter)
      _ = reducer.reduce(.insertText("secret"), requestID: "insert-\(suffix)")
      _ = reducer.reduce(.startConversion, requestID: "convert-\(suffix)")
      converter.purgeCalls = 0
      converter.stopCalls = 0

      _ = reducer.reduce(action, requestID: "lifecycle-\(suffix)")

      XCTAssertGreaterThan(converter.purgeCalls, 0)
      XCTAssertEqual(converter.stopCalls, 0)
    }
  }

  func testSecureLifecycleBoundariesEraseAllSessionSecrets() {
    for (suffix, event) in [
      ("deactivate", ImeLifecycleEvent.deactivate),
      ("focus", ImeLifecycleEvent.focusChanged),
      ("restart", ImeLifecycleEvent.serverRestarted),
    ] {
      let converter = DeepReviewRegressionConverter()
      var session = CompositionSession()
      session.policy.secureInput = true
      session.phase = .composing
      session.composingText = CompositionBuffer(
        elements: "secret".map { CompositionElement(text: String($0)) }
      )
      session.context.leftContext = "private-left"
      session.context.rightContext = "private-right"
      session.reconversionReplacement = ReconversionReplacement(before: 1, after: 0)
      session.unicodeInputBuffer = "deadbeef"
      session.phaseBeforeUnicodeInput = .composing
      session.livePresentation.pendingRevision = 42
      session.pendingLearningTransactions = [
        PendingLearningTransaction(
          token: ConverterLearningToken(rawValue: "secret-token"),
          reading: "secret-reading",
          surface: "secret-surface",
          origin: .explicitConversion,
          createdRevision: 0
        )
      ]
      let reducer = ImeReducer(session: session, converter: converter)

      let result = reducer.reduce(
        .lifecycle(event),
        requestID: "secure-\(suffix)"
      )

      XCTAssertEqual(result.status, .success)
      XCTAssertEqual(result.snapshot.phase, .idle)
      XCTAssertTrue(result.snapshot.preedit.isEmpty)
      XCTAssertNil(result.snapshot.aux)
      XCTAssertNil(result.snapshot.recovery)
      XCTAssertFalse(result.snapshot.pendingLearning)
      XCTAssertTrue(reducer.session.composingText.isEmpty)
      XCTAssertEqual(reducer.session.context.leftContext, "")
      XCTAssertEqual(reducer.session.context.rightContext, "")
      XCTAssertNil(reducer.session.reconversionReplacement)
      XCTAssertEqual(reducer.session.unicodeInputBuffer, "")
      XCTAssertNil(reducer.session.phaseBeforeUnicodeInput)
      XCTAssertNil(reducer.session.livePresentation.pendingRevision)
      XCTAssertEqual(converter.discardedStagedLearning, 1)
      XCTAssertEqual(Array(converter.events.prefix(2)), ["discard-staged", "purge"])
      XCTAssertGreaterThan(converter.purgeCalls, 0)
      XCTAssertEqual(converter.stopCalls, 0)
    }
  }

  func testSecureBoundaryClearsCachedSecretSnapshots() throws {
    let converter = DeepReviewRegressionConverter()
    var session = CompositionSession()
    session.policy.secureInput = true
    let reducer = ImeReducer(session: session, converter: converter)

    let inserted = reducer.reduce(.insertText("secret"), requestID: "secret-request")
    XCTAssertEqual(inserted.snapshot.preedit.map(\.text).joined(), "secret")

    let boundary = reducer.reduce(
      .lifecycle(.secureInputChanged(false)),
      requestID: "leave-secure",
      expectedRevision: inserted.snapshot.revision
    )
    XCTAssertEqual(boundary.status, .success)
    XCTAssertTrue(boundary.snapshot.preedit.isEmpty)
    let recovery = try XCTUnwrap(boundary.snapshot.recovery)
    XCTAssertEqual(recovery.revision, boundary.snapshot.revision)
    XCTAssertEqual(recovery.phase, .idle)
    XCTAssertTrue(recovery.composition.isEmpty)
    XCTAssertEqual(recovery.leftContext, "")
    XCTAssertEqual(recovery.rightContext, "")
    XCTAssertFalse(recovery.policy.secureInput)
    XCTAssertNil(recovery.reconversionReplacement)
    XCTAssertEqual(recovery.unicodeInputBuffer, "")
    XCTAssertNil(recovery.phaseBeforeUnicodeInput)

    let replayedOldID = reducer.reduce(
      .insertText("secret"),
      requestID: "secret-request",
      expectedRevision: 0
    )
    XCTAssertEqual(replayedOldID.status, .staleRevision)
    XCTAssertFalse(replayedOldID.snapshot.preedit.map(\.text).joined().contains("secret"))
  }

  func testRedundantSecureInputEventPreservesCompositionAndPendingLearning() {
    let converter = DeepReviewRegressionConverter()
    let reducer = ImeReducer(converter: converter)

    _ = reducer.reduce(.insertText("a"), requestID: "insert")
    let converted = reducer.reduce(.startConversion, requestID: "convert")
    let redundantWhileComposing = reducer.reduce(
      .lifecycle(.secureInputChanged(false)),
      requestID: "secure-still-off-composing"
    )

    XCTAssertEqual(redundantWhileComposing.snapshot.phase, converted.snapshot.phase)
    XCTAssertEqual(redundantWhileComposing.snapshot.preedit, converted.snapshot.preedit)
    XCTAssertEqual(
      redundantWhileComposing.snapshot.candidateWindow,
      converted.snapshot.candidateWindow
    )
    XCTAssertEqual(redundantWhileComposing.snapshot.revision, converted.snapshot.revision + 1)
    XCTAssertEqual(converter.purgeCalls, 0)

    let committed = reducer.reduce(.commitAll, requestID: "commit")
    XCTAssertTrue(committed.snapshot.pendingLearning)
    let redundantWithPendingLearning = reducer.reduce(
      .lifecycle(.secureInputChanged(false)),
      requestID: "secure-still-off-pending"
    )

    XCTAssertTrue(redundantWithPendingLearning.snapshot.pendingLearning)
    XCTAssertEqual(converter.discardedStagedLearning, 0)
    XCTAssertEqual(converter.purgeCalls, 0)
  }

  func testSuggestionLimitRemainsPinnedForTheWholeComposition() {
    let converter = DeepReviewRegressionConverter()
    var configuredLimit = 1
    let controller = ImeV2SessionController(
      reducer: ImeReducer(converter: converter),
      policyProvider: {
        PinnedCompositionPolicy(
          allowsLearning: true,
          secureInput: false,
          zenzaiEnabled: false,
          projectRevision: 0,
          suggestionListMode: .normal,
          suggestionListLimit: configuredLimit
        )
      }
    )

    let inserted = controller.handle(ImeV2Request(
      requestID: "insert",
      expectedRevision: 0,
      action: .insertText("a")
    ))
    configuredLimit = 9
    _ = controller.handle(ImeV2Request(
      requestID: "convert",
      expectedRevision: inserted.snapshot.revision,
      action: .startConversion
    ))

    XCTAssertEqual(converter.lastOptions?.suggestionListMode, .normal)
    XCTAssertEqual(converter.lastOptions?.suggestionListLimit, 1)
  }
}
