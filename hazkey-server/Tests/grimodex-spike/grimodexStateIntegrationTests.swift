import Foundation
import KanaKanjiConverterModule
import KanaKanjiConverterModuleWithDefaultDictionary
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

private final class RecordingHazkeyCandidateLearning: HazkeyCandidateLearning {
  private(set) var setCompletedDataCount = 0
  private(set) var updateLearningDataCount = 0
  private(set) var commitCount = 0

  func setCompletedData(_ candidate: Candidate) {
    setCompletedDataCount += 1
  }

  func updateLearningData(_ candidate: Candidate) {
    updateLearningDataCount += 1
  }

  func commitUpdateLearningData() {
    commitCount += 1
  }
}

final class GrimodexStateIntegrationTests: XCTestCase {
  func testStatePinsOnFirstInputAndAppliesLatestAtReset() {
    let conditionsA = GrimodexProjectConditions(
      topic: "project-a-topic",
      style: "project-a-style",
      preference: "project-a-preference"
    )
    let conditionsB = GrimodexProjectConditions(
      topic: "project-b-topic",
      style: "project-b-style",
      preference: "project-b-preference"
    )
    let revisionA = makeRevision(
      1,
      projectID: "project-a",
      surface: "刹那",
      conditions: conditionsA
    )
    let revisionB = makeRevision(
      2,
      projectID: "project-b",
      surface: "星海",
      conditions: conditionsB
    )
    let provider = MutableGrimodexRevisionProvider(revisionA)
    let state = HazkeyServerState(revisionProvider: provider)
    state.serverConfig.currentProfile.zenzaiProfile = "user-profile"
    state.serverConfig.currentProfile.zenzaiTopic = "user-topic"
    state.serverConfig.currentProfile.zenzaiStyle = "user-style"
    state.serverConfig.currentProfile.zenzaiPreference = "user-preference"

    XCTAssertEqual(
      state.setContext(surroundingText: "left", anchorIndex: 4).status,
      .success
    )
    XCTAssertEqual(
      state.grimodexResolvedZenzaiConditions,
      GrimodexResolvedZenzaiConditions(
        profile: "user-profile",
        topic: "user-topic",
        style: "user-style",
        preference: "user-preference"
      )
    )

    XCTAssertEqual(state.inputChar(inputString: "a").status, .success)
    XCTAssertEqual(state.grimodexPinnedRevision, revisionA)
    XCTAssertEqual(state.grimodexActiveConditions, conditionsA)
    XCTAssertEqual(state.grimodexResolvedZenzaiConditions?.topic, "project-a-topic")

    provider.update(revisionB)
    XCTAssertEqual(state.inputChar(inputString: "i").status, .success)
    XCTAssertEqual(state.grimodexPinnedRevision, revisionA)
    XCTAssertEqual(state.grimodexActiveConditions, conditionsA)

    XCTAssertEqual(state.createComposingTextInstanse().status, .success)
    XCTAssertNil(state.grimodexPinnedRevision)
    XCTAssertEqual(state.grimodexAppliedRevision, revisionB)
    XCTAssertEqual(state.grimodexActiveConditions, conditionsB)
    XCTAssertEqual(state.grimodexResolvedZenzaiConditions?.topic, "project-b-topic")

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
      4,
      projectID: "project-a",
      surface: "刹那",
      conditions: conditions
    )
    let provider = MutableGrimodexRevisionProvider(active)
    let learning = RecordingHazkeyCandidateLearning()
    let state = HazkeyServerState(
      revisionProvider: provider,
      candidateLearning: learning
    )
    state.serverConfig.zenzaiAvailable = true
    state.serverConfig.zenzaiModelPath = URL(fileURLWithPath: "/tmp/grimodex-test-model.gguf")
    state.serverConfig.currentProfile.zenzaiEnable = true
    _ = state.setContext(surroundingText: "left", anchorIndex: 4)
    _ = state.inputChar(inputString: "a")
    XCTAssertNotEqual(state.baseConvertRequestOptions.zenzaiMode, .off)
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
    XCTAssertEqual(state.baseConvertRequestOptions.zenzaiMode, .off)
    _ = state.saveLearningData()
    XCTAssertEqual(learning.commitCount, 0)
  }

  func testSecureInputNeverRetainsOrReusesSurroundingText() {
    let active = makeRevision(
      9,
      projectID: "project-a",
      surface: "切那"
    )
    let provider = MutableGrimodexRevisionProvider(active)
    let state = HazkeyServerState(revisionProvider: provider)
    state.serverConfig.zenzaiAvailable = true
    state.serverConfig.zenzaiModelPath = URL(fileURLWithPath: "/tmp/grimodex-test-model.gguf")
    state.serverConfig.currentProfile.zenzaiEnable = true
    state.serverConfig.currentProfile.zenzaiContextualMode = true

    _ = state.setContext(surroundingText: "safe-before", anchorIndex: 11)
    XCTAssertEqual(
      state.baseConvertRequestOptions.zenzaiMode,
      state.serverConfig.genZenzaiMode(leftContext: "safe-before")
    )

    provider.update(
      GrimodexIntegrationRevision(
        generation: 8,
        payload: nil,
        allowsLearning: false,
        secureInput: true
      )
    )
    state.refreshGrimodexIntegration()
    XCTAssertEqual(state.baseConvertRequestOptions.zenzaiMode, .off)

    _ = state.setContext(surroundingText: "password-secret", anchorIndex: 15)
    XCTAssertEqual(state.baseConvertRequestOptions.zenzaiMode, .off)

    provider.update(makeRevision(10, projectID: "project-a", surface: "切那"))
    state.refreshGrimodexIntegration()
    XCTAssertEqual(
      state.baseConvertRequestOptions.zenzaiMode,
      state.serverConfig.genZenzaiMode(leftContext: "")
    )
    XCTAssertNotEqual(
      state.baseConvertRequestOptions.zenzaiMode,
      state.serverConfig.genZenzaiMode(leftContext: "password-secret")
    )

    _ = state.setContext(surroundingText: "safe-after", anchorIndex: 10)
    XCTAssertEqual(
      state.baseConvertRequestOptions.zenzaiMode,
      state.serverConfig.genZenzaiMode(leftContext: "safe-after")
    )
  }

  func testCompletePrefixDoesNotQueueLearningWhenRevisionDisallowsIt() {
    let disabled = GrimodexIntegrationRevision(
      generation: 1,
      payload: nil,
      allowsLearning: false,
      secureInput: false
    )
    let learning = RecordingHazkeyCandidateLearning()
    let state = HazkeyServerState(
      revisionProvider: MutableGrimodexRevisionProvider(disabled),
      candidateLearning: learning
    )
    _ = state.inputChar(inputString: "a")
    state.currentCandidateList = [makeCandidate()]

    XCTAssertEqual(state.completePrefix(candidateIndex: 0).status, .success)
    XCTAssertFalse(state.learningDataNeedsCommit)
    XCTAssertEqual(learning.setCompletedDataCount, 1)
    XCTAssertEqual(learning.updateLearningDataCount, 0)
  }

  func testCompletePrefixKeepsNormalHazkeyLearningOutsideSecureInput() {
    let enabled = GrimodexIntegrationRevision(generation: 1, payload: nil)
    let learning = RecordingHazkeyCandidateLearning()
    let state = HazkeyServerState(
      revisionProvider: MutableGrimodexRevisionProvider(enabled),
      candidateLearning: learning
    )
    _ = state.inputChar(inputString: "a")
    state.currentCandidateList = [makeCandidate()]

    XCTAssertEqual(state.completePrefix(candidateIndex: 0).status, .success)
    XCTAssertTrue(state.learningDataNeedsCommit)
    XCTAssertEqual(learning.setCompletedDataCount, 1)
    XCTAssertEqual(learning.updateLearningDataCount, 1)
  }

  func testStateImportsRevisionDictionaryIntoTheRealConverter() {
    let revision = makeRevision(
      1,
      projectID: "project-wiring",
      surface: "Grimodex配線確認"
    )
    let state = HazkeyServerState(
      revisionProvider: MutableGrimodexRevisionProvider(revision),
      converter: .withDefaultDictionary()
    )

    state.isSubInputMode = true
    for character in "せつな" {
      _ = state.inputChar(inputString: String(character))
    }
    let response = state.getCandidates(is_suggest: false)

    XCTAssertEqual(response.status, .success)
    XCTAssertEqual(
      response.candidates.candidates.first?.text,
      "Grimodex配線確認",
      "the active Grimodex project term must be the highest-ranked conversion candidate"
    )
  }

  func testZenzaiResolverKeepsUserProfileAndOnlyOverlaysProjectValues() {
    let resolved = GrimodexZenzaiConditionResolver.resolve(
      profile: "user-profile",
      topic: "user-topic",
      style: "user-style",
      preference: "user-preference",
      project: GrimodexProjectConditions(
        topic: "project-topic",
        style: "project-style",
        preference: "project-preference"
      )
    )

    XCTAssertEqual(
      resolved,
      GrimodexResolvedZenzaiConditions(
        profile: "user-profile",
        topic: "project-topic",
        style: "project-style",
        preference: "project-preference"
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
