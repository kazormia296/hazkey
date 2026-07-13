import Foundation
import XCTest

@testable import hazkey_server

final class ZenzaiRuntimeDiagnosticsTests: XCTestCase {
  func testConfigurationOnlySnapshotExplainsWhyZenzaiCannotRun() {
    XCTAssertEqual(
      ZenzaiRuntimeDiagnosticsSnapshot.configurationOnly(
        decision: .profileDisabled
      ).status,
      .profileDisabled
    )
    XCTAssertEqual(
      ZenzaiRuntimeDiagnosticsSnapshot.configurationOnly(
        decision: .policyDisabled
      ).status,
      .policyDisabled
    )
    XCTAssertEqual(
      ZenzaiRuntimeDiagnosticsSnapshot.configurationOnly(
        decision: .backendUnavailable
      ).status,
      .backendUnavailable
    )
    XCTAssertEqual(
      ZenzaiRuntimeDiagnosticsSnapshot.configurationOnly(
        decision: .modelMissing
      ).status,
      .modelMissing
    )
  }

  func testModelLoadVerifiedRequestsAreCountedAndTimestamped() {
    let store = ZenzaiRuntimeDiagnosticsStore()
    let modelURL = URL(fileURLWithPath: "/tmp/zenzai.gguf")
    let decision = ZenzaiRuntimeDecision.enabled(modelURL: modelURL)

    store.record(
      decision: decision,
      converterStatus: "load \(modelURL.absoluteString)",
      at: Date(timeIntervalSince1970: 42.5)
    )
    store.record(
      decision: decision,
      converterStatus: "load \(modelURL.absoluteString)",
      at: Date(timeIntervalSince1970: 43)
    )

    let snapshot = store.snapshot()
    XCTAssertEqual(snapshot.status, .modelLoadVerified)
    XCTAssertTrue(snapshot.modelLoadVerified)
    XCTAssertEqual(snapshot.zenzaiEnabledRequestCount, 2)
    XCTAssertEqual(snapshot.modelLoadFailureCount, 0)
    XCTAssertEqual(snapshot.lastZenzaiRequestUnixMillis, 43_000)
    XCTAssertEqual(snapshot.detail, "")

    let protobuf = snapshot.protobuf
    XCTAssertEqual(protobuf.status, .modelLoadVerified)
    XCTAssertTrue(protobuf.modelLoadVerified)
    XCTAssertEqual(protobuf.zenzaiEnabledRequestCount, 2)
    XCTAssertTrue(protobuf.hasLastZenzaiRequestUnixMillis)
    XCTAssertEqual(protobuf.lastZenzaiRequestUnixMillis, 43_000)
  }

  func testModelLoadFailureIsVisibleAndCounted() {
    let store = ZenzaiRuntimeDiagnosticsStore()
    let modelURL = URL(fileURLWithPath: "/tmp/broken.gguf")
    let decision = ZenzaiRuntimeDecision.enabled(modelURL: modelURL)
    let failure = "load \(modelURL.absoluteString)    invalid model"

    store.record(decision: decision, converterStatus: failure)

    let snapshot = store.snapshot()
    XCTAssertEqual(snapshot.status, .modelLoadFailed)
    XCTAssertFalse(snapshot.modelLoadVerified)
    XCTAssertEqual(snapshot.zenzaiEnabledRequestCount, 1)
    XCTAssertEqual(snapshot.modelLoadFailureCount, 1)
    XCTAssertEqual(snapshot.detail, failure)
  }

  func testResetStartsANewConfigurationGenerationWithoutStaleCounters() {
    let store = ZenzaiRuntimeDiagnosticsStore()
    let modelURL = URL(fileURLWithPath: "/tmp/zenzai.gguf")
    let enabled = ZenzaiRuntimeDecision.enabled(modelURL: modelURL)
    store.record(
      decision: enabled,
      converterStatus: "load \(modelURL.absoluteString)"
    )
    store.record(decision: .modelMissing, converterStatus: "")

    store.reset(decision: .modelMissing)
    let missing = store.snapshot()
    XCTAssertEqual(missing.status, .modelMissing)
    XCTAssertFalse(missing.modelLoadVerified)
    XCTAssertEqual(missing.zenzaiEnabledRequestCount, 0)
    XCTAssertEqual(missing.modelLoadFailureCount, 0)

    store.reset(decision: enabled)
    let readyAgain = store.snapshot()
    XCTAssertEqual(readyAgain.status, .ready)
    XCTAssertFalse(readyAgain.modelLoadVerified)
    XCTAssertEqual(readyAgain.zenzaiEnabledRequestCount, 0)
    XCTAssertNil(readyAgain.lastZenzaiRequestUnixMillis)
  }

  func testPolicyBlockedRequestDoesNotEraseServerWideModelHistory() {
    let store = ZenzaiRuntimeDiagnosticsStore()
    let modelURL = URL(fileURLWithPath: "/tmp/zenzai.gguf")
    let enabled = ZenzaiRuntimeDecision.enabled(modelURL: modelURL)
    store.reset(decision: enabled)
    store.record(
      decision: enabled,
      converterStatus: "load \(modelURL.absoluteString)",
      at: Date(timeIntervalSince1970: 42)
    )

    store.record(decision: .policyDisabled, converterStatus: "")

    let blocked = store.snapshot()
    XCTAssertEqual(blocked.status, .policyDisabled)
    XCTAssertFalse(blocked.modelLoadVerified)
    XCTAssertEqual(blocked.zenzaiEnabledRequestCount, 1)
    XCTAssertEqual(blocked.modelLoadFailureCount, 0)
    XCTAssertEqual(blocked.lastZenzaiRequestUnixMillis, 42_000)

    store.record(
      decision: enabled,
      converterStatus: "load \(modelURL.absoluteString)",
      at: Date(timeIntervalSince1970: 43)
    )
    XCTAssertEqual(store.snapshot().zenzaiEnabledRequestCount, 2)
  }
}
