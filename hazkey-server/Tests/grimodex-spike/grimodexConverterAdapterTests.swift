import Foundation
import KanaKanjiConverterModule
import KanaKanjiConverterModuleWithDefaultDictionary
import XCTest

@testable import hazkey_server

final class GrimodexConverterAdapterTests: XCTestCase {
  func testNormalRealtimeSuggestionsRespectConfiguredLimit() throws {
    var requestOptions = HazkeyServerConfig().genBaseConvertRequestOptions()
    requestOptions.N_best = 9
    requestOptions.zenzaiMode = .off
    let adapter = HazkeyKanaKanjiConverterAdapter(
      converter: .withDefaultDictionary(),
      optionsProvider: { _ in requestOptions },
      predictionConfigurationProvider: { (false, 1) },
      suggestionListModeProvider: { .normal }
    )
    let elements = "かな".map {
      CompositionElement(text: String($0), inputStyle: .direct)
    }

    let output = try adapter.realtimeCandidates(
      for: CompositionInput(
        elements: elements,
        cursor: elements.count,
        leftContext: ""
      ),
      options: .default
    )

    XCTAssertEqual(output.candidates.count, 1)
    XCTAssertEqual(output.pageSize, 1)
  }
}
