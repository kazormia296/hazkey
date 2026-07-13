import Foundation
import KanaKanjiConverterModule
import KanaKanjiConverterModuleWithDefaultDictionary
import XCTest

@testable import hazkey_server

final class GrimodexUserDictionaryTests: XCTestCase {
  func testCrudRejectsDuplicatesAndUnknownIDs() throws {
    let store = UserDictionaryStore()
    let entry = UserDictionaryEntry(
      id: "entry-a",
      reading: "せつな",
      surface: "刹那",
      partOfSpeech: "noun"
    )

    XCTAssertEqual(try store.add(entry), entry)
    XCTAssertThrowsError(try store.add(entry)) { error in
      XCTAssertEqual(error as? UserDictionaryError, .duplicate)
    }

    var changed = entry
    changed.surface = "刹那の人"
    try store.update(changed)
    XCTAssertEqual(store.entries.first?.surface, "刹那の人")
    XCTAssertThrowsError(try store.remove(id: "missing")) { error in
      XCTAssertEqual(error as? UserDictionaryError, .missingEntry)
    }
  }

  func testImportExportSupportsMergeAndRejectsMalformedPayload() throws {
    let source = UserDictionaryStore(entries: [
      UserDictionaryEntry(
        id: "entry-a",
        reading: "りゅうせいこう",
        surface: "龍星港",
        partOfSpeech: "noun"
      )
    ])
    let data = try source.exportJSON()

    let merged = UserDictionaryStore(entries: [
      UserDictionaryEntry(
        id: "entry-b",
        reading: "きどうれき",
        surface: "軌道暦",
        partOfSpeech: "noun"
      )
    ])
    try merged.importJSON(data, merge: true)
    try merged.importJSON(data, merge: true)
    XCTAssertEqual(merged.entries.map(\.id), ["entry-b", "entry-a"])

    XCTAssertThrowsError(try merged.importJSON(Data("not-json".utf8))) { error in
      XCTAssertEqual(error as? UserDictionaryError, .invalidImport)
    }
  }

  func testPersistentStoreReloadsAtomicCrud() throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(
      "GrimodexUserDictionary-\(UUID().uuidString)",
      isDirectory: true
    )
    defer { try? FileManager.default.removeItem(at: directory) }
    let url = directory.appendingPathComponent("user-dictionary-v1.json")
    let entry = UserDictionaryEntry(
      id: "persistent-entry",
      reading: "せつな",
      surface: "刹那",
      partOfSpeech: "person"
    )

    let first = UserDictionaryStore(persistenceURL: url)
    try first.add(entry)
    XCTAssertTrue(FileManager.default.fileExists(atPath: url.path))
    XCTAssertEqual(UserDictionaryStore(persistenceURL: url).entries, [entry])

    try first.remove(id: entry.id)
    XCTAssertTrue(UserDictionaryStore(persistenceURL: url).entries.isEmpty)
  }

  func testDictionaryElementNormalizesReadingAndPartOfSpeech() {
    let entry = UserDictionaryEntry(
      reading: "せつな",
      surface: "刹那",
      partOfSpeech: "person"
    )
    let element = entry.dictionaryElement

    XCTAssertEqual(element.ruby, "セツナ")
    XCTAssertEqual(element.word, "刹那")
    XCTAssertEqual(element.lcid, CIDData.人名一般.cid)
    XCTAssertEqual(element.rcid, CIDData.人名一般.cid)
  }

  func testRegistryImportsPersonalDictionaryIntoNewSessionConverter() throws {
    let store = UserDictionaryStore(entries: [
      UserDictionaryEntry(
        id: "converter-entry",
        reading: "ぐりもでっくすじしょ",
        surface: "個人辞書成功",
        partOfSpeech: "noun"
      )
    ])
    let registry = HazkeySessionRegistry(
      converterFactory: { .withDefaultDictionary() },
      userDictionaryStore: store
    )
    let session = registry.open(
      clientContext: GrimodexClientContext(
        program: "firefox",
        frontend: "wayland",
        secureInput: false
      ),
      ownerFd: 10
    )
    let environment = try XCTUnwrap(registry.environment(for: session, ownerFd: 10))
    XCTAssertEqual(environment.userDictionaryEntryCount, 1)
    var text = ComposingText()
    text.insertAtCursorPosition("ぐりもでっくすじしょ", inputStyle: .direct)
    let candidates = environment.converter.requestCandidates(
      text,
      options: environment.baseConvertRequestOptions
    ).mainResults.map(\.text)

    XCTAssertTrue(
      candidates.contains("個人辞書成功"),
      "personal dictionary candidate missing: \(candidates)"
    )
  }
}
