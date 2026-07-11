import Foundation
import XCTest

@testable import hazkey_server

private struct FixedGrimodexSnapshotProvider: GrimodexSnapshotProviding, Sendable {
  let snapshot: GrimodexPublishedSnapshot

  func latest() -> GrimodexPublishedSnapshot {
    snapshot
  }
}

final class GrimodexSessionRegistryTests: XCTestCase {
  func testEachSessionOwnsIndependentCompositionState() {
    let config = HazkeyServerConfig()
    let registry = HazkeySessionRegistry(serverConfig: config)
    let sessionA = registry.open(
      clientContext: context(program: "grimodex"),
      ownerFd: 10
    )
    let sessionB = registry.open(
      clientContext: context(program: "grimodex"),
      ownerFd: 10
    )

    let stateA = registry.state(for: sessionA, ownerFd: 10)!
    let stateB = registry.state(for: sessionB, ownerFd: 10)!
    XCTAssertTrue(stateA !== stateB)
    XCTAssertTrue(stateA.serverConfig === config)
    XCTAssertTrue(stateB.serverConfig === config)

    _ = stateA.inputChar(inputString: "a")
    _ = stateB.inputChar(inputString: "i")
    XCTAssertEqual(hiragana(stateA), "あ")
    XCTAssertEqual(hiragana(stateB), "い")

    _ = stateA.createComposingTextInstanse()
    XCTAssertEqual(hiragana(stateA), "")
    XCTAssertEqual(hiragana(stateB), "い")
  }

  func testUnknownClosedAndForeignOwnerSessionsAreRejected() {
    let registry = HazkeySessionRegistry()
    let session = registry.open(
      clientContext: context(program: "grimodex"),
      ownerFd: 10
    )

    XCTAssertNil(registry.state(for: "missing", ownerFd: 10))
    XCTAssertNil(registry.state(for: session, ownerFd: 11))
    XCTAssertFalse(registry.close(sessionID: session, ownerFd: 11))
    XCTAssertNotNil(registry.state(for: session, ownerFd: 10))
    XCTAssertTrue(registry.close(sessionID: session, ownerFd: 10))
    XCTAssertNil(registry.state(for: session, ownerFd: 10))
  }

  func testDisconnectOnlyClosesSessionsOwnedByThatSocket() {
    let registry = HazkeySessionRegistry()
    let sessionA = registry.open(
      clientContext: context(program: "grimodex"),
      ownerFd: 10
    )
    let sessionB = registry.open(
      clientContext: context(program: "grimodex"),
      ownerFd: 11
    )

    registry.closeAll(ownerFd: 10)

    XCTAssertNil(registry.state(for: sessionA, ownerFd: 10))
    XCTAssertNotNil(registry.state(for: sessionB, ownerFd: 11))
    XCTAssertEqual(registry.count, 1)
  }

  func testSessionRevisionProviderAppliesScopeAndSecurePolicy() {
    let snapshot = FixedGrimodexSnapshotProvider(snapshot: publishedSnapshot())

    let grimodex = GrimodexSessionRevisionProvider(
      snapshotProvider: snapshot,
      scopeMode: .grimodexOnly,
      clientContext: context(program: "grimodex")
    ).latest()
    XCTAssertNotNil(grimodex.payload)
    XCTAssertTrue(grimodex.allowsLearning)
    XCTAssertFalse(grimodex.secureInput)

    let firefox = GrimodexSessionRevisionProvider(
      snapshotProvider: snapshot,
      scopeMode: .grimodexOnly,
      clientContext: context(program: "firefox")
    ).latest()
    XCTAssertNil(firefox.payload)
    XCTAssertTrue(firefox.allowsLearning)
    XCTAssertFalse(firefox.secureInput)

    let secure = GrimodexSessionRevisionProvider(
      snapshotProvider: snapshot,
      scopeMode: .allApplications,
      clientContext: context(program: "grimodex", secureInput: true)
    ).latest()
    XCTAssertNil(secure.payload)
    XCTAssertFalse(secure.allowsLearning)
    XCTAssertTrue(secure.secureInput)
  }

  func testSecureSessionIsRevokedBeforeItsFirstCommand() {
    let snapshot = FixedGrimodexSnapshotProvider(snapshot: publishedSnapshot())
    let registry = HazkeySessionRegistry(
      revisionProviderFactory: { clientContext in
        GrimodexSessionRevisionProvider(
          snapshotProvider: snapshot,
          scopeMode: .grimodexOnly,
          clientContext: clientContext
        )
      }
    )
    let session = registry.open(
      clientContext: context(program: "grimodex", secureInput: true),
      ownerFd: 10
    )
    let state = registry.state(for: session, ownerFd: 10)!

    XCTAssertTrue(state.grimodexSecureInput)
    XCTAssertFalse(state.grimodexAllowsLearning)
    _ = state.setContext(surroundingText: "password-secret", anchorIndex: 15)
    XCTAssertEqual(state.baseConvertRequestOptions.zenzaiMode, .off)
  }

  private func context(
    program: String,
    secureInput: Bool = false
  ) -> GrimodexClientContext {
    GrimodexClientContext(
      program: program,
      frontend: "wayland",
      secureInput: secureInput
    )
  }

  private func hiragana(_ state: HazkeyServerState) -> String {
    state.getComposingString(charType: .hiragana, currentPreedit: "").text
  }

  private func publishedSnapshot() -> GrimodexPublishedSnapshot {
    GrimodexPublishedSnapshot(
      generation: 4,
      payload: GrimodexIntegrationPayload(
        projectID: "project-a",
        projectName: "Project A",
        dictionaryEntries: [],
        conditions: .empty
      ),
      diagnostic: .loaded
    )
  }
}
