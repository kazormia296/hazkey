import Foundation
import KanaKanjiConverterModule
import XCTest

@testable import hazkey_server

private final class MutableGrimodexRevisionProvider: GrimodexRevisionProviding, @unchecked Sendable {
  private let lock = NSLock()
  private var revision: GrimodexIntegrationRevision

  init(_ revision: GrimodexIntegrationRevision) {
    self.revision = revision
  }

  func latest() -> GrimodexIntegrationRevision {
    lock.lock()
    defer { lock.unlock() }
    return revision
  }

  func update(_ revision: GrimodexIntegrationRevision) {
    lock.lock()
    self.revision = revision
    lock.unlock()
  }
}

final class GrimodexStateIntegrationTests: XCTestCase {
  func testStatePinsOnFirstInputAndAppliesLatestAtReset() {
    let revisionA = makeRevision(1, projectID: "project-a", surface: "刹那")
    let revisionB = makeRevision(2, projectID: "project-b", surface: "星海")
    let provider = MutableGrimodexRevisionProvider(revisionA)
    let state = HazkeyServerState(revisionProvider: provider)

    XCTAssertEqual(state.inputChar(inputString: "a").status, .success)
    XCTAssertEqual(state.grimodexPinnedRevision, revisionA)

    provider.update(revisionB)
    XCTAssertEqual(state.inputChar(inputString: "i").status, .success)
    XCTAssertEqual(state.grimodexPinnedRevision, revisionA)

    XCTAssertEqual(state.createComposingTextInstanse().status, .success)
    XCTAssertNil(state.grimodexPinnedRevision)
    XCTAssertEqual(state.grimodexAppliedRevision, revisionB)

    XCTAssertEqual(state.inputChar(inputString: "u").status, .success)
    XCTAssertEqual(state.grimodexPinnedRevision, revisionB)
  }

  func testSecureRefreshAbortsPreeditCandidatesAndPendingLearning() {
    let conditions = GrimodexProjectConditions(
      topic: "星海年代記",
      style: "簡潔",
      preference: "固有名詞を優先"
    )
    let active = makeRevision(
      3,
      projectID: "project-a",
      surface: "刹那",
      conditions: conditions
    )
    let provider = MutableGrimodexRevisionProvider(active)
    let state = HazkeyServerState(revisionProvider: provider)
    _ = state.inputChar(inputString: "a")
    state.currentCandidateList = []
    state.learningDataNeedsCommit = true
    XCTAssertFalse(state.composingText.value.toHiragana().isEmpty)

    provider.update(
      GrimodexIntegrationRevision(
        generation: 3,
        payload: nil,
        allowsLearning: false,
        secureInput: true
      )
    )
    state.refreshGrimodexIntegration()

    XCTAssertTrue(state.composingText.value.toHiragana().isEmpty)
    XCTAssertNil(state.currentCandidateList)
    XCTAssertFalse(state.learningDataNeedsCommit)
    XCTAssertFalse(state.grimodexAllowsLearning)
    XCTAssertTrue(state.grimodexSecureInput)
    XCTAssertEqual(state.grimodexActiveConditions, .empty)
  }

  func testCompletePrefixDoesNotQueueLearningWhenRevisionDisallowsIt() {
    let disabled = GrimodexIntegrationRevision(
      generation: 1,
      payload: nil,
      allowsLearning: false,
      secureInput: false
    )
    let state = HazkeyServerState(
      revisionProvider: MutableGrimodexRevisionProvider(disabled)
    )
    _ = state.inputChar(inputString: "a")
    state.currentCandidateList = [makeCandidate()]

    XCTAssertEqual(state.completePrefix(candidateIndex: 0).status, .success)
    XCTAssertFalse(state.learningDataNeedsCommit)
  }

  func testCompletePrefixKeepsNormalHazkeyLearningOutsideSecureInput() {
    let enabled = GrimodexIntegrationRevision(generation: 1, payload: nil)
    let state = HazkeyServerState(
      revisionProvider: MutableGrimodexRevisionProvider(enabled)
    )
    _ = state.inputChar(inputString: "a")
    state.currentCandidateList = [makeCandidate()]

    XCTAssertEqual(state.completePrefix(candidateIndex: 0).status, .success)
    XCTAssertTrue(state.learningDataNeedsCommit)
  }

  func testZenzaiResolverKeepsUserProfileAndOnlyOverlaysProjectValues() {
    let resolved = GrimodexZenzaiConditionResolver.resolve(
      profile: "user-profile",
      topic: "user-topic",
      style: "user-style",
      preference: "user-preference",
      project: GrimodexProjectConditions(
        topic: "project-topic",
        style: nil,
        preference: nil
      )
    )

    XCTAssertEqual(
      resolved,
      GrimodexResolvedZenzaiConditions(
        profile: "user-profile",
        topic: "project-topic",
        style: "user-style",
        preference: "user-preference"
      )
    )
  }

  func testZenzaiResolverReturnsPersistentValuesAfterPayloadRemoval() {
    let resolved = GrimodexZenzaiConditionResolver.resolve(
      profile: "user-profile",
      topic: "user-topic",
      style: "user-style",
      preference: "user-preference",
      project: .empty
    )

    XCTAssertEqual(
      resolved,
      GrimodexResolvedZenzaiConditions(
        profile: "user-profile",
        topic: "user-topic",
        style: "user-style",
        preference: "user-preference"
      )
    )
  }

  private func makeCandidate() -> Candidate {
    Candidate(
      text: "あ",
      value: PValue(-5),
      composingCount: .inputCount(1),
      lastMid: 501,
      data: [
        DicdataElement(
          word: "あ",
          ruby: "ア",
          cid: 1288,
          mid: 501,
          value: PValue(-5)
        )
      ]
    )
  }

  private func makeRevision(
    _ generation: UInt64,
    projectID: String,
    surface: String,
    conditions: GrimodexProjectConditions = .empty
  ) -> GrimodexIntegrationRevision {
    GrimodexIntegrationRevision(
      generation: generation,
      payload: GrimodexIntegrationPayload(
        projectID: projectID,
        projectName: projectID,
        dictionaryEntries: [
          GrimodexMappedDictionaryEntry(
            ruby: "セツナ",
            word: surface,
            cid: 1289,
            mid: 501,
            value: -5,
            entryID: "entry-\(projectID)"
          )
        ],
        conditions: conditions
      )
    )
  }
}
