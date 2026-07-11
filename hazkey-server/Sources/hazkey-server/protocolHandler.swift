import Foundation
import SwiftProtobuf

class ProtocolHandler {
    private let sessionRegistry: HazkeySessionRegistry
    private let onConfigurationChanged: (HazkeyServerConfig) -> Void

    init(
        sessionRegistry: HazkeySessionRegistry,
        onConfigurationChanged: @escaping (HazkeyServerConfig) -> Void = { _ in }
    ) {
        self.sessionRegistry = sessionRegistry
        self.onConfigurationChanged = onConfigurationChanged
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
            let sessionID = sessionRegistry.open(
                clientContext: context,
                ownerFd: clientFd
            )
            response = Hazkey_ResponseEnvelope.with {
                $0.status = .success
                $0.openSessionResult = Hazkey_OpenSessionResult.with {
                    $0.sessionID = sessionID
                }
            }
        case .closeSession(let request):
            if sessionRegistry.close(sessionID: request.sessionID, ownerFd: clientFd) {
                response = successResponse()
            } else {
                response = sessionNotFoundResponse()
            }
        case .getConfig:
            response = sessionRegistry.serverConfig.getCurrentConfig()
        case .setConfig(let request):
            response = sessionRegistry.serverConfig.setCurrentConfig(
                request.fileHashes,
                request.profiles
            )
            if response.status == .success {
                onConfigurationChanged(sessionRegistry.serverConfig)
                sessionRegistry.reinitializeAll()
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
        default:
            guard let state = sessionRegistry.state(for: query.sessionID, ownerFd: clientFd) else {
                return serializeResult(unserialized: sessionNotFoundResponse())
            }
            response = processSessionPayload(query.payload, state: state)
        }
        return serializeResult(unserialized: response)
    }

    private func processSessionPayload(
        _ payload: Hazkey_RequestEnvelope.OneOf_Payload?,
        state: HazkeyServerState
    ) -> Hazkey_ResponseEnvelope {
        let response: Hazkey_ResponseEnvelope
        switch payload {
        case .setContext(let req):
            response = state.setContext(
                surroundingText: req.context, anchorIndex: Int(req.anchor))
        case .newComposingText:
            response = state.createComposingTextInstanse()
        case .inputChar(let req):
            response = state.inputChar(inputString: req.text)
        case .modifierEvent(let req):
            response = state.processModifierEvent(modifier: req.modType, event: req.eventType)
        case .deleteLeft:
            response = state.deleteLeft()
        case .deleteRight:
            response = state.deleteRight()
        case .prefixComplete(let req):
            response = state.completePrefix(candidateIndex: Int(req.index))
        case .moveCursor(let req):
            response = state.moveCursor(offset: Int(req.offset))
        case .getHiraganaWithCursor:
            response = state.getHiraganaWithCursor()
        case .getComposingString(let req):
            response = state.getComposingString(
                charType: req.charType, currentPreedit: req.currentPreedit)
        case .getCandidates(let req):
            response = state.getCandidates(is_suggest: req.isSuggest)
        case .getCurrentInputMode:
            response = state.getCurrentInputMode()
        case .saveLearningData:
            response = state.saveLearningData()
        case .openSession, .closeSession, .getConfig, .setConfig, .getDefaultProfile,
            .clearAllHistory_p, .reloadZenzaiModel, .none:
            response = Hazkey_ResponseEnvelope.with {
                $0.status = .failed
                $0.errorMessage = "Command is not valid for a conversion session"
            }
        }
        return response
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
