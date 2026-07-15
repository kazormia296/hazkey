import Foundation
import KanaKanjiConverterModule
import KanaKanjiConverterModuleWithDefaultDictionary
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
  func testSessionRemovalReasonsChooseSafeLearningDisposition() {
    XCTAssertFalse(HazkeySessionRemovalReason.socketDisconnect.commitsPendingLearning)
    XCTAssertTrue(HazkeySessionRemovalReason.explicitClose.commitsPendingLearning)
    XCTAssertTrue(HazkeySessionRemovalReason.capacityEviction.commitsPendingLearning)
    XCTAssertTrue(HazkeySessionRemovalReason.idleTimeout.commitsPendingLearning)
  }

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

    let controllerA = registry.semanticController(for: sessionA, ownerFd: 10)!
    let controllerB = registry.semanticController(for: sessionB, ownerFd: 10)!
    let firstA = controllerA.handle(ImeV2Request(
      requestID: "a",
      expectedRevision: 0,
      action: .insertText("a")
    ))
    let firstB = controllerB.handle(ImeV2Request(
      requestID: "i",
      expectedRevision: 0,
      action: .insertText("i")
    ))
    XCTAssertEqual(firstA.status, .success)
    XCTAssertEqual(firstB.status, .success)
    XCTAssertEqual(controllerA.snapshot.revision, 1)
    XCTAssertEqual(controllerB.snapshot.revision, 1)

    _ = controllerA.handle(ImeV2Request(
      requestID: "cancel-a",
      expectedRevision: firstA.snapshot.revision,
      action: .cancel
    ))
    XCTAssertEqual(controllerA.snapshot.phase, .idle)
    XCTAssertEqual(controllerB.snapshot.phase, .composing)
  }

  func testAllSessionsShareTheRegistryHazkeyExecutionFence() {
    let registry = HazkeySessionRegistry()
    let sessionA = registry.open(
      clientContext: context(program: "grimodex"),
      ownerFd: 10
    )
    let sessionB = registry.open(
      clientContext: context(program: "grimodex"),
      ownerFd: 10
    )

    let environmentA = registry.environment(for: sessionA, ownerFd: 10)
    let environmentB = registry.environment(for: sessionB, ownerFd: 10)

    XCTAssertTrue(environmentA?.executionGate === environmentB?.executionGate)
  }

  func testFailedConfigurationMutationKeepsPublishedCompositionUnchanged() {
    let registry = HazkeySessionRegistry()
    let sessionID = registry.open(
      clientContext: context(program: "grimodex"),
      ownerFd: 10
    )
    let controller = registry.semanticController(for: sessionID, ownerFd: 10)!
    let inserted = controller.handle(ImeV2Request(
      requestID: "insert-before-failed-config",
      expectedRevision: 0,
      action: .insertText("かな")
    ))
    let converted = controller.handle(ImeV2Request(
      requestID: "convert-before-failed-config",
      expectedRevision: inserted.snapshot.revision,
      action: .startConversion
    ))
    XCTAssertEqual(converted.status, .success)
    let before = controller.snapshot

    let result = registry.performConfigurationMutation(
      { false },
      reinitializeWhen: { $0 }
    )

    XCTAssertFalse(result)
    XCTAssertEqual(controller.snapshot, before)
  }

  func testUnknownClosedAndForeignOwnerSessionsAreRejected() {
    let registry = HazkeySessionRegistry()
    let session = registry.open(
      clientContext: context(program: "grimodex"),
      ownerFd: 10
    )

    XCTAssertNil(registry.semanticController(for: "missing", ownerFd: 10))
    XCTAssertNil(registry.semanticController(for: session, ownerFd: 11))
    XCTAssertFalse(registry.close(sessionID: session, ownerFd: 11))
    XCTAssertNotNil(registry.semanticController(for: session, ownerFd: 10))
    XCTAssertTrue(registry.close(sessionID: session, ownerFd: 10))
    XCTAssertNil(registry.semanticController(for: session, ownerFd: 10))
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

    XCTAssertNil(registry.semanticController(for: sessionA, ownerFd: 10))
    XCTAssertNotNil(registry.semanticController(for: sessionB, ownerFd: 11))
    XCTAssertEqual(registry.count, 1)
  }

  func testRegistryRefusesNewOwnerWhenGlobalCapacityIsFullAndExpiresIdleOwners() {
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
    XCTAssertNotNil(registry.semanticController(for: sessionA, ownerFd: 10))
    clock.advance(10)
    let thirdOwnerResult = registry.attemptOpen(
      clientContext: context(program: "grimodex"),
      ownerFd: 12
    )

    XCTAssertNotNil(registry.semanticController(for: sessionA, ownerFd: 10))
    XCTAssertNotNil(registry.semanticController(for: sessionB, ownerFd: 11))
    switch thirdOwnerResult {
    case .success(let sessionID):
      XCTFail("foreign owner unexpectedly opened session \(sessionID)")
    case .failure(let error):
      XCTAssertEqual(error, .resourceExhausted)
    }
    XCTAssertEqual(registry.count, 2)

    clock.advance(61)
    XCTAssertNil(registry.semanticController(for: sessionA, ownerFd: 10))
    XCTAssertNil(registry.semanticController(for: sessionB, ownerFd: 11))
    XCTAssertEqual(registry.count, 0)
  }

  func testOneOwnerCannotEvictAnotherOwnersActiveComposition() {
    let registry = HazkeySessionRegistry(
      maximumSessions: 3,
      maximumSessionsPerOwner: 2
    )
    let protected = registry.open(
      clientContext: context(program: "grimodex"),
      ownerFd: 10
    )
    let firstAttackerSession = registry.open(
      clientContext: context(program: "other"),
      ownerFd: 20
    )
    let secondAttackerSession = registry.open(
      clientContext: context(program: "other"),
      ownerFd: 20
    )
    let thirdAttackerSession = registry.open(
      clientContext: context(program: "other"),
      ownerFd: 20
    )

    XCTAssertNotNil(registry.semanticController(for: protected, ownerFd: 10))
    XCTAssertNil(registry.semanticController(for: firstAttackerSession, ownerFd: 20))
    XCTAssertNotNil(registry.semanticController(for: secondAttackerSession, ownerFd: 20))
    XCTAssertNotNil(registry.semanticController(for: thirdAttackerSession, ownerFd: 20))
    XCTAssertEqual(registry.count, 3)
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
    let controller = registry.semanticController(for: session, ownerFd: 10)!
    let result = controller.handle(ImeV2Request(
      requestID: "secure-input",
      expectedRevision: 0,
      action: .insertText("秘密")
    ))
    XCTAssertEqual(result.status, .success)
    XCTAssertNil(result.snapshot.recovery)
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
