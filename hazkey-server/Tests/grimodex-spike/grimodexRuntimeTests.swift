import Foundation
import XCTest

@testable import hazkey_server

private final class GrimodexReloadProbe: @unchecked Sendable {
  private let lock = NSLock()
  private let semaphore = DispatchSemaphore(value: 0)
  private var retryDecisions: [Bool]
  private var recordedCount = 0

  init(retryDecisions: [Bool] = []) {
    self.retryDecisions = retryDecisions
  }

  func record() -> Bool {
    lock.lock()
    recordedCount += 1
    let shouldRetry = retryDecisions.isEmpty ? false : retryDecisions.removeFirst()
    lock.unlock()
    semaphore.signal()
    return shouldRetry
  }

  func wait(timeout: TimeInterval = 2) -> Bool {
    semaphore.wait(timeout: .now() + timeout) == .success
  }

  var count: Int {
    lock.lock()
    defer { lock.unlock() }
    return recordedCount
  }
}

private final class GrimodexMutableClock: @unchecked Sendable {
  private let lock = NSLock()
  private var value: Date

  init(_ value: Date) {
    self.value = value
  }

  func now() -> Date {
    lock.lock()
    defer { lock.unlock() }
    return value
  }

  func advance(by interval: TimeInterval) {
    lock.lock()
    value = value.addingTimeInterval(interval)
    lock.unlock()
  }
}

private final class GrimodexRuntimeSandbox {
  let parent: URL
  let root: URL

  init(createRoot: Bool = true) throws {
    parent = FileManager.default.temporaryDirectory.appendingPathComponent(
      "grimodex-runtime-tests-\(UUID().uuidString)",
      isDirectory: true
    )
    root = parent.appendingPathComponent("ime", isDirectory: true)
    try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
    if createRoot {
      try createSnapshotDirectories()
    }
  }

  deinit {
    try? FileManager.default.removeItem(at: parent)
  }

  func createSnapshotDirectories() throws {
    try FileManager.default.createDirectory(
      at: root.appendingPathComponent("projects", isDirectory: true),
      withIntermediateDirectories: true
    )
  }

  func fixtureData(_ relativePath: String) throws -> Data {
    try Data(
      contentsOf: Bundle.module.resourceURL!
        .appendingPathComponent("Fixtures", isDirectory: true)
        .appendingPathComponent(relativePath)
    )
  }

  func replaceState(with relativePath: String = "valid/state-active.json") throws {
    try fixtureData(relativePath).write(
      to: root.appendingPathComponent("state.json"),
      options: .atomic
    )
  }

  func replaceProject(with relativePath: String = "valid/project-with-zenzai-context.json") throws {
    try fixtureData(relativePath).write(
      to: root.appendingPathComponent("projects/project-a.json"),
      options: .atomic
    )
  }
}

final class GrimodexRuntimeTests: XCTestCase {
  func testManagerPreservesLastGoodPayloadDuringRetryableStateRace() throws {
    let sandbox = try GrimodexRuntimeSandbox()
    let stateA = try sandbox.fixtureData("valid/state-active.json")
    let stateB = Data(
      "{\"format_version\":1,\"active_project_id\":\"project-b\",\"updated_at\":\"2026-07-11T00:00:01.000Z\"}".utf8
    )
    let project = try sandbox.fixtureData("valid/project-with-zenzai-context.json")
    let reader = RuntimeScriptedReader(states: [stateA, stateA, stateA, stateB], project: project)
    let manager = GrimodexSnapshotManager(
      loader: GrimodexSnapshotLoader(rootURL: sandbox.root, fileReader: reader)
    )

    XCTAssertEqual(manager.reload().generation, 1)
    let raced = manager.reload()

    XCTAssertEqual(raced.generation, 1)
    XCTAssertNotNil(raced.payload)
    XCTAssertEqual(raced.diagnostic, .stateChangedDuringRead)
  }

  func testManagerPreservesPayloadForMissingSnapshotButInvalidJsonFailsClosed() throws {
    let sandbox = try GrimodexRuntimeSandbox()
    try sandbox.replaceState()
    try sandbox.replaceProject()
    let manager = GrimodexSnapshotManager(
      loader: GrimodexSnapshotLoader(rootURL: sandbox.root)
    )
    XCTAssertEqual(manager.reload().generation, 1)

    let projectURL = sandbox.root.appendingPathComponent("projects/project-a.json")
    try FileManager.default.removeItem(at: projectURL)
    let missing = manager.reload()
    XCTAssertEqual(missing.diagnostic, .missingSnapshot)
    XCTAssertEqual(missing.generation, 1)
    XCTAssertNotNil(missing.payload)

    try Data("not-json".utf8).write(to: projectURL)
    let invalid = manager.reload()
    XCTAssertEqual(invalid.diagnostic, .invalidSnapshot)
    XCTAssertEqual(invalid.generation, 2)
    XCTAssertNil(invalid.payload)
  }

  func testWatcherDebouncesAtomicSnapshotReplacements() throws {
    let sandbox = try GrimodexRuntimeSandbox()
    let probe = GrimodexReloadProbe()
    let watcher = GrimodexSnapshotWatcher(
      rootURL: sandbox.root,
      debounceInterval: 0.08,
      retryInterval: 0.03,
      reload: { probe.record() }
    )
    try watcher.start()
    defer { watcher.stop() }
    XCTAssertTrue(probe.wait(), "start must perform the initial load after arming watches")

    try sandbox.replaceProject()
    try sandbox.replaceState()
    try sandbox.replaceProject()

    XCTAssertTrue(probe.wait())
    Thread.sleep(forTimeInterval: 0.25)
    XCTAssertEqual(probe.count, 2)
  }

  func testWatcherRearmsWhenRootIsCreatedAfterStartup() throws {
    let sandbox = try GrimodexRuntimeSandbox(createRoot: false)
    let probe = GrimodexReloadProbe()
    let watcher = GrimodexSnapshotWatcher(
      rootURL: sandbox.root,
      debounceInterval: 0.05,
      retryInterval: 0.03,
      reload: { probe.record() }
    )
    try watcher.start()
    defer { watcher.stop() }
    XCTAssertTrue(probe.wait(), "the initial missing-root load must still occur")

    try sandbox.createSnapshotDirectories()
    try sandbox.replaceState()
    try sandbox.replaceProject()

    XCTAssertTrue(probe.wait())
    XCTAssertGreaterThanOrEqual(probe.count, 2)
  }

  func testWatcherRetriesAtMostOnceForARetryableReload() throws {
    let sandbox = try GrimodexRuntimeSandbox()
    let probe = GrimodexReloadProbe(retryDecisions: [true, true, true])
    let watcher = GrimodexSnapshotWatcher(
      rootURL: sandbox.root,
      debounceInterval: 0.04,
      retryInterval: 0.04,
      reload: { probe.record() }
    )
    try watcher.start()
    defer { watcher.stop() }

    XCTAssertTrue(probe.wait())
    XCTAssertTrue(probe.wait())
    Thread.sleep(forTimeInterval: 0.2)
    XCTAssertEqual(probe.count, 2)
  }

  func testConsumerRegistrarWritesCanonicalHandshakeWithPrivatePermissions() throws {
    let sandbox = try GrimodexRuntimeSandbox(createRoot: false)
    let clock = GrimodexMutableClock(Date(timeIntervalSince1970: 1_752_192_000))
    let registrar = GrimodexConsumerRegistrar(
      rootURL: sandbox.root,
      version: "0.1.0",
      now: { clock.now() }
    )

    let destination = try registrar.registerNow()
    let json = try XCTUnwrap(
      JSONSerialization.jsonObject(with: Data(contentsOf: destination)) as? [String: Any]
    )
    let fixture = try XCTUnwrap(
      JSONSerialization.jsonObject(
        with: sandbox.fixtureData("valid/consumer-linux.json")
      ) as? [String: Any]
    )

    for key in ["format_version", "consumer_id", "name", "version", "platform"] {
      XCTAssertEqual(json[key] as? NSObject, fixture[key] as? NSObject, key)
    }
    let capabilities = try XCTUnwrap(json["capabilities"] as? [String: Bool])
    let fixtureCapabilities = try XCTUnwrap(fixture["capabilities"] as? [String: Bool])
    for key in ["profile", "dynamic_dictionary", "zenzai_v3_conditions", "application_scoping"] {
      XCTAssertEqual(capabilities[key], fixtureCapabilities[key], key)
    }
    XCTAssertNil(json["future_optional_field"])
    XCTAssertNil(capabilities["future_capability"])
    XCTAssertEqual(json["last_seen"] as? String, "2025-07-11T00:00:00.000Z")
    XCTAssertEqual(destination.lastPathComponent, "fcitx5-grimodex.json")

    let destinationMode = try XCTUnwrap(
      FileManager.default.attributesOfItem(atPath: destination.path)[.posixPermissions] as? NSNumber
    )
    let consumersURL = destination.deletingLastPathComponent()
    let consumersMode = try XCTUnwrap(
      FileManager.default.attributesOfItem(atPath: consumersURL.path)[.posixPermissions] as? NSNumber
    )
    XCTAssertEqual(destinationMode.intValue, 0o600)
    XCTAssertEqual(consumersMode.intValue, 0o700)
    XCTAssertEqual(
      try FileManager.default.contentsOfDirectory(atPath: consumersURL.path),
      ["fcitx5-grimodex.json"]
    )
  }

  func testConsumerHeartbeatUpdatesLastSeenAndKeepsIdentityStable() throws {
    let sandbox = try GrimodexRuntimeSandbox(createRoot: false)
    let clock = GrimodexMutableClock(Date(timeIntervalSince1970: 1_752_192_000))
    let registrar = GrimodexConsumerRegistrar(
      rootURL: sandbox.root,
      version: "0.1.0",
      now: { clock.now() }
    )
    let destination = try registrar.registerNow()
    let first = try handshake(at: destination)

    clock.advance(by: 15 * 60)
    XCTAssertEqual(try registrar.registerNow(), destination)
    let second = try handshake(at: destination)

    XCTAssertEqual(first["consumer_id"] as? String, second["consumer_id"] as? String)
    XCTAssertEqual(first["version"] as? String, second["version"] as? String)
    XCTAssertNotEqual(first["last_seen"] as? String, second["last_seen"] as? String)
  }

  private func handshake(at url: URL) throws -> [String: Any] {
    try XCTUnwrap(
      JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any]
    )
  }
}

private final class RuntimeScriptedReader: GrimodexFileReading, @unchecked Sendable {
  private let lock = NSLock()
  private var states: [Data]
  private let project: Data

  init(states: [Data], project: Data) {
    self.states = states
    self.project = project
  }

  func read(_ url: URL, maxBytes: Int) throws -> Data? {
    lock.lock()
    defer { lock.unlock() }
    if url.lastPathComponent == "state.json" {
      return states.isEmpty ? nil : states.removeFirst()
    }
    return project
  }
}
