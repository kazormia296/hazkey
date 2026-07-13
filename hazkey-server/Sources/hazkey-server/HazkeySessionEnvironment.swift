import Foundation
import KanaKanjiConverterModule

/// Session-scoped converter/configuration environment.
///
/// This type deliberately owns no preedit, cursor, candidate selection, or
/// commit state. `CompositionSession` is the only semantic IME state owner;
/// the environment supplies the converter and policy values pinned when a new
/// composition begins.
final class HazkeySessionEnvironment {
    let serverConfig: HazkeyServerConfig
    let converter: KanaKanjiConverter
    let boundaryConverter: KanaKanjiConverter
    private let grimodexRevisionProvider: any GrimodexRevisionProviding

    var keymap: Keymap
    var currentTableName: String
    var baseConvertRequestOptions: ConvertRequestOptions
    private(set) var grimodexProjectDictionaryIndex = GrimodexProjectDictionaryIndex.empty
    private var grimodexDynamicDictionaryEntries: [DicdataElement] = []
    private var personalDynamicDictionaryEntries: [DicdataElement] = []
    private var temporaryShortcutEntries: [DicdataElement] = []
    private let spikeDictionaryEntries: [DicdataElement]

    var userDictionaryEntryCount: Int {
        personalDynamicDictionaryEntries.count + temporaryShortcutEntries.count
    }

    private lazy var grimodexIntegration = GrimodexCompositionIntegrationController(
        applier: GrimodexEnvironmentDictionaryApplier(environment: self)
    )
    private(set) var grimodexResolvedZenzaiConditions: GrimodexResolvedZenzaiConditions?

    var grimodexAppliedRevision: GrimodexIntegrationRevision? {
        grimodexIntegration.appliedRevision
    }
    var grimodexPinnedRevision: GrimodexIntegrationRevision? {
        grimodexIntegration.pinnedRevision
    }
    var grimodexActiveConditions: GrimodexProjectConditions {
        grimodexIntegration.activeConditions
    }
    var grimodexAllowsLearning: Bool { grimodexIntegration.allowsLearning }
    var grimodexSecureInput: Bool { grimodexIntegration.secureInput }
    var grimodexAutoConvertMode: ImeAutoConvertMode {
        switch serverConfig.currentProfile.autoConvertMode {
        case .autoConvertAlways:
            return .always
        case .autoConvertForMultipleChars:
            return .forMultipleChars
        case .autoConvertDisabled, .unspecified:
            return .disabled
        default:
            return .disabled
        }
    }
    var grimodexLiveConversionDelayMilliseconds: UInt32 {
        guard serverConfig.currentProfile.hasLiveConversionDelayMsec else {
            return 228
        }
        return min(serverConfig.currentProfile.liveConversionDelayMsec, 1_000)
    }
    var grimodexSuggestionListMode: ImeSuggestionListMode {
        switch serverConfig.currentProfile.suggestionListMode {
        case .suggestionListShowNormalResults:
            return .normal
        case .suggestionListShowPredictiveResults:
            return .predictive
        case .suggestionListDisabled, .unspecified:
            return .disabled
        default:
            return .disabled
        }
    }

    init(
        serverConfig injectedServerConfig: HazkeyServerConfig? = nil,
        revisionProvider: any GrimodexRevisionProviding = GrimodexDisabledRevisionProvider(),
        converter injectedConverter: KanaKanjiConverter? = nil,
        boundaryConverter injectedBoundaryConverter: KanaKanjiConverter? = nil
    ) {
        grimodexRevisionProvider = revisionProvider
        let serverConfig = injectedServerConfig ?? HazkeyServerConfig()
        self.serverConfig = serverConfig
        let converter: KanaKanjiConverter
        let boundaryConverter: KanaKanjiConverter
        if let injectedConverter {
            guard let injectedBoundaryConverter else {
                preconditionFailure(
                    "an injected primary converter requires an independent boundary converter"
                )
            }
            precondition(
                injectedConverter !== injectedBoundaryConverter,
                "primary and boundary converters must be distinct instances"
            )
            converter = injectedConverter
            boundaryConverter = injectedBoundaryConverter
        } else {
            precondition(
                injectedBoundaryConverter == nil,
                "a boundary converter cannot be injected without a primary converter"
            )
            let store = DicdataStore(dictionaryURL: serverConfig.dictionaryPath)
            converter = KanaKanjiConverter(dicdataStore: store)
            boundaryConverter = KanaKanjiConverter(dicdataStore: store)
        }
        self.converter = converter
        self.boundaryConverter = boundaryConverter

        let spikeEntries = GrimodexDictionarySpike.isEnabled(
            environment: ProcessInfo.processInfo.environment
        ) ? GrimodexDictionarySpike.fixedEntries : []
        spikeDictionaryEntries = spikeEntries
        if !spikeEntries.isEmpty {
            converter.importDynamicUserDictionary(spikeEntries)
            if boundaryConverter !== converter {
                boundaryConverter.importDynamicUserDictionary(spikeEntries)
            }
            NSLog(
                "Injected \(spikeEntries.count) fixed Grimodex dictionary spike entries"
            )
        }

        keymap = serverConfig.loadKeymap()
        currentTableName = UUID().uuidString
        serverConfig.loadInputTable(tableName: currentTableName)
        baseConvertRequestOptions = serverConfig.genBaseConvertRequestOptions()

        prepareRuntimeDirectories()
        if let report = GrimodexDictionarySpike.runBenchmarkIfConfigured(
            converter: converter,
            options: baseConvertRequestOptions
        ) {
            let rss = report.residentMemoryKilobytes.map(String.init) ?? "unavailable"
            let rssDelta = report.residentMemoryDeltaKilobytes.map(String.init)
                ?? "unavailable"
            let candidateRank = report.candidateRank.map(String.init) ?? "missing"
            NSLog(
                "Grimodex dictionary benchmark entries=\(report.entryCount) "
                    + "import_ms=\(report.importMilliseconds) "
                    + "warm_p95_ms=\(report.warmP95Milliseconds) rss_kib=\(rss) "
                    + "rss_delta_kib=\(rssDelta) candidate_rank=\(candidateRank)"
            )
        }
    }

    func refreshGrimodexIntegration() {
        grimodexIntegration.observe(grimodexRevisionProvider.latest())
        refreshResolvedZenzaiConditions()
    }

    func replaceGrimodexDynamicDictionary(
        _ entries: [GrimodexMappedDictionaryEntry],
        projectIndex: GrimodexProjectDictionaryIndex
    ) {
        grimodexProjectDictionaryIndex = projectIndex
        grimodexDynamicDictionaryEntries = entries.map(\.dictionaryElement)
        applyDynamicDictionaries()
    }

    func replaceUserDictionary(_ entries: [UserDictionaryEntry]) {
        personalDynamicDictionaryEntries = entries
            .filter { $0.layer != .temporary }
            .map(\.dictionaryElement)
        temporaryShortcutEntries = entries
            .filter { $0.layer == .temporary }
            .map(\.dictionaryElement)
        applyDynamicDictionaries()
    }

    func clearProfileLearningData() {
        // Prime AzooKey's memory configuration before reset. A maintenance
        // environment may not have issued a conversion yet.
        var options = baseConvertRequestOptions
        options.zenzaiMode = .off
        var probe = ComposingText()
        probe.insertAtCursorPosition("あ", inputStyle: .direct)
        _ = converter.requestCandidates(probe, options: options)
        stopComposition()
        converter.resetMemory()
    }

    func reinitializeConfiguration() {
        stopComposition()
        keymap = serverConfig.loadKeymap()
        let newTableName = UUID().uuidString
        serverConfig.loadInputTable(tableName: newTableName)
        currentTableName = newTableName
        baseConvertRequestOptions = serverConfig.genBaseConvertRequestOptions()
        refreshGrimodexIntegration()
    }

    func close() {
        stopComposition()
    }

    func stopComposition() {
        converter.stopComposition()
        if boundaryConverter !== converter {
            boundaryConverter.stopComposition()
        }
    }

    private func applyDynamicDictionaries() {
        let entries = spikeDictionaryEntries
            + grimodexDynamicDictionaryEntries
            + personalDynamicDictionaryEntries
        let shortcuts = personalDynamicDictionaryEntries + temporaryShortcutEntries
        converter.importDynamicUserDictionary(entries, shortcuts: shortcuts)
        if boundaryConverter !== converter {
            boundaryConverter.importDynamicUserDictionary(entries, shortcuts: shortcuts)
        }
    }

    private func refreshResolvedZenzaiConditions() {
        let conditions = grimodexIntegration.activeConditions
        grimodexResolvedZenzaiConditions = GrimodexZenzaiConditionResolver.resolve(
            profile: serverConfig.currentProfile.zenzaiProfile,
            topic: serverConfig.currentProfile.zenzaiTopic,
            style: serverConfig.currentProfile.zenzaiStyle,
            preference: serverConfig.currentProfile.zenzaiPreference,
            project: conditions
        )
    }

    private func prepareRuntimeDirectories() {
        do {
            let newPath = HazkeyServerConfig.getStateDirectory().appendingPathComponent(
                "memory",
                isDirectory: true
            )
            if !FileManager.default.fileExists(atPath: newPath.path) {
                let oldPath = HazkeyServerConfig.getDataDirectory().appendingPathComponent(
                    "memory",
                    isDirectory: true
                )
                try FileManager.default.createDirectory(
                    at: newPath.deletingLastPathComponent(),
                    withIntermediateDirectories: true
                )
                if FileManager.default.fileExists(atPath: oldPath.path) {
                    try FileManager.default.moveItem(at: oldPath, to: newPath)
                } else {
                    try FileManager.default.createDirectory(
                        at: newPath,
                        withIntermediateDirectories: true
                    )
                }
            }
        } catch {
            NSLog(
                "Failed to create user memory directory: \(error.localizedDescription)"
            )
        }
        do {
            try FileManager.default.createDirectory(
                at: HazkeyServerConfig.getCacheDirectory().appendingPathComponent(
                    "shared",
                    isDirectory: true
                ),
                withIntermediateDirectories: true
            )
        } catch {
            NSLog(
                "Failed to create user cache directory: \(error.localizedDescription)"
            )
        }
    }
}
