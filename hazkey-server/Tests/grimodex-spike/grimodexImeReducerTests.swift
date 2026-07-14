import Foundation
import XCTest

@testable import hazkey_server

private final class ReducerFixtureConverter: KanaKanjiConverting {
  let supportsSegmentEditing = true

  var shouldFail = false
  var learningUpdates = 0
  var completed = 0
  var forgotten = 0
  var stopCount = 0
  var realtimeRequests = 0
  var displayOverride: String?
  var predictionCandidates: [ConverterCandidate] = []
  var lastOptions: ConversionOptions?
  var lastComposition: CompositionInput?
  var useStagedLearning = false
  var stagedLearningCount = 0
  var committedStagedLearningCount = 0
  var discardedStagedLearningCount = 0

  func display(for composition: CompositionInput) -> CompositionDisplay {
    let raw = composition.elements.map(\.text).joined()
    let text = displayOverride ?? raw
    let caret = if displayOverride == nil {
      composition.elements.prefix(composition.cursor).reduce(0) {
        $0 + $1.text.utf8.count
      }
    } else {
      text.utf8.count
    }
    return CompositionDisplay(
      text: text,
      caretUtf8ByteOffset: UInt32(caret)
    )
  }

  func candidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    lastComposition = composition
    lastOptions = options
    if shouldFail { throw FixtureError.failed }
    let input = composition.elements.map(\.text).joined()
    guard !input.isEmpty else {
      return ConversionOutput(candidates: [], pageSize: 0)
    }
    let target = min(
      max(composition.targetCount ?? composition.elements.count, 1),
      composition.elements.count
    )
    let count = min(2, target)
    return ConversionOutput(
      candidates: [
        ConverterCandidate(text: "変換", consumingCount: count),
        ConverterCandidate(text: input, annotation: "読み", consumingCount: count),
      ],
      pageSize: 2
    )
  }

  func predictions(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    lastComposition = composition
    lastOptions = options
    if shouldFail { throw FixtureError.failed }
    return ConversionOutput(
      candidates: predictionCandidates,
      pageSize: predictionCandidates.count
    )
  }

  func realtimeCandidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> RealtimeConversionOutput {
    realtimeRequests += 1
    let output = try candidates(for: composition, options: options)
    return RealtimeConversionOutput(
      liveCandidate: output.candidates.first,
      candidates: output.candidates,
      pageSize: output.pageSize
    )
  }

  func setCompletedData(_ candidate: ConverterCandidate) { completed += 1 }
  func updateLearningData(_ candidate: ConverterCandidate) { learningUpdates += 1 }
  func commitLearning() {}
  func stageLearning(
    candidate: ConverterCandidate,
    reading: String
  ) -> ConverterLearningToken? {
    guard useStagedLearning else { return nil }
    stagedLearningCount += 1
    return ConverterLearningToken(rawValue: "fixture-\(stagedLearningCount)")
  }
  func commitStagedLearning(_ token: ConverterLearningToken) {
    committedStagedLearningCount += 1
    completed += 1
    learningUpdates += 1
  }
  func discardStagedLearning(_ token: ConverterLearningToken) {
    discardedStagedLearningCount += 1
  }
  func forget(_ candidate: ConverterCandidate) { forgotten += 1 }
  func stopComposition() { stopCount += 1 }

  private enum FixtureError: Error { case failed }
}

private final class PagingFixtureConverter: KanaKanjiConverting {
  func candidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    ConversionOutput(
      candidates: (0..<11).map {
        ConverterCandidate(text: "候補\($0)", consumingCount: composition.elements.count)
      },
      pageSize: 3
    )
  }

  func setCompletedData(_ candidate: ConverterCandidate) {}
  func updateLearningData(_ candidate: ConverterCandidate) {}
  func commitLearning() {}
  func forget(_ candidate: ConverterCandidate) {}
  func stopComposition() {}
}

private final class DuplicateSurfaceFixtureConverter: KanaKanjiConverting {
  var completedSourceIDs: [String?] = []
  var learnedSourceIDs: [String?] = []

  func candidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    ConversionOutput(
      candidates: [
        ConverterCandidate(
          text: "同じ",
          consumingCount: composition.elements.count,
          sourceID: "first"
        ),
        ConverterCandidate(
          text: "同じ",
          consumingCount: composition.elements.count,
          sourceID: "second"
        ),
      ],
      pageSize: 2
    )
  }

  func setCompletedData(_ candidate: ConverterCandidate) {
    completedSourceIDs.append(candidate.sourceID)
  }
  func updateLearningData(_ candidate: ConverterCandidate) {
    learnedSourceIDs.append(candidate.sourceID)
  }
  func commitLearning() {}
  func forget(_ candidate: ConverterCandidate) {}
  func stopComposition() {}
}

private struct ReducerDeterministicGenerator {
  private var state: UInt64 = 0x4752_494d_4f44_4558

  mutating func next(_ upperBound: Int) -> Int {
    precondition(upperBound > 0)
    state = state &* 6_364_136_223_846_793_005 &+ 1_442_695_040_888_963_407
    return Int(state % UInt64(upperBound))
  }
}

final class GrimodexImeReducerTests: XCTestCase {
  func testProtectedSurfacePolicyPreservesAsciiTokensAndAllowsDictionaryTerms() {
    let generic = ConverterCandidate(
      text: "変換",
      consumingCount: 1,
      provenance: .standard
    )
    XCTAssertFalse(ProtectedSurfacePolicy.allows(generic, for: "https://example.com"))
    XCTAssertFalse(ProtectedSurfacePolicy.allows(generic, for: "foo?bar"))

    let preserved = ConverterCandidate(
      text: "https://example.com",
      consumingCount: 1,
      provenance: .standard
    )
    XCTAssertTrue(ProtectedSurfacePolicy.allows(preserved, for: "https://example.com"))

    let projectTerm = ConverterCandidate(
      text: "変換",
      consumingCount: 1,
      provenance: .projectDictionary
    )
    XCTAssertTrue(ProtectedSurfacePolicy.allows(projectTerm, for: "https://example.com"))
  }

  func testBuiltInGuardDictionaryIsSmallAndReviewable() {
    XCTAssertLessThanOrEqual(GrimodexBuiltInGuardDictionary.count, 200)
    let candidates = GrimodexBuiltInGuardDictionary.candidates(
      for: "かんそくせい",
      consumingCount: 5
    )
    XCTAssertEqual(candidates.first?.text, "可観測性")
    XCTAssertEqual(candidates.first?.provenance, .builtInGuard)
  }

  func testAuxiliaryReadingFollowsPinnedPolicy() {
    let always = PinnedCompositionPolicy(
      allowsLearning: false,
      secureInput: false,
      zenzaiEnabled: false,
      projectRevision: 0,
      auxTextMode: .always
    )
    let alwaysReducer = ImeReducer(session: CompositionSession(policy: always))
    XCTAssertEqual(
      alwaysReducer.reduce(.insertText("かな"), requestID: "always").snapshot.aux,
      "かな"
    )

    let defaultReducer = ImeReducer()
    let inserted = defaultReducer.reduce(.insertText("かな"), requestID: "insert")
    XCTAssertNil(inserted.snapshot.aux)
    let moved = defaultReducer.reduce(
      .moveCursor(-1),
      requestID: "left",
      expectedRevision: inserted.snapshot.revision
    )
    XCTAssertEqual(moved.snapshot.aux, "かな")
  }

  func testDirectCommitUsesRenderedPunctuationSuffix() {
    let policy = PinnedCompositionPolicy(
      allowsLearning: false,
      secureInput: false,
      zenzaiEnabled: false,
      projectRevision: 0,
      directCommitTargets: [.comma]
    )
    let reducer = ImeReducer(session: CompositionSession(policy: policy))
    let result = reducer.reduce(.insertText("かな、"), requestID: "direct")

    XCTAssertEqual(result.snapshot.phase, .idle)
    XCTAssertEqual(
      result.snapshot.effects,
      [.commitText(effectID: 1, text: "かな、")]
    )
  }

  func testMaterializedLivePrefixStaysVisibleWhileNewSuffixIsDebounced() {
    let converter = ReducerFixtureConverter()
    var session = CompositionSession()
    session.policy.autoConvertMode = .always
    session.policy.liveConversionDelayMilliseconds = 228
    let reducer = ImeReducer(session: session, converter: converter)

    let inserted = reducer.reduce(.insertText("かな"), requestID: "insert")
    let live = reducer.reduce(
      .applyLiveConversion(scheduledRevision: inserted.snapshot.revision),
      requestID: "live",
      expectedRevision: inserted.snapshot.revision
    )
    XCTAssertEqual(live.snapshot.preedit.first?.text, "変換")

    let suffix = reducer.reduce(
      .insertText("に"),
      requestID: "suffix",
      expectedRevision: live.snapshot.revision
    )
    XCTAssertEqual(
      suffix.snapshot.preedit,
      [
        PreeditSpan(text: "変換", style: .active),
        PreeditSpan(text: "に", style: .underline),
      ]
    )
    XCTAssertEqual(converter.realtimeRequests, 1)
  }

  func testPendingLearningCanBeCommittedOrCancelledAfterVisibleCommit() {
    let converter = ReducerFixtureConverter()
    converter.useStagedLearning = true
    let reducer = ImeReducer(converter: converter)
    _ = reducer.reduce(.insertText("かな"), requestID: "insert")
    _ = reducer.reduce(.startConversion, requestID: "convert")
    let committed = reducer.reduce(.commitAll, requestID: "commit")

    XCTAssertTrue(committed.snapshot.pendingLearning)
    XCTAssertEqual(converter.committedStagedLearningCount, 0)
    let cancelled = reducer.reduce(
      .resolvePendingLearning(commit: false),
      requestID: "cancel-learning",
      expectedRevision: committed.snapshot.revision
    )
    XCTAssertFalse(cancelled.snapshot.pendingLearning)
    XCTAssertEqual(converter.discardedStagedLearningCount, 1)

    _ = reducer.reduce(.insertText("かな"), requestID: "insert-again")
    _ = reducer.reduce(.startConversion, requestID: "convert-again")
    let visible = reducer.reduce(.commitAll, requestID: "commit-again")
    let resolved = reducer.reduce(
      .resolvePendingLearning(commit: true),
      requestID: "commit-learning",
      expectedRevision: visible.snapshot.revision
    )
    XCTAssertFalse(resolved.snapshot.pendingLearning)
    XCTAssertEqual(converter.committedStagedLearningCount, 1)
  }

  func testEditorUsesInputElementsAndUtf8Caret() {
    let reducer = ImeReducer()

    _ = reducer.reduce(.insertText("𠮷"), requestID: "insert")
    XCTAssertEqual(reducer.session.composingText.elements.count, 1)
    XCTAssertEqual(
      reducer.reduce(.insertText("👨‍👩‍👧‍👦"), requestID: "emoji").snapshot.caretUtf8ByteOffset,
      UInt32("𠮷👨‍👩‍👧‍👦".utf8.count)
    )

    _ = reducer.reduce(.moveCursor(-1), requestID: "left")
    XCTAssertEqual(
      reducer.reduce(.deleteBackward, requestID: "backspace").snapshot.preedit.first?.text,
      "👨‍👩‍👧‍👦"
    )
  }

  func testDuplicateRequestReturnsIdenticalSnapshotAndEffect() {
    let converter = ReducerFixtureConverter()
    let reducer = ImeReducer(converter: converter)
    _ = reducer.reduce(.insertText("かな"), requestID: "insert")
    _ = reducer.reduce(.startConversion, requestID: "convert")

    let first = reducer.reduce(.commitSelected, requestID: "commit")
    let duplicate = reducer.reduce(.commitSelected, requestID: "commit")
    XCTAssertEqual(first, duplicate)
    XCTAssertEqual(first.snapshot.effects.count, 1)
    XCTAssertEqual(reducer.session.revision, first.snapshot.revision)
  }

  func testRequestIDCollisionWithDifferentActionFailsClosed() {
    let reducer = ImeReducer()
    let inserted = reducer.reduce(.insertText("a"), requestID: "same-id")
    let collision = reducer.reduce(.insertText("b"), requestID: "same-id")

    XCTAssertEqual(collision.status, .invalidAction)
    XCTAssertEqual(collision.snapshot.revision, inserted.snapshot.revision)
    XCTAssertEqual(reducer.session.composingText.text, "a")
  }

  func testProtocolControllerRejectsEmptyAndOversizedRequestIDsWithoutMutation() {
    let controller = ImeV2SessionController()

    for requestID in ["", String(repeating: "x", count: 129)] {
      let result = controller.handle(ImeV2Request(
        requestID: requestID,
        expectedRevision: 0,
        action: .insertText("secret")
      ))
      XCTAssertEqual(result.status, .invalidAction)
      XCTAssertEqual(result.snapshot.revision, 0)
      XCTAssertTrue(result.snapshot.preedit.isEmpty)
    }
  }

  func testStaleCandidateDoesNotCommit() {
    let reducer = ImeReducer(converter: ReducerFixtureConverter())
    _ = reducer.reduce(.insertText("かな"), requestID: "insert")
    let converted = reducer.reduce(.startConversion, requestID: "convert")
    let generation = converted.snapshot.candidateWindow.generation

    let stale = reducer.reduce(
      .selectCandidate(id: "old", generation: generation - 1),
      requestID: "stale"
    )
    XCTAssertEqual(stale.status, .staleCandidate)
    XCTAssertTrue(stale.snapshot.effects.isEmpty)
    XCTAssertEqual(reducer.session.composingText.text, "かな")
  }

  func testDuplicateSurfaceCandidatesKeepTheSelectedConverterIdentity() {
    let converter = DuplicateSurfaceFixtureConverter()
    let reducer = ImeReducer(converter: converter)
    _ = reducer.reduce(.insertText("かな"), requestID: "insert")
    let converted = reducer.reduce(.startConversion, requestID: "convert")
    let window = converted.snapshot.candidateWindow

    let selected = reducer.reduce(
      .selectCandidate(id: window.items[1].id, generation: window.generation),
      requestID: "select-second"
    )

    XCTAssertEqual(selected.status, .success)
    XCTAssertTrue(selected.snapshot.effects.isEmpty)
    XCTAssertTrue(converter.completedSourceIDs.isEmpty)
    XCTAssertTrue(converter.learnedSourceIDs.isEmpty)

    let committed = reducer.reduce(.commitAll, requestID: "commit-all")
    XCTAssertEqual(committed.snapshot.effects.count, 1)
    XCTAssertEqual(converter.completedSourceIDs, ["second"])
    XCTAssertEqual(converter.learnedSourceIDs, ["second"])
  }

  func testPartialCommitKeepsRemainingCompositionAndLearnsOnce() {
    let converter = ReducerFixtureConverter()
    let reducer = ImeReducer(converter: converter)
    _ = reducer.reduce(.insertText("きょうはいしゃ"), requestID: "insert")
    _ = reducer.reduce(.startConversion, requestID: "convert")

    let result = reducer.reduce(.commitSelected, requestID: "partial")
    XCTAssertEqual(result.snapshot.effects.count, 1)
    XCTAssertEqual(result.snapshot.effects.first, .commitText(effectID: 1, text: "変換"))
    XCTAssertFalse(reducer.session.composingText.isEmpty)
    XCTAssertEqual(reducer.session.phase, .previewing)
    XCTAssertEqual(converter.completed, 1)
    XCTAssertEqual(converter.learningUpdates, 1)
  }

  func testConverterFailureAndEmptyCandidatesPreserveInput() {
    let converter = ReducerFixtureConverter()
    let reducer = ImeReducer(converter: converter)
    _ = reducer.reduce(.insertText("入力"), requestID: "insert")
    converter.shouldFail = true

    let failed = reducer.reduce(.startConversion, requestID: "failed")
    XCTAssertEqual(failed.status, .converterUnavailable)
    XCTAssertEqual(reducer.session.composingText.text, "入力")
    XCTAssertEqual(reducer.session.phase, .composing)
  }

  func testSecureInputDisablesLearningAndCheckpoint() throws {
    let converter = ReducerFixtureConverter()
    var session = CompositionSession()
    session.policy = PinnedCompositionPolicy(
      allowsLearning: true,
      secureInput: true,
      zenzaiEnabled: true,
      projectRevision: 42
    )
    let reducer = ImeReducer(session: session, converter: converter)
    _ = reducer.reduce(.insertText("秘密"), requestID: "insert")
    _ = reducer.reduce(.startConversion, requestID: "convert")
    let result = reducer.reduce(.commitSelected, requestID: "commit")

    XCTAssertEqual(converter.completed, 1)
    XCTAssertEqual(converter.learningUpdates, 0)
    XCTAssertNil(result.snapshot.recovery)
  }

  func testSecureInputCanReturnToThePinnedLearningPolicy() {
    let converter = ReducerFixtureConverter()
    let reducer = ImeReducer(converter: converter)
    _ = reducer.reduce(.lifecycle(.secureInputChanged(true)), requestID: "secure-on")
    _ = reducer.reduce(.lifecycle(.secureInputChanged(false)), requestID: "secure-off")
    _ = reducer.reduce(.insertText("学習"), requestID: "insert")
    _ = reducer.reduce(.startConversion, requestID: "convert")
    _ = reducer.reduce(.commitSelected, requestID: "commit")

    XCTAssertEqual(converter.learningUpdates, 1)
  }

  func testSecureBoundaryDropsCompositionContextAndCheckpointState() throws {
    let reducer = ImeReducer()
    _ = reducer.reduce(
      .updateContext(leftContext: "private-left", rightContext: "private-right"),
      requestID: "context"
    )
    _ = reducer.reduce(.insertText("draft"), requestID: "draft")

    let entered = reducer.reduce(
      .lifecycle(.secureInputChanged(true)),
      requestID: "secure-on"
    )
    XCTAssertEqual(entered.snapshot.phase, .idle)
    XCTAssertTrue(entered.snapshot.preedit.isEmpty)
    XCTAssertNil(entered.snapshot.recovery)
    XCTAssertTrue(reducer.session.composingText.isEmpty)
    XCTAssertEqual(reducer.session.context.leftContext, "")
    XCTAssertEqual(reducer.session.context.rightContext, "")
    XCTAssertNil(reducer.session.recoveryCheckpoint)

    let secureText = reducer.reduce(.insertText("password"), requestID: "secure-text")
    XCTAssertNil(secureText.snapshot.recovery)
    XCTAssertNil(reducer.session.recoveryCheckpoint)

    let exited = reducer.reduce(
      .lifecycle(.secureInputChanged(false)),
      requestID: "secure-off"
    )
    XCTAssertEqual(exited.snapshot.phase, .idle)
    XCTAssertTrue(exited.snapshot.preedit.isEmpty)
    XCTAssertTrue(reducer.session.composingText.isEmpty)
    let recovery = try XCTUnwrap(exited.snapshot.recovery)
    XCTAssertEqual(recovery.revision, exited.snapshot.revision)
    XCTAssertEqual(recovery.phase, .idle)
    XCTAssertTrue(recovery.composition.isEmpty)
    XCTAssertEqual(recovery.leftContext, "")
    XCTAssertEqual(recovery.rightContext, "")
    XCTAssertFalse(recovery.policy.secureInput)
    XCTAssertNil(recovery.reconversionReplacement)
    XCTAssertEqual(recovery.unicodeInputBuffer, "")
    XCTAssertNil(recovery.phaseBeforeUnicodeInput)
    XCTAssertEqual(reducer.session.recoveryCheckpoint, recovery)
  }

  func testStaleRevisionDoesNotMutateTheSession() {
    let reducer = ImeReducer()
    let first = reducer.reduce(.insertText("a"), requestID: "insert")
    let stale = reducer.reduce(
      .insertText("b"),
      requestID: "stale-revision",
      expectedRevision: first.snapshot.revision - 1
    )

    XCTAssertEqual(stale.status, .staleRevision)
    XCTAssertEqual(reducer.session.composingText.text, "a")
    XCTAssertEqual(reducer.session.revision, first.snapshot.revision)
  }

  func testCommitAllUsesTheSameConvertedDisplayAsTheSnapshot() {
    let converter = ReducerFixtureConverter()
    converter.displayOverride = "かな"
    let reducer = ImeReducer(converter: converter)

    let composing = reducer.reduce(.insertText("kana"), requestID: "insert")
    XCTAssertEqual(composing.snapshot.preedit.first?.text, "かな")
    let committed = reducer.reduce(.commitAll, requestID: "commit")

    XCTAssertEqual(
      committed.snapshot.effects,
      [.commitText(effectID: 1, text: "かな")]
    )
    XCTAssertEqual(converter.stopCount, 1)
  }

  func testKanaAndWidthTransformsCoverVoicedKatakana() {
    let reducer = ImeReducer()
    _ = reducer.reduce(.insertText("がく"), requestID: "insert")

    let halfwidth = reducer.reduce(
      .transformActiveSegment(.katakanaHalfwidth),
      requestID: "halfwidth"
    )
    XCTAssertEqual(halfwidth.snapshot.preedit.first?.text, "ｶﾞｸ")

    let hiragana = reducer.reduce(
      .transformActiveSegment(.hiragana),
      requestID: "hiragana"
    )
    XCTAssertEqual(hiragana.snapshot.preedit.first?.text, "がく")
  }

  func testCheckpointRestorePreservesCompositionAndNextEffectID() throws {
    let original = ImeReducer()
    _ = original.reduce(.insertText("a"), requestID: "a")
    _ = original.reduce(.commitAll, requestID: "commit-a")
    let pending = original.reduce(.insertText("b"), requestID: "b")
    let checkpoint = try XCTUnwrap(pending.snapshot.recovery)
    let data = try XCTUnwrap(checkpoint.persistedData(isSecureInput: false))

    let restored = ImeReducer()
    let restoreResult = restored.reduce(
      .restoreCheckpoint(data),
      requestID: "restore",
      expectedRevision: 0
    )
    XCTAssertEqual(restoreResult.status, .success)
    XCTAssertEqual(restoreResult.snapshot.preedit.first?.text, "b")

    let committed = restored.reduce(.commitAll, requestID: "commit-b")
    XCTAssertEqual(
      committed.snapshot.effects,
      [.commitText(effectID: 2, text: "b")]
    )
  }

  func testCheckpointRestoreRebindsProcessLocalInputTable() throws {
    var originalSession = CompositionSession()
    originalSession.policy = PinnedCompositionPolicy(
      allowsLearning: false,
      secureInput: false,
      zenzaiEnabled: false,
      projectRevision: 42,
      inputTableName: "old-process-table",
      keymap: ["k": PinnedKeymapRule(intention: "k", inputOverride: nil)]
    )
    let original = ImeReducer(session: originalSession)
    let pending = original.reduce(.insertText("k"), requestID: "insert")
    let data = try XCTUnwrap(
      try XCTUnwrap(pending.snapshot.recovery).persistedData(isSecureInput: false)
    )

    var replacementSession = CompositionSession()
    replacementSession.policy.inputTableName = "new-process-table"
    let replacement = ImeReducer(session: replacementSession)
    let result = replacement.reduce(
      .restoreCheckpoint(data),
      requestID: "restore",
      expectedRevision: 0
    )

    XCTAssertEqual(result.status, .success)
    XCTAssertEqual(replacement.session.policy.inputTableName, "new-process-table")
    XCTAssertEqual(replacement.session.policy.projectRevision, 42)
    XCTAssertFalse(replacement.session.policy.allowsLearning)
    XCTAssertEqual(replacement.session.policy.keymap, originalSession.policy.keymap)
  }

  func testCheckpointCannotElevateCurrentLearningOrZenzaiPolicy() throws {
    var checkpointPolicy = PinnedCompositionPolicy.default
    checkpointPolicy.allowsLearning = true
    checkpointPolicy.zenzaiEnabled = true
    let checkpoint = RecoveryCheckpoint(
      revision: 1,
      phase: .composing,
      composition: CompositionBuffer(
        elements: [CompositionElement(text: "a")]
      ),
      nextCandidateGeneration: 0,
      nextEffectID: 1,
      leftContext: "",
      rightContext: "",
      policy: checkpointPolicy
    )
    let data = try XCTUnwrap(checkpoint.persistedData(isSecureInput: false))

    var restrictedSession = CompositionSession()
    restrictedSession.policy.allowsLearning = false
    restrictedSession.policy.zenzaiEnabled = false
    let reducer = ImeReducer(session: restrictedSession)
    let result = reducer.reduce(
      .restoreCheckpoint(data),
      requestID: "restore",
      expectedRevision: 0
    )

    XCTAssertEqual(result.status, .success)
    XCTAssertFalse(reducer.session.policy.allowsLearning)
    XCTAssertFalse(reducer.session.policy.zenzaiEnabled)
  }

  func testCheckpointRejectsWrappingCountersAndInvalidReplacementRanges() throws {
    let wrapping = RecoveryCheckpoint(
      revision: UInt64.max,
      phase: .composing,
      composition: CompositionBuffer(
        elements: [CompositionElement(text: "a")]
      ),
      nextCandidateGeneration: UInt64.max,
      nextEffectID: UInt64.max,
      leftContext: "",
      rightContext: "",
      policy: .default
    )
    let wrappingData = try XCTUnwrap(wrapping.persistedData(isSecureInput: false))
    XCTAssertEqual(
      ImeReducer().reduce(
        .restoreCheckpoint(wrappingData),
        requestID: "wrapping",
        expectedRevision: 0
      ).status,
      .invalidAction
    )

    let invalidRange = RecoveryCheckpoint(
      revision: 1,
      phase: .reconverting,
      composition: CompositionBuffer(
        elements: [CompositionElement(text: "a")]
      ),
      nextCandidateGeneration: 0,
      nextEffectID: 1,
      leftContext: "",
      rightContext: "",
      policy: .default,
      reconversionReplacement: ReconversionReplacement(
        before: Int(Int32.max) + 1,
        after: 0
      )
    )
    let invalidRangeData = try XCTUnwrap(
      invalidRange.persistedData(isSecureInput: false)
    )
    XCTAssertEqual(
      ImeReducer().reduce(
        .restoreCheckpoint(invalidRangeData),
        requestID: "invalid-range",
        expectedRevision: 0
      ).status,
      .invalidAction
    )
  }

  func testSecureContextNeverStoresSurroundingTextOrRestoresCheckpoint() throws {
    var session = CompositionSession()
    session.policy.secureInput = true
    let reducer = ImeReducer(session: session)

    let context = reducer.reduce(
      .updateContext(leftContext: "secret-left", rightContext: "secret-right"),
      requestID: "context"
    )
    XCTAssertEqual(context.status, .success)
    XCTAssertEqual(reducer.session.context.leftContext, "")
    XCTAssertEqual(reducer.session.context.rightContext, "")

    let normal = ImeReducer()
    let pending = normal.reduce(.insertText("safe"), requestID: "insert")
    let checkpoint = try XCTUnwrap(pending.snapshot.recovery)
    let data = try XCTUnwrap(checkpoint.persistedData(isSecureInput: false))
    let rejected = reducer.reduce(
      .restoreCheckpoint(data),
      requestID: "restore",
      expectedRevision: context.snapshot.revision
    )
    XCTAssertEqual(rejected.status, .secureInputViolation)
  }

  func testUnicodeInputPreservesCompositionAndInsertsOneScalar() {
    let reducer = ImeReducer()
    _ = reducer.reduce(.insertText("a"), requestID: "a")
    let began = reducer.reduce(.beginUnicodeInput, requestID: "unicode-begin")
    XCTAssertEqual(began.snapshot.phase, .unicodeInput)

    for (index, digit) in ["1", "f", "6", "0", "0"].enumerated() {
      _ = reducer.reduce(
        .appendUnicodeDigit(digit),
        requestID: "unicode-digit-\(index)"
      )
    }
    XCTAssertEqual(reducer.currentSnapshot().aux, "Unicode U+1F600")
    let inserted = reducer.reduce(.commitUnicodeInput, requestID: "unicode-commit")

    XCTAssertEqual(inserted.status, .success)
    XCTAssertEqual(inserted.snapshot.phase, .composing)
    XCTAssertEqual(inserted.snapshot.preedit.first?.text, "a😀")
    XCTAssertEqual(
      inserted.snapshot.caretUtf8ByteOffset,
      UInt32("a😀".utf8.count)
    )
  }

  func testUnicodeInputRejectsInvalidScalarWithoutLosingDigits() {
    let reducer = ImeReducer()
    _ = reducer.reduce(.beginUnicodeInput, requestID: "begin")
    for (index, digit) in ["d", "8", "0", "0"].enumerated() {
      _ = reducer.reduce(.appendUnicodeDigit(digit), requestID: "digit-\(index)")
    }

    let invalid = reducer.reduce(.commitUnicodeInput, requestID: "commit")
    XCTAssertEqual(invalid.status, .invalidAction)
    XCTAssertEqual(invalid.snapshot.phase, .unicodeInput)
    XCTAssertEqual(invalid.snapshot.aux, "Unicode U+D800")

    let cancelled = reducer.reduce(.cancel, requestID: "cancel")
    XCTAssertEqual(cancelled.snapshot.phase, .idle)
    XCTAssertTrue(cancelled.snapshot.effects.isEmpty)
  }

  func testReconversionDeletesSelectionExactlyOnceBeforeCommit() {
    let converter = ReducerFixtureConverter()
    let reducer = ImeReducer(converter: converter)
    _ = reducer.reduce(
      .reconvert(
        text: "かな",
        leftContext: "左",
        rightContext: "右",
        deleteBefore: 2,
        deleteAfter: 0
      ),
      requestID: "reconvert"
    )

    let committed = reducer.reduce(.commitSelected, requestID: "commit")
    XCTAssertEqual(
      committed.snapshot.effects,
      [
        .deleteSurroundingText(effectID: 1, before: 2, after: 0),
        .commitText(effectID: 2, text: "変換"),
      ]
    )
    XCTAssertEqual(
      reducer.reduce(.commitSelected, requestID: "commit"),
      committed
    )
  }

  func testForgetCandidateUsesGenerationAndDoesNotCommit() {
    let converter = ReducerFixtureConverter()
    let reducer = ImeReducer(converter: converter)
    _ = reducer.reduce(.insertText("かな"), requestID: "insert")
    let converted = reducer.reduce(.startConversion, requestID: "convert")
    let candidate = converted.snapshot.candidateWindow.items[0]
    let forgotten = reducer.reduce(
      .forgetCandidate(
        id: candidate.id,
        generation: converted.snapshot.candidateWindow.generation
      ),
      requestID: "forget"
    )

    XCTAssertEqual(forgotten.status, .success)
    XCTAssertTrue(forgotten.snapshot.effects.isEmpty)
    XCTAssertEqual(converter.forgotten, 1)
    XCTAssertFalse(reducer.session.composingText.isEmpty)
  }

  func testForgetCandidateIsSuccessfulNoOpWhenLearningIsDisabled() {
    let converter = ReducerFixtureConverter()
    var session = CompositionSession()
    session.policy.allowsLearning = false
    let reducer = ImeReducer(session: session, converter: converter)
    _ = reducer.reduce(.insertText("かな"), requestID: "insert")
    let converted = reducer.reduce(.startConversion, requestID: "convert")
    let candidate = converted.snapshot.candidateWindow.items[0]
    let forgotten = reducer.reduce(
      .forgetCandidate(
        id: candidate.id,
        generation: converted.snapshot.candidateWindow.generation
      ),
      requestID: "forget-disabled"
    )

    XCTAssertEqual(forgotten.status, .success)
    XCTAssertEqual(forgotten.snapshot.revision, converted.snapshot.revision + 1)
    XCTAssertTrue(forgotten.snapshot.effects.isEmpty)
    XCTAssertFalse(forgotten.snapshot.pendingLearning)
    XCTAssertEqual(converter.forgotten, 0)
    XCTAssertFalse(reducer.session.composingText.isEmpty)
    XCTAssertEqual(
      reducer.reduce(
        .forgetCandidate(
          id: candidate.id,
          generation: converted.snapshot.candidateWindow.generation
        ),
        requestID: "forget-disabled"
      ),
      forgotten
    )
    XCTAssertEqual(converter.forgotten, 0)
  }

  func testPredictionsStayInComposingUntilExplicitlySelected() {
    let converter = ReducerFixtureConverter()
    converter.predictionCandidates = [
      ConverterCandidate(text: "かな予測", annotation: "予測", consumingCount: 2)
    ]
    let reducer = ImeReducer(converter: converter)

    let composing = reducer.reduce(.insertText("かな"), requestID: "insert")
    XCTAssertEqual(composing.snapshot.phase, .composing)
    XCTAssertNil(composing.snapshot.candidateWindow.selectedIndex)
    XCTAssertEqual(composing.snapshot.candidateWindow.items.first?.text, "かな予測")
    XCTAssertEqual(composing.snapshot.preedit.first?.text, "かな")

    let selected = reducer.reduce(.navigateCandidate(0), requestID: "select")
    XCTAssertEqual(selected.snapshot.phase, .selecting)
    XCTAssertEqual(selected.snapshot.candidateWindow.selectedIndex, 0)
    XCTAssertEqual(selected.snapshot.preedit.first?.text, "かな予測")

    let accepted = reducer.reduce(.commitSelected, requestID: "accept")
    XCTAssertEqual(
      accepted.snapshot.effects,
      [.commitText(effectID: 1, text: "かな予測")]
    )
  }

  func testAutoConversionPublishesLiveCandidateWithoutEnteringPreviewing() {
    let converter = ReducerFixtureConverter()
    var session = CompositionSession()
    session.policy.autoConvertMode = .forMultipleChars
    session.policy.liveConversionDelayMilliseconds = 0
    let reducer = ImeReducer(session: session, converter: converter)

    let first = reducer.reduce(.insertText("か"), requestID: "first")
    XCTAssertEqual(first.snapshot.phase, .composing)
    XCTAssertEqual(first.snapshot.preedit.first?.text, "か")
    XCTAssertEqual(first.snapshot.preedit.first?.style, .underline)

    let second = reducer.reduce(.insertText("な"), requestID: "second")
    XCTAssertEqual(second.snapshot.phase, .composing)
    XCTAssertEqual(second.snapshot.preedit.first?.text, "変換")
    XCTAssertEqual(second.snapshot.preedit.first?.style, .active)
    XCTAssertNil(second.snapshot.candidateWindow.selectedIndex)

    let committed = reducer.reduce(.commitAll, requestID: "commit")
    XCTAssertEqual(
      committed.snapshot.effects,
      [.commitText(effectID: 1, text: "変換")]
    )
    XCTAssertEqual(converter.completed, 1)
  }

  func testAutoConversionAlwaysPublishesSingleCharacterLiveCandidate() {
    let converter = ReducerFixtureConverter()
    var session = CompositionSession()
    session.policy.autoConvertMode = .always
    session.policy.liveConversionDelayMilliseconds = 0
    let reducer = ImeReducer(session: session, converter: converter)

    let result = reducer.reduce(.insertText("か"), requestID: "insert")
    XCTAssertEqual(result.snapshot.phase, .composing)
    XCTAssertEqual(result.snapshot.preedit.first?.text, "変換")
    XCTAssertEqual(result.snapshot.preedit.first?.style, .active)
    XCTAssertTrue(result.snapshot.effects.isEmpty)
    XCTAssertEqual(converter.realtimeRequests, 1)
  }

  func testAutoConversionSchedulesWithoutCallingTheConverter() throws {
    let converter = ReducerFixtureConverter()
    var session = CompositionSession()
    session.policy.autoConvertMode = .always
    session.policy.liveConversionDelayMilliseconds = 228
    let reducer = ImeReducer(session: session, converter: converter)

    let result = reducer.reduce(.insertText("か"), requestID: "insert")

    XCTAssertEqual(result.status, .success)
    XCTAssertEqual(result.snapshot.revision, 1)
    XCTAssertEqual(result.snapshot.phase, .composing)
    XCTAssertEqual(
      result.snapshot.preedit,
      [PreeditSpan(text: "か", style: .underline)]
    )
    XCTAssertEqual(
      result.snapshot.effects,
      [
        .scheduleLiveConversion(
          effectID: 1,
          delayMilliseconds: 228,
          scheduledRevision: 1
        )
      ]
    )
    XCTAssertEqual(converter.realtimeRequests, 0)
    XCTAssertEqual(try XCTUnwrap(result.snapshot.recovery).nextEffectID, 2)
  }

  func testOnlyLatestScheduledRevisionAppliesLiveConversionOnce() {
    let converter = ReducerFixtureConverter()
    var session = CompositionSession()
    session.policy.autoConvertMode = .always
    session.policy.liveConversionDelayMilliseconds = 228
    let reducer = ImeReducer(session: session, converter: converter)

    let first = reducer.reduce(.insertText("か"), requestID: "first")
    let second = reducer.reduce(
      .insertText("な"),
      requestID: "second",
      expectedRevision: first.snapshot.revision
    )
    XCTAssertEqual(
      second.snapshot.effects,
      [
        .scheduleLiveConversion(
          effectID: 2,
          delayMilliseconds: 228,
          scheduledRevision: 2
        )
      ]
    )
    XCTAssertEqual(converter.realtimeRequests, 0)

    let stale = reducer.reduce(
      .applyLiveConversion(scheduledRevision: 1),
      requestID: "stale-timer",
      expectedRevision: second.snapshot.revision
    )
    XCTAssertEqual(stale.status, .success)
    XCTAssertEqual(stale.snapshot.revision, second.snapshot.revision)
    XCTAssertEqual(
      stale.snapshot.preedit,
      [PreeditSpan(text: "かな", style: .underline)]
    )
    XCTAssertTrue(stale.snapshot.effects.isEmpty)
    XCTAssertEqual(converter.realtimeRequests, 0)

    let latest = reducer.reduce(
      .applyLiveConversion(scheduledRevision: second.snapshot.revision),
      requestID: "latest-timer",
      expectedRevision: second.snapshot.revision
    )
    XCTAssertEqual(latest.status, .success)
    XCTAssertEqual(latest.snapshot.revision, 3)
    XCTAssertEqual(
      latest.snapshot.preedit,
      [PreeditSpan(text: "変換", style: .active)]
    )
    XCTAssertTrue(latest.snapshot.effects.isEmpty)
    XCTAssertEqual(converter.realtimeRequests, 1)

    let duplicate = reducer.reduce(
      .applyLiveConversion(scheduledRevision: second.snapshot.revision),
      requestID: "latest-timer",
      expectedRevision: second.snapshot.revision
    )
    XCTAssertEqual(duplicate, latest)
    XCTAssertEqual(converter.realtimeRequests, 1)
  }

  func testSecureInputNeverSchedulesOrAppliesLiveConversion() {
    let converter = ReducerFixtureConverter()
    var session = CompositionSession()
    session.policy.autoConvertMode = .always
    session.policy.liveConversionDelayMilliseconds = 228
    session.policy.secureInput = true
    let reducer = ImeReducer(session: session, converter: converter)

    let result = reducer.reduce(.insertText("秘密"), requestID: "secure")

    XCTAssertEqual(result.status, .success)
    XCTAssertEqual(
      result.snapshot.preedit,
      [PreeditSpan(text: "秘密", style: .underline)]
    )
    XCTAssertTrue(result.snapshot.effects.isEmpty)
    XCTAssertNil(result.snapshot.recovery)
    XCTAssertEqual(converter.realtimeRequests, 0)

    let delayed = reducer.reduce(
      .applyLiveConversion(scheduledRevision: result.snapshot.revision),
      requestID: "secure-timer",
      expectedRevision: result.snapshot.revision
    )
    XCTAssertEqual(delayed.status, .success)
    XCTAssertEqual(delayed.snapshot.revision, result.snapshot.revision)
    XCTAssertTrue(delayed.snapshot.effects.isEmpty)
    XCTAssertEqual(converter.realtimeRequests, 0)
  }

  func testReturningTheCaretToTheEndSchedulesLiveConversion() {
    let converter = ReducerFixtureConverter()
    var session = CompositionSession()
    session.policy.autoConvertMode = .always
    session.policy.liveConversionDelayMilliseconds = 228
    let reducer = ImeReducer(session: session, converter: converter)

    let inserted = reducer.reduce(.insertText("かな"), requestID: "insert")
    let movedLeft = reducer.reduce(
      .moveCursor(-1),
      requestID: "left",
      expectedRevision: inserted.snapshot.revision
    )
    XCTAssertTrue(movedLeft.snapshot.effects.isEmpty)
    XCTAssertEqual(movedLeft.snapshot.caretUtf8ByteOffset, UInt32("か".utf8.count))

    let movedToEnd = reducer.reduce(
      .moveCursorToEnd,
      requestID: "end",
      expectedRevision: movedLeft.snapshot.revision
    )
    XCTAssertEqual(
      movedToEnd.snapshot.effects,
      [
        .scheduleLiveConversion(
          effectID: 2,
          delayMilliseconds: 228,
          scheduledRevision: 3
        )
      ]
    )
    XCTAssertEqual(converter.realtimeRequests, 0)
  }

  func testAutoConversionForMultipleCharsUsesRenderedReadingLength() {
    let converter = ReducerFixtureConverter()
    converter.displayOverride = "か"
    var session = CompositionSession()
    session.policy.autoConvertMode = .forMultipleChars
    session.policy.liveConversionDelayMilliseconds = 228
    let reducer = ImeReducer(session: session, converter: converter)

    let result = reducer.reduce(.insertText("ka"), requestID: "insert")

    XCTAssertEqual(result.snapshot.preedit.first?.text, "か")
    XCTAssertEqual(result.snapshot.preedit.first?.style, .underline)
    XCTAssertNil(reducer.session.candidates?.liveCandidate)
    XCTAssertTrue(result.snapshot.effects.isEmpty)
    XCTAssertEqual(converter.realtimeRequests, 0)
  }

  func testForMultipleCharsStopsSchedulingAfterDeletionToOneCharacter() {
    let converter = ReducerFixtureConverter()
    var session = CompositionSession()
    session.policy.autoConvertMode = .forMultipleChars
    session.policy.liveConversionDelayMilliseconds = 228
    let reducer = ImeReducer(session: session, converter: converter)

    let inserted = reducer.reduce(.insertText("かな"), requestID: "insert")
    XCTAssertEqual(
      inserted.snapshot.effects,
      [
        .scheduleLiveConversion(
          effectID: 1,
          delayMilliseconds: 228,
          scheduledRevision: 1
        )
      ]
    )

    let deleted = reducer.reduce(
      .deleteBackward,
      requestID: "delete",
      expectedRevision: inserted.snapshot.revision
    )
    XCTAssertEqual(
      deleted.snapshot.preedit,
      [PreeditSpan(text: "か", style: .underline)]
    )
    XCTAssertTrue(deleted.snapshot.effects.isEmpty)
    XCTAssertEqual(converter.realtimeRequests, 0)
  }

  func testLeftDuringAutoConversionMovesTheReadingCursor() {
    let converter = ReducerFixtureConverter()
    var session = CompositionSession()
    session.policy.autoConvertMode = .always
    session.policy.liveConversionDelayMilliseconds = 0
    let reducer = ImeReducer(session: session, converter: converter)
    _ = reducer.reduce(.insertText("かな"), requestID: "insert")

    let moved = reducer.reduce(.moveCursor(-1), requestID: "left")

    XCTAssertEqual(moved.snapshot.phase, .composing)
    XCTAssertEqual(moved.snapshot.preedit.first?.text, "かな")
    XCTAssertEqual(moved.snapshot.preedit.first?.style, .underline)
    XCTAssertEqual(moved.snapshot.caretUtf8ByteOffset, UInt32("か".utf8.count))
    XCTAssertNil(reducer.session.candidates?.liveCandidate)
    XCTAssertEqual(reducer.session.composingText.cursor, 1)
    XCTAssertNil(reducer.session.activeSegmentIndex)
    XCTAssertTrue(reducer.session.segments.isEmpty)
  }

  func testTextTransformUsesReadingInsteadOfUnselectedLiveCandidate() {
    let converter = ReducerFixtureConverter()
    var session = CompositionSession()
    session.policy.autoConvertMode = .always
    session.policy.liveConversionDelayMilliseconds = 0
    let reducer = ImeReducer(session: session, converter: converter)
    _ = reducer.reduce(.insertText("かな"), requestID: "insert")

    let transformed = reducer.reduce(
      .transformActiveSegment(.katakanaFullwidth),
      requestID: "katakana"
    )

    XCTAssertEqual(transformed.snapshot.preedit.first?.text, "カナ")
    XCTAssertEqual(transformed.snapshot.preedit.first?.style, .underline)
    XCTAssertTrue(transformed.snapshot.candidateWindow.items.isEmpty)
    XCTAssertEqual(reducer.session.composingText.text, "カナ")
  }

  func testComposingSelectionAndSegmentResizeAreSemanticActions() {
    let converter = ReducerFixtureConverter()
    let reducer = ImeReducer(converter: converter)
    _ = reducer.reduce(.insertText("きょう"), requestID: "insert")

    let selected = reducer.reduce(.navigateCandidate(0), requestID: "down")
    XCTAssertEqual(selected.status, .success)
    XCTAssertEqual(selected.snapshot.phase, .selecting)
    XCTAssertEqual(selected.snapshot.candidateWindow.selectedIndex, 0)

    _ = reducer.reduce(.cancel, requestID: "back-to-preview")
    _ = reducer.reduce(.cancel, requestID: "back-to-composing")
    let resized = reducer.reduce(.resizeSegment(1), requestID: "shift-right")
    XCTAssertEqual(resized.status, .success)
    XCTAssertEqual(resized.snapshot.phase, .selecting)
    XCTAssertEqual(resized.snapshot.candidateWindow.items.first?.consumingCount, 1)
    XCTAssertEqual(reducer.session.activeBoundary, 1)
    XCTAssertEqual(reducer.session.segments.map(\.inputCount), [1, 2])
    XCTAssertEqual(resized.snapshot.preedit.count, 2)
    XCTAssertEqual(resized.snapshot.preedit[0], PreeditSpan(text: "変換", style: .active))
    XCTAssertEqual(resized.snapshot.preedit[1].style, .underline)
    XCTAssertEqual(
      reducer.session.segments.map(\.inputCount).reduce(0, +),
      reducer.session.composingText.elements.count,
      "converted segments must cover the input exactly once"
    )
  }

  func testCandidatePagingUsesGlobalIndicesAndClampsAtEdges() {
    let reducer = ImeReducer(converter: PagingFixtureConverter())
    _ = reducer.reduce(.insertText("かな"), requestID: "insert")
    let converted = reducer.reduce(.startConversion, requestID: "convert")
    let generation = converted.snapshot.candidateWindow.generation

    let nextPage = reducer.reduce(.navigateCandidatePage(1), requestID: "page-1")
    XCTAssertEqual(nextPage.snapshot.candidateWindow.selectedIndex, 3)
    let lastPage = reducer.reduce(.navigateCandidatePage(100), requestID: "page-last")
    XCTAssertEqual(lastPage.snapshot.candidateWindow.selectedIndex, 10)
    let clamped = reducer.reduce(.navigateCandidate(1), requestID: "edge")
    XCTAssertEqual(clamped.snapshot.candidateWindow.selectedIndex, 10)
    XCTAssertEqual(clamped.snapshot.candidateWindow.generation, generation)
  }

  func testReconversionKeepsItsPhaseAndPassesBothContexts() {
    let converter = ReducerFixtureConverter()
    let reducer = ImeReducer(converter: converter)
    let converted = reducer.reduce(
      .reconvert(
        text: "かな",
        leftContext: "左",
        rightContext: "右",
        deleteBefore: 2,
        deleteAfter: 0
      ),
      requestID: "reconvert"
    )

    XCTAssertEqual(converted.snapshot.phase, .reconverting)
    XCTAssertEqual(converter.lastOptions?.leftContext, "左")
    XCTAssertEqual(converter.lastOptions?.rightContext, "右")
    XCTAssertEqual(
      reducer.reduce(.cancel, requestID: "cancel").snapshot.phase,
      .idle
    )
  }

  func testPolicyProviderIsPinnedOncePerComposition() {
    let converter = ReducerFixtureConverter()
    var providerCalls = 0
    let controller = ImeV2SessionController(
      reducer: ImeReducer(converter: converter),
      policyProvider: {
        providerCalls += 1
        return PinnedCompositionPolicy(
          allowsLearning: true,
          secureInput: false,
          zenzaiEnabled: true,
          projectRevision: UInt64(providerCalls),
          inputTableName: "table-\(providerCalls)"
        )
      }
    )

    let first = controller.handle(ImeV2Request(
      requestID: "a",
      expectedRevision: 0,
      action: .insertText("a")
    ))
    let second = controller.handle(ImeV2Request(
      requestID: "b",
      expectedRevision: first.snapshot.revision,
      action: .insertText("b")
    ))
    XCTAssertEqual(second.status, .success)
    XCTAssertEqual(providerCalls, 1)

    let committed = controller.handle(ImeV2Request(
      requestID: "commit",
      expectedRevision: second.snapshot.revision,
      action: .commitAll
    ))
    _ = controller.handle(ImeV2Request(
      requestID: "c",
      expectedRevision: committed.snapshot.revision,
      action: .insertText("c")
    ))
    XCTAssertEqual(providerCalls, 2)
  }

  func testPinnedKeymapIsAttachedToMappedElements() {
    let converter = ReducerFixtureConverter()
    var session = CompositionSession()
    session.policy.keymap = [
      "q": PinnedKeymapRule(intention: "た", inputOverride: nil),
      "x": PinnedKeymapRule(intention: "ん", inputOverride: "n"),
    ]
    let reducer = ImeReducer(session: session, converter: converter)

    _ = reducer.reduce(.insertText("qx"), requestID: "mapped")

    XCTAssertEqual(converter.lastComposition?.elements[0].text, "q")
    XCTAssertEqual(converter.lastComposition?.elements[0].mappedIntention, "た")
    XCTAssertNil(converter.lastComposition?.elements[0].mappedInputOverride)
    XCTAssertEqual(converter.lastComposition?.elements[1].mappedIntention, "ん")
    XCTAssertEqual(converter.lastComposition?.elements[1].mappedInputOverride, "n")
  }

  func testDeterministicRandomActionsPreserveCoreInvariants() {
    let converter = ReducerFixtureConverter()
    let reducer = ImeReducer(converter: converter)
    let corpus = [
      "あいう",
      "𠮷野家",
      "は\u{3099}",
      "👨‍👩‍👧‍👦",
      "✈️",
      "A日本語",
      "ｶﾅ",
      "😀",
    ]
    let transforms: [ImeTextTransform] = [
      .hiragana,
      .katakanaFullwidth,
      .katakanaHalfwidth,
      .alphabetFullwidth,
      .alphabetHalfwidth,
    ]
    var generator = ReducerDeterministicGenerator()
    var seenEffectIDs = Set<UInt64>()

    func effectID(_ effect: ClientEffect) -> UInt64 {
      switch effect {
      case .commitText(let id, _),
           .deleteSurroundingText(let id, _, _),
           .switchInputMode(let id, _),
           .notify(let id, _),
           .scheduleLiveConversion(let id, _, _):
        return id
      }
    }

    for step in 0..<1_000 {
      let before = reducer.currentSnapshot()
      let action: ImeAction
      switch generator.next(20) {
      case 0:
        action = .insertText(corpus[generator.next(corpus.count)])
      case 1:
        action = .deleteBackward
      case 2:
        action = .deleteForward
      case 3:
        action = .moveCursor(generator.next(2) == 0 ? -1 : 1)
      case 4:
        action = generator.next(2) == 0 ? .moveCursorToStart : .moveCursorToEnd
      case 5:
        action = .startConversion
      case 6:
        action = .navigateCandidate(generator.next(3) - 1)
      case 7:
        action = .navigateCandidatePage(generator.next(2) == 0 ? -1 : 1)
      case 8:
        action = .resizeSegment(generator.next(2) == 0 ? -1 : 1)
      case 9:
        action = .commitSelected
      case 10:
        action = .commitAll
      case 11:
        action = .cancel
      case 12:
        action = .transformActiveSegment(transforms[generator.next(transforms.count)])
      case 13:
        if let candidates = reducer.session.candidates,
           let candidate = candidates.items.first {
          action = .selectCandidate(
            id: candidate.id,
            generation: generator.next(3) == 0
              ? candidates.generation &+ 1
              : candidates.generation
          )
        } else {
          action = .selectCandidate(id: "missing", generation: 0)
        }
      case 14:
        if let candidates = reducer.session.candidates,
           let candidate = candidates.items.first {
          action = .forgetCandidate(
            id: candidate.id,
            generation: generator.next(3) == 0
              ? candidates.generation &+ 1
              : candidates.generation
          )
        } else {
          action = .forgetCandidate(id: "missing", generation: 0)
        }
      case 15:
        action = .beginUnicodeInput
      case 16:
        action = .appendUnicodeDigit(["0", "a", "F"][generator.next(3)])
      case 17:
        action = .commitUnicodeInput
      case 18:
        action = .lifecycle(.capabilityChanged(clientPreedit: generator.next(2) == 0))
      default:
        action = .updateContext(leftContext: "左", rightContext: "右")
      }

      let result = reducer.reduce(
        action,
        requestID: "random-\(step)",
        expectedRevision: before.revision
      )
      let after = reducer.currentSnapshot()

      if result.status != .success {
        XCTAssertEqual(after, before, "failed action mutated state at step \(step): \(action)")
      }
      if let caret = after.caretUtf8ByteOffset {
        XCTAssertLessThanOrEqual(
          Int(caret),
          after.preedit.map(\.text).joined().utf8.count,
          "caret escaped preedit at step \(step)"
        )
      }
      if after.phase == .idle {
        XCTAssertTrue(reducer.session.composingText.isEmpty)
        XCTAssertTrue(after.candidateWindow.items.isEmpty)
      }
      if after.phase == .selecting {
        XCTAssertFalse(after.candidateWindow.items.isEmpty)
      }
      if let selected = after.candidateWindow.selectedIndex {
        XCTAssertTrue(after.candidateWindow.items.indices.contains(selected))
        XCTAssertGreaterThan(after.candidateWindow.generation, 0)
      }
      if reducer.session.policy.secureInput {
        XCTAssertNil(after.recovery)
      }
      for effect in result.snapshot.effects {
        XCTAssertTrue(
          seenEffectIDs.insert(effectID(effect)).inserted,
          "effect ID was reused at step \(step)"
        )
      }
    }
  }
}
