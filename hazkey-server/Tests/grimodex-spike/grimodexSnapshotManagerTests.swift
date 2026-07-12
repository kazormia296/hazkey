import Foundation
import XCTest

@testable import hazkey_server

private final class GrimodexFixtureSandbox {
  let root: URL

  init() throws {
    root = FileManager.default.temporaryDirectory.appendingPathComponent(
      "grimodex-snapshot-tests-\(UUID().uuidString)",
      isDirectory: true
    )
    try FileManager.default.createDirectory(
      at: root.appendingPathComponent("projects", isDirectory: true),
      withIntermediateDirectories: true
    )
  }

  deinit {
    try? FileManager.default.removeItem(at: root)
  }

  func installState(_ fixture: String) throws {
    try fixtureData(fixture).write(to: root.appendingPathComponent("state.json"))
  }

  func installProject(_ fixture: String, id: String = "project-a") throws {
    try fixtureData(fixture).write(
      to: root.appendingPathComponent("projects/\(id).json")
    )
  }

  func fixtureData(_ relativePath: String) throws -> Data {
    let url = Bundle.module.resourceURL!
      .appendingPathComponent("Fixtures", isDirectory: true)
      .appendingPathComponent(relativePath)
    return try Data(contentsOf: url)
  }
}

private final class ScriptedGrimodexReader: GrimodexFileReading, @unchecked Sendable {
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
      guard !states.isEmpty else { return nil }
      return states.removeFirst()
    }
    return project
  }
}

final class GrimodexSnapshotManagerTests: XCTestCase {
  func testPathResolverUsesOverrideThenXdgThenHomeFallback() {
    let home = URL(fileURLWithPath: "/home/tester", isDirectory: true)

    XCTAssertEqual(
      GrimodexPathResolver.resolve(
        environment: ["GRIMODEX_IME_ROOT": "/custom/ime"],
        homeDirectory: home
      ).path,
      "/custom/ime"
    )
    XCTAssertEqual(
      GrimodexPathResolver.resolve(
        environment: ["XDG_DATA_HOME": "/xdg/data"],
        homeDirectory: home
      ).path,
      "/xdg/data/com.miyakey.grimodex/ime"
    )
    XCTAssertEqual(
      GrimodexPathResolver.resolve(environment: [:], homeDirectory: home).path,
      "/home/tester/.local/share/com.miyakey.grimodex/ime"
    )
  }

  func testValidSharedFixtureMapsDictionaryAndStructuredZenzaiContext() throws {
    let sandbox = try GrimodexFixtureSandbox()
    try sandbox.installState("valid/state-active.json")
    try sandbox.installProject("valid/project-with-zenzai-context.json")

    let result = GrimodexSnapshotLoader(rootURL: sandbox.root).load()

    XCTAssertEqual(result.diagnostic, .loaded)
    XCTAssertEqual(result.payload?.projectID, "project-a")
    XCTAssertEqual(
      result.payload?.dictionaryEntries,
      [
        GrimodexMappedDictionaryEntry(
          ruby: "セツナ",
          word: "刹那",
          cid: 1289,
          mid: 501,
          value: -5,
          entryID: "entry-setsuna"
        )
      ]
    )
    XCTAssertEqual(result.payload?.conditions.topic, "星海年代記・軍事SF・宇宙植民地を舞台にした物語")
    XCTAssertNil(result.payload?.conditions.style)
    XCTAssertNil(result.payload?.conditions.preference)
  }

  func testLegacyProfileFallsBackToBoundedTopicWithoutZenzaiContext() throws {
    let sandbox = try GrimodexFixtureSandbox()
    try sandbox.installState("valid/state-active.json")
    var project = try JSONSerialization.jsonObject(
      with: sandbox.fixtureData("valid/project-with-zenzai-context.json")
    ) as! [String: Any]
    project.removeValue(forKey: "zenzai_context")
    project["profile"] = "1234567890123456789012345・後半は切り捨て"
    try JSONSerialization.data(withJSONObject: project).write(
      to: sandbox.root.appendingPathComponent("projects/project-a.json")
    )

    let result = GrimodexSnapshotLoader(rootURL: sandbox.root).load()

    XCTAssertEqual(result.diagnostic, .loaded)
    XCTAssertEqual(result.payload?.conditions.topic, "1234567890123456789012345")
    XCTAssertNil(result.payload?.conditions.style)
    XCTAssertNil(result.payload?.conditions.preference)
  }

  func testInactiveStateProducesAnEmptyPayloadWithoutAnError() throws {
    let sandbox = try GrimodexFixtureSandbox()
    try sandbox.installState("valid/state-inactive.json")

    let result = GrimodexSnapshotLoader(rootURL: sandbox.root).load()

    XCTAssertNil(result.payload)
    XCTAssertEqual(result.diagnostic, .inactive)
  }

  func testSharedInvalidAndMaliciousFixturesFailClosed() throws {
    let stateCases: [(String, GrimodexLoadDiagnostic)] = [
      ("invalid/state-unsupported-version.json", .invalidState),
      ("malicious/state-path-traversal.json", .invalidState),
    ]
    for (fixture, expected) in stateCases {
      let sandbox = try GrimodexFixtureSandbox()
      try sandbox.installState(fixture)
      XCTAssertEqual(GrimodexSnapshotLoader(rootURL: sandbox.root).load().diagnostic, expected)
    }

    for fixture in [
      "invalid/project-unsupported-version.json",
      "invalid/project-unknown-category.json",
      "malicious/project-path-traversal.json",
      "malicious/project-control-character.json",
    ] {
      let sandbox = try GrimodexFixtureSandbox()
      try sandbox.installState("valid/state-active.json")
      try sandbox.installProject(fixture)
      let result = GrimodexSnapshotLoader(rootURL: sandbox.root).load()
      XCTAssertNil(result.payload)
      XCTAssertEqual(result.diagnostic, .invalidSnapshot, fixture)
    }
  }

  func testBoundedReadersRejectOversizedStateAndProjectFiles() throws {
    let stateSandbox = try GrimodexFixtureSandbox()
    try Data(repeating: 0x20, count: 65_537).write(
      to: stateSandbox.root.appendingPathComponent("state.json")
    )
    XCTAssertEqual(
      GrimodexSnapshotLoader(rootURL: stateSandbox.root).load().diagnostic,
      .invalidState
    )

    let projectSandbox = try GrimodexFixtureSandbox()
    try projectSandbox.installState("valid/state-active.json")
    try Data(repeating: 0x20, count: 16_777_217).write(
      to: projectSandbox.root.appendingPathComponent("projects/project-a.json")
    )
    XCTAssertEqual(
      GrimodexSnapshotLoader(rootURL: projectSandbox.root).load().diagnostic,
      .invalidSnapshot
    )
  }

  func testStateDoubleReadNeverPublishesAMixedProjectSwitch() throws {
    let sandbox = try GrimodexFixtureSandbox()
    let stateA = try sandbox.fixtureData("valid/state-active.json")
    let stateB = Data(
      "{\"format_version\":1,\"active_project_id\":\"project-b\",\"updated_at\":\"2026-07-11T00:00:01.000Z\"}".utf8
    )
    let projectA = try sandbox.fixtureData("valid/project-with-zenzai-context.json")
    let reader = ScriptedGrimodexReader(states: [stateA, stateB], project: projectA)

    let result = GrimodexSnapshotLoader(rootURL: sandbox.root, fileReader: reader).load()

    XCTAssertNil(result.payload)
    XCTAssertEqual(result.diagnostic, .stateChangedDuringRead)
  }

  func testManagerAdvancesGenerationOnlyForSemanticPayloadChanges() throws {
    let sandbox = try GrimodexFixtureSandbox()
    try sandbox.installState("valid/state-active.json")
    try sandbox.installProject("valid/project-with-zenzai-context.json")
    let manager = GrimodexSnapshotManager(loader: GrimodexSnapshotLoader(rootURL: sandbox.root))

    let first = manager.reload()
    XCTAssertEqual(first.generation, 1)
    XCTAssertNotNil(first.payload)

    var timestampOnly = try JSONSerialization.jsonObject(
      with: sandbox.fixtureData("valid/project-with-zenzai-context.json")
    ) as! [String: Any]
    timestampOnly["generated_at"] = "2026-07-11T00:00:01.000Z"
    try JSONSerialization.data(withJSONObject: timestampOnly).write(
      to: sandbox.root.appendingPathComponent("projects/project-a.json")
    )
    XCTAssertEqual(manager.reload().generation, 1)

    var changed = timestampOnly
    var entries = changed["entries"] as! [[String: Any]]
    entries[0]["surface"] = "刹那改"
    changed["entries"] = entries
    try JSONSerialization.data(withJSONObject: changed).write(
      to: sandbox.root.appendingPathComponent("projects/project-a.json")
    )
    XCTAssertEqual(manager.reload().generation, 2)

    try Data("not-json".utf8).write(
      to: sandbox.root.appendingPathComponent("projects/project-a.json")
    )
    let cleared = manager.reload()
    XCTAssertEqual(cleared.generation, 3)
    XCTAssertNil(cleared.payload)
    XCTAssertEqual(manager.reload().generation, 3)
  }
}
