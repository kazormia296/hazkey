import Foundation
import XCTest

@testable import hazkey_server

private final class RecordingGrimodexDictionaryApplier: GrimodexDynamicDictionaryApplying {
  enum Event: Equatable {
    case stopComposition
    case abortSessionComposition
    case replace([GrimodexMappedDictionaryEntry])
  }

  private(set) var events: [Event] = []

  func stopComposition() {
    events.append(.stopComposition)
  }

  func abortSessionComposition() {
    events.append(.abortSessionComposition)
  }

  func replaceDynamicDictionary(_ entries: [GrimodexMappedDictionaryEntry]) {
    events.append(.replace(entries))
  }

  func clear() {
    events.removeAll()
  }
}

final class GrimodexCompositionIntegrationTests: XCTestCase {
  func testFirstInputStopsConverterThenImportsAndPinsRevision() {
    let applier = RecordingGrimodexDictionaryApplier()
    let controller = GrimodexCompositionIntegrationController(applier: applier)
    let revision = makeRevision(1, projectID: "project-a", surface: "刹那")

    controller.prepareFirstInput(latest: revision)

    XCTAssertEqual(
      applier.events,
      [.stopComposition, .replace(revision.payload!.dictionaryEntries)]
    )
    XCTAssertTrue(controller.isComposing)
    XCTAssertEqual(controller.pinnedRevision, revision)
  }

  func testActiveProjectUpdateIsDeferredUntilResetBoundary() {
    let applier = RecordingGrimodexDictionaryApplier()
    let controller = GrimodexCompositionIntegrationController(applier: applier)
    let revisionA = makeRevision(1, projectID: "project-a", surface: "刹那")
    let revisionB = makeRevision(2, projectID: "project-b", surface: "星海")
    controller.prepareFirstInput(latest: revisionA)
    applier.clear()

    controller.observe(revisionB)
    XCTAssertTrue(applier.events.isEmpty)
    XCTAssertEqual(controller.pinnedRevision, revisionA)

    controller.endOrReset(latest: revisionB)
    XCTAssertEqual(
      applier.events,
      [.stopComposition, .replace(revisionB.payload!.dictionaryEntries)]
    )
    XCTAssertFalse(controller.isComposing)
    XCTAssertEqual(controller.appliedRevision, revisionB)
  }

  func testBoundaryUsesLatestRevisionWhenWatcherNotificationWasMissed() {
    let applier = RecordingGrimodexDictionaryApplier()
    let controller = GrimodexCompositionIntegrationController(applier: applier)
    let revisionA = makeRevision(1, projectID: "project-a", surface: "刹那")
    let revisionB = makeRevision(2, projectID: "project-b", surface: "星海")
    controller.prepareFirstInput(latest: revisionA)
    applier.clear()

    controller.endOrReset(latest: revisionB)

    XCTAssertEqual(
      applier.events,
      [.stopComposition, .replace(revisionB.payload!.dictionaryEntries)]
    )
    XCTAssertEqual(controller.appliedRevision, revisionB)
  }

  func testResetAlwaysStopsConverterEvenWhenRevisionDidNotChange() {
    let applier = RecordingGrimodexDictionaryApplier()
    let controller = GrimodexCompositionIntegrationController(applier: applier)
    let revision = makeRevision(1, projectID: "project-a", surface: "刹那")
    controller.prepareFirstInput(latest: revision)
    applier.clear()

    controller.endOrReset(latest: revision)

    XCTAssertEqual(applier.events, [.stopComposition])
    XCTAssertFalse(controller.isComposing)
  }

  func testIdleProjectUpdateIsAppliedByNextInputWithoutWatcherMutation() {
    let applier = RecordingGrimodexDictionaryApplier()
    let controller = GrimodexCompositionIntegrationController(applier: applier)
    let revisionA = makeRevision(1, projectID: "project-a", surface: "刹那")
    let revisionB = makeRevision(2, projectID: "project-b", surface: "星海")
    controller.prepareFirstInput(latest: revisionA)
    controller.endOrReset(latest: revisionA)
    applier.clear()

    controller.prepareFirstInput(latest: revisionB)
    XCTAssertEqual(
      applier.events,
      [.stopComposition, .replace(revisionB.payload!.dictionaryEntries)]
    )
    XCTAssertEqual(controller.pinnedRevision, revisionB)
  }

  func testNilPayloadClearsDictionaryAndConditions() {
    let applier = RecordingGrimodexDictionaryApplier()
    let controller = GrimodexCompositionIntegrationController(applier: applier)
    controller.observe(makeRevision(1, projectID: "project-a", surface: "刹那"))
    applier.clear()
    let cleared = GrimodexIntegrationRevision(generation: 2, payload: nil)

    controller.observe(cleared)

    XCTAssertEqual(applier.events, [.stopComposition, .replace([])])
    XCTAssertEqual(controller.activeConditions, .empty)
    XCTAssertEqual(controller.appliedRevision, cleared)
  }

  func testSameRevisionNeverReimportsDictionary() {
    let applier = RecordingGrimodexDictionaryApplier()
    let controller = GrimodexCompositionIntegrationController(applier: applier)
    let revision = makeRevision(1, projectID: "project-a", surface: "刹那")
    controller.observe(revision)
    applier.clear()

    controller.observe(revision)

    XCTAssertTrue(applier.events.isEmpty)
  }

  func testSecureScopeChangeAtSameGenerationRevokesImmediately() {
    let applier = RecordingGrimodexDictionaryApplier()
    let controller = GrimodexCompositionIntegrationController(applier: applier)
    let nonSecure = makeRevision(
      7,
      projectID: "project-a",
      surface: "刹那",
      conditions: GrimodexProjectConditions(
        topic: "星海年代記",
        style: "簡潔",
        preference: "固有名詞を優先"
      )
    )
    controller.prepareFirstInput(latest: nonSecure)
    applier.clear()
    let secure = GrimodexIntegrationRevision(
      generation: 7,
      payload: nil,
      allowsLearning: false,
      secureInput: true
    )

    controller.observe(secure)

    XCTAssertEqual(applier.events, [.abortSessionComposition, .replace([])])
    XCTAssertFalse(controller.isComposing)
    XCTAssertFalse(controller.allowsLearning)
    XCTAssertTrue(controller.secureInput)
    XCTAssertEqual(controller.activeConditions, .empty)
    XCTAssertEqual(controller.appliedRevision, secure)
  }

  func testProjectConditionsAndLearningFollowAppliedRevision() {
    let applier = RecordingGrimodexDictionaryApplier()
    let controller = GrimodexCompositionIntegrationController(applier: applier)
    let conditions = GrimodexProjectConditions(
      topic: "星海年代記",
      style: "簡潔",
      preference: "固有名詞を優先"
    )
    let revision = makeRevision(
      4,
      projectID: "project-a",
      surface: "刹那",
      conditions: conditions,
      allowsLearning: false
    )

    controller.observe(revision)

    XCTAssertEqual(controller.activeConditions, conditions)
    XCTAssertFalse(controller.allowsLearning)
    XCTAssertFalse(controller.secureInput)
  }

  func testSecureRevisionRevokesEvenWhenItsGenerationIsStale() {
    let applier = RecordingGrimodexDictionaryApplier()
    let controller = GrimodexCompositionIntegrationController(applier: applier)
    let active = makeRevision(9, projectID: "project-a", surface: "刹那")
    controller.prepareFirstInput(latest: active)
    controller.endOrReset(latest: active)
    applier.clear()

    controller.prepareFirstInput(
      latest: GrimodexIntegrationRevision(
        generation: 8,
        payload: nil,
        allowsLearning: false,
        secureInput: true
      )
    )

    XCTAssertEqual(applier.events, [.abortSessionComposition, .replace([])])
    XCTAssertTrue(controller.isComposing)
    XCTAssertFalse(controller.allowsLearning)
    XCTAssertTrue(controller.secureInput)
    XCTAssertEqual(controller.appliedRevision?.generation, 9)
    XCTAssertEqual(controller.pinnedRevision, controller.appliedRevision)
  }

  func testResetBoundaryAbortsForAStaleSecureRevision() {
    let applier = RecordingGrimodexDictionaryApplier()
    let controller = GrimodexCompositionIntegrationController(applier: applier)
    let active = makeRevision(9, projectID: "project-a", surface: "切那")
    controller.prepareFirstInput(latest: active)
    applier.clear()

    controller.endOrReset(
      latest: GrimodexIntegrationRevision(
        generation: 8,
        payload: nil,
        allowsLearning: false,
        secureInput: true
      )
    )

    XCTAssertEqual(applier.events, [.abortSessionComposition, .replace([])])
    XCTAssertFalse(controller.isComposing)
    XCTAssertFalse(controller.allowsLearning)
    XCTAssertTrue(controller.secureInput)
    XCTAssertEqual(controller.appliedRevision?.generation, 9)
  }

  func testRepeatedSecureNotificationDoesNotAbortSecureComposition() {
    let applier = RecordingGrimodexDictionaryApplier()
    let controller = GrimodexCompositionIntegrationController(applier: applier)
    let secure = GrimodexIntegrationRevision(
      generation: 5,
      payload: nil,
      allowsLearning: false,
      secureInput: true
    )
    controller.prepareFirstInput(latest: secure)
    applier.clear()

    controller.observe(secure)

    XCTAssertTrue(applier.events.isEmpty)
    XCTAssertTrue(controller.isComposing)
    XCTAssertTrue(controller.secureInput)
  }

  private func makeRevision(
    _ generation: UInt64,
    projectID: String,
    surface: String,
    conditions: GrimodexProjectConditions = .empty,
    allowsLearning: Bool = true
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
      ),
      allowsLearning: allowsLearning
    )
  }
}
