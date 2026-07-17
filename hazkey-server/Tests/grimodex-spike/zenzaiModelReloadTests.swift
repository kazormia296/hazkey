import Foundation
import KanaKanjiConverterModule
import KanaKanjiConverterModuleWithDefaultDictionary
import XCTest

@testable import hazkey_server

final class ZenzaiModelReloadTests: XCTestCase {
  func testContextualModeOverrideTrueBeatsDisabledProfile() {
    let config = makeEnabledZenzaiConfig()
    let leftContext = "前の文脈"

    config.currentProfile.zenzaiContextualMode = false
    let profileDisabledMode = config.genZenzaiMode(leftContext: leftContext)
    let overriddenMode = config.genZenzaiMode(
      leftContext: leftContext,
      contextualModeOverride: true
    )

    config.currentProfile.zenzaiContextualMode = true
    let profileEnabledMode = config.genZenzaiMode(leftContext: leftContext)

    XCTAssertNotEqual(overriddenMode, profileDisabledMode)
    XCTAssertEqual(overriddenMode, profileEnabledMode)
  }

  func testContextualModeOverrideFalseBeatsEnabledProfile() {
    let config = makeEnabledZenzaiConfig()
    let leftContext = "前の文脈"

    config.currentProfile.zenzaiContextualMode = true
    let profileEnabledMode = config.genZenzaiMode(leftContext: leftContext)
    let overriddenMode = config.genZenzaiMode(
      leftContext: leftContext,
      contextualModeOverride: false
    )

    config.currentProfile.zenzaiContextualMode = false
    let profileDisabledMode = config.genZenzaiMode(leftContext: leftContext)

    XCTAssertNotEqual(overriddenMode, profileEnabledMode)
    XCTAssertEqual(overriddenMode, profileDisabledMode)
  }

  func testContextualModeOverrideNilFollowsProfile() {
    let config = makeEnabledZenzaiConfig()
    let leftContext = "前の文脈"

    config.currentProfile.zenzaiContextualMode = false
    let inheritedDisabledMode = config.genZenzaiMode(
      leftContext: leftContext,
      contextualModeOverride: nil
    )
    let explicitlyDisabledMode = config.genZenzaiMode(
      leftContext: leftContext,
      contextualModeOverride: false
    )

    config.currentProfile.zenzaiContextualMode = true
    let inheritedEnabledMode = config.genZenzaiMode(
      leftContext: leftContext,
      contextualModeOverride: nil
    )
    let explicitlyEnabledMode = config.genZenzaiMode(
      leftContext: leftContext,
      contextualModeOverride: true
    )

    XCTAssertEqual(inheritedDisabledMode, explicitlyDisabledMode)
    XCTAssertEqual(inheritedEnabledMode, explicitlyEnabledMode)
    XCTAssertNotEqual(inheritedDisabledMode, inheritedEnabledMode)
  }

  func testRuntimeGenerationChangesURLIdentityWithoutChangingFilesystemPath() {
    let modelURL = URL(fileURLWithPath: "/tmp/grimodex model/zenzai.gguf")
    let generation1 = UUID(uuidString: "00000000-0000-0000-0000-000000000001")!
    let generation2 = UUID(uuidString: "00000000-0000-0000-0000-000000000002")!

    let first = makeZenzaiRuntimeModelURL(
      modelURL: modelURL,
      generation: generation1
    )
    let second = makeZenzaiRuntimeModelURL(
      modelURL: modelURL,
      generation: generation2
    )

    XCTAssertNotEqual(first, second)
    XCTAssertEqual(first.path, modelURL.path)
    XCTAssertEqual(second.path, modelURL.path)
    XCTAssertTrue(first.isFileURL)
    XCTAssertTrue(second.isFileURL)
    XCTAssertEqual(
      URLComponents(url: first, resolvingAgainstBaseURL: false)?
        .queryItems?
        .first(where: { $0.name == "grimodex_zenzai_generation" })?
        .value,
      generation1.uuidString.lowercased()
    )
  }

  func testReloadRotatesConverterResourceURLAndKeepsPublishedModelPath() throws {
    let temporaryDirectory = FileManager.default.temporaryDirectory
      .appendingPathComponent(UUID().uuidString, isDirectory: true)
    try FileManager.default.createDirectory(
      at: temporaryDirectory,
      withIntermediateDirectories: true
    )
    defer { try? FileManager.default.removeItem(at: temporaryDirectory) }

    let modelURL = temporaryDirectory.appendingPathComponent("zenzai.gguf")
    try Data("old model".utf8).write(to: modelURL, options: .atomic)

    var generations = [
      UUID(uuidString: "00000000-0000-0000-0000-000000000011")!,
      UUID(uuidString: "00000000-0000-0000-0000-000000000012")!,
    ].makeIterator()
    let config = HazkeyServerConfig(
      zenzaiBackendDevicesProvider: { [] },
      zenzaiModelPathProvider: { modelURL },
      zenzaiRuntimeGenerationProvider: {
        guard let generation = generations.next() else {
          XCTFail("the test requested an unexpected Zenzai generation")
          return UUID()
        }
        return generation
      },
      zenzaiBackendAvailableOverride: true
    )
    config.currentProfile.zenzaiEnable = true

    let firstMode = config.genZenzaiMode(leftContext: "")
    guard case .enabled(let firstRuntimeURL) = config.zenzaiRuntimeDecision(
      zenzaiAllowed: true
    ) else {
      return XCTFail("the injected Zenzai model should be enabled")
    }

    try Data("new model".utf8).write(to: modelURL, options: .atomic)
    config.reloadZenzaiModel()

    let secondMode = config.genZenzaiMode(leftContext: "")
    guard case .enabled(let secondRuntimeURL) = config.zenzaiRuntimeDecision(
      zenzaiAllowed: true
    ) else {
      return XCTFail("the reloaded Zenzai model should be enabled")
    }

    XCTAssertNotEqual(firstMode, secondMode)
    XCTAssertNotEqual(firstRuntimeURL, secondRuntimeURL)
    XCTAssertEqual(firstRuntimeURL.path, modelURL.path)
    XCTAssertEqual(secondRuntimeURL.path, modelURL.path)
    XCTAssertEqual(
      config.getCurrentConfig().currentConfig.zenzaiModelPath,
      modelURL.path,
      "settings must keep publishing the stable managed model path"
    )
    XCTAssertEqual(
      try Data(contentsOf: URL(fileURLWithPath: secondRuntimeURL.path)),
      Data("new model".utf8),
      "the rotated runtime URL must resolve to the atomically replaced file"
    )
  }

  func testRealGGUFIsLoadedAgainAfterSamePathReplacement() throws {
    guard let fixturePath = ProcessInfo.processInfo.environment[
      "GRIMODEX_ZENZAI_RELOAD_TEST_MODEL"
    ], !fixturePath.isEmpty else {
      throw XCTSkip(
        "Set GRIMODEX_ZENZAI_RELOAD_TEST_MODEL to run the real GGUF reload test"
      )
    }
    let fixtureURL = URL(fileURLWithPath: fixturePath)
    guard FileManager.default.fileExists(atPath: fixtureURL.path) else {
      XCTFail("The configured GGUF reload fixture does not exist")
      return
    }

    let temporaryDirectory = FileManager.default.temporaryDirectory
      .appendingPathComponent(UUID().uuidString, isDirectory: true)
    try FileManager.default.createDirectory(
      at: temporaryDirectory,
      withIntermediateDirectories: true
    )
    defer { try? FileManager.default.removeItem(at: temporaryDirectory) }

    let modelURL = temporaryDirectory.appendingPathComponent("zenzai.gguf")
    try FileManager.default.copyItem(at: fixtureURL, to: modelURL)
    let config = HazkeyServerConfig(zenzaiModelPathProvider: { modelURL })
    guard config.zenzaiAvailable else {
      throw XCTSkip("No Zenzai backend is available for the real GGUF reload test")
    }
    config.currentProfile.zenzaiEnable = true
    config.currentProfile.zenzaiInferLimit = 1
    config.currentProfile.useInputHistory = false

    var composingText = ComposingText()
    composingText.insertAtCursorPosition("かな", inputStyle: .direct)
    let converter = KanaKanjiConverter.withDefaultDictionary()
    guard case .enabled(let firstRuntimeURL) = config.zenzaiRuntimeDecision(
      zenzaiAllowed: true
    ) else {
      return XCTFail("the first real model generation should be enabled")
    }
    _ = converter.requestCandidates(
      composingText,
      options: config.genBaseConvertRequestOptions()
    )
    XCTAssertEqual(
      converter.zenzStatus,
      "load \(firstRuntimeURL.absoluteString)"
    )

    let replacementURL = temporaryDirectory.appendingPathComponent(
      "replacement.gguf"
    )
    try FileManager.default.copyItem(at: fixtureURL, to: replacementURL)
    try FileManager.default.removeItem(at: modelURL)
    try FileManager.default.moveItem(at: replacementURL, to: modelURL)
    config.reloadZenzaiModel()

    guard case .enabled(let secondRuntimeURL) = config.zenzaiRuntimeDecision(
      zenzaiAllowed: true
    ) else {
      return XCTFail("the replacement real model generation should be enabled")
    }
    XCTAssertNotEqual(firstRuntimeURL, secondRuntimeURL)
    _ = converter.requestCandidates(
      composingText,
      options: config.genBaseConvertRequestOptions()
    )
    XCTAssertEqual(
      converter.zenzStatus,
      "load \(secondRuntimeURL.absoluteString)",
      "the same converter must load the replacement instead of reusing its old URL"
    )
  }

  private func makeEnabledZenzaiConfig() -> HazkeyServerConfig {
    let modelURL = URL(fileURLWithPath: "/tmp/fake-zenzai-model.gguf")
    let generation = UUID(
      uuidString: "00000000-0000-0000-0000-000000000021"
    )!
    let config = HazkeyServerConfig(
      zenzaiBackendDevicesProvider: { [] },
      zenzaiModelPathProvider: { modelURL },
      zenzaiRuntimeGenerationProvider: { generation },
      zenzaiBackendAvailableOverride: true
    )
    config.currentProfile.zenzaiEnable = true
    return config
  }
}
