import Foundation
import XCTest

@testable import hazkey_server

final class GrimodexSessionStateTests: XCTestCase {
  func testScopeDefaultsToGrimodexOnly() {
    XCTAssertEqual(GrimodexScopeMode.defaultValue, .grimodexOnly)
  }

  func testGrimodexOnlyAllowsExactNormalizedProductIdentifiers() {
    for program in ["grimodex", " GRIMODEX ", "com.miyakey.grimodex"] {
      XCTAssertEqual(
        scope(program: program),
        GrimodexScopeDecision(
          allowsGrimodexIntegration: true,
          allowsLearning: true,
          reason: .allowedGrimodex
        ),
        program
      )
    }
  }

  func testGrimodexOnlyRejectsUnknownPathsSubstringsAndOtherApps() {
    XCTAssertEqual(scope(program: "").reason, .unknownProgram)
    for program in ["firefox", "electron", "/usr/bin/grimodex", "grimodex-helper"] {
      let decision = scope(program: program)
      XCTAssertFalse(decision.allowsGrimodexIntegration, program)
      XCTAssertTrue(decision.allowsLearning, program)
      XCTAssertEqual(decision.reason, .otherProgram, program)
    }
  }

  func testFrontendAloneNeverWidensGrimodexOnlyScope() {
    let decision = GrimodexScopePolicy.evaluate(
      mode: .grimodexOnly,
      context: GrimodexClientContext(
        program: "",
        frontend: "wayland",
        secureInput: false
      )
    )

    XCTAssertFalse(decision.allowsGrimodexIntegration)
    XCTAssertEqual(decision.reason, .unknownProgram)
  }

  func testAllApplicationsAllowsKnownAndUnknownPrograms() {
    for program in ["", "firefox", "grimodex"] {
      XCTAssertEqual(
        GrimodexScopePolicy.evaluate(
          mode: .allApplications,
          context: GrimodexClientContext(
            program: program,
            frontend: "wayland",
            secureInput: false
          )
        ),
        GrimodexScopeDecision(
          allowsGrimodexIntegration: true,
          allowsLearning: true,
          reason: .allowedAllApplications
        )
      )
    }
  }

  func testSecureInputOverridesEveryScopeAndDisablesLearning() {
    for mode in GrimodexScopeMode.allCases {
      let decision = GrimodexScopePolicy.evaluate(
        mode: mode,
        context: GrimodexClientContext(
          program: "grimodex",
          frontend: "wayland",
          secureInput: true
        )
      )
      XCTAssertEqual(
        decision,
        GrimodexScopeDecision(
          allowsGrimodexIntegration: false,
          allowsLearning: false,
          reason: .secureInput
        ),
        mode.rawValue
      )
    }
  }

  func testOffAndOtherApplicationsKeepNormalHazkeyLearningEnabled() {
    let off = scope(mode: .off, program: "grimodex")
    XCTAssertEqual(off.reason, .disabled)
    XCTAssertFalse(off.allowsGrimodexIntegration)
    XCTAssertTrue(off.allowsLearning)

    let other = scope(program: "terminal")
    XCTAssertFalse(other.allowsGrimodexIntegration)
    XCTAssertTrue(other.allowsLearning)
  }

  func testCompositionPinsGenerationUntilTheBoundary() {
    var pin = GrimodexCompositionGenerationPin()
    let generation1 = revision(1, projectID: "project-a")
    let generation2 = revision(2, projectID: "project-b")

    XCTAssertEqual(pin.beginComposition(latest: generation1), generation1)
    XCTAssertEqual(pin.pinned, generation1)
    XCTAssertNil(pin.observe(generation2))
    XCTAssertEqual(pin.pinned, generation1)
    XCTAssertEqual(pin.pending, generation2)

    XCTAssertEqual(pin.endComposition(latest: generation2), generation2)
    XCTAssertNil(pin.pinned)
    XCTAssertNil(pin.pending)
    XCTAssertEqual(pin.applied, generation2)
  }

  func testNewestPendingGenerationWinsDuringComposition() {
    var pin = GrimodexCompositionGenerationPin()
    let generation1 = revision(1, projectID: "project-a")
    let generation2 = revision(2, projectID: "project-b")
    let generation3 = revision(3, projectID: "project-c")

    _ = pin.beginComposition(latest: generation1)
    XCTAssertNil(pin.observe(generation2))
    XCTAssertNil(pin.observe(generation3))

    XCTAssertEqual(pin.pending, generation3)
    XCTAssertEqual(pin.endComposition(latest: generation3), generation3)
  }

  func testBoundaryAppliesLatestEvenWhenWatcherNotificationWasMissed() {
    var pin = GrimodexCompositionGenerationPin()
    let generation1 = revision(1, projectID: "project-a")
    let generation2 = revision(2, projectID: "project-b")
    _ = pin.beginComposition(latest: generation1)

    XCTAssertEqual(pin.endComposition(latest: generation2), generation2)
    XCTAssertEqual(pin.applied, generation2)
  }

  func testInactiveObservationAppliesImmediatelyAndIgnoresStaleGeneration() {
    var pin = GrimodexCompositionGenerationPin()
    let generation2 = revision(2, projectID: "project-b")
    let generation1 = revision(1, projectID: "project-a")

    XCTAssertEqual(pin.observe(generation2), generation2)
    XCTAssertNil(pin.observe(generation1))
    XCTAssertEqual(pin.applied, generation2)
  }

  func testNewNilPayloadClearsAtTheCompositionBoundary() {
    var pin = GrimodexCompositionGenerationPin()
    let active = revision(1, projectID: "project-a")
    let cleared = GrimodexIntegrationRevision(generation: 2, payload: nil)
    _ = pin.beginComposition(latest: active)

    XCTAssertNil(pin.observe(cleared))
    XCTAssertEqual(pin.endComposition(latest: cleared), cleared)
    XCTAssertNil(pin.applied?.payload)
  }

  func testScopeChangeWithTheSameGenerationStillApplies() {
    let snapshot = GrimodexPublishedSnapshot(
      generation: 4,
      payload: payload(projectID: "project-a"),
      diagnostic: .loaded
    )
    let allowed = GrimodexIntegrationRevision(
      snapshot: snapshot,
      decision: GrimodexScopeDecision(
        allowsGrimodexIntegration: true,
        allowsLearning: true,
        reason: .allowedGrimodex
      )
    )
    let denied = GrimodexIntegrationRevision(
      snapshot: snapshot,
      decision: GrimodexScopeDecision(
        allowsGrimodexIntegration: false,
        allowsLearning: true,
        reason: .otherProgram
      )
    )
    var pin = GrimodexCompositionGenerationPin()

    XCTAssertEqual(pin.observe(allowed), allowed)
    XCTAssertEqual(pin.observe(denied), denied)
    XCTAssertNil(pin.applied?.payload)
  }

  func testRepeatedBeginDoesNotRepinAnActiveComposition() {
    var pin = GrimodexCompositionGenerationPin()
    let generation1 = revision(1, projectID: "project-a")
    let generation2 = revision(2, projectID: "project-b")
    _ = pin.beginComposition(latest: generation1)

    XCTAssertNil(pin.beginComposition(latest: generation2))
    XCTAssertEqual(pin.pinned, generation1)
    XCTAssertEqual(pin.pending, generation2)
  }

  private func scope(
    mode: GrimodexScopeMode = .grimodexOnly,
    program: String
  ) -> GrimodexScopeDecision {
    GrimodexScopePolicy.evaluate(
      mode: mode,
      context: GrimodexClientContext(
        program: program,
        frontend: "wayland",
        secureInput: false
      )
    )
  }

  private func revision(_ generation: UInt64, projectID: String) -> GrimodexIntegrationRevision {
    GrimodexIntegrationRevision(
      generation: generation,
      payload: payload(projectID: projectID)
    )
  }

  private func payload(projectID: String) -> GrimodexIntegrationPayload {
    GrimodexIntegrationPayload(
      projectID: projectID,
      projectName: projectID,
      dictionaryEntries: [],
      conditions: .empty
    )
  }
}
