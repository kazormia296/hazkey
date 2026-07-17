import Foundation
import KanaKanjiConverterModule
import SwiftProtobuf

let KEYMAP_FILE_SIZE_LIMIT = 1024 * 1024  //1MB
let TABLE_FILE_SIZE_LIMIT = 1024 * 1024  //1MB

private let zenzaiRuntimeGenerationQueryName = "grimodex_zenzai_generation"

/// An empty saved device name means "Automatic (GPU preferred)". Keep the
/// selection independent of backend-specific names such as Vulkan0 or CUDA0,
/// and retain CPU as the safe fallback for CPU-only or stale configurations.
struct ZenzaiBackendDeviceCandidate: Sendable {
    enum Kind: Sendable {
        case cpu
        case gpu
        case other
    }

    let name: String
    let kind: Kind

    init(name: String, kind: Kind) {
        self.name = name
        self.kind = kind
    }

    init(_ device: GGMLBackendDevice) {
        self.name = device.name
        self.kind = switch device.type {
        case .cpu: .cpu
        case .gpu: .gpu
        case .accel, .unknown: .other
        }
    }
}

func resolveZenzaiBackendDeviceName(
    configuredName: String,
    availableDevices: [ZenzaiBackendDeviceCandidate]
) -> String {
    let cpuName = availableDevices.first {
        if case .cpu = $0.kind { return true }
        return false
    }?.name ?? "CPU"

    if !configuredName.isEmpty {
        return availableDevices.contains { $0.name == configuredName }
            ? configuredName : cpuName
    }

    return availableDevices.first {
        if case .gpu = $0.kind { return true }
        return false
    }?.name ?? cpuName
}

/// Gives each model reload a distinct cache identity without changing the file
/// that the pinned Zenzai runtime opens.
///
/// `KanaKanjiConverter` compares the complete `resourceURL` when deciding
/// whether it can reuse a loaded model, while `Zenz` passes only `URL.path` to
/// llama.cpp. A generation query therefore invalidates the former and remains
/// invisible to the latter. This also avoids filesystem aliases that can fail
/// to be created or outlive a crashed service.
func makeZenzaiRuntimeModelURL(
    modelURL: URL,
    generation: UUID = UUID()
) -> URL {
    precondition(modelURL.isFileURL, "Zenzai models must use file URLs")
    guard var components = URLComponents(
        url: modelURL,
        resolvingAgainstBaseURL: false
    ) else {
        preconditionFailure("Unable to create Zenzai runtime URL components")
    }
    var queryItems = components.queryItems ?? []
    queryItems.removeAll { $0.name == zenzaiRuntimeGenerationQueryName }
    queryItems.append(
        URLQueryItem(
            name: zenzaiRuntimeGenerationQueryName,
            value: generation.uuidString.lowercased()
        )
    )
    components.queryItems = queryItems
    guard let runtimeURL = components.url, runtimeURL.path == modelURL.path else {
        preconditionFailure("Unable to preserve the Zenzai model filesystem path")
    }
    return runtimeURL
}

private enum HazkeyServerConfigError: LocalizedError {
    case emptyProfiles
    case invalidProfileDocument

    var errorDescription: String? {
        switch self {
        case .emptyProfiles:
            "Configuration profiles must not be empty"
        case .invalidProfileDocument:
            "Configuration must be an array of profile objects"
        }
    }
}

let builtInKeymaps = [
    "JIS Kana",
    "Japanese Symbol",
    "Fullwidth Period",
    "Fullwidth Comma",
    "Fullwidth Symbol",
    "Fullwidth Number",
    "Fullwidth Space",
].map { name in
    Hazkey_Config_Keymap.with {
        $0.name = name
        $0.isBuiltIn = true
        $0.filename = name
    }
}

let builtInInputTables = [
    "Romaji",
    "Kana",
].map { name in
    Hazkey_Config_InputTable.with {
        $0.name = name
        $0.isBuiltIn = true
        $0.filename = name
    }
}

enum HazkeyConverterBackend: Equatable, Sendable {
    case hazkey
    case mozc
    case mozcHybrid

    var usesMozcCore: Bool {
        self == .mozc || self == .mozcHybrid
    }

    var usesHazkeyCore: Bool {
        self == .hazkey || self == .mozcHybrid
    }

    var allowsZenzai: Bool {
        self != .mozc
    }

    var learningCapability: ConverterLearningCapability {
        switch self {
        case .hazkey, .mozcHybrid: return .persistent
        case .mozc: return .conversionOnly
        }
    }

    init(environment: [String: String]) {
        // The experimental process backend is exact-match opt-in. Unknown
        // values retain the in-process Hazkey backend.
        switch environment["FCITX5_GRIMODEX_CONVERTER"] {
        case "mozc": self = .mozc
        case "mozc-hybrid": self = .mozcHybrid
        default: self = .hazkey
        }
    }
}

class HazkeyServerConfig {
    var profiles: [Hazkey_Config_Profile]
    var currentProfile: Hazkey_Config_Profile
    let dictionaryPath: URL
    private(set) var zenzaiAvailable: Bool
    private(set) var zenzaiModelPath: URL?
    private(set) var zenzaiRuntimeModelURL: URL?
    var ggmlBackendDevices: [GGMLBackendDevice]
    private let zenzaiModelPathProvider: () -> URL?
    private let zenzaiRuntimeGenerationProvider: () -> UUID
    private let zenzaiBackendAvailableOverride: Bool?
    let converterBackend: HazkeyConverterBackend
    let mozcHelperPath: String
    let mozcDataPath: String

    var grimodexScopeMode: GrimodexScopeMode {
        switch currentProfile.grimodexScopeMode {
        case .grimodexOnly:
            .grimodexOnly
        case .grimodexOff:
            .off
        case .grimodexAllApplications:
            .allApplications
        case .UNRECOGNIZED:
            .off
        }
    }

    init(
        zenzaiBackendDevicesProvider: () -> [GGMLBackendDevice] = {
            getZenzaiDevices()
        },
        zenzaiModelPathProvider: @escaping () -> URL? = { getZenzaiModelPath() },
        zenzaiRuntimeGenerationProvider: @escaping () -> UUID = { UUID() },
        zenzaiBackendAvailableOverride: Bool? = nil,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.zenzaiModelPathProvider = zenzaiModelPathProvider
        self.zenzaiRuntimeGenerationProvider = zenzaiRuntimeGenerationProvider
        self.zenzaiBackendAvailableOverride = zenzaiBackendAvailableOverride
        self.converterBackend = HazkeyConverterBackend(environment: environment)
        self.mozcHelperPath = environment["FCITX5_GRIMODEX_MOZC_HELPER"]
            ?? (systemLibraryPath + "/fcitx5-grimodex-mozc-helper")
        self.mozcDataPath = environment["FCITX5_GRIMODEX_MOZC_DATA"]
            ?? (systemResourcePath + "/mozc/mozc.data")
        do {
            profiles = try Self.loadConfig()
        } catch {
            NSLog("Failed to load config: \(error)")
            NSLog("Loading default config...")
            profiles = [HazkeyServerConfig.genDefaultConfig()]
        }

        // TODO: add [0] out of range handling
        currentProfile = profiles[0]

        let fileManager = FileManager()

        // set dictionary path
        dictionaryPath = {
            if let envPath = ProcessInfo.processInfo.environment[
                "FCITX5_GRIMODEX_DICTIONARY"
            ],
                fileManager.fileExists(atPath: envPath)
            {
                return URL(filePath: envPath)
            } else {
                return URL(fileURLWithPath: systemResourcePath).appendingPathComponent(
                    "Dictionary", isDirectory: true)
            }
        }()

        self.ggmlBackendDevices = zenzaiBackendDevicesProvider()
        let backendAvailable = zenzaiBackendAvailableOverride
            ?? !ggmlBackendDevices.isEmpty
        let modelPath = backendAvailable ? zenzaiModelPathProvider() : nil
        self.zenzaiModelPath = modelPath
        self.zenzaiRuntimeModelURL = modelPath.map {
            makeZenzaiRuntimeModelURL(
                modelURL: $0,
                generation: zenzaiRuntimeGenerationProvider()
            )
        }
        self.zenzaiAvailable = backendAvailable && zenzaiRuntimeModelURL != nil
    }

    func getCurrentConfig() -> Hazkey_ResponseEnvelope {
        let profiles: [Hazkey_Config_Profile]
        do {
            profiles = try Self.loadConfig()
        } catch {
            NSLog("Failed to reload config: \(error)")
            NSLog("Returning active in-memory config...")
            profiles = self.profiles
        }

        let userKeymapDir = Self.getConfigDirectory().appendingPathComponent(
            "keymap", isDirectory: true
        )
        var keymaps = builtInKeymaps
        do {
            try FileManager.default.createDirectory(
                at: userKeymapDir, withIntermediateDirectories: true)
            let fileURLs = try FileManager.default.contentsOfDirectory(
                at: userKeymapDir,
                includingPropertiesForKeys: [.fileSizeKey],
                options: [.skipsHiddenFiles]
            )

            let keymapFiles = try fileURLs.filter { url in
                guard url.pathExtension.lowercased() == "tsv" else { return false }
                let attrs = try url.resourceValues(forKeys: [.fileSizeKey])
                if let size = attrs.fileSize {
                    return size < KEYMAP_FILE_SIZE_LIMIT
                }
                return false
            }

            for file in keymapFiles {
                keymaps.append(
                    Hazkey_Config_Keymap.with {
                        $0.name = file.deletingPathExtension().lastPathComponent
                        $0.isBuiltIn = false
                        $0.filename = file.lastPathComponent
                    })
            }
        } catch {
            return Hazkey_ResponseEnvelope.with {
                $0.status = .failed
                $0.errorMessage = "Failed to get user keymap files: \(error)"
            }
        }

        let userInputTableDir = Self.getConfigDirectory().appendingPathComponent(
            "table", isDirectory: true
        )
        var inputTables = builtInInputTables
        do {
            try FileManager.default.createDirectory(
                at: userInputTableDir, withIntermediateDirectories: true)
            let fileURLs = try FileManager.default.contentsOfDirectory(
                at: userInputTableDir,
                includingPropertiesForKeys: [.fileSizeKey],
                options: [.skipsHiddenFiles]
            )

            let inputTableFiles = try fileURLs.filter { url in
                guard url.pathExtension.lowercased() == "tsv" else { return false }
                let attrs = try url.resourceValues(forKeys: [.fileSizeKey])
                if let size = attrs.fileSize {
                    return size < TABLE_FILE_SIZE_LIMIT
                }
                return false
            }

            for file in inputTableFiles {
                inputTables.append(
                    Hazkey_Config_InputTable.with {
                        $0.name = file.deletingPathExtension().lastPathComponent
                        $0.isBuiltIn = false
                        $0.filename = file.lastPathComponent
                    })
            }
        } catch {
            return Hazkey_ResponseEnvelope.with {
                $0.status = .failed
                $0.errorMessage = "Failed to get user input table files: \(error)"
            }
        }

        var zenzaiDevices: [Hazkey_Config_BackendDevice] = []
        for devices in ggmlBackendDevices {
            zenzaiDevices.append(
                Hazkey_Config_BackendDevice.with {
                    $0.name = devices.name
                    $0.desc = devices.description
                }
            )
        }

        let currentConfig = Hazkey_Config_CurrentConfig.with {
            $0.fileHashes = []
            $0.zenzaiModelAvailable = zenzaiModelPath != nil
            $0.zenzaiModelPath = zenzaiModelPath?.path ?? ""
            $0.xdgConfigHomePath = Self.getConfigDirectory().path
            $0.availableKeymaps = keymaps
            $0.availableTables = inputTables
            $0.availableZenzaiBackendDevices = zenzaiDevices
            $0.profiles = profiles
        }
        return Hazkey_ResponseEnvelope.with {
            $0.status = .success
            $0.currentConfig = currentConfig
        }
    }

    func setCurrentConfig(
        _ hashes: [Hazkey_Config_FileHash],
        _ profiles: [Hazkey_Config_Profile]
    ) -> Hazkey_ResponseEnvelope {
        guard !profiles.isEmpty else {
            return Hazkey_ResponseEnvelope.with {
                $0.status = .failed
                $0.errorMessage = HazkeyServerConfigError.emptyProfiles.localizedDescription
            }
        }
        do {
            try saveConfig(profiles)
        } catch {
            return Hazkey_ResponseEnvelope.with {
                $0.status = .failed
                $0.errorMessage = "\(error)"
            }
        }

        return Hazkey_ResponseEnvelope.with {
            $0.status = .success
        }
    }

    static func genDefaultConfig() -> Hazkey_Config_Profile {
        var newConf = Hazkey_Config_Profile.init()
        newConf.profileName = "Default"
        newConf.autoConvertMode =
            Hazkey_Config_Profile.AutoConvertMode.autoConvertForMultipleChars
        newConf.liveConversionDelayMsec = 228
        newConf.auxTextMode = Hazkey_Config_Profile.AuxTextMode.auxTextShowWhenCursorNotAtEnd
        newConf.suggestionListMode =
            Hazkey_Config_Profile.SuggestionListMode.suggestionListShowPredictiveResults
        newConf.numSuggestions = 3
        newConf.useRichSuggestion = false
        newConf.numCandidatesPerPage = 9
        newConf.useRichCandidates = false
        newConf.useInputHistory = true
        newConf.specialConversionMode = Hazkey_Config_Profile.SpecialConversionMode.with {
            $0.commaSeparatedNumber = true
            $0.mailDomain = true
            $0.calendar = true
            $0.time = true
            $0.romanTypography = true
            $0.unicodeCodepoint = true
            $0.hazkeyVersion = true
            $0.halfwidthKatakana = true
            $0.extendedEmoji = true
        }
        newConf.stopStoreNewHistory = false
        newConf.enabledKeymaps = [
            Hazkey_Config_Profile.EnabledKeymap.with {
                $0.name = "Fullwidth Number"
                $0.isBuiltIn = true
                $0.filename = "Fullwidth Number"
            },
            Hazkey_Config_Profile.EnabledKeymap.with {
                $0.name = "Fullwidth Symbol"
                $0.isBuiltIn = true
                $0.filename = "Fullwidth Symbol"
            },
            Hazkey_Config_Profile.EnabledKeymap.with {
                $0.name = "Japanese Symbol"
                $0.isBuiltIn = true
                $0.filename = "Japanese Symbol"
            },
            Hazkey_Config_Profile.EnabledKeymap.with {
                $0.name = "Fullwidth Space"
                $0.isBuiltIn = true
                $0.filename = "Fullwidth Space"
            },
        ]
        newConf.enabledTables = [
            Hazkey_Config_Profile.EnabledInputTable.with {
                $0.name = "Romaji"
                $0.isBuiltIn = true
                $0.filename = "Romaji"
            }
        ]
        newConf.submodeEntryPointChars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        // Empty means automatic selection: prefer an enumerated GPU and fall
        // back to CPU when no GPU backend is available.
        newConf.zenzaiBackendDeviceName = ""
        newConf.zenzaiEnable = true
        newConf.zenzaiInferLimit = 10
        newConf.zenzaiContextualMode = true
        newConf.zenzaiProfile = ""
        newConf.grimodexScopeMode = .grimodexOnly
        return newConf
    }

    func saveConfig(_ newProfiles: [Hazkey_Config_Profile]) throws {
        guard !newProfiles.isEmpty else {
            throw HazkeyServerConfigError.emptyProfiles
        }
        let configDir = Self.getConfigDirectory()
        let configPath = configDir.appendingPathComponent("config.json")

        try FileManager.default.createDirectory(
            at: configDir, withIntermediateDirectories: true, attributes: nil)

        var jsonObjects: [Any] = []
        var encodeOptions = JSONEncodingOptions()
        encodeOptions.alwaysPrintEnumsAsInts = true
        encodeOptions.useDeterministicOrdering = true
        for profile in newProfiles {
            let jsonData = try profile.jsonUTF8Data(options: encodeOptions)
            let jsonObject = try JSONSerialization.jsonObject(with: jsonData, options: [])
            jsonObjects.append(jsonObject)
        }

        let jsonData = try JSONSerialization.data(
            withJSONObject: jsonObjects, options: [.prettyPrinted, .sortedKeys])

        try jsonData.write(to: configPath, options: .atomic)

        NSLog("Config saved to: \(configPath.path)")

        profiles = newProfiles
        currentProfile = profiles[0]

    }

    static func loadConfig() throws -> [Hazkey_Config_Profile] {
        let configDir = Self.getConfigDirectory()
        let configPath = configDir.appendingPathComponent("config.json")

        // Check if config file exists
        guard FileManager.default.fileExists(atPath: configPath.path) else {
            NSLog("Config file does not exist at: \(configPath.path), returning empty config")
            return [Self.genDefaultConfig()]
        }

        // Read file contents
        let jsonData = try Data(contentsOf: configPath)

        // Parse JSON array
        let jsonObject = try JSONSerialization.jsonObject(with: jsonData, options: [])
        guard let jsonArray = jsonObject as? [[String: Any]] else {
            throw HazkeyServerConfigError.invalidProfileDocument
        }

        var configs: [Hazkey_Config_Profile] = []
        var decodeOptions = JSONDecodingOptions()
        decodeOptions.ignoreUnknownFields = true
        for jsonObject in jsonArray {
            let jsonObjectData = try JSONSerialization.data(withJSONObject: jsonObject, options: [])
            let config = try Hazkey_Config_Profile(
                jsonUTF8Data: jsonObjectData, options: decodeOptions)
            configs.append(config)
        }

        if configs.count == 0 {
            NSLog("Loaded empty config. returning default config...")
            return [genDefaultConfig()]
        }

        NSLog("Config loaded from: \(configPath.path)")
        return configs
    }

    static func getConfigDirectory() -> URL {
        GrimodexProductPaths().configDirectory
    }

    static func getDataDirectory() -> URL {
        GrimodexProductPaths().dataDirectory
    }

    static func getStateDirectory() -> URL {
        GrimodexProductPaths().stateDirectory
    }

    static func getCacheDirectory() -> URL {
        GrimodexProductPaths().cacheDirectory
    }

    func genZenzaiMode(
        leftContext: String,
        rightContext: String = "",
        projectConditions: GrimodexProjectConditions = .empty,
        zenzaiAllowed: Bool = true,
        contextualModeOverride: Bool? = nil
    )
        -> ConvertRequestOptions.ZenzaiMode
    {
        guard case .enabled(let zenzaiModelPath) = zenzaiRuntimeDecision(
            zenzaiAllowed: zenzaiAllowed
        ) else {
            return .off
        }
        let deviceName = resolveZenzaiBackendDeviceName(
            configuredName: currentProfile.zenzaiBackendDeviceName,
            availableDevices: ggmlBackendDevices.map(ZenzaiBackendDeviceCandidate.init)
        )
        let resolved = GrimodexZenzaiConditionResolver.resolve(
            profile: currentProfile.zenzaiProfile,
            topic: currentProfile.zenzaiTopic,
            style: currentProfile.zenzaiStyle,
            preference: currentProfile.zenzaiPreference,
            project: projectConditions
        )

        return ConvertRequestOptions.ZenzaiMode.on(
            weight: zenzaiModelPath,
            inferenceLimit: Int(currentProfile.zenzaiInferLimit),
            requestRichCandidates: currentProfile.useRichCandidates,
            personalizationMode: nil,
            versionDependentMode: .v3(
                ConvertRequestOptions.ZenzaiV3DependentMode.init(
                    profile: resolved.profile,
                    topic: resolved.topic,
                    style: resolved.style,
                    preference: resolved.preference,
                    leftSideContext: (
                        contextualModeOverride
                            ?? currentProfile.zenzaiContextualMode
                    )
                        ? Self.contextForZenzai(
                            left: leftContext,
                            right: rightContext
                        ) : nil
                )),
            deviceConfig: createDeviceConfig(deviceName: deviceName)
        )
    }

    func zenzaiRuntimeDecision(
        zenzaiAllowed: Bool
    ) -> ZenzaiRuntimeDecision {
        guard zenzaiAllowed else { return .policyDisabled }
        guard currentProfile.zenzaiEnable else { return .profileDisabled }
        let backendAvailable = zenzaiBackendAvailableOverride
            ?? !ggmlBackendDevices.isEmpty
        guard backendAvailable else { return .backendUnavailable }
        guard zenzaiModelPath != nil, let zenzaiRuntimeModelURL else {
            return .modelMissing
        }
        return .enabled(modelURL: zenzaiRuntimeModelURL)
    }

    private static func contextForZenzai(left: String, right: String) -> String {
        guard !right.isEmpty else { return left }
        // The pinned converter revision exposes only a left-context field.
        // Preserve right-side reconversion context as an explicit natural-
        // language suffix until the upstream Zenzai API grows a dedicated
        // rightSideContext parameter.
        return left + "\n[変換対象の右文脈: " + right + "]"
    }

    func genBaseConvertRequestOptions() -> ConvertRequestOptions {
        let learningType =
            switch (currentProfile.useInputHistory, currentProfile.stopStoreNewHistory) {
            case (true, false):
                LearningType.inputAndOutput
            case (true, true):
                LearningType.onlyOutput
            default:
                LearningType.nothing
            }

        let specialCandidateProviders: [any SpecialCandidateProvider] = {
            let mode = currentProfile.specialConversionMode
            let providers: [SpecialCandidateProvider?] = [
                mode.commaSeparatedNumber ? CommaSeparatedNumberSpecialCandidateProvider() : nil,
                mode.calendar ? CalendarSpecialCandidateProvider() : nil,
                mode.hazkeyVersion ? VersionSpecialCandidateProvider() : nil,
                mode.mailDomain ? EmailAddressSpecialCandidateProvider() : nil,
                mode.romanTypography ? TypographySpecialCandidateProvider() : nil,
                mode.time ? TimeExpressionSpecialCandidateProvider() : nil,
                mode.unicodeCodepoint ? UnicodeSpecialCandidateProvider() : nil,
            ]
            return providers.compactMap { $0 }
        }()

        let zenzaiMode = genZenzaiMode(leftContext: "")

        return ConvertRequestOptions.init(
            N_best: Int(currentProfile.numCandidatesPerPage),
            needTypoCorrection: false,
            requireJapanesePrediction: .disabled,
            requireEnglishPrediction: .disabled,
            keyboardLanguage: .none,
            englishCandidateInRoman2KanaInput: false,
            fullWidthRomanCandidate: true,
            halfWidthKanaCandidate: true,
            learningType: learningType,
            maxMemoryCount: 65536,
            shouldResetMemory: false,
            memoryDirectoryURL: HazkeyServerConfig.getStateDirectory().appendingPathComponent(
                "memory", isDirectory: true),
            sharedContainerURL: HazkeyServerConfig.getCacheDirectory().appendingPathComponent(
                "shared", isDirectory: true),
            textReplacer: .empty,
            specialCandidateProviders: specialCandidateProviders,
            zenzaiMode: zenzaiMode,
            preloadDictionary: false,
            metadata: ConvertRequestOptions.Metadata.init(
                versionString: "Grimodex IME \(hazkeyVersion)"
            )
        )
    }

    func loadKeymap() -> Keymap {
        var maps: Keymap = [:]
        outer: for enabledKeymap in currentProfile.enabledKeymaps.reversed() {
            var newKeymapRule: Keymap
            if enabledKeymap.isBuiltIn {
                switch enabledKeymap.filename {
                case "JIS Kana":
                    newKeymapRule = JISKanaMap
                case "Japanese Symbol":
                    newKeymapRule = japaneseSymbolMap
                case "Fullwidth Period":
                    newKeymapRule = fullwidthPeriodMap
                case "Fullwidth Comma":
                    newKeymapRule = fullwidthCommaMap
                case "Fullwidth Symbol":
                    newKeymapRule = fullwidthSymbolMap
                case "Fullwidth Number":
                    newKeymapRule = fullwidthNumberMap
                case "Fullwidth Space":
                    newKeymapRule = fullwidthSpaceMap
                default:
                    NSLog("Unknown built-in keymap: \(enabledKeymap.name)")
                    continue outer
                }
            } else {
                // load custom keymap
                let customKeymapFile = HazkeyServerConfig.getConfigDirectory()
                    .appendingPathComponent(
                        "keymap", isDirectory: true
                    ).appendingPathComponent(enabledKeymap.filename, isDirectory: false)
                do {
                    let lines = try String(contentsOf: customKeymapFile, encoding: .utf8)
                        .split(separator: "\n")
                        .map { $0.split(separator: "\t") }
                    newKeymapRule = [:]
                    inner: for cols in lines {
                        guard let key = cols[0].first else { continue inner }
                        switch cols.count {
                        case 1:
                            newKeymapRule[key] = nil
                        case 2:
                            newKeymapRule[key] = (cols[1].first!, nil)
                        case 3...:
                            newKeymapRule[key] = (cols[1].first!, cols[2].first)
                        default:
                            NSLog("Unknown columns count: \(cols.count)")
                            continue inner
                        }
                    }
                } catch {
                    NSLog(
                        "Failed to load custom keymap \(enabledKeymap.name): \(error)"
                    )
                    continue outer
                }
            }
            maps.merge(newKeymapRule) { (_, second) in second }
        }

        return maps
    }

    func loadInputTable(tableName: String) {
        var tables: [InputTable] = [compositionSeparatorTable]
        outer: for enabledTable in currentProfile.enabledTables.reversed() {
            let tableToAdd: InputTable
            if enabledTable.isBuiltIn {
                switch enabledTable.filename {
                case "Romaji":
                    tableToAdd = romajiTable
                case "Kana":
                    tableToAdd = kanaTable
                default:
                    debugLog("Unknown built-in input table: \(enabledTable.name)")
                    continue outer
                }
            } else {
                // load custom table
                let customTableFile = HazkeyServerConfig.getConfigDirectory()
                    .appendingPathComponent(
                        "table", isDirectory: true
                    ).appendingPathComponent(enabledTable.filename, isDirectory: false)
                do {
                    tableToAdd = try InputStyleManager.loadTable(from: customTableFile)
                } catch {
                    NSLog("Failed to load custom table \(enabledTable.name)Q \(error)")
                    continue outer
                }
            }
            tables.append(tableToAdd)
        }

        let inputTable = InputTable(tables: tables, order: InputTable.Ordering.lastInputWins)
        InputStyleManager.registerInputStyle(table: inputTable, for: tableName)
    }

    func getSubModeEntryPointChars() -> [Character] {
        return Array(currentProfile.submodeEntryPointChars)
    }

    func reloadZenzaiModel() {
        let backendAvailable = zenzaiBackendAvailableOverride
            ?? !ggmlBackendDevices.isEmpty
        let modelPath = backendAvailable ? zenzaiModelPathProvider() : nil
        zenzaiModelPath = modelPath
        zenzaiRuntimeModelURL = modelPath.map {
            makeZenzaiRuntimeModelURL(
                modelURL: $0,
                generation: zenzaiRuntimeGenerationProvider()
            )
        }
        self.zenzaiAvailable = backendAvailable && zenzaiRuntimeModelURL != nil
    }
}

func getZenzaiDevices() -> [GGMLBackendDevice] {
    var ggmlBackendDirectory =
        ProcessInfo.processInfo.environment["GGML_BACKEND_DIR"]
        ?? (systemLibraryPath + "/libllama/backends/")
    // trailing slash is important
    if !ggmlBackendDirectory.hasSuffix("/") {
        ggmlBackendDirectory.append("/")
    }
    loadGGMLBackends(from: ggmlBackendDirectory)

    let backendDevices = enumerateGGMLBackendDevices()
    #if DEBUG
        for device in backendDevices {
            NSLog(
                "GGML Backend Device: \(device.name), Type: \(device.type), Description: \(device.description)"
            )
        }
    #endif
    return backendDevices
}

func getZenzaiModelPath() -> URL? {
    let systemZenzaiModelPath = URL(fileURLWithPath: systemResourcePath)
        .appendingPathComponent("zenzai.gguf", isDirectory: false)
    let userZenzaiModelPath = HazkeyServerConfig.getDataDirectory()
        .appendingPathComponent("zenzai", isDirectory: true)
        .appendingPathComponent("zenzai.gguf", isDirectory: false)

    let paths: [URL] = [
        ProcessInfo.processInfo.environment["FCITX5_GRIMODEX_ZENZAI_MODEL"].map {
            URL(filePath: $0)
        },
        userZenzaiModelPath,
        systemZenzaiModelPath,
    ].compactMap { $0 }

    for url in paths {
        if let values = try? url.resourceValues(forKeys: [.isDirectoryKey]),
            values.isDirectory == false
        {
            NSLog(url.path)
            return url
        }
    }
    return nil
}
