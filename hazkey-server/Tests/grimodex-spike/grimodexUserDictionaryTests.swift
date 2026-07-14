import Foundation
import KanaKanjiConverterModule
import KanaKanjiConverterModuleWithDefaultDictionary
import XCTest

@testable import hazkey_server

final class GrimodexUserDictionaryTests: XCTestCase {
  func testCandidateIndexNormalizesExactReadingAndPreservesStableOrder() {
    let index = UserDictionaryCandidateIndex(entries: [
      UserDictionaryEntry(
        id: "first",
        reading: "せつな",
        surface: "刹那",
        partOfSpeech: "noun"
      ),
      UserDictionaryEntry(
        id: "unrelated",
        reading: "きどうれき",
        surface: "軌道暦",
        partOfSpeech: "noun"
      ),
      UserDictionaryEntry(
        id: "second",
        reading: "ｾﾂﾅ",
        surface: "セツナ",
        partOfSpeech: "noun",
        layer: .temporary
      ),
    ])

    let matches = index.entries(forRuby: "セツナ")
    XCTAssertEqual(matches.map(\.id), ["first", "second"])
    XCTAssertEqual(index.entryCount, 3)
    XCTAssertTrue(index.entries(forRuby: "せつ").isEmpty)
    XCTAssertTrue(UserDictionaryCandidateIndex.empty.entries(forRuby: "せつな").isEmpty)
  }

  func testCandidateIndexSnapshotRefreshesAtomicallyAfterMutations() throws {
    let first = UserDictionaryEntry(
      id: "first",
      reading: "せつな",
      surface: "刹那",
      partOfSpeech: "noun"
    )
    let second = UserDictionaryEntry(
      id: "second",
      reading: "せつな",
      surface: "セツナ",
      partOfSpeech: "noun"
    )
    let store = UserDictionaryStore(entries: [first])
    let original = store.candidateIndexSnapshot

    try store.add(second)
    XCTAssertEqual(
      store.candidateIndexSnapshot.entries(forRuby: "せつな").map(\.id),
      ["first", "second"]
    )
    XCTAssertEqual(
      original.entries(forRuby: "せつな").map(\.id),
      ["first"],
      "an already published immutable snapshot must not change"
    )

    var moved = first
    moved.reading = "きどうれき"
    try store.update(moved)
    XCTAssertEqual(
      store.candidateIndexSnapshot.entries(forRuby: "せつな").map(\.id),
      ["second"]
    )
    XCTAssertEqual(
      store.candidateIndexSnapshot.entries(forRuby: "キドウレキ").map(\.id),
      ["first"]
    )

    try store.remove(id: second.id)
    XCTAssertTrue(store.candidateIndexSnapshot.entries(forRuby: "せつな").isEmpty)

    let imported = UserDictionaryEntry(
      id: "imported",
      reading: "りゅうせいこう",
      surface: "龍星港",
      partOfSpeech: "noun"
    )
    try store.importJSON(try JSONEncoder().encode([imported]))
    XCTAssertEqual(
      store.candidateIndexSnapshot.entries(forRuby: "リュウセイコウ")
        .map(\.id),
      ["imported"]
    )
    XCTAssertTrue(store.candidateIndexSnapshot.entries(forRuby: "きどうれき").isEmpty)
  }

  func testGlobalStoreRejectsUnsupportedLayersForAddUpdateAndImport() throws {
    let personal = UserDictionaryEntry(
      id: "personal",
      reading: "せつな",
      surface: "刹那",
      partOfSpeech: "noun"
    )
    let store = UserDictionaryStore(entries: [personal])
    let unsupported = UserDictionaryEntry(
      id: "system",
      reading: "しすてむ",
      surface: "システム",
      partOfSpeech: "noun",
      layer: .system
    )

    XCTAssertThrowsError(try store.add(unsupported)) { error in
      XCTAssertEqual(error as? UserDictionaryError, .invalidField)
    }

    var changed = personal
    changed.layer = .project
    XCTAssertThrowsError(try store.update(changed)) { error in
      XCTAssertEqual(error as? UserDictionaryError, .invalidField)
    }

    let imported = UserDictionaryEntry(
      id: "project",
      reading: "ぷろじぇくと",
      surface: "プロジェクト",
      partOfSpeech: "noun",
      layer: .project
    )
    XCTAssertThrowsError(
      try store.importJSON(try JSONEncoder().encode([imported]))
    ) { error in
      XCTAssertEqual(error as? UserDictionaryError, .invalidField)
    }
    XCTAssertEqual(store.entries, [personal])
    XCTAssertEqual(
      store.candidateIndexSnapshot.entries(forRuby: "せつな").map(\.id),
      ["personal"]
    )
    XCTAssertTrue(store.candidateIndexSnapshot.entries(forRuby: "しすてむ").isEmpty)
    XCTAssertTrue(store.candidateIndexSnapshot.entries(forRuby: "ぷろじぇくと").isEmpty)
  }

  func testInitFiltersInvalidUnsupportedAndDuplicateEntriesFirstWins() throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(
      "GrimodexUserDictionaryInvalidLoad-\(UUID().uuidString)",
      isDirectory: true
    )
    try FileManager.default.createDirectory(
      at: directory,
      withIntermediateDirectories: true
    )
    defer { try? FileManager.default.removeItem(at: directory) }

    let valid = UserDictionaryEntry(
      id: "valid-first",
      reading: "せつな",
      surface: "刹那",
      partOfSpeech: "noun"
    )
    let candidates = [
      valid,
      UserDictionaryEntry(
        id: "empty-surface",
        reading: "から",
        surface: "",
        partOfSpeech: "noun"
      ),
      UserDictionaryEntry(
        id: "control",
        reading: "せつ\u{0001}な",
        surface: "制御文字",
        partOfSpeech: "noun"
      ),
      UserDictionaryEntry(
        id: "oversized",
        reading: String(repeating: "あ", count: 257),
        surface: "長すぎる読み",
        partOfSpeech: "noun"
      ),
      UserDictionaryEntry(
        id: "unsupported",
        reading: "ぷろじぇくと",
        surface: "プロジェクト",
        partOfSpeech: "noun",
        layer: .project
      ),
      UserDictionaryEntry(
        id: "valid-first",
        reading: "べつのよみ",
        surface: "重複ID",
        partOfSpeech: "noun"
      ),
      UserDictionaryEntry(
        id: "duplicate-surface",
        reading: "せつな",
        surface: "刹那",
        partOfSpeech: "noun"
      ),
    ]
    let persistenceURL = directory.appendingPathComponent("user-dictionary-v1.json")
    try JSONEncoder().encode(candidates).write(to: persistenceURL, options: .atomic)

    let persisted = UserDictionaryStore(persistenceURL: persistenceURL)
    let programmatic = UserDictionaryStore(entries: candidates)
    for store in [persisted, programmatic] {
      XCTAssertEqual(store.entries, [valid])
      XCTAssertEqual(store.candidateIndexSnapshot.entryCount, 1)
      XCTAssertEqual(
        store.candidateIndexSnapshot.entries(forRuby: "セツナ").map(\.id),
        ["valid-first"]
      )
      XCTAssertTrue(
        store.candidateIndexSnapshot.entries(forRuby: "ぷろじぇくと").isEmpty
      )
    }
  }

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
    let reloaded = UserDictionaryStore(persistenceURL: url)
    XCTAssertEqual(reloaded.entries, [entry])
    XCTAssertEqual(
      reloaded.candidateIndexSnapshot.entries(forRuby: "セツナ").map(\.id),
      ["persistent-entry"]
    )

    try first.remove(id: entry.id)
    let removed = UserDictionaryStore(persistenceURL: url)
    XCTAssertTrue(removed.entries.isEmpty)
    XCTAssertTrue(removed.candidateIndexSnapshot.entries(forRuby: "せつな").isEmpty)
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
      dicdataStoreFactory: { .withDefaultDictionary() },
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
