import KanaKanjiConverterModule
import KanaKanjiConverterModuleWithDefaultDictionary
import XCTest

@testable import hazkey_server

final class GrimodexConverterPolicyTests: XCTestCase {
  func testAsciiTokenHelperRetainsJoinedURLAndAddressSemantics() {
    XCTAssertEqual(
      ProtectedSurfacePolicy.asciiTokens(
        in: "https://example.com メール foo+bar@example.com ++"
      ),
      ["https://example.com", "foo+bar@example.com"]
    )
  }

  func testProtectedSymbolStyleHelperRetainsASCIISubsequenceSemantics() {
    XCTAssertTrue(
      ProtectedSurfacePolicy.protectedSymbolStyleIsPreserved(
        input: "foo/bar",
        output: "foo://bar"
      )
    )
    XCTAssertFalse(
      ProtectedSurfacePolicy.protectedSymbolStyleIsPreserved(
        input: "foo/bar",
        output: "foo:bar"
      )
    )
    XCTAssertTrue(
      ProtectedSurfacePolicy.protectedSymbolStyleIsPreserved(
        input: "かな。",
        output: "仮名"
      )
    )
  }

  func testProtectedSurfaceRequiresOrderedTokensWithExactCardinalityAndBoundaries() {
    XCTAssertTrue(allows("API API", for: "API API"))
    XCTAssertFalse(allows("API", for: "API API"))
    XCTAssertFalse(allows("API API API", for: "API API"))
    XCTAssertFalse(allows("bar foo", for: "foo bar"))
    XCTAssertFalse(allows("RAPID", for: "API"))
    XCTAssertFalse(allows("XAPI", for: "API"))
    XCTAssertTrue(allows("APIを呼びます", for: "APIを呼ぶ"))

    // Non-protected scalars must not be insertable at an existing boundary
    // inside a protected URL span.
    XCTAssertFalse(
      allows("https日本://example.com", for: "https://example.com")
    )
    XCTAssertFalse(
      allows("https\u{200B}://example.com", for: "https://example.com")
    )
  }

  func testProtectedSurfaceRequiresExactSymbolOrderCardinalityAndWidth() {
    XCTAssertTrue(allows("foo:/bar", for: "foo:/bar"))
    XCTAssertFalse(allows("foo/:bar", for: "foo:/bar"))
    XCTAssertFalse(allows("foo:bar", for: "foo:/bar"))
    XCTAssertFalse(allows("foo://bar", for: "foo:/bar"))

    XCTAssertTrue(allows("foo：bar", for: "foo：bar"))
    XCTAssertFalse(allows("foo:bar", for: "foo：bar"))
    XCTAssertFalse(allows("foobar", for: "foo：bar"))
    XCTAssertTrue(allows("ＡＰＩ：１", for: "ＡＰＩ：１"))
    XCTAssertFalse(allows("API:1", for: "ＡＰＩ：１"))

    XCTAssertTrue(allows("仮名。", for: "かな。"))
    XCTAssertFalse(allows("仮名", for: "かな。"))
    XCTAssertTrue(allows("仮名。デス", for: "かな。です"))
    XCTAssertFalse(allows("仮名です。", for: "かな。です"))
    XCTAssertFalse(allows("。仮名", for: "かな。"))
  }

  func testProtectedSurfaceAllowsUnprotectedJapaneseAndFullyTrustedDictionaries() {
    XCTAssertTrue(allows("変換", for: "へんかん"))
    let trusted = ConverterCandidate(
      text: "置換",
      consumingCount: 1,
      provenance: .projectDictionary
    )
    XCTAssertTrue(ProtectedSurfacePolicy.allows(trusted, for: "https://example.com"))
  }

  func testCompositeCandidateWithGenericNodeIsNotDictionaryTrusted() {
    let projectEntry = GrimodexMappedDictionaryEntry(
      ruby: "エーピーアイ",
      word: "API",
      cid: 1289,
      mid: 501,
      value: -4,
      priority: 3,
      entryID: "api"
    )
    let projectData = DicdataElement(
      word: projectEntry.word,
      ruby: projectEntry.ruby,
      cid: projectEntry.cid,
      mid: projectEntry.mid,
      value: projectEntry.value,
      metadata: .isFromUserDictionary
    )
    let genericData = DicdataElement(
      word: "RAPID",
      ruby: "ラピッド",
      cid: 1288,
      mid: 501,
      value: -10
    )
    let composite = Candidate(
      text: "APIRAPID",
      value: projectData.value() + genericData.value(),
      composingCount: .inputCount(9),
      lastMid: genericData.mid,
      data: [projectData, genericData]
    )
    let allProject = Candidate(
      text: "API",
      value: projectData.value(),
      composingCount: .inputCount(6),
      lastMid: projectData.mid,
      data: [projectData]
    )
    let adapter = makeAdapter(
      projectIndex: GrimodexProjectDictionaryIndex(entries: [projectEntry])
    )

    XCTAssertEqual(adapter.candidateProvenance(composite), .standard)
    XCTAssertEqual(adapter.candidateProvenance(allProject), .projectDictionary)
  }

  func testOrdinaryStopRetainsRollbackCandidateButSensitivePurgeRemovesIt() throws {
    let adapter = makeAdapter()
    let elements = "かな".map {
      CompositionElement(text: String($0), inputStyle: .direct)
    }
    let output = try adapter.candidates(
      for: CompositionInput(
        elements: elements,
        cursor: elements.count,
        leftContext: ""
      ),
      options: .default
    )
    let candidate = try XCTUnwrap(output.candidates.first { $0.sourceID != nil })

    adapter.stopComposition()
    let retained = try XCTUnwrap(
      adapter.stageLearning(candidate: candidate, reading: "かな")
    )
    adapter.discardStagedLearning(retained)

    let purgedToken = try XCTUnwrap(
      adapter.stageLearning(candidate: candidate, reading: "かな")
    )
    adapter.purgeSensitiveState()
    XCTAssertNil(adapter.stageLearning(candidate: candidate, reading: "かな"))
    // A token that referenced the purged Candidate is intentionally inert.
    adapter.commitStagedLearning(purgedToken)
  }

  func testSuggestionModeAndLimitComeOnlyFromPinnedConversionOptions() throws {
    var liveProviderReads = 0
    let adapter = makeAdapter(
      predictionConfigurationProvider: {
        liveProviderReads += 1
        return (false, 99)
      }
    )
    let elements = "かな".map {
      CompositionElement(text: String($0), inputStyle: .direct)
    }
    let input = CompositionInput(
      elements: elements,
      cursor: elements.count,
      leftContext: ""
    )
    let predictive = ConversionOptions(
      allowLearning: false,
      zenzaiEnabled: false,
      leftContext: "",
      rightContext: "",
      suggestionListMode: .predictive,
      suggestionListLimit: 1
    )

    let realtime = try adapter.realtimeCandidates(for: input, options: predictive)
    let predictions = try adapter.predictions(for: input, options: predictive)
    XCTAssertLessThanOrEqual(realtime.candidates.count, 1)
    XCTAssertLessThanOrEqual(predictions.candidates.count, 1)
    XCTAssertEqual(liveProviderReads, 0)

    let normal = ConversionOptions(
      allowLearning: false,
      zenzaiEnabled: false,
      leftContext: "",
      rightContext: "",
      suggestionListMode: .normal,
      suggestionListLimit: 9
    )
    XCTAssertTrue(try adapter.predictions(for: input, options: normal).candidates.isEmpty)
    XCTAssertEqual(liveProviderReads, 0)
  }

  func testSuggestionLimitIsClampedToTheSettingsSupportedRange() {
    let tooSmall = ConversionOptions(
      allowLearning: false,
      zenzaiEnabled: false,
      leftContext: "",
      rightContext: "",
      suggestionListMode: .predictive,
      suggestionListLimit: Int.min
    )
    let tooLarge = ConversionOptions(
      allowLearning: false,
      zenzaiEnabled: false,
      leftContext: "",
      rightContext: "",
      suggestionListMode: .predictive,
      suggestionListLimit: Int.max
    )
    let policy = PinnedCompositionPolicy(
      allowsLearning: true,
      secureInput: false,
      zenzaiEnabled: false,
      projectRevision: 0,
      suggestionListLimit: Int.max
    )

    XCTAssertEqual(tooSmall.suggestionListLimit, 1)
    XCTAssertEqual(tooLarge.suggestionListLimit, 10)
    XCTAssertEqual(policy.suggestionListLimit, 10)
  }

  func testSuggestionLimitCodableCompatibilityAndRecoveryValidation() throws {
    let encodedPolicy = try JSONEncoder().encode(PinnedCompositionPolicy.default)
    var legacyPolicy = try XCTUnwrap(
      JSONSerialization.jsonObject(with: encodedPolicy) as? [String: Any]
    )
    XCTAssertEqual(legacyPolicy["suggestionListLimit"] as? Int, 9)
    XCTAssertNil(legacyPolicy["suggestionListLimitWasPresentInEncodedPolicy"])
    legacyPolicy.removeValue(forKey: "suggestionListLimit")
    let legacyData = try JSONSerialization.data(withJSONObject: legacyPolicy)
    let decodedLegacyPolicy = try JSONDecoder().decode(
      PinnedCompositionPolicy.self,
      from: legacyData
    )
    XCTAssertEqual(decodedLegacyPolicy.suggestionListLimit, 9)
    XCTAssertFalse(decodedLegacyPolicy.suggestionListLimitWasPresentInEncodedPolicy)

    let checkpoint = RecoveryCheckpoint(
      revision: 1,
      phase: .composing,
      composition: CompositionBuffer(
        elements: [CompositionElement(text: "a")]
      ),
      nextCandidateGeneration: 0,
      nextEffectID: 1,
      leftContext: "",
      rightContext: "",
      policy: .default
    )
    let checkpointData = try XCTUnwrap(
      checkpoint.persistedData(isSecureInput: false)
    )

    var legacyCheckpointObject = try XCTUnwrap(
      JSONSerialization.jsonObject(with: checkpointData) as? [String: Any]
    )
    var legacyCheckpointPolicy = try XCTUnwrap(
      legacyCheckpointObject["policy"] as? [String: Any]
    )
    legacyCheckpointPolicy.removeValue(forKey: "suggestionListLimit")
    legacyCheckpointObject["policy"] = legacyCheckpointPolicy
    let legacyCheckpointData = try JSONSerialization.data(
      withJSONObject: legacyCheckpointObject
    )
    for currentLimit in [1, 3] {
      let currentPolicy = PinnedCompositionPolicy(
        allowsLearning: true,
        secureInput: false,
        zenzaiEnabled: false,
        projectRevision: 0,
        suggestionListLimit: currentLimit
      )
      let reducer = ImeReducer(session: CompositionSession(policy: currentPolicy))

      let restored = reducer.reduce(
        .restoreCheckpoint(legacyCheckpointData),
        requestID: "legacy-limit-\(currentLimit)",
        expectedRevision: 0
      )

      XCTAssertEqual(restored.status, .success)
      XCTAssertEqual(reducer.session.policy.suggestionListLimit, currentLimit)
      XCTAssertTrue(
        reducer.session.policy.suggestionListLimitWasPresentInEncodedPolicy
      )
    }

    var checkpointObject = try XCTUnwrap(
      JSONSerialization.jsonObject(with: checkpointData) as? [String: Any]
    )
    var checkpointPolicy = try XCTUnwrap(
      checkpointObject["policy"] as? [String: Any]
    )
    for invalidLimit in [0, 11] {
      checkpointPolicy["suggestionListLimit"] = invalidLimit
      checkpointObject["policy"] = checkpointPolicy
      let invalidData = try JSONSerialization.data(
        withJSONObject: checkpointObject
      )

      XCTAssertEqual(
        ImeReducer().reduce(
          .restoreCheckpoint(invalidData),
          requestID: "invalid-suggestion-limit-\(invalidLimit)",
          expectedRevision: 0
        ).status,
        .invalidAction
      )
    }
  }

  private func allows(_ output: String, for input: String) -> Bool {
    ProtectedSurfacePolicy.allows(
      ConverterCandidate(
        text: output,
        consumingCount: 1,
        provenance: .standard
      ),
      for: input
    )
  }

  private func makeAdapter(
    projectIndex: GrimodexProjectDictionaryIndex = .empty,
    predictionConfigurationProvider: @escaping () -> (
      enabled: Bool,
      limit: Int
    ) = { (false, 0) }
  ) -> HazkeyKanaKanjiConverterAdapter {
    let store = DicdataStore.withDefaultDictionary()
    return HazkeyKanaKanjiConverterAdapter(
      converter: KanaKanjiConverter(dicdataStore: store),
      boundaryConverter: KanaKanjiConverter(dicdataStore: store),
      optionsProvider: { _ in
        var options = HazkeyServerConfig().genBaseConvertRequestOptions()
        options.zenzaiMode = .off
        return options
      },
      predictionConfigurationProvider: predictionConfigurationProvider,
      projectDictionaryIndexProvider: { projectIndex }
    )
  }
}
