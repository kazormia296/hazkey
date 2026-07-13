import Foundation
import SwiftProtobuf

class ProtocolHandler {
    private let sessionRegistry: HazkeySessionRegistry
    private let onConfigurationChanged: (HazkeyServerConfig) -> Void
    private let diagnosticsProvider: () -> GrimodexDiagnosticsSnapshot

    init(
        sessionRegistry: HazkeySessionRegistry,
        onConfigurationChanged: @escaping (HazkeyServerConfig) -> Void = { _ in },
        diagnosticsProvider: @escaping () -> GrimodexDiagnosticsSnapshot = {
            .unavailable
        }
    ) {
        self.sessionRegistry = sessionRegistry
        self.onConfigurationChanged = onConfigurationChanged
        self.diagnosticsProvider = diagnosticsProvider
    }

    func processProto(data: Data, clientFd: Int32) -> Data {
        let query: Hazkey_RequestEnvelope
        let response: Hazkey_ResponseEnvelope

        do {
            query = try Hazkey_RequestEnvelope(serializedBytes: data)
        } catch {
            NSLog("Failed to parse protobuf: \(error)")
            response = Hazkey_ResponseEnvelope.with {
                $0.status = .failed
                $0.errorMessage = "Failed to parse protobuf: \(error)"
            }
            return serializeResult(unserialized: response)
        }

        switch query.payload {
        case .openSession(let request):
            let context = GrimodexClientContext(
                program: request.client.program,
                frontend: request.client.frontend,
                secureInput: request.client.secureInput
            )
            let openResult = sessionRegistry.attemptOpen(
                clientContext: context,
                ownerFd: clientFd
            )
            switch openResult {
            case .success(let sessionID):
                response = Hazkey_ResponseEnvelope.with {
                    $0.status = .success
                    $0.openSessionResult = Hazkey_OpenSessionResult.with {
                        $0.sessionID = sessionID
                        $0.protocolVersion = ImeV2Negotiation.protocolVersion
                        $0.featureBits = ImeV2Negotiation.current.featureBits
                        $0.maxSnapshotVersion = ImeV2Negotiation.snapshotVersion
                        $0.recoverySupport = ImeV2Negotiation.current.recoverySupport
                        $0.idempotentRequestSupport =
                            ImeV2Negotiation.current.idempotentRequestSupport
                    }
                }
            case .failure(.resourceExhausted):
                response = Hazkey_ResponseEnvelope.with {
                    $0.status = .failed
                    $0.errorMessage = "Session capacity exhausted"
                }
            }
        case .closeSession(let request):
            if sessionRegistry.close(sessionID: request.sessionID, ownerFd: clientFd) {
                response = successResponse()
            } else {
                response = sessionNotFoundResponse()
            }
        case .handleImeAction(let request):
            guard let controller = sessionRegistry.semanticController(
                for: query.sessionID,
                ownerFd: clientFd
            ) else {
                response = sessionNotFoundResponse()
                break
            }
            response = controller.handle(request)
        case .listUserDictionary:
            response = userDictionaryResponse(
                entries: sessionRegistry.userDictionaryEntries()
            )
        case .addUserDictionaryEntry(let request):
            guard request.hasEntry else {
                response = invalidRequestResponse("User dictionary entry is required")
                break
            }
            do {
                let entry = try userDictionaryEntry(request.entry)
                _ = try sessionRegistry.addUserDictionaryEntry(entry)
                response = userDictionaryResponse(
                    entries: sessionRegistry.userDictionaryEntries()
                )
            } catch {
                response = userDictionaryFailure(error)
            }
        case .updateUserDictionaryEntry(let request):
            guard request.hasEntry else {
                response = invalidRequestResponse("User dictionary entry is required")
                break
            }
            do {
                try sessionRegistry.updateUserDictionaryEntry(
                    userDictionaryEntry(request.entry)
                )
                response = userDictionaryResponse(
                    entries: sessionRegistry.userDictionaryEntries()
                )
            } catch {
                response = userDictionaryFailure(error)
            }
        case .removeUserDictionaryEntry(let request):
            do {
                try sessionRegistry.removeUserDictionaryEntry(id: request.id)
                response = userDictionaryResponse(
                    entries: sessionRegistry.userDictionaryEntries()
                )
            } catch {
                response = userDictionaryFailure(error)
            }
        case .importUserDictionary(let request):
            do {
                try sessionRegistry.importUserDictionary(
                    request.json,
                    merge: request.merge
                )
                response = userDictionaryResponse(
                    entries: sessionRegistry.userDictionaryEntries()
                )
            } catch {
                response = userDictionaryFailure(error)
            }
        case .exportUserDictionary:
            do {
                response = userDictionaryResponse(
                    entries: sessionRegistry.userDictionaryEntries(),
                    exportedJSON: try sessionRegistry.exportUserDictionary()
                )
            } catch {
                response = userDictionaryFailure(error)
            }
        case .getConfig:
            var configResponse = sessionRegistry.serverConfig.getCurrentConfig()
            if configResponse.status == .success {
                configResponse.currentConfig.grimodexDiagnostics = diagnosticsProvider().protobuf
            }
            response = configResponse
        case .setConfig(let request):
            if request.profiles.isEmpty {
                response = invalidRequestResponse("Configuration profiles must not be empty")
            } else {
                response = sessionRegistry.serverConfig.setCurrentConfig(
                    request.fileHashes,
                    request.profiles
                )
                if response.status == .success {
                    onConfigurationChanged(sessionRegistry.serverConfig)
                    sessionRegistry.reinitializeAll()
                }
            }
        case .clearAllHistory_p:
            sessionRegistry.clearAllLearningData()
            response = successResponse()
        case .reloadZenzaiModel:
            sessionRegistry.serverConfig.reloadZenzaiModel()
            sessionRegistry.reinitializeAll()
            response = successResponse()
        case .getDefaultProfile:
            NSLog("Unimplemented: getDefaultProfile")
            response = Hazkey_ResponseEnvelope.with {
                $0.status = .failed
                $0.errorMessage = "Unimplemented: getDefaultProfile"
            }
        case .none:
            NSLog("Payload not specified")
            response = Hazkey_ResponseEnvelope.with {
                $0.status = .failed
                $0.errorMessage = "Payload not specified"
            }
        }
        return serializeResult(unserialized: response)
    }

    private func successResponse() -> Hazkey_ResponseEnvelope {
        Hazkey_ResponseEnvelope.with { $0.status = .success }
    }

    private func sessionNotFoundResponse() -> Hazkey_ResponseEnvelope {
        Hazkey_ResponseEnvelope.with {
            $0.status = .sessionNotFound
            $0.errorMessage = "Session not found"
        }
    }

    private func invalidRequestResponse(_ message: String) -> Hazkey_ResponseEnvelope {
        Hazkey_ResponseEnvelope.with {
            $0.status = .failed
            $0.errorMessage = message
        }
    }

    private func userDictionaryEntry(
        _ value: Hazkey_Config_UserDictionaryEntry
    ) throws -> UserDictionaryEntry {
        let layer: UserDictionaryLayer
        switch value.layer {
        case .unspecified, .personal:
            layer = .personal
        case .temporary:
            layer = .temporary
        case .system, .project, .UNRECOGNIZED:
            throw UserDictionaryError.invalidField
        }
        return UserDictionaryEntry(
            id: value.id.isEmpty ? UUID().uuidString.lowercased() : value.id,
            reading: value.reading,
            surface: value.surface,
            partOfSpeech: value.partOfSpeech,
            layer: layer
        )
    }

    private func userDictionaryResponse(
        entries: [UserDictionaryEntry],
        exportedJSON: Data = Data()
    ) -> Hazkey_ResponseEnvelope {
        Hazkey_ResponseEnvelope.with {
            $0.status = .success
            $0.userDictionaryResult = Hazkey_Config_UserDictionaryResult.with {
                $0.entries = entries.map { entry in
                    Hazkey_Config_UserDictionaryEntry.with {
                        $0.id = entry.id
                        $0.reading = entry.reading
                        $0.surface = entry.surface
                        $0.partOfSpeech = entry.partOfSpeech
                        $0.layer = switch entry.layer {
                        case .system: .system
                        case .personal: .personal
                        case .project: .project
                        case .temporary: .temporary
                        }
                    }
                }
                $0.exportedJson = exportedJSON
            }
        }
    }

    private func userDictionaryFailure(_ error: Error) -> Hazkey_ResponseEnvelope {
        Hazkey_ResponseEnvelope.with {
            $0.status = .failed
            $0.errorMessage = "User dictionary operation failed: \(error)"
        }
    }

    private func serializeResult(unserialized: Hazkey_ResponseEnvelope) -> Data {
        do {
            let serialized = try unserialized.serializedData()
            return serialized
        } catch {
            NSLog("Failed to serialize response message: \(unserialized)")
            return Data()
        }
    }
}
