import Foundation
import KanaKanjiConverterModule
import KanaKanjiConverterModuleWithDefaultDictionary
import XCTest

@testable import hazkey_server

private final class MutableEnvironmentRevisionProvider:
  GrimodexRevisionProviding,
  @unchecked Sendable
{
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
  func testEnvironmentImportsRevisionDictionaryIntoItsConverter() {
    let revision = makeRevision(
      1,
      projectID: "project-wiring",
      surface: "Grimodex配線確認"
    )
    let environment = HazkeySessionEnvironment(
      revisionProvider: MutableEnvironmentRevisionProvider(revision),
      converter: .withDefaultDictionary(),
      boundaryConverter: .withDefaultDictionary()
    )
    environment.refreshGrimodexIntegration()

    var text = ComposingText()
    text.insertAtCursorPosition("せつな", inputStyle: .direct)
    var options = environment.baseConvertRequestOptions
    options.N_best = 9
    options.zenzaiMode = .off
    let candidates = environment.converter.requestCandidates(
      text,
      options: options
    ).mainResults

    XCTAssertEqual(
      candidates.first?.text,
      "Grimodex配線確認",
      "the active project term must be the highest-ranked candidate"
    )
  }

  func testSecureRevisionRevokesProjectDictionaryAndPolicyImmediately() {
    let provider = MutableEnvironmentRevisionProvider(
      makeRevision(1, projectID: "project-a", surface: "刹那")
    )
    let environment = HazkeySessionEnvironment(
      revisionProvider: provider,
      converter: .withDefaultDictionary(),
      boundaryConverter: .withDefaultDictionary()
    )
    environment.refreshGrimodexIntegration()
    XCTAssertTrue(environment.grimodexAllowsLearning)
    XCTAssertFalse(environment.grimodexSecureInput)

    provider.update(GrimodexIntegrationRevision(
      generation: 2,
      payload: nil,
      allowsLearning: false,
      secureInput: true
    ))
    environment.refreshGrimodexIntegration()

    XCTAssertFalse(environment.grimodexAllowsLearning)
    XCTAssertTrue(environment.grimodexSecureInput)
    XCTAssertNil(environment.grimodexAppliedRevision?.payload)
    XCTAssertEqual(environment.grimodexActiveConditions, .empty)
  }

  func testZenzaiResolverKeepsProfileAndOverlaysProjectValues() {
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

  func testZenzaiResolverReturnsProfileValuesWithoutProjectPayload() {
    XCTAssertEqual(
      GrimodexZenzaiConditionResolver.resolve(
        profile: "user-profile",
        topic: "user-topic",
        style: "user-style",
        preference: "user-preference",
        project: .empty
      ),
      GrimodexResolvedZenzaiConditions(
        profile: "user-profile",
        topic: "user-topic",
        style: "user-style",
        preference: "user-preference"
      )
    )
  }

  private func makeRevision(
    _ generation: UInt64,
    projectID: String,
    surface: String
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
        conditions: .empty
      )
    )
  }
}
