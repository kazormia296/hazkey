import Foundation
import XCTest

@testable import hazkey_server

private struct FixedGrimodexSnapshotProvider: GrimodexSnapshotProviding, Sendable {
  let snapshot: GrimodexPublishedSnapshot

  func latest() -> GrimodexPublishedSnapshot {
    snapshot
  }
}

private final class MutableSessionClock: @unchecked Sendable {
  var now: Date

  init(_ now: Date) {
    self.now = now
  }

  func advance(_ seconds: TimeInterval) {
    now = now.addingTimeInterval(seconds)
  }
}

private final class LearningClearProbe: @unchecked Sendable {
  private let lock = NSLock()
  private var value = 0

  var count: Int {
    lock.lock()
    defer { lock.unlock() }
    return value
  }

  func record() {
    lock.lock()
    value += 1
    lock.unlock()
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

  func testRegistryBoundsSessionsAndExpiresIdleOwners() {
    let clock = MutableSessionClock(Date(timeIntervalSince1970: 1_000))
    let registry = HazkeySessionRegistry(
      maximumSessions: 2,
      idleTimeout: 60,
      now: { clock.now }
    )
    let sessionA = registry.open(
      clientContext: context(program: "grimodex"),
      ownerFd: 10
    )
    clock.advance(10)
    let sessionB = registry.open(
      clientContext: context(program: "grimodex"),
      ownerFd: 11
    )
    clock.advance(10)
    XCTAssertNotNil(registry.state(for: sessionA, ownerFd: 10))
    clock.advance(10)
    let sessionC = registry.open(
      clientContext: context(program: "grimodex"),
      ownerFd: 12
    )

    XCTAssertNotNil(registry.state(for: sessionA, ownerFd: 10))
    XCTAssertNil(registry.state(for: sessionB, ownerFd: 11))
    XCTAssertNotNil(registry.state(for: sessionC, ownerFd: 12))
    XCTAssertEqual(registry.count, 2)

    clock.advance(61)
    XCTAssertNil(registry.state(for: sessionA, ownerFd: 10))
    XCTAssertNil(registry.state(for: sessionC, ownerFd: 12))
    XCTAssertEqual(registry.count, 0)
  }

  func testClearAllLearningDataStillClearsPersistentHistoryWithoutActiveSessions() {
    let probe = LearningClearProbe()
    let registry = HazkeySessionRegistry(
      idleLearningDataClearer: { probe.record() }
    )

    XCTAssertEqual(registry.count, 0)
    registry.clearAllLearningData()

    XCTAssertEqual(probe.count, 1)
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

  func testScopeStoreUpdatesExistingRevisionProviders() {
    let snapshot = FixedGrimodexSnapshotProvider(snapshot: publishedSnapshot())
    let scopeStore = GrimodexScopeModeStore(.grimodexOnly)
    let provider = GrimodexSessionRevisionProvider(
      snapshotProvider: snapshot,
      scopeModeProvider: scopeStore,
      clientContext: context(program: "grimodex")
    )

    XCTAssertNotNil(provider.latest().payload)

    scopeStore.update(.off)
    XCTAssertNil(provider.latest().payload)
    XCTAssertTrue(provider.latest().allowsLearning)

    scopeStore.update(.allApplications)
    let firefoxProvider = GrimodexSessionRevisionProvider(
      snapshotProvider: snapshot,
      scopeModeProvider: scopeStore,
      clientContext: context(program: "firefox")
    )
    XCTAssertNotNil(firefoxProvider.latest().payload)
  }

  func testServerConfigMapsEveryWireScopeMode() {
    let config = HazkeyServerConfig()
    for (wire, expected) in [
      (Hazkey_Config_Profile.GrimodexScopeMode.grimodexOnly, GrimodexScopeMode.grimodexOnly),
      (.grimodexOff, .off),
      (.grimodexAllApplications, .allApplications),
    ] {
      config.currentProfile.grimodexScopeMode = wire
      XCTAssertEqual(config.grimodexScopeMode, expected)
    }
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
